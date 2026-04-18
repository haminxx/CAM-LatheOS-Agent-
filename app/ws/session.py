"""Per-connection duplex orchestrator.

One `CamSession` owns the lifetime of a single LatheOS client. It runs
two cooperative tasks inside an asyncio.TaskGroup:

    1. ingress  — decode text/binary frames from client, enqueue audio
    2. pipeline — consume audio -> Deepgram -> LLM -> (TTS + Command)

TTS bytes and JSON control events are sent directly to the client from
the pipeline task; they share nothing with ingress but the WebSocket,
and Starlette serialises concurrent writes correctly.

Back-pressure is bounded by the audio queue size — if a client uploads
faster than Deepgram can consume, frames are dropped (logged), never
silently buffered to unbounded memory.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import orjson
from fastapi import WebSocket, WebSocketDisconnect

from app.config import Settings
from app.core.auth import AuthError, HardwareIdentity, TokenVerifier
from app.core.logging import get_logger
from app.schemas.messages import (
    Command,
    ErrorMsg,
    Hello,
    ServerEvent,
    SpeechEnd,
    SpeechStart,
    Transcript,
)
from app.services.cartesia_tts import CartesiaClient
from app.services.deepgram_stt import DeepgramClient
from app.services.llm_router import LLMRouter

log = get_logger(__name__)

_AUDIO_QUEUE_MAX = 128  # ~5s at 40ms frames
_HANDSHAKE_TIMEOUT_S = 5.0


@dataclass(slots=True)
class SessionDeps:
    settings: Settings
    verifier: TokenVerifier
    stt: DeepgramClient
    llm: LLMRouter
    tts: CartesiaClient


class CamSession:
    def __init__(self, ws: WebSocket, deps: SessionDeps) -> None:
        self.ws = ws
        self.deps = deps
        self.id = uuid.uuid4().hex[:12]
        self.identity: HardwareIdentity | None = None
        self.audio_q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_AUDIO_QUEUE_MAX)

    async def run(self) -> None:
        await self.ws.accept()
        log.info("session.open", session=self.id, client=str(self.ws.client))

        # --- handshake ---------------------------------------------------------
        # Deliberately *outside* the TaskGroup so errors surface as plain
        # exceptions, not ExceptionGroups, and we can branch cleanly on the type.
        try:
            await self._handshake()
        except WebSocketDisconnect:
            log.info("session.disconnect_before_handshake", session=self.id)
            return
        except AuthError as exc:
            await self._safe_send_event(
                ErrorMsg(code="auth_failed", message=str(exc))
            )
            await self._safe_close(code=4401)
            return
        except (TimeoutError, asyncio.TimeoutError):
            await self._safe_send_event(
                ErrorMsg(code="handshake_timeout", message="hello frame not received")
            )
            await self._safe_close(code=4408)
            return
        except Exception as exc:
            log.exception("session.handshake_error", session=self.id, error=str(exc))
            await self._safe_close(code=1011)
            return

        # --- main duplex loop --------------------------------------------------
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._ingress(), name=f"ingress-{self.id}")
                tg.create_task(self._pipeline(), name=f"pipeline-{self.id}")
        except* WebSocketDisconnect:
            log.info("session.disconnect", session=self.id)
        except* Exception as eg:
            for exc in eg.exceptions:
                log.exception("session.error", session=self.id, error=str(exc))
            await self._safe_close(code=1011)
        finally:
            log.info("session.closed", session=self.id)

    # ---------- handshake ----------

    async def _handshake(self) -> None:
        raw = await asyncio.wait_for(
            self.ws.receive_text(), timeout=_HANDSHAKE_TIMEOUT_S
        )
        hello = Hello.model_validate_json(raw)
        self.identity = await self.deps.verifier.verify(hello.hardware_token)
        log.info(
            "session.auth.ok",
            session=self.id,
            user=self.identity.user_id,
            tier=self.identity.tier,
        )

    # ---------- ingress ----------

    async def _ingress(self) -> None:
        """Decode incoming frames. Binary -> audio_q. Text -> control (future)."""
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if (audio := msg.get("bytes")) is not None:
                    try:
                        self.audio_q.put_nowait(audio)
                    except asyncio.QueueFull:
                        log.warning("ingress.backpressure.drop", session=self.id)
                elif (text := msg.get("text")) is not None:
                    log.debug("ingress.control", session=self.id, text=text[:120])
        finally:
            # Sentinel so the STT stream can terminate cleanly.
            await self.audio_q.put(None)

    # ---------- pipeline ----------

    async def _pipeline(self) -> None:
        """STT -> LLM -> (TTS + command) -> client."""
        final_buffer: list[str] = []

        async for transcript in self.deps.stt.stream(self.audio_q):
            await self._safe_send_event(transcript)
            if not transcript.is_final:
                continue

            final_buffer.append(transcript.text)
            utterance = " ".join(final_buffer).strip()
            final_buffer.clear()

            if not utterance:
                continue

            log.info("pipeline.final", session=self.id, text=utterance)
            llm_result = await self.deps.llm.complete(utterance)

            if llm_result.command:
                await self._safe_send_event(llm_result.command)

            if llm_result.spoken:
                await self._stream_tts(llm_result.spoken)

    async def _stream_tts(self, text: str) -> None:
        utter_id = uuid.uuid4().hex[:8]
        await self._safe_send_event(SpeechStart(utterance_id=utter_id))
        try:
            async for pcm_chunk in self.deps.tts.synthesize(text):
                await self.ws.send_bytes(pcm_chunk)
        except WebSocketDisconnect:
            raise
        finally:
            await self._safe_send_event(SpeechEnd(utterance_id=utter_id))

    # ---------- write helpers ----------

    async def _safe_send_event(
        self, event: ServerEvent | Transcript | Command
    ) -> None:
        try:
            payload = orjson.dumps(event.model_dump())
            await self.ws.send_text(payload.decode())
        except WebSocketDisconnect:
            raise
        except Exception as exc:
            log.warning(
                "egress.send_failed", session=self.id, error=str(exc),
                event_type=event.__class__.__name__,
            )

    async def _safe_close(self, code: int) -> None:
        try:
            await self.ws.close(code=code)
        except Exception:
            pass
