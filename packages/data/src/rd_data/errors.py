"""Typed data errors (PROMPT §7.4). Tools map each one to an explicit "data not available" outcome, never a guess."""

from __future__ import annotations

from rd_core.errors import ResearchDeskError


class DataError(ResearchDeskError):
    """Base class; ``provider`` names the source when known."""

    code = "data_error"

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class NotFound(DataError):
    code = "not_found"


class Stale(DataError):
    code = "stale"


class LicenceBlocked(DataError):
    code = "licence_blocked"


class ProviderDown(DataError):
    """Provider unreachable, circuit open, or retries exhausted on 5xx."""

    code = "provider_down"


class RateLimited(DataError):
    code = "rate_limited"

    def __init__(self, message: str, *, provider: str | None = None, retry_after_s: float | None = None) -> None:
        super().__init__(message, provider=provider)
        self.retry_after_s = retry_after_s


class InvalidArgs(DataError):
    code = "invalid_args"


class DataQualityError(DataError):
    """A blocking data-quality check failed: the data must not be used (PROMPT §5.3)."""

    code = "data_quality"


class FeatureDisabled(DataError):
    """The provider is behind a feature flag that is off (e.g. no vendor contract yet)."""

    code = "feature_disabled"
