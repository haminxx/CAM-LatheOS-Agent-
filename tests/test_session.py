"""Smoke test for the WebSocket pipeline in mock mode.

Run with:  pytest -q   (requires httpx + pytest-asyncio)
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    settings = Settings(env="dev", allow_unverified_tokens=True)
    return TestClient(create_app(settings))


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ws_handshake_and_mock_pipeline(client: TestClient) -> None:
    with client.websocket_connect("/ws/cam") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "hardware_token": "dev-token",
                    "sample_rate": 16_000,
                    "encoding": "linear16",
                    "client_version": "0.1.0",
                }
            )
        )
        # Feed ~6s of silence so the mock STT emits its scripted phrase.
        silence = b"\x00" * (16_000 * 2 // 25)  # 40ms chunks
        for _ in range(150):
            ws.send_bytes(silence)

        saw_final = False
        for _ in range(50):
            msg = json.loads(ws.receive_text())
            if msg.get("type") == "transcript" and msg.get("is_final"):
                saw_final = True
                break
        assert saw_final
