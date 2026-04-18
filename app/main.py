"""CAM Cloud Proxy — FastAPI entry point.

This module is deliberately small. Its only responsibilities are:
    * Construct singletons (settings, auth verifier, vendor clients)
    * Expose a health probe for EC2 target-group checks
    * Mount the single WebSocket route that drives the full pipeline

All per-connection logic lives in `app.ws.session.CamSession` so it
can be tested headlessly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings, get_settings
from app.core.auth import TokenVerifier
from app.core.logging import configure_logging, get_logger
from app.services.cartesia_tts import CartesiaClient
from app.services.deepgram_stt import DeepgramClient
from app.services.llm_router import LLMRouter
from app.ws.session import CamSession, SessionDeps

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("proxy.boot", version=__version__, env=settings.env)
        app.state.settings = settings
        app.state.verifier = TokenVerifier(settings)
        app.state.stt = DeepgramClient(settings)
        app.state.llm = LLMRouter(settings)
        app.state.tts = CartesiaClient(settings)
        try:
            yield
        finally:
            await app.state.llm.aclose()
            log.info("proxy.shutdown")

    app = FastAPI(
        title="CAM Cloud Proxy",
        version=__version__,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "version": __version__, "env": settings.env}
        )

    @app.websocket("/ws/cam")
    async def cam_socket(ws: WebSocket) -> None:
        deps = SessionDeps(
            settings=ws.app.state.settings,
            verifier=ws.app.state.verifier,
            stt=ws.app.state.stt,
            llm=ws.app.state.llm,
            tts=ws.app.state.tts,
        )
        session = CamSession(ws, deps)
        await session.run()

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        ws_ping_interval=20,
        ws_ping_timeout=20,
        reload=settings.env == "dev",
    )
