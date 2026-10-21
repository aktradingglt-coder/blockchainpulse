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
from rd_data.errors import InvalidArgs, NotFound, ProviderDown
from rd_data.licence import public_domain
from rd_data.quality import (
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
#: For series outside the catalogue: the latest a value of that frequency can be released after its period date.
DEFAULT_RELEASE_LAG: Mapping[str, timedelta] = {
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=62),
    "quarterly": timedelta(days=120),
    "annual": timedelta(days=400),
}
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
            raise InvalidArgs(f"eia: frequency must be one of {sorted(DEFAULT_RELEASE_LAG)}", provider=self.name)

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
            "length": int(request.params.get("length", 260)),
        }
        result = await self.http.get(f"https://api.eia.gov/v2/{route}/data/", params)
        return RawPayload.from_results([result], self.ctx.clock.now())

    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        payload = orjson.loads(raw.bodies[0])
        if "error" in payload:
            raise InvalidArgs(f"eia: {payload['error']}", provider=self.name)
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
            if str(row.get("series", series)) != series:
                rejected += 1  # a row for a different series: never relabel another series' data
                continue
            try:
                ts = datetime.strptime(str(row["period"])[:10], "%Y-%m-%d").replace(tzinfo=UTC)
                raw_times.append(ts)
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
            obs, subject=f"eia:{series}", lo=info.lo if info else None, hi=info.hi if info else None
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
            extra.append(check_cadence([o.ts for o in obs], period=_PERIODS[frequency]))
        quality = with_schema_rejections(
            DataQualityReport(report.subject, (*report.checks, *extra), report.quarantined, report.record_count),
            rejected,
        )
        latest = usable[-1].ts if usable else None
        lag = info.release_lag if info else DEFAULT_RELEASE_LAG[frequency]
        released = datetime.combine(latest.date(), time(0, tzinfo=UTC)) + lag if latest else None
        return ParseResult(
            tuple(usable),
            quality,
            latest,
            FreshnessSpec(DataType.MACRO_RELEASE, release_frequency=frequency),
            source_tz="America/New_York",
            quarantined=tuple(o for o in obs if o not in usable),
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
