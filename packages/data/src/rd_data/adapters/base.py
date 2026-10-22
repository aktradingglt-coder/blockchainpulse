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
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Protocol

import httpx
import orjson
from pydantic import BaseModel, ConfigDict

from rd_core.clock import Clock, SystemClock, isoformat_z
from rd_core.settings import DeploymentMode, Settings
from rd_data.errors import DataError, DataQualityError, FeatureDisabled, InvalidArgs, ProviderDown
from rd_data.freshness import FreshnessResult, TradingCalendar, classify
from rd_data.http import FetchResult, HttpFetcher
from rd_data.licence import LicenceTag, LicenceTerms
from rd_data.quality import DataQualityReport
from rd_data.resilience import (
    PROBE_TOKEN,
    CircuitBreaker,
    CircuitStatus,
    RateLimit,
    RateLimiter,
    ResponseCache,
    request_fingerprint,
)
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
    bar_width: timedelta | None = None
    #: Fundamentals: when the next filing actually arrived, if it did but could not be used (e.g. held whole for scale
    #: errors). The figures shown are then superseded from that moment, not from when a filing would be expected.
    next_filing_at: datetime | None = None


@dataclass(frozen=True)
class ParseResult:
    records: tuple[CanonicalRecord, ...]
    quality: DataQualityReport
    as_of: datetime | date | None
    freshness: FreshnessSpec | None
    source_tz: str = "UTC"
    quarantined: tuple[CanonicalRecord, ...] = ()
    notes: tuple[str, ...] = ()
    #: Time freshness is measured from when it differs from ``as_of`` (e.g. COT: release date, not position date).
    freshness_as_of: datetime | date | None = None
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
        """``fresh`` / ``stale`` / ``blocked``, or ``not_applicable`` (historical windows, reference data)."""
        return self.freshness.status.value if self.freshness else "not_applicable"


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
    #: Parameter names each request kind accepts; anything else is refused (it would also split the cache key).
    params_allowed: ClassVar[Mapping[RequestKind, frozenset[str]]]
    #: Whether HTTP 403 means "not covered by the plan" for this vendor (a licence limit) rather than a bad key.
    forbidden_is_licence: ClassVar[bool] = False
    #: Settings field holding the credential this provider needs (``None`` = keyless), for the status page.
    credential_setting: ClassVar[str | None] = None

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
            record_ok=False,  # success is recorded after the body parses (_parse_checked)
            forbidden_is_licence=self.forbidden_is_licence,
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

    async def _from_cache(self, request: DataRequest, fingerprint: str) -> tuple[RawPayload | None, ParseResult | None]:
        """A cached payload and its parse, or (None, None) when absent or unusable (the entry is then dropped)."""
        assert self.ctx.cache is not None  # noqa: S101 - only called when caching is on
        cached = await self.ctx.cache.get(fingerprint)
        if cached is None:
            return None, None
        try:
            raw = RawPayload.from_json(cached)
            return raw, await self._parse_checked(request, raw, from_cache=True)
        except (*MALFORMED, ProviderDown):
            await self.ctx.cache.drop(fingerprint)
            return None, None

    async def _parse_checked(self, request: DataRequest, raw: RawPayload, *, from_cache: bool) -> ParseResult:
        """Run the parser; a payload it cannot understand becomes :class:`ProviderDown`, never a raw exception.

        A malformed live response counts as a provider failure for the circuit breaker.
        """
        try:
            parsed = self._parse(request, raw)
        except ProviderDown as exc:
            if not from_cache:
                await self.ctx.breaker.record_failure(self.name, str(exc))
            raise
        except MALFORMED as exc:
            detail = f"{self.name}: malformed response ({type(exc).__name__})"
            if not from_cache:
                await self.ctx.breaker.record_failure(self.name, detail)
            raise ProviderDown(detail, provider=self.name) from exc
        except DataError:
            # The provider answered with something we understood (e.g. "not found"): it is up.
            if not from_cache:
                await self.ctx.breaker.record_success(self.name)
            raise
        if not from_cache:
            await self.ctx.breaker.record_success(self.name)
        return parsed

    async def _download_checked(self, request: DataRequest) -> RawPayload:
        """Download; a client library choking on a malformed body (CCXT parses responses itself) is a provider
        failure, never a raw exception."""
        try:
            return await self._download(request)
        except DataError:
            raise
        except MALFORMED as exc:
            detail = f"{self.name}: malformed response ({type(exc).__name__})"
            await self.ctx.breaker.record_failure(self.name, detail)
            raise ProviderDown(detail, provider=self.name) from exc

    async def fetch(self, request: DataRequest) -> DataResponse:
        """Validated, quality-checked, freshness-labelled, licence-tagged data for ``request``."""
        probe = uuid.uuid4().hex
        token = PROBE_TOKEN.set(probe)  # one half-open probe covers every HTTP call of this fetch
        try:
            return await self._fetch(request)
        finally:
            PROBE_TOKEN.reset(token)
            await self.ctx.breaker.release_probe(self.name, probe)

    async def _fetch(self, request: DataRequest) -> DataResponse:
        if request.kind not in self.supported:
            raise InvalidArgs(f"{self.name} does not serve {request.kind.value}", provider=self.name)
        if not self.enabled:
            raise FeatureDisabled(f"{self.name} is disabled (feature flag {self.feature_flag!r})", provider=self.name)
        unknown = sorted(set(request.params) - self.params_allowed.get(request.kind, frozenset()))
        if unknown:
            raise InvalidArgs(
                f"{self.name}: unknown parameter(s) {unknown} for {request.kind.value}", provider=self.name
            )
        self._validate(request)
        fingerprint = request.fingerprint(self.name)
        cacheable = self.ctx.cache is not None and self.cache_ttl_s.get(request.kind, 0) > 0
        raw: RawPayload | None = None
        parsed: ParseResult | None = None
        if cacheable:
            raw, parsed = await self._from_cache(request, fingerprint)
        from_cache = parsed is not None
        if raw is None or parsed is None:
            raw = await self._download_checked(request)
            parsed = await self._parse_checked(request, raw, from_cache=False)
            # The status page is public: it names the failing checks, never the request (company, series, symbol).
            failing = sorted({c.name for c in parsed.quality.checks if not c.passed and c.blocking})
            await self.ctx.breaker.note_quality(self.name, ", ".join(failing) if failing else None)
            parsed.quality.require_passed(provider=self.name)
            if cacheable:  # only payloads that passed QA are cached
                assert self.ctx.cache is not None  # noqa: S101 - narrowed by ``cacheable``
                await self.ctx.cache.put(fingerprint, raw.to_json(), self.cache_ttl_s[request.kind])
        parsed.quality.require_passed(provider=self.name)
        if parsed.quarantined and not parsed.records:
            raise DataQualityError(
                f"{self.name}: every record was quarantined ({len(parsed.quarantined)}); nothing usable to show",
                provider=self.name,
            )
        now = self.ctx.clock.now()
        _require_not_future(parsed.as_of, now, provider=self.name)
        freshness: FreshnessResult | None = None
        if parsed.freshness is not None and parsed.as_of is not None:
            spec = parsed.freshness
            freshness = classify(
                spec.data_type,
                _freshness_origin(parsed, raw.retrieved_at, provider=self.name),
                now,
                delay_minutes=spec.delay_minutes,
                calendar=spec.calendar,
                release_frequency=spec.release_frequency,
                next_release_at=spec.next_release_at,
                bar_width=spec.bar_width,
                next_filing_expected_at=spec.next_filing_at,
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


#: Clock skew and timezone slack allowed when checking that data is not from the future.
FUTURE_TOLERANCE = timedelta(minutes=5)
#: How far an *estimated* scheduled release may lie after the moment we already held the data (summer-time offsets,
#: early publication). Beyond it the row cannot be genuine: its data was not yet published.
EARLY_RELEASE_TOLERANCE = timedelta(hours=12)


def _require_not_future(as_of: datetime | date | None, now: datetime, *, provider: str) -> None:
    """The newest record may not describe a time that has not happened yet."""
    if as_of is None:
        return
    future = as_of > now + FUTURE_TOLERANCE if isinstance(as_of, datetime) else as_of > (now + FUTURE_TOLERANCE).date()
    if future:
        raise DataQualityError(
            f"{provider}: latest record is dated {as_of.isoformat()}, after {now.isoformat()}", provider=provider
        )


def _freshness_origin(parsed: ParseResult, retrieved_at: datetime, *, provider: str) -> datetime | date:
    """Where freshness is measured from.

    An *estimated* release time (``freshness_as_of``) is capped at the moment the data was retrieved: a value
    cannot have been published after we already held it. If the estimate lies far beyond retrieval, the data was
    not yet scheduled for publication — a mislabelled or fabricated row — and the response is refused.
    """
    estimate = parsed.freshness_as_of
    if estimate is None:
        assert parsed.as_of is not None  # noqa: S101 - caller checks
        return parsed.as_of
    if not isinstance(estimate, datetime):
        return estimate
    if estimate > retrieved_at + EARLY_RELEASE_TOLERANCE:
        raise DataQualityError(
            f"{provider}: data as of {parsed.as_of} is not scheduled for release until {estimate.isoformat()}, "
            f"but was retrieved at {retrieved_at.isoformat()}",
            provider=provider,
        )
    return min(estimate, retrieved_at)


#: Exceptions a parser raises on a payload it cannot understand (wrong type, missing key, not JSON, bad number).
MALFORMED = (ValueError, KeyError, TypeError, AttributeError, IndexError, UnicodeDecodeError, OverflowError)


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
