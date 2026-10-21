"""Provider adapter contract (PROMPT §5.1) and the shared fetch pipeline.

Every adapter declares its name, asset classes, licence terms, published rate limit and allow-listed hosts, and
implements two methods: ``_download`` (raw bodies) and ``_parse`` (canonical records + data-quality report).
:meth:`BaseAdapter.fetch` wraps them with: request validation → feature flag → cache → rate limit / retries /
circuit breaker (inside :class:`~rd_data.http.HttpFetcher`) → normalisation → QA (blocking failures raise) →
freshness (blocked data raises, stale data is labelled) → licence tag for the current deployment mode.
"""

from __future__ import annotations

import base64
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol

import httpx
import orjson
from pydantic import BaseModel, ConfigDict

from rd_core.clock import Clock, SystemClock, isoformat_z
from rd_core.settings import DeploymentMode, Settings
from rd_data.errors import FeatureDisabled, InvalidArgs
from rd_data.freshness import Freshness, FreshnessResult, TradingCalendar, classify
from rd_data.http import FetchResult, HttpFetcher
from rd_data.licence import LicenceTag, LicenceTerms
from rd_data.quality import DataQualityReport
from rd_data.resilience import CircuitBreaker, CircuitStatus, RateLimit, RateLimiter, ResponseCache, request_fingerprint
from rd_data.schemas import AssetClass, CanonicalRecord, DataType

ParamValue = str | int | float | bool


class RequestKind(StrEnum):
    BARS = "bars"
    COT = "cot"
    COMPANY_FACTS = "company_facts"
    FILINGS = "filings"
    MACRO_SERIES = "macro_series"
    TREASURY_CURVE = "treasury_curve"
    CRYPTO_DERIVATIVES = "crypto_derivatives"
    INSTRUMENTS = "instruments"


class DataRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RequestKind
    params: dict[str, ParamValue] = {}

    def fingerprint(self, provider: str) -> str:
        return request_fingerprint(provider, self.kind.value, self.params)


@dataclass(frozen=True)
class RawPayload:
    """What came over the wire (or out of the cache) for one request: one body per HTTP call."""

    bodies: tuple[bytes, ...]
    urls: tuple[str, ...]
    retrieved_at: datetime
    #: Already-decoded data for adapters whose client library parses responses itself (CCXT). Never cached.
    decoded: Any = field(default=None, compare=False)

    def to_json(self) -> str:
        return orjson.dumps(
            {
                "bodies": [base64.b64encode(b).decode() for b in self.bodies],
                "urls": list(self.urls),
                "retrieved_at": isoformat_z(self.retrieved_at),
            }
        ).decode()

    @classmethod
    def from_json(cls, text: str) -> RawPayload:
        data = orjson.loads(text)
        return cls(
            tuple(base64.b64decode(b) for b in data["bodies"]),
            tuple(data["urls"]),
            datetime.fromisoformat(data["retrieved_at"].replace("Z", "+00:00")),
        )

    @classmethod
    def from_results(cls, results: Sequence[FetchResult], retrieved_at: datetime) -> RawPayload:
        return cls(tuple(r.body for r in results), tuple(r.url for r in results), retrieved_at)

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        for body in self.bodies:
            h.update(hashlib.sha256(body).digest())
        return "sha256:" + h.hexdigest()


@dataclass(frozen=True)
class FreshnessSpec:
    """How to judge freshness of a parse result (see :func:`rd_data.freshness.classify`)."""

    data_type: DataType
    calendar: TradingCalendar | None = None
    release_frequency: str | None = None
    next_release_at: datetime | None = None
    delay_minutes: int = 0


@dataclass(frozen=True)
class ParseResult:
    records: tuple[CanonicalRecord, ...]
    quality: DataQualityReport
    as_of: datetime | date | None
    freshness: FreshnessSpec | None
    source_tz: str = "UTC"
    quarantined: tuple[CanonicalRecord, ...] = ()
    notes: tuple[str, ...] = ()
    #: Stricter terms for this particular response (e.g. a copyrighted FRED series); never looser than the adapter's.
    licence_override: LicenceTerms | None = None


@dataclass(frozen=True)
class DataResponse:
    provider: str
    request: DataRequest
    records: tuple[CanonicalRecord, ...]
    quarantined: tuple[CanonicalRecord, ...]
    retrieved_at: datetime
    as_of: datetime | date | None
    source_tz: str
    licence: LicenceTerms
    licence_tag: LicenceTag
    quality: DataQualityReport
    freshness: FreshnessResult | None
    from_cache: bool
    content_hash: str
    source_urls: tuple[str, ...]
    notes: tuple[str, ...] = ()

    @property
    def freshness_label(self) -> str:
        return self.freshness.status.value if self.freshness else Freshness.FRESH.value


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    enabled: bool
    circuit: CircuitStatus
    licence_verified: bool


class ProviderAdapter(Protocol):
    name: str
    asset_classes: frozenset[AssetClass]
    licence: LicenceTerms
    rate_limit: RateLimit

    async def fetch(self, request: DataRequest) -> DataResponse: ...

    async def health(self) -> ProviderHealth: ...


@dataclass
class AdapterContext:
    """Everything an adapter needs from the outside world; tests swap the transport, clock and stores."""

    settings: Settings
    limiter: RateLimiter
    breaker: CircuitBreaker
    cache: ResponseCache | None = None
    clock: Clock = field(default_factory=SystemClock)
    transport: httpx.AsyncBaseTransport | None = None
    flags: Mapping[str, bool] = field(default_factory=dict)

    @property
    def deployment_mode(self) -> DeploymentMode:
        return self.settings.deployment_mode


class BaseAdapter(ABC):
    name: ClassVar[str]
    asset_classes: ClassVar[frozenset[AssetClass]]
    licence: ClassVar[LicenceTerms]
    rate_limit: ClassVar[RateLimit]
    allowed_hosts: ClassVar[frozenset[str]]
    supported: ClassVar[frozenset[RequestKind]]
    #: Feature flag that must be on (config/feature_flags.yaml, FEATURE_FLAGS); ``None`` = always available.
    feature_flag: ClassVar[str | None] = None
    #: Cache TTL per request kind in seconds (0 = do not cache).
    cache_ttl_s: ClassVar[Mapping[RequestKind, int]] = {}

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.http = HttpFetcher(
            provider=self.name,
            allowed_hosts=self.allowed_hosts,
            limiter=ctx.limiter,
            rate_limit=self.rate_limit,
            breaker=ctx.breaker,
            transport=ctx.transport,
            user_agent=self.user_agent(),
        )

    def user_agent(self) -> str:
        return "ResearchDesk/0.1 (+support@example.com)"

    @property
    def enabled(self) -> bool:
        return self.feature_flag is None or bool(self.ctx.flags.get(self.feature_flag, False))

    async def aclose(self) -> None:
        await self.http.aclose()

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            self.name, self.enabled, await self.ctx.breaker.status(self.name), self.licence.terms_verified
        )

    @abstractmethod
    async def _download(self, request: DataRequest) -> RawPayload:
        """Fetch raw bodies for the request (via ``self.http``)."""

    @abstractmethod
    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        """Normalise raw bodies into canonical records and a data-quality report."""

    def _validate(self, request: DataRequest) -> None:  # noqa: B027 - optional hook; default accepts the request
        """Adapter-specific parameter validation; raise :class:`InvalidArgs`."""

    async def fetch(self, request: DataRequest) -> DataResponse:
        if request.kind not in self.supported:
            raise InvalidArgs(f"{self.name} does not serve {request.kind.value}", provider=self.name)
        if not self.enabled:
            raise FeatureDisabled(f"{self.name} is disabled (feature flag {self.feature_flag!r})", provider=self.name)
        self._validate(request)
        fingerprint = request.fingerprint(self.name)
        raw: RawPayload | None = None
        from_cache = False
        if self.ctx.cache is not None and self.cache_ttl_s.get(request.kind, 0) > 0:
            cached = await self.ctx.cache.get(fingerprint)
            if cached is not None:
                raw, from_cache = RawPayload.from_json(cached), True
        if raw is None:
            raw = await self._download(request)
            if self.ctx.cache is not None and self.cache_ttl_s.get(request.kind, 0) > 0:
                await self.ctx.cache.put(fingerprint, raw.to_json(), self.cache_ttl_s[request.kind])
        parsed = self._parse(request, raw)
        parsed.quality.require_passed(provider=self.name)
        now = self.ctx.clock.now()
        freshness: FreshnessResult | None = None
        if parsed.freshness is not None and parsed.as_of is not None:
            spec = parsed.freshness
            freshness = classify(
                spec.data_type,
                parsed.as_of,
                now,
                delay_minutes=spec.delay_minutes,
                calendar=spec.calendar,
                release_frequency=spec.release_frequency,
                next_release_at=spec.next_release_at,
            )
            freshness.require_usable(provider=self.name)
        terms = parsed.licence_override or self.licence
        return DataResponse(
            provider=self.name,
            request=request,
            records=parsed.records,
            quarantined=parsed.quarantined,
            retrieved_at=raw.retrieved_at,
            as_of=parsed.as_of,
            source_tz=parsed.source_tz,
            licence=terms,
            licence_tag=terms.tag_for(self.ctx.deployment_mode),
            quality=parsed.quality,
            freshness=freshness,
            from_cache=from_cache,
            content_hash=raw.content_hash,
            source_urls=raw.urls,
            notes=parsed.notes,
        )


def require_param(request: DataRequest, name: str, provider: str) -> ParamValue:
    if name not in request.params:
        raise InvalidArgs(f"{provider}: missing parameter {name!r}", provider=provider)
    return request.params[name]


def as_float(value: Any) -> float | None:  # noqa: ANN401 - provider fields are untyped
    """Provider numbers often arrive as strings; empty or non-numeric becomes ``None``."""
    if value is None or value in {"", "."}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
