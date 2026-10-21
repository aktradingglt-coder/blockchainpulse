"""Seeded bad data, freshness edge cases and malformed payloads pushed end to end through the adapters.

Most cases start from a *real* recorded cassette (``cassettes/``), corrupt one value the way a provider bug would,
and assert that the pipeline quarantines it, blocks it or raises a typed error — never passes it through.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from rd_core.clock import FixedClock
from rd_core.settings import Settings
from rd_data.adapters.base import AdapterContext, BaseAdapter, DataRequest, DataResponse, RequestKind
from rd_data.adapters.cftc import CftcAdapter
from rd_data.adapters.eia import EiaAdapter
from rd_data.adapters.fred import FredAdapter
from rd_data.adapters.kraken_futures import KrakenFuturesAdapter
from rd_data.adapters.sec_edgar import SecEdgarAdapter
from rd_data.adapters.treasury import TreasuryAdapter
from rd_data.adapters.twelvedata import TwelveDataAdapter
from rd_data.cassette import Cassette, RecordingTransport, ReplayTransport
from rd_data.errors import DataQualityError, InvalidArgs, ProviderDown, Stale
from rd_data.freshness import Freshness
from rd_data.http import redact_params
from rd_data.recording import cassette_name
from rd_data.resilience import (
    CircuitBreaker,
    CircuitState,
    LocalRateLimiter,
    MemoryStateStore,
    RateLimit,
    ResponseCache,
)
from rd_data.schemas import Bar, CryptoDerivativeMetric, FundamentalFact, MacroObservation, TreasuryYieldPoint

CASSETTES = Path(__file__).parent / "cassettes"
SETTINGS: dict[str, Any] = {
    "sec_edgar_user_agent": "Research Desk test ops@example.com",
    "eia_api_key": "k" * 20,
    "fred_api_key": "k" * 20,
    "twelvedata_api_key": "k" * 20,
}
Mutator = Callable[[str], str]


def _plan(cls: type[BaseAdapter], kind: RequestKind, **match: Any) -> DataRequest:
    plan: list[DataRequest] = cls.recording_plan()  # type: ignore[attr-defined]  # every adapter here defines it
    for request in plan:
        if request.kind is kind and all(request.params.get(k) == v for k, v in match.items()):
            return request
    raise AssertionError(f"no {kind} request in {cls.name}'s recording plan")


async def _replay(
    cls: type[BaseAdapter],
    request: DataRequest,
    mutate: dict[int, Mutator] | None = None,
    *,
    now: datetime | None = None,
    params: dict[str, Any] | None = None,
) -> DataResponse:
    """Replay the adapter's cassette for ``request``, with interaction bodies rewritten by ``mutate``."""
    cassette = Cassette.load(CASSETTES / cls.name / cassette_name(request))
    for i, fn in (mutate or {}).items():
        cassette.interactions[i].body = fn(cassette.interactions[i].body)
    ctx = AdapterContext(
        settings=Settings(**SETTINGS),
        limiter=LocalRateLimiter(),
        breaker=CircuitBreaker(MemoryStateStore()),
        clock=FixedClock(now or cassette.replay_time),
        transport=ReplayTransport([cassette]),
        flags={cls.feature_flag: True} if cls.feature_flag else {},
    )
    adapter = cls(ctx)
    try:
        req = request if params is None else DataRequest(kind=request.kind, params={**request.params, **params})
        return await adapter.fetch(req)
    finally:
        await adapter.aclose()


def _json_edit(edit: Callable[[Any], None]) -> Mutator:
    def apply(body: str) -> str:
        data = json.loads(body)
        edit(data)
        return json.dumps(data)

    return apply


# ------------------------------------------------------------------------------------------- bad prints (major 1)


async def test_bad_print_on_latest_kraken_candle_is_quarantined() -> None:
    request = _plan(KrakenFuturesAdapter, RequestKind.BARS)
    clean = await _replay(KrakenFuturesAdapter, request)
    candles_at = next(
        i
        for i, it in enumerate(Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request)).interactions)
        if '"candles"' in it.body
    )

    def x50(data: dict[str, Any]) -> None:
        last = data["candles"][-1]
        for k in ("open", "high", "low", "close"):
            last[k] = str(float(last[k]) * 50)

    seeded = await _replay(KrakenFuturesAdapter, request, {candles_at: _json_edit(x50)})
    bars = [r for r in seeded.records if isinstance(r, Bar)]
    assert len(seeded.quarantined) == 1
    assert max(b.close for b in bars) < 1_000_000
    assert seeded.as_of == [r for r in clean.records if isinstance(r, Bar)][-2].ts
    assert any("price_spikes" in w for w in seeded.quality.warnings)


async def test_bad_print_on_latest_twelvedata_bar_is_quarantined() -> None:
    request = _plan(TwelveDataAdapter, RequestKind.BARS)

    def x100(data: dict[str, Any]) -> None:
        last = data["values"][-1]
        for k in ("open", "high", "low", "close"):
            last[k] = f"{float(last[k]) * 100:.5f}"

    resp = await _replay(TwelveDataAdapter, request, {0: _json_edit(x100)})
    assert len(resp.quarantined) == 1
    assert all(isinstance(b, Bar) and b.close < 2 for b in resp.records)


@pytest.mark.parametrize("bad", [4.2e12, -5.0])
async def test_impossible_eia_inventory_is_quarantined(bad: float) -> None:
    request = _plan(EiaAdapter, RequestKind.MACRO_SERIES, series="WCESTUS1")

    def seed(data: dict[str, Any]) -> None:
        data["response"]["data"][0]["value"] = bad  # newest row (descending order)

    resp = await _replay(EiaAdapter, request, {0: _json_edit(seed)})
    assert all(isinstance(o, MacroObservation) and o.value != bad for o in resp.records)
    assert len(resp.quarantined) == 1
    assert any("value_range" in w for w in resp.quality.warnings)


async def test_eia_rows_for_another_series_are_rejected_not_relabelled() -> None:
    request = _plan(EiaAdapter, RequestKind.MACRO_SERIES, series="WCESTUS1")

    def other(data: dict[str, Any]) -> None:
        data["response"]["data"][0]["series"] = "WGTSTUS1"

    with pytest.raises(DataQualityError, match="schema"):
        await _replay(EiaAdapter, request, {0: _json_edit(other)})


def _csv_edit(edit: Callable[[list[list[str]]], None]) -> Mutator:
    def apply(body: str) -> str:
        rows = list(csv.reader(io.StringIO(body.lstrip("﻿"))))
        edit(rows)
        out = io.StringIO()
        csv.writer(out, lineterminator="\n").writerows(rows)
        return out.getvalue()

    return apply


def _set_latest(column: str, value: str) -> Mutator:
    def edit(rows: list[list[str]]) -> None:
        rows[1][rows[0].index(column)] = value  # row 1 is the newest day

    return _csv_edit(edit)


async def test_impossible_treasury_yield_is_quarantined() -> None:
    request = _plan(TreasuryAdapter, RequestKind.TREASURY_CURVE)
    resp = await _replay(TreasuryAdapter, request, {0: _set_latest("10 Yr", "410")})
    assert all(isinstance(p, TreasuryYieldPoint) and p.yield_pct < 30 for p in resp.records)
    assert [p.yield_pct for p in resp.quarantined if isinstance(p, TreasuryYieldPoint)] == [410.0]


@pytest.mark.parametrize("value", ["NaN", "-inf"])
async def test_non_finite_treasury_yield_blocks(value: str) -> None:
    request = _plan(TreasuryAdapter, RequestKind.TREASURY_CURVE)
    with pytest.raises(DataQualityError, match="schema"):
        await _replay(TreasuryAdapter, request, {0: _set_latest("10 Yr", value)})


async def test_renamed_treasury_column_is_schema_drift() -> None:
    request = _plan(TreasuryAdapter, RequestKind.TREASURY_CURVE)

    def rename(rows: list[list[str]]) -> None:
        rows[0][rows[0].index("10 Yr")] = "10 Year"

    with pytest.raises(DataQualityError, match="10 Yr"):
        await _replay(TreasuryAdapter, request, {0: _csv_edit(rename)})


async def test_treasury_rows_from_another_year_are_rejected() -> None:
    request = _plan(TreasuryAdapter, RequestKind.TREASURY_CURVE)

    def old(rows: list[list[str]]) -> None:
        rows[-1][0] = "12/31/2025"

    with pytest.raises(DataQualityError, match="schema"):
        await _replay(TreasuryAdapter, request, {0: _csv_edit(old)})


async def test_sec_non_finite_fact_blocks_and_wrong_company_blocks() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)

    def nan_fact(data: dict[str, Any]) -> None:
        concept = next(iter(data["facts"]["us-gaap"].values()))
        unit = next(iter(concept["units"].values()))
        fact = next(f for f in unit if f.get("form") in ("10-K", "10-Q"))
        fact["val"] = "NaN"

    with pytest.raises(DataQualityError, match="schema"):
        await _replay(SecEdgarAdapter, request, {0: _json_edit(nan_fact)})

    def other_company(data: dict[str, Any]) -> None:
        data["cik"] = 999999

    with pytest.raises(DataQualityError, match="CIK 999999"):
        await _replay(SecEdgarAdapter, request, {0: _json_edit(other_company)})


async def test_sec_future_dated_filing_is_rejected() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.FILINGS)

    def future(data: dict[str, Any]) -> None:
        data["filings"]["recent"]["filingDate"][0] = "2030-01-02"

    with pytest.raises(DataQualityError, match="schema"):
        await _replay(SecEdgarAdapter, request, {0: _json_edit(future)})


def _ticker_edit(symbol: str, edit: Callable[[dict[str, Any]], None]) -> Mutator:
    def apply(body: str) -> str:
        data = json.loads(body)
        if "tickers" in data:
            for t in data["tickers"]:
                if t.get("symbol") == symbol:
                    edit(t)
        return json.dumps(data)

    return apply


async def _derivatives(edit: Callable[[dict[str, Any]], None], symbol: str = "PF_XBTUSD") -> DataResponse:
    request = _plan(KrakenFuturesAdapter, RequestKind.CRYPTO_DERIVATIVES)
    cassette = Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request))
    mutate = {i: _ticker_edit(symbol, edit) for i in range(len(cassette.interactions))}
    return await _replay(KrakenFuturesAdapter, request, mutate)


async def test_negative_open_interest_and_mark_price_are_quarantined() -> None:
    def seed(t: dict[str, Any]) -> None:
        t["openInterest"] = -10.0
        t["markPrice"] = -5.0

    resp = await _derivatives(seed)
    shown = [m for m in resp.records if isinstance(m, CryptoDerivativeMetric)]
    assert all(m.value >= 0 for m in shown if m.metric in ("open_interest", "mark_price"))
    bad = {(m.metric, m.value) for m in resp.quarantined if isinstance(m, CryptoDerivativeMetric)}
    assert ("open_interest", -10.0) in bad
    assert ("mark_price", -5.0) in bad


async def test_stale_symbol_cannot_ride_on_a_fresh_one() -> None:
    def old(t: dict[str, Any]) -> None:
        t["lastTime"] = "2026-10-01T00:00:00.000Z"

    resp = await _derivatives(old, symbol="PF_ETHUSD")
    symbols = {m.symbol for m in resp.records if isinstance(m, CryptoDerivativeMetric)}
    assert symbols == {"BTC/USD:USD"}
    assert {m.symbol for m in resp.quarantined if isinstance(m, CryptoDerivativeMetric)} == {"ETH/USD:USD"}
    assert any("stale_symbols" in w for w in resp.quality.warnings)
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.FRESH


# -------------------------------------------------------------------------------------------- freshness (major 2)


async def test_eia_weekly_freshness_follows_the_release_calendar() -> None:
    """Week ending Fri 10-16 is released Wed 10-21: fresh until the next release is a day late, blocked a week on."""
    request = _plan(EiaAdapter, RequestKind.MACRO_SERIES, series="WCESTUS1")
    expect = {
        datetime(2026, 10, 24, 12, tzinfo=UTC): Freshness.FRESH,
        datetime(2026, 10, 29, 12, tzinfo=UTC): Freshness.FRESH,
        datetime(2026, 10, 29, 16, tzinfo=UTC): Freshness.STALE,
        datetime(2026, 11, 3, 12, tzinfo=UTC): Freshness.STALE,
    }
    for now, status in expect.items():
        resp = await _replay(EiaAdapter, request, now=now)
        assert resp.freshness is not None
        assert resp.freshness.status is status, now
    with pytest.raises(Stale):
        await _replay(EiaAdapter, request, now=datetime(2026, 11, 4, 16, tzinfo=UTC))


async def test_fred_monthly_value_is_fresh_on_release_day() -> None:
    """A monthly value dated the 1st is released weeks later; judged from FRED's last update, not its date."""

    def monthly(data: dict[str, Any]) -> None:
        data["seriess"][0].update(frequency_short="M", last_updated="2026-09-11 07:46:02-05")

    def months(data: dict[str, Any]) -> None:
        data["observations"] = [
            {"realtime_start": "2026-10-21", "realtime_end": "2026-10-21", "date": f"2025-{m:02d}-01", "value": "3.5"}
            for m in range(1, 13)
        ] + [
            {"realtime_start": "2026-10-21", "realtime_end": "2026-10-21", "date": f"2026-{m:02d}-01", "value": "3.6"}
            for m in range(1, 9)
        ]

    request = _plan(FredAdapter, RequestKind.MACRO_SERIES)
    now = datetime(2026, 9, 11, 14, tzinfo=UTC)
    resp = await _replay(FredAdapter, request, {0: _json_edit(monthly), 1: _json_edit(months)}, now=now)
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.FRESH
    assert resp.freshness.stale_after.date().isoformat() == "2026-10-13"  # next release (+31 d) + 1 day


async def test_kraken_intraday_bars_are_judged_from_their_close(ctx_factory: Any) -> None:
    hour = datetime(2026, 10, 21, 10, tzinfo=UTC)
    candles = [
        {"time": int((hour - timedelta(hours=k + 1)).timestamp() * 1000), "open": "100", "high": "101",
         "low": "99", "close": f"{100 + 0.1 * math.sin(k)}", "volume": "5"}
        for k in range(30, -1, -1)
    ]  # fmt: skip
    ctx, _ = ctx_factory(_kraken_handler(candles), now=hour + timedelta(minutes=5), cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "BTC/USD:USD", "timeframe": "1h"}))
    await adapter.aclose()
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.FRESH  # the 09:00 bar closed at 10:00, five minutes ago
    assert resp.freshness.as_of == hour


def _kraken_handler(candles: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    request = _plan(KrakenFuturesAdapter, RequestKind.BARS)
    markets = next(
        it.body
        for it in Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request)).interactions
        if "instruments" in it.url
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if "instruments" in req.url.path:
            return httpx.Response(200, text=markets)
        return httpx.Response(200, json={"candles": candles, "more_candles": False})

    return handler


async def test_twelvedata_weekly_bars_and_delay_come_from_terms_not_the_caller(ctx_factory: Any) -> None:
    weeks = [datetime(2026, 10, 12, tzinfo=UTC) - timedelta(weeks=k) for k in range(20, -1, -1)]
    values = [
        {"datetime": w.strftime("%Y-%m-%d"), "open": "1.1", "high": "1.2", "low": "1.0", "close": f"{1.1 + k / 1e4}"}
        for k, w in enumerate(weeks)
    ]
    body = {"meta": {"symbol": "EUR/USD", "interval": "1week"}, "values": values, "status": "ok"}
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)  # the week of 10-12 closed on 10-19; 10-19's is still forming
    ctx, _ = ctx_factory(
        lambda _r: httpx.Response(200, json=body), now=now, flags={"provider_twelvedata": True}, **SETTINGS
    )
    adapter = TwelveDataAdapter(ctx)
    resp = await adapter.fetch(
        DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "interval": "1w", "delay_minutes": 99999})
    )
    await adapter.aclose()
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.FRESH
    assert resp.freshness.as_of == datetime(2026, 10, 19, tzinfo=UTC)  # judged from the close of the 10-12 week
    assert resp.freshness.blocked_after == datetime(2026, 11, 9, 0, 30, tzinfo=UTC)  # delay 0 from the terms


async def test_twelvedata_drops_forming_candle_and_checks_staleness_only_while_open(ctx_factory: Any) -> None:
    days = [datetime(2026, 10, 21, tzinfo=UTC) - timedelta(days=k) for k in range(30, -1, -1)]
    values = [
        {"datetime": d.strftime("%Y-%m-%d"), "open": "1.1", "high": "1.2", "low": "1.0", "close": "1.1"}
        for d in days
        if d.weekday() < 5
    ]
    body = {"meta": {"symbol": "EUR/USD", "interval": "1day"}, "values": values, "status": "ok"}
    weekend = datetime(2026, 10, 18, 12, tzinfo=UTC)
    ctx, _ = ctx_factory(
        lambda _r: httpx.Response(200, json={**body, "values": [v for v in values if v["datetime"] <= "2026-10-16"]}),
        now=weekend,
        flags={"provider_twelvedata": True},
        **SETTINGS,
    )
    adapter = TwelveDataAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "interval": "1d"}))
    await adapter.aclose()
    assert "stale_quotes" not in " ".join(resp.quality.failures)  # flat closes over a closed weekend are fine
    ctx, _ = ctx_factory(
        lambda _r: httpx.Response(200, json=body),
        now=datetime(2026, 10, 21, 12, tzinfo=UTC),
        flags={"provider_twelvedata": True},
        **SETTINGS,
    )
    adapter = TwelveDataAdapter(ctx)
    with pytest.raises(DataQualityError, match="stale_quotes"):  # flat for days while the market is open
        await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "interval": "1d"}))
    await adapter.aclose()


# -------------------------------------------------------------------------------------- typed errors (major 4)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json={"unexpected": "object"}),
        httpx.Response(302, headers={"location": "https://elsewhere.example.com"}),
        httpx.Response(204),
    ],
    ids=["html", "wrong-type", "redirect", "no-content"],
)
async def test_malformed_responses_raise_provider_down_and_count_as_failures(
    response: httpx.Response, ctx_factory: Any
) -> None:
    ctx, _ = ctx_factory(lambda _r: response)
    adapter = CftcAdapter(ctx)
    request = DataRequest(kind=RequestKind.COT, params={"report_type": "tff", "market_code": "099741"})
    with pytest.raises(ProviderDown):
        await adapter.fetch(request)
    await adapter.aclose()
    assert (await ctx.breaker.status("cftc")).consecutive_failures == 1


@pytest.mark.parametrize("body", [[], [1, 2], "text"])
async def test_wrong_json_types_from_keyed_vendors_are_typed(body: object, ctx_factory: Any) -> None:
    for cls, params in (
        (EiaAdapter, {"series": "WCESTUS1"}),
        (TwelveDataAdapter, {"symbol": "EUR/USD"}),
    ):
        ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=body), flags={"provider_twelvedata": True}, **SETTINGS)
        adapter = cls(ctx)
        kind = RequestKind.MACRO_SERIES if cls is EiaAdapter else RequestKind.BARS
        with pytest.raises(ProviderDown, match="malformed"):
            await adapter.fetch(DataRequest(kind=kind, params=params))
        await adapter.aclose()


async def test_corrupt_cache_entry_is_dropped_and_refetched() -> None:
    request = _plan(CftcAdapter, RequestKind.COT, report_type="tff")
    cassette = Cassette.load(CASSETTES / "cftc" / cassette_name(request))
    store = MemoryStateStore()
    cache = ResponseCache(store)
    await cache.put(request.fingerprint("cftc"), "{not json", 3600)
    transport = ReplayTransport([cassette])
    ctx = AdapterContext(
        settings=Settings(),
        limiter=LocalRateLimiter(),
        breaker=CircuitBreaker(store),
        cache=cache,
        clock=FixedClock(cassette.replay_time),
        transport=transport,
    )
    adapter = CftcAdapter(ctx)
    resp = await adapter.fetch(request)
    assert not resp.from_cache
    assert len(transport.calls) == 1
    again = await adapter.fetch(request)  # the good payload replaced the corrupt entry
    assert again.from_cache
    await adapter.aclose()


async def test_payload_failing_qa_is_not_cached(ctx_factory: Any) -> None:
    rows = [{"report_date_as_yyyy_mm_dd": "2026-10-13T00:00:00.000"}]  # schema rejection: blocking
    ctx, rec = ctx_factory(lambda _r: httpx.Response(200, json=rows))
    adapter = CftcAdapter(ctx)
    request = DataRequest(kind=RequestKind.COT, params={"report_type": "tff", "market_code": "099741"})
    for _ in range(2):
        with pytest.raises(DataQualityError):
            await adapter.fetch(request)
    await adapter.aclose()
    assert len(rec.requests) == 2


async def test_open_circuit_stops_remaining_retries(ctx_factory: Any) -> None:
    sleeps: list[float] = []
    calls = 0

    def failing(_r: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    store = MemoryStateStore()
    ctx, _ = ctx_factory(failing)
    ctx.breaker = CircuitBreaker(store, failure_threshold=2)
    adapter = CftcAdapter(ctx)
    adapter.http._sleep = _record_sleep(sleeps)  # no real waiting in tests
    request = DataRequest(kind=RequestKind.COT, params={"report_type": "tff", "market_code": "099741"})
    with pytest.raises(ProviderDown, match="circuit open"):
        await adapter.fetch(request)
    await adapter.aclose()
    assert calls == 2  # the third attempt never left once the breaker opened
    assert (await ctx.breaker.status("cftc")).state is CircuitState.OPEN


def _record_sleep(into: list[float]) -> Callable[[float], Any]:
    async def sleep(seconds: float) -> None:
        into.append(seconds)

    return sleep


def test_fractional_rate_limit_can_be_acquired() -> None:
    now = [0.0]
    limiter = LocalRateLimiter(clock=lambda: now[0])
    limit = RateLimit(per_second=0.5)
    assert limiter.try_acquire("p", limit) == 0.0
    wait = limiter.try_acquire("p", limit)
    assert wait == pytest.approx(2.0, abs=0.01)  # one call every two seconds
    now[0] += 2.0
    assert limiter.try_acquire("p", limit) == 0.0


# --------------------------------------------------------------------------------------- secrets (minor 7)


@pytest.mark.parametrize("name", ["access_token", "password", "client_secret", "api-key", "X-Api-Key", "signature"])
def test_credential_params_are_redacted(name: str) -> None:
    assert redact_params({name: "v" * 12, "symbol": "BTC"}) == {name: "REDACTED", "symbol": "BTC"}


async def test_echoed_key_is_scrubbed_from_recorded_body_and_cache(ctx_factory: Any) -> None:
    key = "eia-" + "s3cr3tvalue" * 2
    echo = {
        "response": {"frequency": "weekly", "data": [{"period": "2026-10-16", "series": "WCESTUS1", "value": 4e5}]},
        "request": {"params": {"api_key": key}},
    }
    recording = RecordingTransport(httpx.MockTransport(lambda _r: httpx.Response(200, json=echo)))
    async with httpx.AsyncClient(transport=recording) as client:
        await client.get("https://api.eia.gov/v2/x/data/", params={"api_key": key})
    stored = recording.interactions[0]
    assert key not in stored.body
    assert key not in json.dumps(stored.params)

    store = MemoryStateStore()
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=echo), eia_api_key=key)
    ctx.cache = ResponseCache(store)
    adapter = EiaAdapter(ctx)
    await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1", "length": 1}))
    await adapter.aclose()
    assert all(key not in value for value, _ in store._data.values())  # inspecting the cache backend


# -------------------------------------------------------------------------------------- CFTC params (minor 9/10)


async def test_cftc_futures_only_must_be_boolean_and_order_is_checked(ctx_factory: Any) -> None:
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=[]))
    adapter = CftcAdapter(ctx)
    with pytest.raises(InvalidArgs, match="boolean"):
        await adapter.fetch(
            DataRequest(
                kind=RequestKind.COT, params={"report_type": "tff", "market_code": "1", "futures_only": "false"}
            )
        )
    await adapter.aclose()
    request = _plan(CftcAdapter, RequestKind.COT, report_type="tff")

    def reverse(body: str) -> str:
        return json.dumps(list(reversed(json.loads(body))))

    with pytest.raises(DataQualityError, match="provider_order"):
        await _replay(CftcAdapter, request, {0: reverse})

    def drop_week(body: str) -> str:
        rows = json.loads(body)
        return json.dumps(rows[:10] + rows[11:])

    resp = await _replay(CftcAdapter, request, {0: drop_week})
    assert any("cadence_gaps" in w for w in resp.quality.warnings)


def test_fundamental_fact_schema_refuses_nan() -> None:
    with pytest.raises(ValueError, match="finite"):
        FundamentalFact.model_validate(
            {
                "cik": "0000320193",
                "concept": "Revenues",
                "taxonomy": "us-gaap",
                "unit": "USD",
                "value": math.nan,
                "period_end": "2026-06-30",
                "form": "10-Q",
                "filed": "2026-08-01",
                "accession": "0000320193-26-000001",
            }
        )
