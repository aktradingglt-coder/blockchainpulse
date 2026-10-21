"""OpenTelemetry setup. Exports via OTLP/HTTP only when an endpoint is configured (D-012)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from rd_core.redaction import REDACTED, is_sensitive_key, redact_text
from rd_core.settings import Settings

#: Attributes holding a full URL: the query string is removed entirely (it can carry keys and personal data).
_URL_ATTRIBUTES = frozenset({"http.url", "url.full", "http.target", "url.path_and_query"})
#: Attributes holding only a query string: dropped.
_QUERY_ATTRIBUTES = frozenset({"url.query", "http.query"})


def _strip_query(value: str) -> str:
    if value.startswith("/"):
        return value.split("?", 1)[0]
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sanitize_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Span/event attributes safe to export: no query strings, no sensitive keys, secret-shaped text masked."""
    clean: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key in _QUERY_ATTRIBUTES:
            continue
        if isinstance(value, str):
            text = _strip_query(value) if key in _URL_ATTRIBUTES else value
            clean[key] = REDACTED if is_sensitive_key(key) and text else redact_text(text)
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            clean[key] = [redact_text(v) if isinstance(v, str) else v for v in value]
        else:
            clean[key] = value
    return clean


class RedactingSpanExporter(SpanExporter):
    """Wraps any exporter so spans leave the process only after URL/query/secret sanitisation (PROMPT §1.5)."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    @staticmethod
    def sanitize(span: ReadableSpan) -> ReadableSpan:
        events = [
            Event(name=e.name, attributes=sanitize_attributes(e.attributes), timestamp=e.timestamp) for e in span.events
        ]
        return ReadableSpan(
            name=redact_text(span.name),
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=sanitize_attributes(span.attributes),
            events=events,
            links=span.links,
            kind=span.kind,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([self.sanitize(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


_provider: TracerProvider | None = None


def configure_tracing(settings: Settings, *, service: str, exporter: SpanExporter | None = None) -> TracerProvider:
    """Install a tracer provider for this process. ``exporter`` overrides the OTLP exporter (tests)."""
    global _provider  # noqa: PLW0603 - one tracer provider per process
    if _provider is not None:
        if exporter is not None:  # an extra exporter (tests, diagnostics) attaches to the existing provider
            _provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(exporter)))
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
        provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(chosen)))
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)
