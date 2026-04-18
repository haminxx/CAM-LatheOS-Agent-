"""Ad-hoc smoke client for /ws/cam.

Connects to the proxy, sends a hello + a few seconds of silence, and prints
every server frame. Exits 0 if the session opens cleanly and no error frame
arrives before the probe window ends — i.e. the vendor WebSockets (Deepgram,
Cartesia) handshake without exceptions.

Silence will not produce transcripts from real Deepgram, so this only proves
the wiring; to test actual STT output, pipe a real PCM capture in instead of
the silence generator.

    python scripts/smoke_ws.py ws://localhost:8080/ws/cam
"""

from __future__ import annotations

import asyncio
import json
import sys

from websockets.asyncio.client import connect

PROBE_SECONDS = 6.0


async def run(url: str) -> int:
    async with connect(url, max_size=None) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "hardware_token": "dev-token",
                    "sample_rate": 16_000,
                    "encoding": "linear16",
                    "client_version": "smoke/0.1",
                }
            )
        )

        silence_chunk = b"\x00" * (16_000 * 2 // 25)  # 40ms of s16le @16k
        stop = asyncio.Event()
        saw_error = False

        async def pump() -> None:
            while not stop.is_set():
                try:
                    await ws.send(silence_chunk)
                except Exception:
                    return
                await asyncio.sleep(0.04)

        async def drain() -> None:
            nonlocal saw_error
            async for msg in ws:
                if isinstance(msg, bytes):
                    print(f"[binary {len(msg)} bytes]")
                    continue
                payload = json.loads(msg)
                print(json.dumps(payload))
                if payload.get("type") == "error":
                    saw_error = True
                    stop.set()
                    return

        pump_task = asyncio.create_task(pump())
        drain_task = asyncio.create_task(drain())
        try:
            await asyncio.wait_for(stop.wait(), timeout=PROBE_SECONDS)
        except TimeoutError:
            pass
        finally:
            stop.set()
            pump_task.cancel()
            drain_task.cancel()

        if saw_error:
            print("SMOKE: FAIL — server emitted an error frame", file=sys.stderr)
            return 1
        print("SMOKE: OK — session held open with no server-side errors", file=sys.stderr)
        return 0


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8080/ws/cam"
    raise SystemExit(asyncio.run(run(url)))
