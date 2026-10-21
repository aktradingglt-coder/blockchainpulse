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
from rd_data.quality import (
    CheckResult,
    DataQualityReport,
    check_duplicates,
    check_provider_order,
    check_range,
    scan_spikes,
    with_schema_rejections,
)
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
#: Tenors the Treasury has published every business day for decades; a header without them means the format changed.
REQUIRED_COLUMNS = ("Date", "1 Mo", "3 Mo", "6 Mo", "1 Yr", "2 Yr", "5 Yr", "10 Yr", "30 Yr")
#: Possible range for a par yield in percent; anything outside is a bad print (quarantined).
YIELD_RANGE = (-5.0, 30.0)
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
        year = int(request.params["year"])
        text = raw.bodies[0].decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        header = [h.strip() for h in reader.fieldnames or []]
        missing_columns = [c for c in REQUIRED_COLUMNS if c not in header]
        points: list[TreasuryYieldPoint] = []
        raw_dates: list[datetime] = []
        rejected = 0
        for row in reader:
            try:
                ts = datetime.strptime(row["Date"], "%m/%d/%Y").replace(tzinfo=UTC)
            except (KeyError, TypeError, ValueError):
                rejected += 1
                continue
            raw_dates.append(ts)
            if ts.year != year:
                rejected += 1  # a row from another year in this year's file: the response is not what we asked for
                continue
            for column, tenor in COLUMNS.items():
                value = (row.get(column) or "").strip()
                if not value:
                    continue  # tenor not published that day (e.g. 4M before 2022)
                try:
                    points.append(TreasuryYieldPoint(ts=ts, tenor=tenor, yield_pct=float(value)))
                except ValueError:
                    rejected += 1
        if not points and not rejected and not missing_columns:
            raise NotFound(f"us_treasury: no curve rows for {year}", provider=self.name)
        points.sort(key=lambda p: (p.ts, p.tenor))
        range_check = check_range([p.yield_pct for p in points], lo=YIELD_RANGE[0], hi=YIELD_RANGE[1], what="yield")
        bad_prints = _bad_prints_by_tenor(points, exclude=set(range_check.indices))
        quarantine = sorted({*range_check.indices, *bad_prints})
        usable = [p for i, p in enumerate(points) if i not in set(quarantine)]
        checks: list[CheckResult] = [
            CheckResult(
                "csv_header",
                passed=not missing_columns,
                blocking=True,
                detail=f"missing column(s) {missing_columns}" if missing_columns else "expected columns present",
            ),
            check_provider_order(raw_dates, descending=True),  # the Treasury CSV lists the newest day first
            check_duplicates([(p.ts, p.tenor) for p in points]),
            range_check,
            CheckResult(
                "outliers",
                passed=not bad_prints,
                blocking=False,
                detail=f"{len(bad_prints)} reverting outlier(s) quarantined" if bad_prints else "ok",
                indices=tuple(bad_prints),
            ),
        ]
        quality = with_schema_rejections(
            DataQualityReport(f"treasury:{year}", tuple(checks), tuple(quarantine), len(points)), rejected
        )
        as_of = usable[-1].ts if usable else None
        return ParseResult(
            tuple(usable),
            quality,
            as_of,
            FreshnessSpec(DataType.DAILY_BAR, ExchangeCalendar("XNYS")),
            source_tz="America/New_York",
            quarantined=tuple(p for i, p in enumerate(points) if i in set(quarantine)),
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [DataRequest(kind=RequestKind.TREASURY_CURVE, params={"year": 2026})]


def _bad_prints_by_tenor(points: list[TreasuryYieldPoint], *, exclude: set[int]) -> list[int]:
    """Indices (into ``points``) of yields that jump and come straight back, scanned per tenor in date order."""
    by_tenor: dict[str, list[int]] = {}
    for i, p in enumerate(points):
        if i not in exclude:
            by_tenor.setdefault(p.tenor, []).append(i)
    flagged: list[int] = []
    for idx in by_tenor.values():
        scan = scan_spikes([points[i].yield_pct for i in idx])
        flagged.extend(idx[j] for j in scan.bad_prints)
    return sorted(flagged)
