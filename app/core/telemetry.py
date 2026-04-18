"""OpenTelemetry bootstrap + tracer accessors.

We wire tracing on process boot so every vendor call (Deepgram, LLM, Cartesia)
emits a span on the same trace as its parent WebSocket session. The exporter
is OTLP/HTTP and is driven entirely by standard env vars
(`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, ...), so no vendor-specific
code lives here.

If `OTEL_EXPORTER_OTLP_ENDPOINT` is unset we install a no-op tracer provider —
spans become free-of-cost attribute builders and nothing is exported. That
keeps `dev` loops and unit tests silent while still exercising the same code
paths as prod.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_INITIALISED = False


def configure_tracing(service_name: str = "cam-cloud-proxy") -> None:
    """Idempotent setup. Safe to call multiple times (tests, lifespan, etc)."""
    global _INITIALISED
    if _INITIALISED:
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", service_name),
            "service.version": os.getenv("OTEL_SERVICE_VERSION", "0.1.0"),
            "deployment.environment": os.getenv("ENV", "dev"),
        }
    )
    provider = TracerProvider(resource=resource)

    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    trace.set_tracer_provider(provider)

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:
        pass

    _INITIALISED = True


def instrument_fastapi(app: Any) -> None:
    """Attach the FastAPI instrumentor. Called after `FastAPI()` is built."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        pass


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any):
    """Thin sugar over `tracer.start_as_current_span`. Swallows cross-cutting
    plumbing so vendor call-sites read as one-liners."""
    tracer = trace.get_tracer("cam.pipeline")
    with tracer.start_as_current_span(name) as s:
        for k, v in attributes.items():
            if v is None:
                continue
            s.set_attribute(k, v)
        try:
            yield s
        except Exception as exc:
            s.record_exception(exc)
            s.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise
