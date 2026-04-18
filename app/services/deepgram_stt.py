"""Deepgram streaming STT client.

This module exposes a single async generator contract:

    async for transcript in stt.stream(audio_queue):
        ...

`audio_queue` is an asyncio.Queue of raw PCM byte-chunks arriving from
the LatheOS client. This lets us keep the session loop vendor-agnostic:
swap Deepgram for Whisper-over-WS later without touching `ws/session.py`.

The implementation below ships with a deterministic MOCK mode so the
full pipeline runs without credentials during development. Flip
`DEEPGRAM_API_KEY` in env and the real client activates automatically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import websockets

from app.config import Settings
from app.core.logging import get_logger
from app.schemas.messages import Transcript

log = get_logger(__name__)

_DEEPGRAM_WS_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model={model}&encoding=linear16&sample_rate={sr}&interim_results=true"
)


@dataclass(slots=True)
class DeepgramClient:
    settings: Settings

    def _live(self) -> bool:
        return bool(self.settings.deepgram_api_key)

    async def stream(
        self,
        audio_queue: asyncio.Queue[bytes | None],
        sample_rate: int = 16_000,
    ) -> AsyncIterator[Transcript]:
        if self._live():
            async for t in self._stream_live(audio_queue, sample_rate):
                yield t
        else:
            async for t in self._stream_mock(audio_queue):
                yield t

    # ---------- mock ----------

    async def _stream_mock(
        self, audio_queue: asyncio.Queue[bytes | None]
    ) -> AsyncIterator[Transcript]:
        """Deterministic transcript emission for local dev.

        Every ~1s of audio we emit a rolling interim, and every ~3s a final.
        """
        log.info("deepgram.mock.engaged")
        bytes_seen = 0
        interim_at = 16_000 * 2 * 1  # 1s of 16kHz s16le
        final_at = 16_000 * 2 * 3
        phrase = "cam spin up the backend"
        words = phrase.split()
        idx = 0

        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                if idx > 0:
                    yield Transcript(
                        text=" ".join(words[:idx]), is_final=True, confidence=0.95
                    )
                return
            bytes_seen += len(chunk)
            if bytes_seen >= interim_at and idx < len(words):
                idx += 1
                bytes_seen = 0
                yield Transcript(
                    text=" ".join(words[:idx]), is_final=False, confidence=0.8
                )
            if idx >= len(words):
                yield Transcript(text=phrase, is_final=True, confidence=0.97)
                idx = 0

    # ---------- live ----------

    async def _stream_live(
        self,
        audio_queue: asyncio.Queue[bytes | None],
        sample_rate: int,
    ) -> AsyncIterator[Transcript]:
        url = _DEEPGRAM_WS_URL.format(model=self.settings.deepgram_model, sr=sample_rate)
        headers = {"Authorization": f"Token {self.settings.deepgram_api_key}"}

        async with websockets.connect(url, additional_headers=headers) as dg:
            log.info("deepgram.live.connected", model=self.settings.deepgram_model)

            async def pump_audio() -> None:
                while True:
                    chunk = await audio_queue.get()
                    if chunk is None:
                        await dg.send(json.dumps({"type": "CloseStream"}))
                        return
                    await dg.send(chunk)

            pump_task = asyncio.create_task(pump_audio())
            try:
                async for raw in dg:
                    if isinstance(raw, bytes):
                        continue
                    payload = json.loads(raw)
                    if payload.get("type") != "Results":
                        continue
                    alt = payload["channel"]["alternatives"][0]
                    text = alt.get("transcript", "")
                    if not text:
                        continue
                    yield Transcript(
                        text=text,
                        is_final=bool(payload.get("is_final")),
                        confidence=float(alt.get("confidence", 0.0)),
                    )
            finally:
                pump_task.cancel()
