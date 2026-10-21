"""U.S. Treasury daily par yield curve (home.treasury.gov CSV; public domain, keyless).

Primary source for U.S. yields while FRED stays off (D-010). One CSV per calendar year; values are percent.
The Treasury publishes on U.S. government-securities business days; we judge freshness against the NYSE calendar,
which differs on a few bond-market-only holidays (e.g. Columbus Day), where the curve is labelled stale, not blocked.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import ClassVar

from rd_data.adapters.base import BaseAdapter, DataRequest, FreshnessSpec, ParseResult, RawPayload, RequestKind
from rd_data.errors import InvalidArgs, NotFound
from rd_data.freshness import ExchangeCalendar
from rd_data.licence import public_domain
from rd_data.quality import DataQualityReport, check_duplicates, with_schema_rejections
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, DataType, TreasuryYieldPoint

#: CSV column -> canonical tenor.
COLUMNS: Mapping[str, str] = {
    "1 Mo": "1M",
    "1.5 Month": "1.5M",
    "2 Mo": "2M",
    "3 Mo": "3M",
    "4 Mo": "4M",
    "6 Mo": "6M",
    "1 Yr": "1Y",
    "2 Yr": "2Y",
    "3 Yr": "3Y",
    "5 Yr": "5Y",
    "7 Yr": "7Y",
    "10 Yr": "10Y",
    "20 Yr": "20Y",
    "30 Yr": "30Y",
}
URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all"


class TreasuryAdapter(BaseAdapter):
    name: ClassVar[str] = "us_treasury"
    asset_classes = frozenset({AssetClass.RATES, AssetClass.MACRO})
    licence = public_domain("Source: U.S. Department of the Treasury, Daily Treasury Par Yield Curve Rates")
    rate_limit = RateLimit(per_second=1, per_minute=30)
    allowed_hosts = frozenset({"home.treasury.gov"})
    supported = frozenset({RequestKind.TREASURY_CURVE})
    cache_ttl_s: ClassVar[Mapping[RequestKind, int]] = {RequestKind.TREASURY_CURVE: 3600}

    def _validate(self, request: DataRequest) -> None:
        year = request.params.get("year")
        if not isinstance(year, int) or not 1990 <= year <= 2100:
            raise InvalidArgs("us_treasury: 'year' must be an integer year", provider=self.name)

    async def _download(self, request: DataRequest) -> RawPayload:
        year = int(request.params["year"])
        params = {"type": "daily_treasury_yield_curve", "field_tdr_date_value": year, "page": "", "_format": "csv"}
        result = await self.http.get(URL.format(year=year), params)
        return RawPayload.from_results([result], self.ctx.clock.now())

    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        text = raw.bodies[0].decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        points: list[TreasuryYieldPoint] = []
        rejected = 0
        for row in reader:
            try:
                ts = datetime.strptime(row["Date"], "%m/%d/%Y").replace(tzinfo=UTC)
            except (KeyError, ValueError):
                rejected += 1
                continue
            for column, tenor in COLUMNS.items():
                value = (row.get(column) or "").strip()
                if not value:
                    continue  # tenor not published that day (e.g. 4M before 2022)
                try:
                    points.append(TreasuryYieldPoint(ts=ts, tenor=tenor, yield_pct=float(value)))
                except ValueError:
                    rejected += 1
        if not points and not rejected:
            raise NotFound(f"us_treasury: no curve rows for {request.params['year']}", provider=self.name)
        points.sort(key=lambda p: (p.ts, p.tenor))
        quality = with_schema_rejections(
            DataQualityReport(
                f"treasury:{request.params['year']}",
                (check_duplicates([(p.ts, p.tenor) for p in points]),),
                (),
                len(points),
            ),
            rejected,
        )
        as_of = points[-1].ts if points else None
        return ParseResult(
            tuple(points),
            quality,
            as_of,
            FreshnessSpec(DataType.DAILY_BAR, ExchangeCalendar("XNYS")),
            source_tz="America/New_York",
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [DataRequest(kind=RequestKind.TREASURY_CURVE, params={"year": 2026})]
