"""Twelve Data (commercial multi-asset vendor) — feature-flagged, no contract yet (PROMPT §2 rule 9, D-024).

Built fully and tested against a *schema-derived* fixture (labelled as such, D-011). Individual Twelve Data plans
forbid display to third parties; until a business licence is recorded the adapter is off (``provider_twelvedata``)
and its terms only allow display where the buyer brings their own key (self-hosted).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import orjson

from rd_data.adapters.base import (
    BaseAdapter,
    DataRequest,
    FreshnessSpec,
    ParseResult,
    RawPayload,
    RequestKind,
    require_param,
)
from rd_data.errors import DataQualityError, InvalidArgs, LicenceBlocked, NotFound, ProviderDown, RateLimited
from rd_data.freshness import ContinuousCalendar, ExchangeCalendar, TradingCalendar, WeekdayCalendar
from rd_data.licence import byo_key_only
from rd_data.quality import QA_CONTEXT, DataQualityReport, check_bars, check_provider_order
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, Bar, DataType

INTERVALS: Mapping[str, str] = {"1h": "1h", "4h": "4h", "1d": "1day", "1w": "1week"}
#: Twelve Data ``meta.type`` -> our asset class (decides the trading calendar).
META_TYPES: Mapping[str, str] = {
    "Digital Currency": "crypto",
    "Physical Currency": "fx",
    "Common Stock": "equity",
    "ETF": "equity",
    "Index": "equity",
}
WIDTHS: Mapping[str, timedelta] = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}
_SYMBOL = re.compile(r"^[A-Za-z0-9.:/^-]{1,20}$")


def calendar_for(asset_class: str) -> TradingCalendar:
    if asset_class == "crypto":
        return ContinuousCalendar()
    if asset_class == "equity":
        return ExchangeCalendar("XNYS")
    return WeekdayCalendar()


class TwelveDataAdapter(BaseAdapter):
    name: ClassVar[str] = "twelvedata"
    asset_classes = frozenset({AssetClass.FX, AssetClass.EQUITY, AssetClass.CRYPTO, AssetClass.COMMODITY})
    licence = byo_key_only("Data: Twelve Data")
    rate_limit = RateLimit(per_minute=8, per_day=800)  # free-tier figures; contract terms replace them
    allowed_hosts = frozenset({"api.twelvedata.com"})
    supported = frozenset({RequestKind.BARS})
    feature_flag = "provider_twelvedata"
    cache_ttl_s: ClassVar[Mapping[RequestKind, int]] = {RequestKind.BARS: 300}
    params_allowed: ClassVar[Mapping[RequestKind, frozenset[str]]] = {
        RequestKind.BARS: frozenset({"symbol", "interval", "asset_class", "outputsize"})
    }
    credential_setting = "twelvedata_api_key"

    def _api_key(self) -> str:
        key = self.ctx.settings.twelvedata_api_key
        if key is None or not key.get_secret_value():
            raise ProviderDown("twelvedata: TWELVEDATA_API_KEY is not configured", provider=self.name)
        return key.get_secret_value()

    def _validate(self, request: DataRequest) -> None:
        symbol = str(require_param(request, "symbol", self.name))
        if not _SYMBOL.match(symbol):
            raise InvalidArgs(f"twelvedata: invalid symbol {symbol!r}", provider=self.name)
        if str(request.params.get("interval", "1d")) not in INTERVALS:
            raise InvalidArgs(f"twelvedata: interval must be one of {sorted(INTERVALS)}", provider=self.name)
        if str(request.params.get("asset_class", "fx")) not in ("fx", "equity", "crypto", "commodity"):
            raise InvalidArgs("twelvedata: asset_class must be fx, equity, crypto or commodity", provider=self.name)
        size = request.params.get("outputsize", 200)
        if not isinstance(size, int) or not 1 <= size <= 5000:
            raise InvalidArgs("twelvedata: outputsize must be between 1 and 5000", provider=self.name)
        self._api_key()

    async def _download(self, request: DataRequest) -> RawPayload:
        params: dict[str, Any] = {
            "symbol": str(request.params["symbol"]),
            "interval": INTERVALS[str(request.params.get("interval", "1d"))],
            # never fewer than QA_CONTEXT bars, so the latest bar is checked for bad prints; trimmed after QA
            "outputsize": max(int(request.params.get("outputsize", 200)), QA_CONTEXT),
            "timezone": "UTC",
            "order": "ASC",
            "apikey": self._api_key(),
        }
        result = await self.http.get("https://api.twelvedata.com/time_series", params)
        return RawPayload.from_results([result], self.ctx.clock.now())

    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        body = orjson.loads(raw.bodies[0])
        if body.get("status") == "error":
            raise _error(body)
        symbol = str(request.params["symbol"])
        meta = body.get("meta") or {}
        answered = str(meta.get("symbol", ""))
        if answered.upper() != symbol.upper():
            raise DataQualityError(
                f"twelvedata: response is for symbol {answered!r}, request was {symbol!r}", provider=self.name
            )
        interval = str(request.params.get("interval", "1d"))
        width = WIDTHS[interval]
        # The instrument type comes from the vendor's answer, never from the caller: a crypto pair judged on the FX
        # calendar would look fresh over a weekend it actually traded through.
        asset_class = META_TYPES.get(str(meta.get("type", "")), str(request.params.get("asset_class", "fx")))
        cal = calendar_for(asset_class)
        # Judge "still forming" and "market open" at the moment the payload was fetched, not now: a cached payload
        # re-parsed later must not turn the partial bar it holds into a final close.
        fetched = raw.retrieved_at
        bars: list[Bar] = []
        rejected = 0
        forming = 0
        for row in body.get("values") or []:
            try:
                text = str(row["datetime"])
                fmt = "%Y-%m-%d %H:%M:%S" if " " in text else "%Y-%m-%d"
                ts = datetime.strptime(text, fmt).replace(tzinfo=UTC)
                if _closes_at(ts, interval, width, cal) > fetched:
                    forming += 1
                    continue  # candle still forming: never present an incomplete bar as a close
                bars.append(
                    Bar(
                        symbol=symbol,
                        interval=interval,
                        ts=ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]) if row.get("volume") not in (None, "") else None,
                    )
                )
            except (KeyError, TypeError, ValueError):
                rejected += 1
        if not bars and not rejected:
            raise NotFound(f"twelvedata: no closed {interval} bars for {symbol}", provider=self.name)
        order = check_provider_order([b.ts for b in bars], descending=False)  # requested order=ASC
        bars.sort(key=lambda b: b.ts)
        report, usable = check_bars(
            bars,
            subject=f"twelvedata:{symbol}:{interval}",
            calendar=cal,
            market_open_at_end=cal.is_open(fetched),
            bar_width=width,
            gap_window=int(request.params.get("outputsize", 200)),
            rejected_rows=rejected,
            split_dates=[] if asset_class == "equity" else None,
        )
        report = DataQualityReport(report.subject, (order, *report.checks), report.quarantined, report.record_count)
        wanted = int(request.params.get("outputsize", 200))
        window = usable[-wanted:]
        first = window[0].ts if window else (bars[-1].ts if bars else fetched)
        quarantined = tuple(b for b in bars if b not in usable and b.ts >= first)
        usable = window
        as_of = usable[-1].ts if usable else None
        delay = self.licence.delay_minutes  # the plan's delay, from the recorded terms, never from the caller
        if interval == "1d":
            spec = FreshnessSpec(DataType.DAILY_BAR, cal)
            freshness_as_of = None
        else:
            spec = FreshnessSpec(DataType.INTERVAL_BAR, cal, delay_minutes=delay, bar_width=width)
            freshness_as_of = as_of + width if as_of is not None else None
        notes = (f"{forming} forming bar(s) dropped.",) if forming else ()
        return ParseResult(
            tuple(usable), report, as_of, spec, quarantined=quarantined, notes=notes, freshness_as_of=freshness_as_of
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "interval": "1d", "outputsize": 30})]


def _closes_at(ts: datetime, interval: str, width: timedelta, cal: TradingCalendar) -> datetime:
    """When the bar labelled ``ts`` closes: the session close for daily bars, else the end of its interval."""
    if interval == "1d":
        return cal.closes_after(ts.date() - timedelta(days=1), 1)[0]
    return ts + width


def _error(body: Mapping[str, Any]) -> Exception:
    """Twelve Data answers errors with HTTP 200 and ``{"status": "error", "code": ..., "message": ...}``."""
    code = int(body.get("code", 0) or 0)
    message = str(body.get("message", "error"))
    if code == 429:
        return RateLimited(f"twelvedata: {message}", provider="twelvedata")
    if code >= 500:
        return ProviderDown(f"twelvedata: server error {code}: {message}", provider="twelvedata")
    if code == 401:
        return ProviderDown(f"twelvedata: authorization rejected ({code}); check the API key", provider="twelvedata")
    if code == 403:
        # Twelve Data answers 403 when the plan does not cover this symbol or endpoint: a licence limit for this
        # request, not an outage of the provider (it must not open the breaker for every other symbol).
        return LicenceBlocked(f"twelvedata: not covered by the plan: {message}", provider="twelvedata")
    if code == 404 or "not found" in message.lower():
        return NotFound(f"twelvedata: {message}", provider="twelvedata")
    return InvalidArgs(f"twelvedata: {message}", provider="twelvedata")
