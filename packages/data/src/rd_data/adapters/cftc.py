"""CFTC Commitments of Traders via the public Socrata API (keyless; PROMPT §5.2).

Report families: legacy, disaggregated and Traders in Financial Futures (TFF), each futures-only or futures-and-
options combined. Dataset IDs live in ``config/providers.yaml``-style constants below and are verified by the
recording workflow (docs/vendor-notes/cftc.md). Positions are as of the report date (a Tuesday); the CFTC
publishes them the following Friday — freshness is judged on the report date.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, ClassVar, Literal, cast

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
    QA_CONTEXT,
    DataQualityReport,
    check_cadence,
    check_open_interest_moves,
    check_positioning,
    check_provider_order,
    with_schema_rejections,
)
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, DataType, PositionCounts, PositioningReport

ReportType = Literal["legacy", "disaggregated", "tff"]

#: Socrata dataset identifiers: (report type, futures_only) -> dataset id.
DATASETS: Mapping[tuple[ReportType, bool], str] = {
    ("legacy", True): "6dca-aqww",
    ("legacy", False): "jun7-fc8e",
    ("disaggregated", True): "72hh-3qpy",
    ("disaggregated", False): "kh3c-gbw2",
    ("tff", True): "gpe5-46if",
    ("tff", False): "yw9f-hn96",
}

#: category -> (long field, short field, spreading field or None); alternatives cover known CFTC column quirks.
FIELDS: Mapping[ReportType, Mapping[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...] | None]]] = {
    "legacy": {
        "noncommercial": (
            ("noncomm_positions_long_all",),
            ("noncomm_positions_short_all",),
            ("noncomm_postions_spread_all", "noncomm_positions_spread_all"),
        ),
        "commercial": (("comm_positions_long_all",), ("comm_positions_short_all",), None),
        "nonreportable": (("nonrept_positions_long_all",), ("nonrept_positions_short_all",), None),
    },
    "disaggregated": {
        "producer_merchant": (
            ("prod_merc_positions_long", "prod_merc_positions_long_all"),
            ("prod_merc_positions_short", "prod_merc_positions_short_all"),
            None,
        ),
        "swap_dealers": (
            ("swap_positions_long_all",),
            ("swap__positions_short_all", "swap_positions_short_all"),
            ("swap__positions_spread_all", "swap_positions_spread_all"),
        ),
        "managed_money": (
            ("m_money_positions_long_all",),
            ("m_money_positions_short_all",),
            ("m_money_positions_spread", "m_money_positions_spread_all"),
        ),
        "other_reportables": (
            ("other_rept_positions_long", "other_rept_positions_long_all"),
            ("other_rept_positions_short", "other_rept_positions_short_all"),
            ("other_rept_positions_spread", "other_rept_positions_spread_all"),
        ),
        "nonreportable": (("nonrept_positions_long_all",), ("nonrept_positions_short_all",), None),
    },
    "tff": {
        "dealer": (("dealer_positions_long_all",), ("dealer_positions_short_all",), ("dealer_positions_spread_all",)),
        "asset_manager": (
            ("asset_mgr_positions_long", "asset_mgr_positions_long_all"),
            ("asset_mgr_positions_short", "asset_mgr_positions_short_all"),
            ("asset_mgr_positions_spread", "asset_mgr_positions_spread_all"),
        ),
        "leveraged_funds": (
            ("lev_money_positions_long", "lev_money_positions_long_all"),
            ("lev_money_positions_short", "lev_money_positions_short_all"),
            ("lev_money_positions_spread", "lev_money_positions_spread_all"),
        ),
        "other_reportables": (
            ("other_rept_positions_long", "other_rept_positions_long_all"),
            ("other_rept_positions_short", "other_rept_positions_short_all"),
            ("other_rept_positions_spread", "other_rept_positions_spread_all"),
        ),
        "nonreportable": (("nonrept_positions_long_all",), ("nonrept_positions_short_all",), None),
    },
}
_MARKET_CODE_CHARS = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+")


def release_time(as_of: date) -> datetime:
    """Scheduled public release: the Friday after the Tuesday position date, 15:30 ET (20:30 UTC in winter, 19:30
    in summer; we use 20:30 UTC, the later of the two, so data is never judged older than it is). Holiday weeks
    release later, which only delays the stale label — never the reverse (D-027)."""
    return datetime.combine(as_of + timedelta(days=3), time(20, 30, tzinfo=UTC))


def _first(row: Mapping[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        if name in row:
            return as_float(row[name])
    return None


class CftcAdapter(BaseAdapter):
    name: ClassVar[str] = "cftc"
    asset_classes = frozenset(
        {AssetClass.FX, AssetClass.COMMODITY, AssetClass.FUTURE, AssetClass.RATES, AssetClass.CRYPTO}
    )
    licence = public_domain("Source: U.S. Commodity Futures Trading Commission, Commitments of Traders")
    # Socrata allows unauthenticated use with throttling; stay polite.
    rate_limit = RateLimit(per_second=2, per_minute=60)
    allowed_hosts = frozenset({"publicreporting.cftc.gov"})
    supported = frozenset({RequestKind.COT})
    cache_ttl_s: ClassVar[Mapping[RequestKind, int]] = {RequestKind.COT: 6 * 3600}
    params_allowed: ClassVar[Mapping[RequestKind, frozenset[str]]] = {
        RequestKind.COT: frozenset({"report_type", "market_code", "weeks", "futures_only"})
    }
    base_url: ClassVar[str] = "https://publicreporting.cftc.gov/resource"

    def _validate(self, request: DataRequest) -> None:
        report_type = require_param(request, "report_type", self.name)
        if report_type not in ("legacy", "disaggregated", "tff"):
            raise InvalidArgs(f"cftc: unknown report_type {report_type!r}", provider=self.name)
        code = str(require_param(request, "market_code", self.name))
        if not code or not set(code) <= _MARKET_CODE_CHARS or len(code) > 12:
            raise InvalidArgs(f"cftc: invalid market_code {code!r}", provider=self.name)
        weeks = request.params.get("weeks", 156)
        if not isinstance(weeks, int) or isinstance(weeks, bool) or not 1 <= weeks <= 1000:
            raise InvalidArgs("cftc: weeks must be an integer between 1 and 1000", provider=self.name)
        if not isinstance(request.params.get("futures_only", True), bool):
            raise InvalidArgs("cftc: futures_only must be a boolean", provider=self.name)

    def _dataset(self, request: DataRequest) -> str:
        report_type = cast("ReportType", request.params["report_type"])
        futures_only = bool(request.params.get("futures_only", True))
        return DATASETS[(report_type, futures_only)]

    async def _download(self, request: DataRequest) -> RawPayload:
        code = str(request.params["market_code"])
        params = {
            "$where": f"cftc_contract_market_code='{code}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            # never fewer than QA_CONTEXT weeks, so the latest week is checked against its history; trimmed after QA
            "$limit": max(int(request.params.get("weeks", 156)), QA_CONTEXT),
        }
        result = await self.http.get(f"{self.base_url}/{self._dataset(request)}.json", params)
        return RawPayload.from_results([result], self.ctx.clock.now())

    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        report_type = cast("ReportType", request.params["report_type"])
        futures_only = bool(request.params.get("futures_only", True))
        rows = orjson.loads(raw.bodies[0])
        if not isinstance(rows, list):
            raise ProviderDown(
                f"cftc: malformed response (expected a list, got {type(rows).__name__})", provider=self.name
            )
        if not rows:
            raise NotFound(
                f"cftc: no {report_type} rows for market {request.params['market_code']}", provider=self.name
            )
        records: list[PositioningReport] = []
        rejected = 0
        asked = str(request.params["market_code"])
        kind = "FutOnly" if futures_only else "Combined"
        for row in rows:
            if str(row.get("cftc_contract_market_code")) != asked or row.get("futonly_or_combined") != kind:
                rejected += 1  # a row for another market or report kind: never relabel it as the one requested
                continue
            try:
                records.append(self._row(row, report_type, futures_only=futures_only))
            except (KeyError, ValueError, TypeError):
                rejected += 1
        order = check_provider_order([r.as_of for r in records], descending=True)  # requested newest first
        records.sort(key=lambda r: r.as_of)
        base = check_positioning(records, subject=f"cftc:{report_type}:{request.params['market_code']}")
        cadence = check_cadence([r.as_of for r in records], period=timedelta(days=7))
        oi_outliers, oi_moves = check_open_interest_moves(records)
        quality = with_schema_rejections(
            DataQualityReport(
                base.subject, (order, *base.checks, cadence, oi_outliers, oi_moves), oi_outliers.indices, len(records)
            ),
            rejected,
        )
        held = [r for i, r in enumerate(records) if i in set(oi_outliers.indices)]
        records = [r for i, r in enumerate(records) if i not in set(oi_outliers.indices)]
        records = records[-int(request.params.get("weeks", 156)) :]
        first = records[0].as_of if records else (held[-1].as_of if held else None)
        quarantined = tuple(r for r in held if first is None or r.as_of >= first)
        as_of = records[-1].as_of if records else None
        released = release_time(as_of) if as_of else None
        return ParseResult(
            tuple(records),
            quality,
            as_of,
            FreshnessSpec(DataType.COT),
            source_tz="America/New_York",
            notes=("Positions as of Tuesday; released by the CFTC on the following Friday.",),
            freshness_as_of=released,
            quarantined=quarantined,
        )

    def _row(self, row: Mapping[str, Any], report_type: ReportType, *, futures_only: bool) -> PositioningReport:
        categories: dict[str, PositionCounts] = {}
        for category, (long_f, short_f, spread_f) in FIELDS[report_type].items():
            long_v, short_v = _first(row, long_f), _first(row, short_f)
            if long_v is None or short_v is None:
                raise KeyError(f"missing {category} fields")
            spread_v = _first(row, spread_f) if spread_f else None
            categories[category] = PositionCounts(long=long_v, short=short_v, spreading=spread_v)
        oi = as_float(row.get("open_interest_all"))
        if oi is None:
            raise KeyError("open_interest_all")
        report_date = str(row["report_date_as_yyyy_mm_dd"])[:10]
        return PositioningReport(
            market_code=str(row["cftc_contract_market_code"]),
            market_name=str(row.get("market_and_exchange_names") or row.get("contract_market_name") or ""),
            report_type=report_type,
            futures_only=futures_only,
            as_of=date.fromisoformat(report_date),
            open_interest=oi,
            categories=categories,
            exchange=str(row["market_and_exchange_names"]).rsplit(" - ", 1)[-1]
            if " - " in str(row.get("market_and_exchange_names", ""))
            else None,
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        """Requests the CI recording workflow captures: every dataset in :data:`DATASETS`, futures-only and
        combined (EUR FX TFF, WTI crude disaggregated, S&P 500 legacy)."""
        markets = {"tff": "099741", "disaggregated": "067651", "legacy": "13874A"}
        plan = []
        for futures_only in (True, False):
            for report_type, code in markets.items():
                params: dict[str, str | int | bool] = {"report_type": report_type, "market_code": code, "weeks": 60}
                if not futures_only:
                    params["futures_only"] = False
                plan.append(DataRequest(kind=RequestKind.COT, params=params))
        return plan
