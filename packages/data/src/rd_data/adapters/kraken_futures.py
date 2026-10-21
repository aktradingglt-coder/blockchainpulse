"""Kraken Futures via CCXT: perpetual OHLCV, funding, open interest, mark/index prices (PROMPT §5.2).

CCXT normalises the exchange's API; we route CCXT's HTTP through :class:`~rd_data.http.HttpFetcher` (by overriding
``fetch``) so the shared rate limiter, circuit breaker, typed errors, redacted logging and cassettes apply exactly as
for every other provider. Exchange terms for commercial display are unverified (docs/vendor-notes/crypto-exchanges.md):
raw values are displayable only in the self-hosted edition until the owner confirms the terms.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import ccxt.async_support as ccxt_async

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
from rd_data.freshness import ContinuousCalendar
from rd_data.http import HttpFetcher
from rd_data.licence import byo_key_only
from rd_data.quality import (
    CheckResult,
    DataQualityReport,
    check_bars,
    check_duplicates,
    check_range,
    with_schema_rejections,
)
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, Bar, CryptoDerivativeMetric, DataType

TIMEFRAMES: Mapping[str, tuple[str, timedelta]] = {
    "1h": ("1h", timedelta(hours=1)),
    "4h": ("4h", timedelta(hours=4)),
    "1d": ("1d", timedelta(days=1)),
}
#: Kraken perpetuals fund hourly.
FUNDING_INTERVAL_HOURS = 1.0
#: Physically possible range per derivative metric; values outside are quarantined, not shown.
METRIC_RANGES: Mapping[str, tuple[float | None, float | None]] = {
    "funding_rate": (-1.0, 1.0),  # a rate of ±100% per hourly interval does not occur; beyond it is a bad print
    "mark_price": (0.0, None),
    "index_price": (0.0, None),
    "open_interest": (0.0, None),
    "volume_24h": (0.0, None),
}
#: A symbol whose ticker is older than the §5.4 block threshold for funding/OI is quarantined from the response.
SYMBOL_MAX_AGE = timedelta(hours=6)


class _RoutedKrakenFutures(ccxt_async.krakenfutures):  # type: ignore[misc]  # ccxt classes are untyped
    """CCXT exchange whose HTTP goes through our fetcher and whose raw bodies are captured for evidence."""

    def __init__(self, fetcher: HttpFetcher) -> None:
        super().__init__({"enableRateLimit": False})
        self._rd_http = fetcher
        self.rd_bodies: list[bytes] = []
        self.rd_urls: list[str] = []

    async def fetch(self, url: str, method: str = "GET", headers: Any = None, body: Any = None) -> Any:  # noqa: ANN401, ARG002 - CCXT signature; public endpoints need no headers
        if method.upper() != "GET" or body:
            raise InvalidArgs("kraken_futures: only public GET endpoints are used", provider="kraken_futures")
        parts = urlsplit(url)
        base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        result = await self._rd_http.get(base, dict(parse_qsl(parts.query)))
        self.rd_bodies.append(result.body)
        self.rd_urls.append(result.url)
        text = result.text
        parsed = self.parse_json(text)
        self.handle_errors(result.status, "", url, method, dict(result.headers), text, parsed, {}, None)
        return parsed


def _require_known(symbols: list[str], markets: Mapping[str, Any]) -> None:
    unknown = [s for s in symbols if s not in markets]
    if unknown:
        raise NotFound(f"kraken_futures: unknown symbol(s) {unknown}", provider="kraken_futures")


class KrakenFuturesAdapter(BaseAdapter):
    name: ClassVar[str] = "kraken_futures"
    asset_classes = frozenset({AssetClass.CRYPTO})
    licence = byo_key_only("Market data: Kraken Futures (Payward, Inc.)")
    rate_limit = RateLimit(per_second=2, per_minute=60)
    allowed_hosts = frozenset({"futures.kraken.com"})
    supported = frozenset({RequestKind.BARS, RequestKind.CRYPTO_DERIVATIVES})
    # Not cached: CCXT parses responses itself, and funding/OI data is minutes-fresh anyway.
    cache_ttl_s: ClassVar[Mapping[RequestKind, int]] = {}

    def _validate(self, request: DataRequest) -> None:
        if request.kind is RequestKind.BARS:
            symbol = str(require_param(request, "symbol", self.name))
            if "/" not in symbol or ":" not in symbol:
                raise InvalidArgs(
                    "kraken_futures: symbol must be a unified perpetual symbol like BTC/USD:USD", provider=self.name
                )
            if str(request.params.get("timeframe", "1d")) not in TIMEFRAMES:
                raise InvalidArgs(f"kraken_futures: timeframe must be one of {sorted(TIMEFRAMES)}", provider=self.name)
            since = request.params.get("since_ms")
            if since is not None and (not isinstance(since, int) or since < 0):
                raise InvalidArgs("kraken_futures: since_ms must be epoch milliseconds", provider=self.name)
            limit = request.params.get("limit", 200)
            if not isinstance(limit, int) or not 1 <= limit <= 2000:
                raise InvalidArgs("kraken_futures: limit must be between 1 and 2000", provider=self.name)
        else:
            symbols = str(require_param(request, "symbols", self.name))
            if not all("/" in s and ":" in s for s in symbols.split(",")):
                raise InvalidArgs("kraken_futures: symbols must be comma-separated unified symbols", provider=self.name)

    async def _download(self, request: DataRequest) -> RawPayload:
        exchange = _RoutedKrakenFutures(self.http)
        try:
            await exchange.load_markets()
            if request.kind is RequestKind.BARS:
                tf = TIMEFRAMES[str(request.params.get("timeframe", "1d"))][0]
                since = request.params.get("since_ms")
                rows = await exchange.fetch_ohlcv(
                    str(request.params["symbol"]),
                    tf,
                    since=int(since) if since is not None else None,
                    limit=int(request.params.get("limit", 200)),
                )
                decoded: dict[str, Any] = {"ohlcv": rows}
            else:
                symbols = str(request.params["symbols"]).split(",")
                _require_known(symbols, exchange.markets)
                tickers = await exchange.fetch_tickers(symbols)
                decoded = {
                    "funding": {
                        s: exchange.parse_funding_rate(t["info"], exchange.market(s)) for s, t in tickers.items()
                    },
                    "tickers": tickers,
                }
        except (NotFound, InvalidArgs, ProviderDown):
            raise
        except ccxt_async.BadSymbol as exc:
            raise NotFound(f"kraken_futures: {exc}", provider=self.name) from exc
        except ccxt_async.BaseError as exc:
            raise ProviderDown(f"kraken_futures: {type(exc).__name__}", provider=self.name) from exc
        finally:
            await exchange.close()
        return RawPayload(tuple(exchange.rd_bodies), tuple(exchange.rd_urls), self.ctx.clock.now(), decoded)

    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        parsed = raw.decoded
        if not isinstance(parsed, dict):
            raise ProviderDown(
                "kraken_futures: no decoded payload (this provider is never served from cache)", provider=self.name
            )
        if request.kind is RequestKind.BARS:
            return self._parse_bars(request, parsed["ohlcv"])
        return self._parse_derivatives(parsed)

    def _parse_bars(self, request: DataRequest, rows: list[list[Any]]) -> ParseResult:
        symbol = str(request.params["symbol"])
        tf, width = TIMEFRAMES[str(request.params.get("timeframe", "1d"))]
        now = self.ctx.clock.now()
        bars: list[Bar] = []
        rejected = 0
        for row in rows:
            try:
                ts = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
                if ts + width > now:
                    continue  # candle still forming: never present an incomplete bar as a close
                bars.append(
                    Bar(
                        symbol=symbol,
                        interval=tf,
                        ts=ts,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=as_float(row[5]),
                    )
                )
            except (IndexError, TypeError, ValueError):
                rejected += 1
        if not bars and not rejected:
            raise NotFound(f"kraken_futures: no closed {tf} candles for {symbol}", provider=self.name)
        report, usable = check_bars(
            bars, subject=f"kraken_futures:{symbol}:{tf}", calendar=ContinuousCalendar(), rejected_rows=rejected
        )
        quarantined = tuple(b for b in bars if b not in usable)
        as_of = usable[-1].ts if usable else None
        notes = ["Perpetual trade-price candles; daily bars close at 00:00 UTC."]
        spec: FreshnessSpec | None = (
            FreshnessSpec(DataType.DAILY_BAR, ContinuousCalendar())
            if tf == "1d"
            else FreshnessSpec(DataType.INTERVAL_BAR, bar_width=width)
        )
        freshness_as_of = as_of + width if as_of is not None and tf != "1d" else None
        since = request.params.get("since_ms")
        if since is not None:
            window_end = datetime.fromtimestamp(int(since) / 1000, tz=UTC) + width * int(
                request.params.get("limit", 200)
            )
            if window_end < now - width:
                # An explicit historical window is not "latest data": freshness does not apply to it.
                spec = None
                notes.append(f"Historical window ending {window_end.date().isoformat()}; freshness not applicable.")
        return ParseResult(
            tuple(usable),
            report,
            as_of,
            spec,
            quarantined=quarantined,
            notes=tuple(notes),
            freshness_as_of=freshness_as_of,
        )

    def _parse_derivatives(self, parsed: Mapping[str, Any]) -> ParseResult:
        metrics: list[CryptoDerivativeMetric] = []
        rejected = 0
        for symbol, fr in parsed["funding"].items():
            info = parsed["tickers"][symbol]["info"]
            try:
                ts = datetime.fromisoformat(str(info["lastTime"]).replace("Z", "+00:00"))
                pairs = [
                    ("funding_rate", fr.get("fundingRate"), "rate per funding interval", FUNDING_INTERVAL_HOURS),
                    ("mark_price", fr.get("markPrice"), "USD", None),
                    ("index_price", fr.get("indexPrice"), "USD", None),
                    ("open_interest", as_float(info.get("openInterest")), "contracts", None),
                    ("volume_24h", as_float(info.get("vol24h")), "contracts", None),
                ]
                for metric, value, unit, interval in pairs:
                    if value is None:
                        continue
                    metrics.append(
                        CryptoDerivativeMetric(
                            exchange=self.name,
                            symbol=symbol,
                            metric=metric,
                            value=float(value),
                            ts=ts,
                            unit=unit,
                            funding_interval_hours=interval,
                        )
                    )
            except (KeyError, TypeError, ValueError):
                rejected += 1
        if not metrics and not rejected:
            raise NotFound("kraken_futures: no derivative metrics returned", provider=self.name)
        metrics.sort(key=lambda m: (m.symbol, m.metric))
        range_checks = [
            check_range([m.value if m.metric == metric else None for m in metrics], lo=lo, hi=hi, what=metric)
            for metric, (lo, hi) in METRIC_RANGES.items()
        ]
        out_of_range = {i for c in range_checks for i in c.indices}
        now = self.ctx.clock.now()
        stale_symbols = sorted({m.symbol for m in metrics if now - m.ts > SYMBOL_MAX_AGE})
        quarantine_idx = sorted(out_of_range | {i for i, m in enumerate(metrics) if m.symbol in stale_symbols})
        usable = [m for i, m in enumerate(metrics) if i not in set(quarantine_idx)]
        stale_check = _stale_symbols_check(stale_symbols) if stale_symbols else ()
        quality = with_schema_rejections(
            DataQualityReport(
                "kraken_futures:derivatives",
                (check_duplicates([(m.symbol, m.metric) for m in metrics]), *range_checks, *stale_check),
                tuple(quarantine_idx),
                len(metrics),
            ),
            rejected,
        )
        # Freshness follows the *oldest* symbol still in the response, so one stale ticker can never ride on a
        # fresh one; symbols past the block threshold are quarantined above and reported.
        pool = usable or metrics
        as_of = min((m.ts for m in pool), default=None)
        return ParseResult(
            tuple(usable),
            quality,
            as_of,
            FreshnessSpec(DataType.CRYPTO_DERIVATIVES),
            quarantined=tuple(m for i, m in enumerate(metrics) if i in set(quarantine_idx)),
            notes=("Funding rate = Kraken absolute funding / mark price, per hourly interval (CCXT).",),
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [
            DataRequest(
                kind=RequestKind.BARS,
                params={"symbol": "BTC/USD:USD", "timeframe": "1d", "since_ms": 1_780_272_000_000, "limit": 60},
            ),
            DataRequest(kind=RequestKind.CRYPTO_DERIVATIVES, params={"symbols": "BTC/USD:USD,ETH/USD:USD"}),
        ]


def _stale_symbols_check(symbols: list[str]) -> tuple[CheckResult, ...]:
    return (
        CheckResult(
            "stale_symbols",
            passed=False,
            blocking=False,
            detail=f"{len(symbols)} symbol(s) older than {SYMBOL_MAX_AGE} quarantined: {', '.join(symbols)}",
        ),
    )
