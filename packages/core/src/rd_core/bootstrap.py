"""One call every process makes at start-up: settings, config, logging, tracing, error reporting."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import NoReturn

import orjson
from pydantic import ValidationError

from rd_core.config import AppConfig, load_config
from rd_core.errors import ConfigError
from rd_core.logging import configure_logging, get_logger
from rd_core.redaction import redact_text
from rd_core.sentry import configure_sentry
from rd_core.settings import Settings, get_settings
from rd_core.tracing import configure_tracing


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    config: AppConfig


def fatal_startup_error(service: str, exc: BaseException) -> NoReturn:
    """Report a start-up failure as one redacted JSON line on stderr (logging may not exist yet), then exit."""
    record = {
        "event": "service.startup_failed",
        "service": service,
        "level": "critical",
        "error": type(exc).__name__,
        "detail": redact_text(str(exc)),
    }
    sys.stderr.write(orjson.dumps(record).decode() + "\n")
    sys.stderr.flush()
    raise SystemExit(2)


def bootstrap(service: str, settings: Settings | None = None) -> Runtime:
    try:
        s = settings or get_settings()
    except ValidationError as exc:  # pydantic errors are input-free (hide_input_in_errors) and redacted again here
        fatal_startup_error(service, exc)
    configure_logging(service=service, level=s.log_level, fmt=s.log_format)
    try:
        config = load_config(s.config_dir)
        config.validate_flag_overrides(s.feature_flags)
    except ConfigError as exc:
        fatal_startup_error(service, exc)
    configure_tracing(s, service=service)
    sentry_on = configure_sentry(s, service=service)
    get_logger(__name__).info(
        "service.bootstrap",
        app_env=s.app_env.value,
        deployment_mode=s.deployment_mode.value,
        product=config.product.identity.product_name,
        sentry=sentry_on,
        otel_export=s.otel_exporter_otlp_endpoint is not None,
    )
    return Runtime(settings=s, config=config)
