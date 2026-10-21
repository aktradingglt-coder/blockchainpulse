"""One call every process makes at start-up: settings, config, logging, tracing, error reporting."""

from __future__ import annotations

from dataclasses import dataclass

from rd_core.config import AppConfig, load_config
from rd_core.logging import configure_logging, get_logger
from rd_core.sentry import configure_sentry
from rd_core.settings import Settings, get_settings
from rd_core.tracing import configure_tracing


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    config: AppConfig


def bootstrap(service: str, settings: Settings | None = None) -> Runtime:
    s = settings or get_settings()
    configure_logging(service=service, level=s.log_level, fmt=s.log_format)
    config = load_config(s.config_dir)
    config.validate_flag_overrides(s.feature_flags)
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
