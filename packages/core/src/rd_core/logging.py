"""Structured JSON logging with mandatory secret redaction (PROMPT §1.5, §4.1)."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import orjson
import structlog
from opentelemetry import trace

from rd_core.redaction import redact

_configured = False
_ROUTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi", "dramatiq", "alembic", "sqlalchemy")


def _redact_processor(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:  # noqa: ANN401
    redacted = redact(dict(event_dict))
    event_dict.clear()
    event_dict.update(redacted)
    return event_dict


def _trace_context(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:  # noqa: ANN401
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _service_processor(service: str) -> structlog.types.Processor:
    def add_service(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:  # noqa: ANN401
        event_dict.setdefault("service", service)
        return event_dict

    return add_service


def _orjson_dumps(obj: Any, default: Any = None) -> str:  # noqa: ANN401 - structlog renderer contract
    return orjson.dumps(obj, default=default).decode()


def configure_logging(*, service: str, level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog + stdlib logging once per process. Idempotent."""
    global _configured  # noqa: PLW0603 - process-wide logging setup is inherently global
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _service_processor(service),
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _trace_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_processor,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(serializer=_orjson_dumps)
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Servers and libraries that install their own handlers (uvicorn configures its loggers before importing the app)
    # are routed through the root handler so every line is JSON and redacted.
    for name in _ROUTED_LOGGERS:
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True
    # uvicorn's access log would print raw request lines (full query strings); the API logs requests itself.
    logging.getLogger("uvicorn.access").disabled = True
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
