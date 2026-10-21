"""Twelve Data (commercial multi-asset vendor) — feature-flagged, no contract yet (PROMPT §2 rule 9, D-024).

Built fully and tested against a *schema-derived* fixture (labelled as such, D-011). Individual Twelve Data plans
forbid display to third parties; until a business licence is recorded the adapter is off (``provider_twelvedata``)
and its terms only allow display where the buyer brings their own key (self-hosted).
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
    require_param,
)
from rd_data.errors import InvalidArgs, NotFound, ProviderDown, RateLimited
from rd_data.freshness import ContinuousCalendar, ExchangeCalendar, TradingCalendar, WeekdayCalendar
from rd_data.licence import byo_key_only
from rd_data.quality import check_bars
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, Bar, DataType

INTERVALS: Mapping[str, str] = {"1h": "1h", "4h": "4h", "1d": "1day", "1w": "1week"}
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

    def _api_key(self) -> str:
        import os  # noqa: PLC0415 - vendor keys follow the <VENDOR>_API_KEY convention (PROMPT §19)

        key = os.environ.get("TWELVEDATA_API_KEY")
        if not key:
            raise ProviderDown("twelvedata: TWELVEDATA_API_KEY is not configured", provider=self.name)
        return key

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
            "outputsize": int(request.params.get("outputsize", 200)),
            "timezone": "UTC",
            "order": "ASC",
            "apikey": self._api_key(),
        }
        result = await self.http.get("https://api.twelvedata.com/time_series", params)
        return RawPayload.from_results([result], self.ctx.clock.now())

    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        body = orjson.loads(raw.bodies[0])
        if body.get("status") == "error":
            code = int(body.get("code", 0) or 0)
            message = str(body.get("message", "error"))
            if code == 429:
                raise RateLimited(f"twelvedata: {message}", provider=self.name)
            if code == 404 or "not found" in message.lower():
                raise NotFound(f"twelvedata: {message}", provider=self.name)
            raise InvalidArgs(f"twelvedata: {message}", provider=self.name)
        symbol = str(request.params["symbol"])
        interval = str(request.params.get("interval", "1d"))
        bars: list[Bar] = []
        rejected = 0
        for row in body.get("values") or []:
            try:
                text = str(row["datetime"])
                fmt = "%Y-%m-%d %H:%M:%S" if " " in text else "%Y-%m-%d"
                bars.append(
                    Bar(
                        symbol=symbol,
                        interval=interval,
                        ts=datetime.strptime(text, fmt).replace(tzinfo=UTC),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]) if row.get("volume") not in (None, "") else None,
                    )
                )
            except (KeyError, ValueError):
                rejected += 1
        if not bars and not rejected:
            raise NotFound(f"twelvedata: no bars for {symbol}", provider=self.name)
        bars.sort(key=lambda b: b.ts)
        asset_class = str(request.params.get("asset_class", "fx"))
        cal = calendar_for(asset_class)
        report, usable = check_bars(
            bars,
            subject=f"twelvedata:{symbol}:{interval}",
            calendar=cal,
            rejected_rows=rejected,
            split_dates=[] if asset_class == "equity" else None,
        )
        quarantined = tuple(b for b in bars if b not in usable)
        as_of = usable[-1].ts if usable else None
        spec = (
            FreshnessSpec(DataType.DAILY_BAR, cal)
            if interval == "1d"
            else FreshnessSpec(DataType.INTRADAY_DELAYED, delay_minutes=int(request.params.get("delay_minutes", 0)))
        )
        return ParseResult(tuple(usable), report, as_of, spec, quarantined=quarantined)
