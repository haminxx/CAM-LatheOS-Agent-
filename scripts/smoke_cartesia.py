"""Direct Cartesia TTS smoke.

Hits Cartesia with the configured voice ID and prints every frame we get
back, so a human can tell at a glance whether the handshake, voice ID, and
Cartesia-Version header are all mutually agreeable. Writes the concatenated
PCM to `/tmp/cartesia_smoke.raw` — `ffplay -f s16le -ar 24000 -ac 1 ...` to
listen.

    python scripts/smoke_cartesia.py [TEXT]
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from app.config import get_settings
from app.services.cartesia_tts import CartesiaClient


async def main(text: str, out: Path) -> int:
    settings = get_settings()
    if not settings.cartesia_api_key or not settings.cartesia_voice_id:
        print("CARTESIA_API_KEY or CARTESIA_VOICE_ID missing in .env", file=sys.stderr)
        return 2

    client = CartesiaClient(settings)
    t0 = time.perf_counter()
    first_ms: float | None = None
    total = 0

    with out.open("wb") as fp:
        async for chunk in client.synthesize(text):
            if first_ms is None:
                first_ms = (time.perf_counter() - t0) * 1000
                print(f"first byte: {first_ms:.0f}ms")
            total += len(chunk)
            fp.write(chunk)

    print(
        f"voice={settings.cartesia_voice_id!r} bytes={total} wall={time.perf_counter() - t0:.2f}s"
    )
    if total == 0:
        print("no audio received — check voice ID / api version / model_id", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "Hello from Cartesia."
    out = Path("cartesia_smoke.raw")
    raise SystemExit(asyncio.run(main(text, out)))
