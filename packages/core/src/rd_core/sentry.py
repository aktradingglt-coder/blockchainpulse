"""Optional Sentry error reporting; enabled only when SENTRY_DSN is set (D-012)."""

from __future__ import annotations

from typing import Any

from rd_core.redaction import redact
from rd_core.settings import Settings


def _scrub(event: Any, _hint: Any) -> Any:  # noqa: ANN401 - sentry before_send contract
    return redact(event)


def configure_sentry(settings: Settings, *, service: str) -> bool:
    if settings.sentry_dsn is None:
        return False
    import sentry_sdk  # noqa: PLC0415 - optional dependency path

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.app_env.value,
        server_name=service,
        send_default_pii=False,
        before_send=_scrub,
        traces_sample_rate=0.0,
    )
    return True
