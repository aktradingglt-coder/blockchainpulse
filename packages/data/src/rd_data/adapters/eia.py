"""EIA Open Data API v2: weekly petroleum stocks and natural-gas storage (PROMPT §5.2).

The API key travels as the ``api_key`` *query parameter* (EIA ignores headers); it is redacted from logs, evidence
and cassettes by :mod:`rd_data.http`. Responses cap at 5,000 rows; we request at most that many.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, ClassVar

import orjson

from rd_data.adapters.base import (
    BaseAdapter,
    DataRequest,
    FreshnessSpec,
    ParseResult,
    RawPayload,
    RequestKind,
    as_float,
    require_param,
)
from rd_data.errors import DataQualityError, InvalidArgs, NotFound, ProviderDown, RateLimited
from rd_data.licence import public_domain
from rd_data.quality import (
    QA_CONTEXT,
    CheckResult,
    DataQualityReport,
    check_cadence,
    check_provider_order,
    check_series,
    with_schema_rejections,
)
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, DataType, MacroObservation

_ROUTE = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+){1,4}$")
_SERIES = re.compile(r"^[A-Z0-9_.]{3,40}$")


@dataclass(frozen=True)
class SeriesInfo:
    route: str
    #: Scheduled release relative to the period date (the week-ending Friday), in UTC. EIA publishes at 10:30
    #: Eastern; 15:30 UTC is the later of the winter/summer offsets, so data is never judged older than it is.
    release_lag: timedelta
    #: Physically possible range in the series' unit; values outside are quarantined.
    lo: float | None = 0.0
    hi: float | None = None
    #: A level of inventory (not a flow): judged on relative changes, extreme latest moves held (D-031).
    stock: bool = True


_WPSR = timedelta(days=5, hours=15, minutes=30)  # Weekly Petroleum Status Report: the following Wednesday
_WNGSR = timedelta(days=6, hours=15, minutes=30)  # Weekly Natural Gas Storage Report: the following Thursday
#: Well-known weekly series. Stocks cannot be negative, and cannot exceed the storage that exists to hold them: the
#: upper bounds are round figures above total U.S. working storage for each product (D-033), so a value beyond them
#: is a unit or decimal error, not a market event.
SERIES: Mapping[str, SeriesInfo] = {
    "WCESTUS1": SeriesInfo("petroleum/stoc/wstk", _WPSR, 0.0, 800_000.0),  # crude excl. SPR, thousand barrels
    "WGTSTUS1": SeriesInfo("petroleum/stoc/wstk", _WPSR, 0.0, 400_000.0),  # motor gasoline, thousand barrels
    "WDISTUS1": SeriesInfo("petroleum/stoc/wstk", _WPSR, 0.0, 300_000.0),  # distillate, thousand barrels
    "NW2_EPG0_SWO_R48_BCF": SeriesInfo("natural-gas/stor/wkly", _WNGSR, 0.0, 6_000.0),  # Lower-48 storage, Bcf
}
SERIES_ROUTES: Mapping[str, str] = {k: v.route for k, v in SERIES.items()}
#: Release lag assumed for weekly series outside the catalogue: the earliest weekly schedule (Wednesday 15:30 UTC
#: after the week-ending Friday). Assuming the earliest never refuses a genuine release as "not yet published"; a
#: series released on Thursday is at worst labelled stale a day early.
DEFAULT_RELEASE_LAG: Mapping[str, timedelta] = {"weekly": _WPSR}
_PERIODS: Mapping[str, timedelta] = {"weekly": timedelta(days=7)}


class EiaAdapter(BaseAdapter):
    name: ClassVar[str] = "eia"
    asset_classes = frozenset({AssetClass.COMMODITY, AssetClass.MACRO})
    licence = public_domain("Source: U.S. Energy Information Administration")
    rate_limit = RateLimit(per_second=2, per_minute=60)
    allowed_hosts = frozenset({"api.eia.gov"})
    supported = frozenset({RequestKind.MACRO_SERIES})
    cache_ttl_s: ClassVar[Mapping[RequestKind, int]] = {RequestKind.MACRO_SERIES: 3600}
    params_allowed: ClassVar[Mapping[RequestKind, frozenset[str]]] = {
        RequestKind.MACRO_SERIES: frozenset({"series", "route", "length", "frequency"})
    }
    credential_setting = "eia_api_key"

    def _validate(self, request: DataRequest) -> None:
        if self.ctx.settings.eia_api_key is None:
            raise ProviderDown("eia: EIA_API_KEY is not configured", provider=self.name)
        series = str(require_param(request, "series", self.name))
        if not _SERIES.match(series):
            raise InvalidArgs(f"eia: invalid series id {series!r}", provider=self.name)
        route = str(request.params.get("route") or SERIES_ROUTES.get(series, ""))
        if not _ROUTE.match(route):
            raise InvalidArgs(f"eia: unknown route for series {series!r}; pass 'route'", provider=self.name)
        length = request.params.get("length", 260)
        if not isinstance(length, int) or not 1 <= length <= 5000:
            raise InvalidArgs("eia: length must be an integer between 1 and 5000", provider=self.name)
        frequency = str(request.params.get("frequency", "weekly"))
        if frequency not in DEFAULT_RELEASE_LAG:
            # Monthly/quarterly/annual EIA periods (YYYY-MM, YYYY-Qn, YYYY) and release calendars are not modelled
            # yet; refusing is better than mis-dating them.
            raise InvalidArgs("eia: only weekly series are supported", provider=self.name)

    async def _download(self, request: DataRequest) -> RawPayload:
        series = str(request.params["series"])
        route = str(request.params.get("route") or SERIES_ROUTES[series])
        key = self.ctx.settings.eia_api_key
        assert key is not None  # checked in _validate  # noqa: S101
        params: dict[str, Any] = {
            "api_key": key.get_secret_value(),
            "frequency": str(request.params.get("frequency", "weekly")),
            "data[0]": "value",
            "facets[series][]": series,
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "offset": 0,
            # never fewer than QA_CONTEXT weeks, so the latest week is checked against its history; trimmed after QA
            "length": max(int(request.params.get("length", 260)), QA_CONTEXT),
        }
        result = await self.http.get(f"https://api.eia.gov/v2/{route}/data/", params)
        return RawPayload.from_results([result], self.ctx.clock.now())

    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        payload = orjson.loads(raw.bodies[0])
        if "error" in payload:
            raise _error(payload, self.name)
        response = payload.get("response") or {}
        rows = response.get("data") or []
        series = str(request.params["series"])
        if not rows:
            raise NotFound(f"eia: no data for series {series}", provider=self.name)
        frequency = str(request.params.get("frequency", "weekly"))
        info = SERIES.get(series)
        obs: list[MacroObservation] = []
        raw_times: list[datetime] = []
        rejected = 0
        for row in rows:
            if str(row.get("series")) != series:
                rejected += 1  # a row for a different (or unnamed) series: never relabel another series' data
                continue
            try:
                ts = datetime.strptime(str(row["period"])[:10], "%Y-%m-%d").replace(tzinfo=UTC)
                raw_times.append(ts)
                if ts > raw.retrieved_at:
                    rejected += 1  # a period that has not happened yet
                    continue
                unit = str(row.get("units") or row.get("value-units") or "")
                obs.append(
                    MacroObservation(
                        series_id=series,
                        ts=ts,
                        value=as_float(row.get("value")),
                        unit=unit,
                        frequency=frequency,
                        note=row.get("series-description"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                rejected += 1
        obs.sort(key=lambda o: o.ts)
        report, usable = check_series(
            obs,
            subject=f"eia:{series}",
            lo=info.lo if info else None,
            hi=info.hi if info else None,
            # Unlisted weekly series are physical quantities too (stocks, production, flows): a x10 week is never
            # real, so they are held like the catalogue's stocks.
            level_like=info.stock if info else True,
        )
        units = sorted({o.unit for o in obs})
        extra = [
            check_provider_order(raw_times, descending=True),
            CheckResult(
                "consistent_units",
                passed=len(units) <= 1,
                blocking=True,
                detail=f"one unit ({units[0] if units else 'none'})" if len(units) <= 1 else f"mixed units {units}",
            ),
        ]
        if frequency in _PERIODS:
            # judged on the weeks asked for, not on the extra QA history behind them
            wanted_ts = [o.ts for o in obs][-int(request.params.get("length", 260)) :]
            extra.append(check_cadence(wanted_ts, period=_PERIODS[frequency]))
        quality = with_schema_rejections(
            DataQualityReport(report.subject, (*report.checks, *extra), report.quarantined, report.record_count),
            rejected,
        )
        wanted = int(request.params.get("length", 260))
        window = usable[-wanted:]
        first = window[0].ts if window else (obs[-1].ts if obs else raw.retrieved_at)
        held = tuple(o for o in obs if o not in usable and o.ts >= first)
        usable = window
        reported = [o for o in usable if o.value is not None]
        if usable and not reported:
            raise DataQualityError(
                f"{self.name}: every observation is reported as missing; nothing to date the series by",
                provider=self.name,
            )
        latest = reported[-1].ts if reported else None  # the newest week with a value, never a missing one
        lag = info.release_lag if info else DEFAULT_RELEASE_LAG[frequency]
        released = datetime.combine(latest.date(), time(0, tzinfo=UTC)) + lag if latest else None
        return ParseResult(
            tuple(usable),
            quality,
            latest,
            FreshnessSpec(DataType.MACRO_RELEASE, release_frequency=frequency),
            source_tz="America/New_York",
            quarantined=held,
            notes=(
                "Weekly values refer to the week ending on the period date; EIA releases them "
                "the following Wednesday (petroleum) or Thursday (natural gas). Freshness is judged from that release.",
            ),
            freshness_as_of=released,
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [
            DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1", "length": 60}),
            DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "NW2_EPG0_SWO_R48_BCF", "length": 60}),
        ]


def _error(payload: Mapping[str, Any], provider: str) -> Exception:
    """EIA's ``{"error": ..., "code": ...}`` bodies: key and server problems are outages, the rest bad requests."""
    message = str(payload.get("error"))
    try:
        code = int(payload.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    lowered = message.lower()
    if code == 429 or "rate limit" in lowered:
        return RateLimited(f"eia: {message}", provider=provider)
    if code in (401, 403) or code >= 500 or "api_key" in lowered or "unavailable" in lowered:
        return ProviderDown(f"eia: {message}", provider=provider)
    return InvalidArgs(f"eia: {message}", provider=provider)
