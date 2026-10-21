"""Kraken Futures adapter branches, driven by the real recorded cassettes plus targeted edits."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from rd_core.clock import FixedClock
from rd_core.settings import Settings
from rd_data.adapters.base import AdapterContext, DataRequest, RequestKind
from rd_data.adapters.kraken_futures import KrakenFuturesAdapter, _RoutedKrakenFutures
from rd_data.cassette import Cassette, ReplayTransport
from rd_data.errors import InvalidArgs, NotFound, ProviderDown
from rd_data.http import HttpFetcher
from rd_data.recording import cassette_name
from rd_data.resilience import CircuitBreaker, LocalRateLimiter, MemoryStateStore, RateLimit

CASSETTES = Path(__file__).parent / "cassettes" / "kraken_futures"
PLAN = KrakenFuturesAdapter.recording_plan()
BARS, DERIV = PLAN[0], PLAN[1]


def _cassette(request: DataRequest) -> Cassette:
    return Cassette.load(CASSETTES / cassette_name(request))


def _ctx(transport: httpx.AsyncBaseTransport, now: datetime) -> AdapterContext:
    return AdapterContext(
        settings=Settings(),
        limiter=LocalRateLimiter(),
        breaker=CircuitBreaker(MemoryStateStore()),
        clock=FixedClock(now),
        transport=transport,
    )


@pytest.mark.parametrize(
    "params",
    [
        {"symbol": "BTCUSD"},
        {"symbol": "BTC/USD:USD", "timeframe": "1m"},
        {"symbol": "BTC/USD:USD", "since_ms": -1},
        {"symbol": "BTC/USD:USD", "limit": 0},
    ],
)
async def test_bar_parameter_validation(params: dict[str, Any]) -> None:
    adapter = KrakenFuturesAdapter(_ctx(httpx.MockTransport(lambda r: httpx.Response(500)), datetime.now(tz=UTC)))
    with pytest.raises(InvalidArgs):
        await adapter.fetch(DataRequest(kind=RequestKind.BARS, params=params))


async def test_derivative_symbol_validation_and_unknown_symbol() -> None:
    cassette = _cassette(DERIV)
    recorded = cassette.replay_time
    adapter = KrakenFuturesAdapter(_ctx(httpx.MockTransport(lambda r: httpx.Response(500)), recorded))
    with pytest.raises(InvalidArgs):
        await adapter.fetch(DataRequest(kind=RequestKind.CRYPTO_DERIVATIVES, params={"symbols": "BTCUSD"}))
    adapter = KrakenFuturesAdapter(_ctx(ReplayTransport([cassette]), recorded))
    with pytest.raises(NotFound, match="DOGE/EUR:EUR"):
        await adapter.fetch(DataRequest(kind=RequestKind.CRYPTO_DERIVATIVES, params={"symbols": "DOGE/EUR:EUR"}))


async def test_forming_candle_is_dropped() -> None:
    cassette = _cassette(BARS)
    candles = json.loads(next(i.body for i in cassette.interactions if "charts" in i.url))["candles"]
    last = datetime.fromtimestamp(int(candles[-1]["time"]) / 1000, tz=UTC)
    bodies = {("instruments" if "instruments" in i.url else "charts"): i.body for i in cassette.interactions}

    def by_path(request: httpx.Request) -> httpx.Response:  # the request range now follows the injected clock
        return httpx.Response(200, text=bodies["instruments" if "instruments" in request.url.path else "charts"])

    adapter = KrakenFuturesAdapter(_ctx(httpx.MockTransport(by_path), last.replace(hour=12)))  # last candle open
    response = await adapter.fetch(BARS)
    assert len(response.records) == len(candles) - 1
    assert max(r.ts for r in response.records) < last  # type: ignore[union-attr]  # bars carry ts


async def test_provider_errors_are_typed() -> None:
    cassette = _cassette(BARS)
    recorded = cassette.replay_time
    instruments = next(i for i in cassette.interactions if "instruments" in i.url)

    def handler(request: httpx.Request) -> httpx.Response:
        if "instruments" in request.url.path:
            return httpx.Response(200, content=instruments.body.encode(), headers={"content-type": "application/json"})
        return httpx.Response(503)

    adapter = KrakenFuturesAdapter(_ctx(httpx.MockTransport(handler), recorded))
    with pytest.raises(ProviderDown):
        await adapter.fetch(BARS)
    adapter = KrakenFuturesAdapter(_ctx(httpx.MockTransport(handler), recorded))
    with pytest.raises(NotFound):
        await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "NOPE/USD:USD", "limit": 5}))


async def test_routed_exchange_refuses_private_calls() -> None:
    fetcher = HttpFetcher(
        provider="kraken_futures",
        allowed_hosts=frozenset({"futures.kraken.com"}),
        limiter=LocalRateLimiter(),
        rate_limit=RateLimit(),
        breaker=CircuitBreaker(MemoryStateStore()),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    ex = _RoutedKrakenFutures(fetcher, FixedClock(datetime(2026, 10, 21, tzinfo=UTC)))
    with pytest.raises(InvalidArgs, match="public GET"):
        await ex.fetch("https://futures.kraken.com/derivatives/api/v3/sendorder", method="POST", body="x")
    await ex.close()
    await fetcher.aclose()


async def test_cached_payload_without_decoded_data_is_refused() -> None:
    from rd_data.adapters.base import RawPayload  # noqa: PLC0415

    adapter = KrakenFuturesAdapter(_ctx(httpx.MockTransport(lambda r: httpx.Response(500)), datetime.now(tz=UTC)))
    raw = RawPayload((b"{}",), ("https://futures.kraken.com/x",), datetime.now(tz=UTC))
    with pytest.raises(ProviderDown, match="never served from cache"):
        adapter._parse(DERIV, raw)
