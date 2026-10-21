"""OpenTelemetry setup. Exports via OTLP/HTTP only when an endpoint is configured (D-012)."""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from rd_core.settings import Settings

_provider: TracerProvider | None = None


def configure_tracing(settings: Settings, *, service: str, exporter: SpanExporter | None = None) -> TracerProvider:
    """Install a tracer provider for this process. ``exporter`` overrides the OTLP exporter (tests)."""
    global _provider  # noqa: PLW0603 - one tracer provider per process
    if _provider is not None:
        if exporter is not None:  # an extra exporter (tests, diagnostics) attaches to the existing provider
            _provider.add_span_processor(BatchSpanProcessor(exporter))
        return _provider
    resource = Resource.create(
        {
            "service.name": service,
            "deployment.environment": settings.app_env.value,
            "service.namespace": "research-desk",
        }
    )
    provider = TracerProvider(resource=resource)
    chosen = exporter
    if chosen is None and settings.otel_exporter_otlp_endpoint is not None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415

        chosen = OTLPSpanExporter(endpoint=f"{str(settings.otel_exporter_otlp_endpoint).rstrip('/')}/v1/traces")
    if chosen is not None:
        provider.add_span_processor(BatchSpanProcessor(chosen))
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)
