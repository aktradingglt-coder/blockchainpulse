"""EIA Open Data API v2: weekly petroleum stocks and natural-gas storage (PROMPT §5.2).

The API key travels as the ``api_key`` *query parameter* (EIA ignores headers); it is redacted from logs, evidence
and cassettes by :mod:`rd_data.http`. Responses cap at 5,000 rows; we request at most that many.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
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
from rd_data.quality import check_series, with_schema_rejections
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, DataType, MacroObservation

_ROUTE = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+){1,4}$")
_SERIES = re.compile(r"^[A-Z0-9_.]{3,40}$")
#: Well-known weekly series: id -> route.
SERIES_ROUTES: Mapping[str, str] = {
    "WCESTUS1": "petroleum/stoc/wstk",  # U.S. crude oil ending stocks excl. SPR (thousand barrels)
    "WGTSTUS1": "petroleum/stoc/wstk",  # total motor gasoline stocks
    "WDISTUS1": "petroleum/stoc/wstk",  # distillate fuel oil stocks
    "NW2_EPG0_SWO_R48_BCF": "natural-gas/stor/wkly",  # Lower-48 working gas in storage (Bcf)
}


class EiaAdapter(BaseAdapter):
    name: ClassVar[str] = "eia"
    asset_classes = frozenset({AssetClass.COMMODITY, AssetClass.MACRO})
    licence = public_domain("Source: U.S. Energy Information Administration")
    rate_limit = RateLimit(per_second=2, per_minute=60)
    allowed_hosts = frozenset({"api.eia.gov"})
    supported = frozenset({RequestKind.MACRO_SERIES})
    cache_ttl_s: ClassVar[Mapping[RequestKind, int]] = {RequestKind.MACRO_SERIES: 3600}

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
        frequency = str(response.get("frequency") or request.params.get("frequency", "weekly"))
        obs: list[MacroObservation] = []
        rejected = 0
        for row in rows:
            try:
                ts = datetime.strptime(str(row["period"])[:10], "%Y-%m-%d").replace(tzinfo=UTC)
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
            except (KeyError, ValueError):
                rejected += 1
        obs.sort(key=lambda o: o.ts)
        quality = with_schema_rejections(check_series(obs, subject=f"eia:{series}"), rejected)
        as_of = obs[-1].ts if obs else None
        return ParseResult(
            tuple(obs),
            quality,
            as_of,
            FreshnessSpec(DataType.MACRO_RELEASE, release_frequency=frequency),
            source_tz="America/New_York",
            notes=(
                "Weekly values refer to the week ending on the period date; EIA releases them "
                "the following Wednesday (petroleum) or Thursday (natural gas).",
            ),
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [
            DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1", "length": 60}),
            DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "NW2_EPG0_SWO_R48_BCF", "length": 60}),
        ]
