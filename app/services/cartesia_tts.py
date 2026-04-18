"""Cartesia streaming TTS.

Emits async byte-chunks of PCM s16le at `output_rate`. By default Cartesia
returns 24 kHz; we optionally resample down to 16 kHz server-side so the
LatheOS daemon can feed a single-rate PipeWire graph (one less conversion
stage on the hot audio path). In mock mode we emit deterministic silence
frames so the downstream audio path stays exercised.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import numpy as np
from websockets.asyncio.client import connect as ws_connect

from app.config import Settings
from app.core.logging import get_logger
from app.core.telemetry import span

log = get_logger(__name__)

_CARTESIA_WS = "wss://api.cartesia.ai/tts/websocket"
# Cartesia requires an API version header; 2024-11-13 is the current stable.
# https://docs.cartesia.ai/api-reference/tts/tts
_CARTESIA_VERSION = "2024-11-13"
_CARTESIA_MODEL = "sonic-2"

_NATIVE_RATE = 24_000


@dataclass(slots=True)
class CartesiaClient:
    settings: Settings
    output_rate: int = _NATIVE_RATE

    def _live(self) -> bool:
        return bool(self.settings.cartesia_api_key and self.settings.cartesia_voice_id)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text:
            return
        with span(
            "tts.cartesia.synthesize",
            **{
                "tts.vendor": "cartesia",
                "tts.mode": "live" if self._live() else "mock",
                "tts.input_chars": len(text),
                "tts.native_rate": _NATIVE_RATE,
                "tts.output_rate": self.output_rate,
            },
        ) as s:
            started = time.perf_counter()
            first_byte_at: float | None = None
            total_bytes = 0
            raw_stream = (
                self._synthesize_live(text) if self._live() else self._synthesize_mock(text)
            )

            def _observe(chunk: bytes) -> None:
                nonlocal first_byte_at, total_bytes
                if first_byte_at is None and chunk:
                    first_byte_at = time.perf_counter() - started
                    s.set_attribute("tts.first_byte_ms", int(first_byte_at * 1000))
                total_bytes += len(chunk)

            if self.output_rate == _NATIVE_RATE:
                async for chunk in raw_stream:
                    _observe(chunk)
                    yield chunk
            else:
                resampler = _LinearResampler(_NATIVE_RATE, self.output_rate)
                async for chunk in raw_stream:
                    out = resampler.process(chunk)
                    if out:
                        _observe(out)
                        yield out
                tail = resampler.flush()
                if tail:
                    _observe(tail)
                    yield tail

            s.set_attribute("tts.total_bytes", total_bytes)

    async def _synthesize_mock(self, text: str) -> AsyncIterator[bytes]:
        log.info("cartesia.mock.engaged", chars=len(text))
        # ~40ms of 24 kHz s16le silence per yield — realistic pacing.
        chunk = b"\x00" * int(_NATIVE_RATE * 2 * 0.04)
        for _ in range(max(1, min(25, len(text) // 20))):
            await asyncio.sleep(0.04)
            yield chunk

    async def _synthesize_live(self, text: str) -> AsyncIterator[bytes]:
        headers = {
            "X-API-Key": self.settings.cartesia_api_key,
            "Cartesia-Version": _CARTESIA_VERSION,
        }
        # Context IDs must be alphanumeric + underscore + hyphen only — uuid4 hex is safe.
        context_id = uuid.uuid4().hex
        async with ws_connect(_CARTESIA_WS, additional_headers=headers) as ws:
            await ws.send(
                json.dumps(
                    {
                        "context_id": context_id,
                        "model_id": _CARTESIA_MODEL,
                        "voice": {"mode": "id", "id": self.settings.cartesia_voice_id},
                        "language": "en",
                        "output_format": {
                            "container": "raw",
                            "encoding": "pcm_s16le",
                            "sample_rate": _NATIVE_RATE,
                        },
                        "transcript": text,
                        "continue": False,
                    }
                )
            )
            async for msg in ws:
                if isinstance(msg, bytes):
                    yield msg
                    continue
                payload = json.loads(msg)
                ptype = payload.get("type")
                if ptype == "done":
                    return
                if ptype == "chunk" and "data" in payload:
                    yield base64.b64decode(payload["data"])
                elif ptype == "error":
                    log.error("cartesia.live.error", **payload)
                    return


# -- Linear resampler ---------------------------------------------------------
# Intentionally naïve: linear interpolation is ~25 dB SNR which is inaudible
# for speech at 16 kHz output. If/when we need transparent quality, swap for
# `soxr_resample` — the surface stays identical.


class _LinearResampler:
    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.ratio = dst_rate / src_rate
        self._tail = np.empty(0, dtype=np.float32)
        self._fractional = 0.0

    def process(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        samples = np.concatenate([self._tail, samples])
        n_out_float = (len(samples) - 1) * self.ratio + self._fractional
        n_out = int(n_out_float)
        if n_out <= 0:
            self._tail = samples
            return b""
        idx = np.arange(n_out, dtype=np.float64) / self.ratio + (
            (1.0 - self._fractional) if self._fractional else 0.0
        )
        idx = np.clip(idx, 0, len(samples) - 1)
        lo = np.floor(idx).astype(np.int64)
        hi = np.minimum(lo + 1, len(samples) - 1)
        frac = (idx - lo).astype(np.float32)
        out = samples[lo] * (1.0 - frac) + samples[hi] * frac
        consumed = int(n_out / self.ratio)
        self._tail = samples[consumed:]
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()

    def flush(self) -> bytes:
        if len(self._tail) == 0:
            return b""
        pcm = self._tail.astype(np.int16).tobytes()
        self._tail = np.empty(0, dtype=np.float32)
        return pcm
