"""Seeded bad data, freshness edge cases and malformed payloads pushed end to end through the adapters.

Most cases start from a *real* recorded cassette (``cassettes/``), corrupt one value the way a provider bug would,
and assert that the pipeline quarantines it, blocks it or raises a typed error — never passes it through.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from rd_core.clock import FixedClock
from rd_core.settings import Settings
from rd_data.adapters import sec_scale
from rd_data.adapters.base import AdapterContext, BaseAdapter, DataRequest, DataResponse, RequestKind
from rd_data.adapters.cftc import CftcAdapter
from rd_data.adapters.eia import EiaAdapter
from rd_data.adapters.fred import FredAdapter
from rd_data.adapters.kraken_futures import KrakenFuturesAdapter
from rd_data.adapters.sec_edgar import SecEdgarAdapter
from rd_data.adapters.treasury import COLUMNS, TreasuryAdapter
from rd_data.adapters.twelvedata import TwelveDataAdapter
from rd_data.cassette import Cassette, RecordingTransport, ReplayTransport
from rd_data.errors import DataQualityError, InvalidArgs, LicenceBlocked, NotFound, ProviderDown, RateLimited, Stale
from rd_data.freshness import ExchangeCalendar, Freshness
from rd_data.http import HttpFetcher, redact_params
from rd_data.quality import check_cadence, check_interval_gaps
from rd_data.recording import cassette_name
from rd_data.registry import provider_statuses
from rd_data.resilience import (
    CircuitBreaker,
    CircuitState,
    LocalRateLimiter,
    MemoryStateStore,
    RateLimit,
    ResponseCache,
)
from rd_data.schemas import (
    Bar,
    CryptoDerivativeMetric,
    FundamentalFact,
    Instrument,
    MacroObservation,
    TreasuryYieldPoint,
)

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
    with pytest.raises(InvalidArgs, match="delay_minutes"):  # the caller cannot set the feed's delay
        await adapter.fetch(
            DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "interval": "1w", "delay_minutes": 99999})
        )
    resp = await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "interval": "1w"}))
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


@pytest.mark.parametrize(
    "name",
    [
        "access_token",
        "password",
        "client_secret",
        "api-key",
        "X-Api-Key",
        "signature",
        "accessToken",
        "secretKey",
        "apiToken",
        "sessionToken",
        "api_secret",
        "private_key",
        "license_key",
        "x_auth_token",
    ],
)
def test_credential_params_are_redacted(name: str) -> None:
    assert redact_params({name: "v" * 12, "symbol": "BTC"}) == {name: "REDACTED", "symbol": "BTC"}


@pytest.mark.parametrize("name", ["symbol", "series_id", "interval", "tokens_per_second", "sort_key", "keyword"])
def test_ordinary_params_are_not_redacted(name: str) -> None:
    assert redact_params({name: "x"}) == {name: "x"}


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


# ------------------------------------------------------------------------------------- review round 2 (P1)


async def test_cot_short_side_corruption_blocks() -> None:
    request = _plan(CftcAdapter, RequestKind.COT, report_type="disaggregated", futures_only=None)

    def short_x10(body: str) -> str:
        rows = json.loads(body)
        rows[0]["m_money_positions_short_all"] = str(float(rows[0]["m_money_positions_short_all"]) * 10)
        return json.dumps(rows)

    with pytest.raises(DataQualityError, match="short_side_equals_open_interest"):
        await _replay(CftcAdapter, request, {0: short_x10})


def _prepend_row(edit: Callable[[dict[str, Any]], None]) -> Mutator:
    def apply(body: str) -> str:
        rows = json.loads(body)
        row = dict(rows[0])
        edit(row)
        return json.dumps([row, *rows])

    return apply


@pytest.mark.parametrize("day", ["2026-11-03", "2026-10-20"])
async def test_cot_rows_from_the_future_or_before_release_are_refused(day: str) -> None:
    """11-03 has not happened; positions of Tue 10-20 are not published until Fri 10-23."""
    request = _plan(CftcAdapter, RequestKind.COT, report_type="tff", futures_only=None)

    def dated(row: dict[str, Any]) -> None:
        row["report_date_as_yyyy_mm_dd"] = f"{day}T00:00:00.000"

    with pytest.raises(DataQualityError, match="2026-1"):
        await _replay(CftcAdapter, request, {0: _prepend_row(dated)})


async def test_cot_rows_for_another_market_are_rejected() -> None:
    request = _plan(CftcAdapter, RequestKind.COT, report_type="tff", futures_only=None)

    def other_market(body: str) -> str:
        return json.dumps([{**r, "cftc_contract_market_code": "ZZZ999"} for r in json.loads(body)])

    with pytest.raises(DataQualityError, match="schema"):
        await _replay(CftcAdapter, request, {0: other_market})


async def test_eia_future_period_and_mixed_units_are_refused() -> None:
    request = _plan(EiaAdapter, RequestKind.MACRO_SERIES, series="WCESTUS1")

    def future(data: dict[str, Any]) -> None:
        data["response"]["data"].insert(0, {**data["response"]["data"][0], "period": "2026-10-30"})

    with pytest.raises(DataQualityError, match="schema"):  # a period after retrieval is never accepted
        await _replay(EiaAdapter, request, {0: _json_edit(future)})

    def other_unit(data: dict[str, Any]) -> None:
        row = data["response"]["data"][0]
        row.update(units="MMBBL", value=row["value"] / 1000)

    with pytest.raises(DataQualityError, match="mixed units"):
        await _replay(EiaAdapter, request, {0: _json_edit(other_unit)})


async def test_eia_latest_value_beyond_storage_capacity_is_quarantined() -> None:
    request = _plan(EiaAdapter, RequestKind.MACRO_SERIES, series="WCESTUS1")

    def x3(data: dict[str, Any]) -> None:
        data["response"]["data"][0]["value"] *= 3

    resp = await _replay(EiaAdapter, request, {0: _json_edit(x3)})
    assert len(resp.quarantined) == 1
    assert all(isinstance(o, MacroObservation) and (o.value or 0) < 800_000 for o in resp.records)


@pytest.mark.parametrize("factor", [2.0, 0.1])
async def test_unconfirmed_latest_treasury_move_is_quarantined(factor: float) -> None:
    request = _plan(TreasuryAdapter, RequestKind.TREASURY_CURVE)
    cassette = Cassette.load(CASSETTES / "us_treasury" / cassette_name(request))
    rows = list(csv.reader(io.StringIO(cassette.interactions[0].body.lstrip("﻿"))))
    latest = float(rows[1][rows[0].index("10 Yr")])
    resp = await _replay(TreasuryAdapter, request, {0: _set_latest("10 Yr", f"{latest * factor:.2f}")})
    held = [p for p in resp.quarantined if isinstance(p, TreasuryYieldPoint)]
    assert [(p.tenor, p.yield_pct) for p in held] == [("10Y", round(latest * factor, 2))]
    assert any("outliers" in w for w in resp.quality.warnings)


async def test_kraken_maintenance_page_is_an_outage_and_trips_the_breaker(ctx_factory: Any) -> None:
    ctx, rec = ctx_factory(lambda _r: httpx.Response(200, text="<html>maintenance</html>"), cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    request = DataRequest(kind=RequestKind.CRYPTO_DERIVATIVES, params={"symbols": "BTC/USD:USD"})
    for _ in range(5):  # default failure threshold
        with pytest.raises(ProviderDown, match="malformed"):
            await adapter.fetch(request)
    calls = len(rec.requests)
    with pytest.raises(ProviderDown, match="circuit open"):
        await adapter.fetch(request)
    await adapter.aclose()
    assert len(rec.requests) == calls  # the open circuit stops further calls


async def test_repeated_malformed_200s_open_the_circuit(ctx_factory: Any) -> None:
    ctx, rec = ctx_factory(lambda _r: httpx.Response(200, text="<html>oops</html>"), cache=False)
    adapter = CftcAdapter(ctx)
    request = DataRequest(kind=RequestKind.COT, params={"report_type": "tff", "market_code": "099741"})
    for _ in range(5):
        with pytest.raises(ProviderDown, match="malformed"):
            await adapter.fetch(request)
    assert (await ctx.breaker.status("cftc")).state is CircuitState.OPEN
    with pytest.raises(ProviderDown, match="circuit open"):
        await adapter.fetch(request)
    await adapter.aclose()
    assert len(rec.requests) == 5


async def test_cached_payload_never_turns_a_forming_bar_into_a_close(ctx_factory: Any) -> None:
    hours = [datetime(2026, 10, 21, 10, tzinfo=UTC) - timedelta(hours=k) for k in range(30, -1, -1)]
    values = [
        {"datetime": h.strftime("%Y-%m-%d %H:%M:%S"), "open": "1.1", "high": "1.2", "low": "1.0",
         "close": f"{1.1 + 0.001 * math.sin(k)}"}
        for k, h in enumerate(hours)
    ]  # fmt: skip
    body = {"meta": {"symbol": "EUR/USD", "interval": "1h"}, "values": values, "status": "ok"}
    clock = FixedClock(datetime(2026, 10, 21, 10, 58, tzinfo=UTC))
    ctx, rec = ctx_factory(lambda _r: httpx.Response(200, json=body), flags={"provider_twelvedata": True}, **SETTINGS)
    ctx.clock = clock
    adapter = TwelveDataAdapter(ctx)
    request = DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "interval": "1h"})
    first = await adapter.fetch(request)
    clock.advance(timedelta(minutes=4))  # 11:02: the 10:00 bar has closed, but the cached copy is from 10:58
    second = await adapter.fetch(request)
    await adapter.aclose()
    assert second.from_cache
    assert len(rec.requests) == 1
    assert first.as_of == second.as_of == datetime(2026, 10, 21, 9, tzinfo=UTC)


async def test_kraken_wick_volume_and_three_bar_bad_prints_are_quarantined() -> None:
    request = _plan(KrakenFuturesAdapter, RequestKind.BARS)
    cassette = Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request))
    candles_at = next(i for i, it in enumerate(cassette.interactions) if '"candles"' in it.body)

    def seed(data: dict[str, Any]) -> None:
        candles = data["candles"]
        candles[-1]["high"] = str(float(candles[-1]["high"]) * 50)  # wick 50x, close untouched
        candles[30]["volume"] = str(float(candles[30]["volume"]) * 1e6)
        for c in candles[40:43]:  # three bad bars in a row, then back to normal
            for k in ("open", "high", "low", "close"):
                c[k] = str(float(c[k]) * 50)

    resp = await _replay(KrakenFuturesAdapter, request, {candles_at: _json_edit(seed)})
    bars = [b for b in resp.records if isinstance(b, Bar)]
    assert len(resp.quarantined) == 5
    assert max(b.high for b in bars) < 1_000_000
    assert any("field_spikes" in w for w in resp.quality.warnings)


async def test_kraken_mark_index_gap_funding_bound_and_missing_symbol() -> None:
    def seed(t: dict[str, Any]) -> None:
        t["markPrice"] = float(t["markPrice"]) * 50

    resp = await _derivatives(seed)
    held = {(m.symbol, m.metric) for m in resp.quarantined if isinstance(m, CryptoDerivativeMetric)}
    assert {("BTC/USD:USD", "mark_price"), ("BTC/USD:USD", "index_price"), ("BTC/USD:USD", "funding_rate")} <= held
    assert any("mark_vs_index" in w for w in resp.quality.warnings)

    def funding(t: dict[str, Any]) -> None:
        t["fundingRate"] = float(t.get("fundingRate") or 0.0) + float(t["markPrice"]) * 0.02  # 2% per hour

    resp = await _derivatives(funding)
    assert ("BTC/USD:USD", "funding_rate") in {
        (m.symbol, m.metric) for m in resp.quarantined if isinstance(m, CryptoDerivativeMetric)
    }

    request = _plan(KrakenFuturesAdapter, RequestKind.CRYPTO_DERIVATIVES)
    cassette = Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request))

    def drop_eth(body: str) -> str:
        data = json.loads(body)
        if "tickers" in data:
            data["tickers"] = [t for t in data["tickers"] if t.get("symbol") != "PF_ETHUSD"]
        return json.dumps(data)

    resp = await _replay(KrakenFuturesAdapter, request, dict.fromkeys(range(len(cassette.interactions)), drop_eth))
    assert any("missing_symbols" in w and "ETH/USD:USD" in w for w in resp.quality.warnings)


async def test_intraday_equity_bars_are_not_overdue_over_the_weekend(ctx_factory: Any) -> None:
    friday = datetime(2026, 10, 16, 19, tzinfo=UTC)  # the 15:00-16:00 ET bar, closing at the 16:00 ET close
    hours = [friday - timedelta(hours=k) for k in range(40, -1, -1)]
    values = [
        {"datetime": h.strftime("%Y-%m-%d %H:%M:%S"), "open": "100", "high": "101", "low": "99",
         "close": f"{100 + math.sin(k)}"}
        for k, h in enumerate(hours)
    ]  # fmt: skip
    body = {"meta": {"symbol": "AAPL", "interval": "1h"}, "values": values, "status": "ok"}
    ctx, _ = ctx_factory(
        lambda _r: httpx.Response(200, json=body),
        now=datetime(2026, 10, 19, 12, tzinfo=UTC),  # Monday morning, before the open
        flags={"provider_twelvedata": True},
        cache=False,
        **SETTINGS,
    )
    adapter = TwelveDataAdapter(ctx)
    resp = await adapter.fetch(
        DataRequest(kind=RequestKind.BARS, params={"symbol": "AAPL", "interval": "1h", "asset_class": "equity"})
    )
    await adapter.aclose()
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.FRESH


async def test_ports_unknown_params_auth_errors_and_market_cache(ctx_factory: Any) -> None:
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json={}), **SETTINGS)
    adapter = SecEdgarAdapter(ctx)
    with pytest.raises(InvalidArgs, match="non-allow-listed"):
        await adapter.http.get("https://data.sec.gov:22/files/x.json")
    with pytest.raises(InvalidArgs, match="unknown parameter"):
        await adapter.fetch(DataRequest(kind=RequestKind.FILINGS, params={"cik": "320193", "cache_buster": 1}))
    await adapter.aclose()

    body = {"status": "error", "code": 401, "message": "**apikey** parameter is incorrect"}
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=body), flags={"provider_twelvedata": True}, **SETTINGS)
    vendor = TwelveDataAdapter(ctx)
    with pytest.raises(ProviderDown, match="authorization"):
        await vendor.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD"}))
    await vendor.aclose()

    request = _plan(KrakenFuturesAdapter, RequestKind.CRYPTO_DERIVATIVES)
    cassette = Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request))
    transport = ReplayTransport([cassette])
    ctx = AdapterContext(
        settings=Settings(),
        limiter=LocalRateLimiter(),
        breaker=CircuitBreaker(MemoryStateStore()),
        clock=FixedClock(cassette.replay_time),
        transport=transport,
    )
    kraken = KrakenFuturesAdapter(ctx)
    await kraken.fetch(request)
    await kraken.fetch(request)
    await kraken.aclose()
    assert sum("instruments" in c for c in transport.calls) == 1  # the 0.9 MB list is reused within its TTL


async def test_status_shows_whether_credentials_are_configured() -> None:
    statuses = await provider_statuses({}, CircuitBreaker(MemoryStateStore()), Settings(eia_api_key=None))
    assert statuses["eia"]["configured"] is False
    assert statuses["cftc"]["configured"] is True
    assert statuses["fred"]["enabled"] is False


# ------------------------------------------------------------------------------------- review round 3 (P1)


def _fred_bodies(series: str = "DGS10") -> tuple[str, str]:
    request = _plan(FredAdapter, RequestKind.MACRO_SERIES)
    cassette = Cassette.load(CASSETTES / "fred" / cassette_name(request))
    info = json.loads(cassette.interactions[0].body)
    info["seriess"][0]["id"] = series
    return json.dumps(info), cassette.interactions[1].body


async def test_multi_call_providers_recover_after_a_half_open_probe(ctx_factory: Any) -> None:
    """FRED makes two calls per fetch; the probe must cover both, or the circuit never closes again."""
    info, observations = _fred_bodies()
    healthy = [False]

    def handler(req: httpx.Request) -> httpx.Response:
        if not healthy[0]:
            return httpx.Response(503)
        return httpx.Response(200, text=info if req.url.path.endswith("/series") else observations)

    clock = [1_000.0]
    ctx, _ = ctx_factory(handler, flags={"provider_fred": True}, cache=False, **SETTINGS)
    ctx.clock = FixedClock(datetime(2026, 10, 21, 12, tzinfo=UTC))
    ctx.breaker = CircuitBreaker(MemoryStateStore(clock=lambda: clock[0]), failure_threshold=2, cooldown_s=60)
    adapter = FredAdapter(ctx)
    adapter.http._sleep = _record_sleep([])  # no real waiting
    request = DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"})
    with pytest.raises(ProviderDown):
        await adapter.fetch(request)
    assert (await ctx.breaker.status("fred")).state is CircuitState.OPEN
    healthy[0] = True
    clock[0] += 61  # cool-down over: one probe fetch is admitted
    resp = await adapter.fetch(request)
    await adapter.aclose()
    assert resp.records
    status = await ctx.breaker.status("fred")
    assert status.state is CircuitState.CLOSED
    assert status.last_error is None  # the status page no longer shows the old error


async def test_kraken_recovers_with_a_fresh_adapter_per_fetch() -> None:
    request = _plan(KrakenFuturesAdapter, RequestKind.CRYPTO_DERIVATIVES)
    cassette = Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request))
    clock = [1_000.0]
    breaker = CircuitBreaker(MemoryStateStore(clock=lambda: clock[0]), failure_threshold=1, cooldown_s=60)
    await breaker.record_failure("kraken_futures", "HTTP 503")
    clock[0] += 61

    ctx = AdapterContext(
        settings=Settings(),
        limiter=LocalRateLimiter(),
        breaker=breaker,
        clock=FixedClock(cassette.replay_time),
        transport=ReplayTransport([cassette]),
    )
    adapter = KrakenFuturesAdapter(ctx)  # instruments + tickers: two calls under one probe
    await adapter.fetch(request)
    await adapter.aclose()
    assert (await breaker.status("kraken_futures")).state is CircuitState.CLOSED


def _eia_body(values: Sequence[float | None], series: str = "WCESTUS1", unit: str = "MBBL") -> dict[str, Any]:
    end = datetime(2026, 10, 16, tzinfo=UTC)
    rows = [
        {"period": (end - timedelta(weeks=k)).strftime("%Y-%m-%d"), "series": series, "value": v, "units": unit}
        for k, v in enumerate(reversed(values))
    ]
    return {"response": {"frequency": "weekly", "data": rows}}


def _crude(n: int = 60) -> list[float | None]:
    return [420_000.0 + 6_000 * math.sin(k / 2.3) + 900 * math.cos(k * 1.7) for k in range(n)]


@pytest.mark.parametrize("factor", [0.1, 0.5, 1.5, 1.8])
async def test_impossible_latest_inventory_change_is_held(ctx_factory: Any, factor: float) -> None:
    """A stock level cannot halve or grow 50% in a week: a decimal/unit error, held until confirmed (D-031)."""
    values = _crude()
    values[-1] = (values[-1] or 0.0) * factor
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=_eia_body(values)), **SETTINGS)
    adapter = EiaAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1"}))
    await adapter.aclose()
    assert len(resp.quarantined) == 1
    assert resp.as_of == datetime(2026, 10, 9, tzinfo=UTC)


async def test_eia_nulls_unnamed_series_and_error_bodies(ctx_factory: Any) -> None:
    async def fetch(body: dict[str, Any]) -> DataResponse:
        ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=body), **SETTINGS)
        adapter = EiaAdapter(ctx)
        try:
            return await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1"}))
        finally:
            await adapter.aclose()

    with pytest.raises(DataQualityError, match="missing"):
        await fetch(_eia_body([None] * 20))
    latest_missing = await fetch(_eia_body([*_crude(30), None]))
    assert latest_missing.as_of == datetime(2026, 10, 9, tzinfo=UTC)  # dated by the newest week with a value
    unnamed = _eia_body(_crude(30))
    del unnamed["response"]["data"][0]["series"]
    with pytest.raises(DataQualityError, match="schema"):
        await fetch(unnamed)
    with pytest.raises(ProviderDown, match="unavailable"):
        await fetch({"error": "Service unavailable", "code": 503})
    with pytest.raises(InvalidArgs):
        await fetch({"error": "invalid facet value", "code": 400})


async def test_treasury_maintenance_page_trips_the_breaker(ctx_factory: Any) -> None:
    ctx, rec = ctx_factory(lambda _r: httpx.Response(200, text="<html>down</html>"), cache=False)
    adapter = TreasuryAdapter(ctx)
    request = DataRequest(kind=RequestKind.TREASURY_CURVE, params={"year": 2026})
    for _ in range(5):
        with pytest.raises(ProviderDown, match="malformed"):
            await adapter.fetch(request)
    with pytest.raises(ProviderDown, match="circuit open"):
        await adapter.fetch(request)
    await adapter.aclose()
    assert len(rec.requests) == 5


async def test_treasury_gaps_warn_and_a_frozen_curve_blocks() -> None:
    request = _plan(TreasuryAdapter, RequestKind.TREASURY_CURVE)

    def drop_day(rows: list[list[str]]) -> None:
        del rows[5]

    resp = await _replay(TreasuryAdapter, request, {0: _csv_edit(drop_day)})
    assert any("calendar_gaps" in w for w in resp.quality.warnings)

    def freeze(rows: list[list[str]]) -> None:
        for r in rows[2:6]:
            r[1:] = rows[1][1:]  # the four days before the latest repeat its curve exactly

    with pytest.raises(DataQualityError, match="stale_quotes"):
        await _replay(TreasuryAdapter, request, {0: _csv_edit(freeze)})


async def test_cot_empty_scaled_and_wrong_kind_rows() -> None:
    request = _plan(CftcAdapter, RequestKind.COT, report_type="disaggregated", futures_only=None)

    def zero(body: str) -> str:
        rows = json.loads(body)
        rows[0] = {k: ("0" if k.endswith("_all") or "positions" in k else v) for k, v in rows[0].items()}
        return json.dumps(rows)

    with pytest.raises(DataQualityError, match="positive_open_interest"):
        await _replay(CftcAdapter, request, {0: zero})

    def scaled(body: str) -> str:
        rows = json.loads(body)
        rows[0] = {
            k: (str(float(v) * 10) if ("positions" in k or k == "open_interest_all") else v) for k, v in rows[0].items()
        }
        return json.dumps(rows)

    resp = await _replay(CftcAdapter, request, {0: scaled})  # still balances, but OI jumped tenfold in a week
    assert len(resp.quarantined) == 1
    assert any("open_interest_outliers" in w for w in resp.quality.warnings)

    def combined(body: str) -> str:
        rows = json.loads(body)
        rows[3]["futonly_or_combined"] = "Combined"
        return json.dumps(rows)

    with pytest.raises(DataQualityError, match="schema"):
        await _replay(CftcAdapter, request, {0: combined})


async def test_kraken_future_ticker_gap_and_misaligned_bar(ctx_factory: Any) -> None:
    def future(t: dict[str, Any]) -> None:
        t["lastTime"] = "2031-01-01T00:00:00.000Z"

    resp = await _derivatives(future, symbol="PF_ETHUSD")
    assert {m.symbol for m in resp.records if isinstance(m, CryptoDerivativeMetric)} == {"BTC/USD:USD"}

    def everything_bad(t: dict[str, Any]) -> None:
        t.update(openInterest=-1.0, markPrice=-1.0, indexPrice=-1.0, vol24h=-1.0, fundingRate=-5.0)

    request = _plan(KrakenFuturesAdapter, RequestKind.CRYPTO_DERIVATIVES)
    cassette = Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request))

    def all_bad(body: str) -> str:
        data = json.loads(body)
        for t in data.get("tickers", []):
            everything_bad(t)
        return json.dumps(data)

    with pytest.raises(DataQualityError, match="every record was quarantined"):
        await _replay(KrakenFuturesAdapter, request, dict.fromkeys(range(len(cassette.interactions)), all_bad))

    hour = datetime(2026, 10, 21, 10, tzinfo=UTC)
    candles = [
        {"time": int((hour - timedelta(hours=k + 1)).timestamp() * 1000), "open": "100", "high": "101",
         "low": "99", "close": f"{100 + 0.1 * math.sin(k)}", "volume": "5"}
        for k in range(40, -1, -1)
        if not 10 <= k < 20  # a ten-hour hole
    ]  # fmt: skip
    ctx, _ = ctx_factory(_kraken_handler(candles), now=hour + timedelta(minutes=5), cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    with pytest.raises(DataQualityError, match="calendar_gaps"):
        await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "BTC/USD:USD", "timeframe": "1h"}))
    await adapter.aclose()

    day = datetime(2026, 10, 21, tzinfo=UTC)
    daily = [
        {"time": int((day - timedelta(days=k + 1, hours=-7 if k == 0 else 0)).timestamp() * 1000), "open": "100",
         "high": "101", "low": "99", "close": "100.5", "volume": "5"}
        for k in range(20, -1, -1)
    ]  # fmt: skip
    ctx, _ = ctx_factory(_kraken_handler(daily), now=day + timedelta(hours=12), cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    with pytest.raises(DataQualityError, match="schema"):  # the newest "daily" bar starts at 07:00 UTC
        await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "BTC/USD:USD", "timeframe": "1d"}))
    await adapter.aclose()


async def test_sec_fact_filed_before_period_end_and_missing_cik() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)

    def lookahead(data: dict[str, Any]) -> None:
        concept = next(iter(data["facts"]["us-gaap"].values()))
        unit = next(iter(concept["units"].values()))
        fact = next(f for f in unit if f.get("form") in ("10-K", "10-Q"))
        fact["filed"] = "2000-01-01"

    resp = await _replay(SecEdgarAdapter, request, {0: _json_edit(lookahead)})
    facts = [f for f in resp.records if isinstance(f, FundamentalFact)]
    assert all(f.filed >= f.period_end for f in facts)  # the look-ahead fact is never shown
    assert any("point_in_time" in w for w in resp.quality.warnings)  # and is reported, not silently dropped

    def no_cik(data: dict[str, Any]) -> None:
        del data["cik"]

    with pytest.raises(ProviderDown, match="malformed"):  # EDGAR always names the company: not an EDGAR payload
        await _replay(SecEdgarAdapter, request, {0: _json_edit(no_cik)})


async def test_fred_and_twelvedata_refuse_a_body_for_another_series(ctx_factory: Any) -> None:
    info, observations = _fred_bodies(series="UNRATE")
    ctx, _ = ctx_factory(
        lambda r: httpx.Response(200, text=info if r.url.path.endswith("/series") else observations),
        flags={"provider_fred": True},
        **SETTINGS,
    )
    adapter = FredAdapter(ctx)
    with pytest.raises(DataQualityError, match="UNRATE"):
        await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"}))
    await adapter.aclose()

    request = _plan(TwelveDataAdapter, RequestKind.BARS)

    def other_symbol(data: dict[str, Any]) -> None:
        data["meta"]["symbol"] = "GBP/USD"

    with pytest.raises(DataQualityError, match="GBP/USD"):
        await _replay(TwelveDataAdapter, request, {0: _json_edit(other_symbol)})


async def test_oversized_bodies_and_long_retry_after_are_bounded(ctx_factory: Any) -> None:
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, content=b"x" * 2048))
    fetcher = HttpFetcher(
        provider="cftc",
        allowed_hosts=frozenset({"publicreporting.cftc.gov"}),
        limiter=ctx.limiter,
        rate_limit=RateLimit(),
        breaker=ctx.breaker,
        transport=ctx.transport,
        max_body_bytes=1024,
    )
    with pytest.raises(ProviderDown, match="larger than"):
        await fetcher.get("https://publicreporting.cftc.gov/resource/x.json")
    await fetcher.aclose()

    sleeps: list[float] = []
    ctx, rec = ctx_factory(lambda _r: httpx.Response(429, headers={"retry-after": "120"}))
    fetcher = HttpFetcher(
        provider="cftc",
        allowed_hosts=frozenset({"publicreporting.cftc.gov"}),
        limiter=ctx.limiter,
        rate_limit=RateLimit(),
        breaker=ctx.breaker,
        transport=ctx.transport,
        sleep=_record_sleep(sleeps),
        max_total_wait_s=60,
    )
    with pytest.raises(RateLimited) as exc:
        await fetcher.get("https://publicreporting.cftc.gov/resource/x.json")
    await fetcher.aclose()
    assert sleeps == []  # a 120 s hint exceeds the 60 s budget: report the rate limit instead of stalling
    assert exc.value.retry_after_s == 120
    assert len(rec.requests) == 1


@pytest.mark.parametrize("name", ["pwd", "pass", "authorization", "access_key_id", "Authorization"])
def test_more_credential_names_are_redacted(name: str) -> None:
    assert redact_params({name: "v" * 12}) == {name: "REDACTED"}


# ------------------------------------------------------------------------------------- review round 4 (P1)


def _hourly_candles(n: int, end: datetime, *, last_factor: float = 1.0) -> list[dict[str, Any]]:
    out = []
    for k in range(n, 0, -1):
        close = 100 + 0.3 * math.sin(k) + 0.1 * math.cos(3 * k)
        factor = last_factor if k == 1 else 1.0
        out.append(
            {
                "time": int((end - timedelta(hours=k)).timestamp() * 1000),
                "open": str(close * factor),
                "high": str(close * 1.002 * factor),
                "low": str(close * 0.998 * factor),
                "close": str(close * factor),
                "volume": "5",
            }
        )
    return out


@pytest.mark.parametrize("limit", [1, 2, 6, 10])
async def test_short_kraken_windows_are_still_checked_for_bad_prints(ctx_factory: Any, limit: int) -> None:
    hour = datetime(2026, 10, 21, 10, tzinfo=UTC)
    candles = _hourly_candles(40, hour, last_factor=50)
    sent: list[httpx.Request] = []
    handler = _kraken_handler(candles)

    def recording(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return handler(req)

    ctx, _ = ctx_factory(recording, now=hour + timedelta(minutes=5), cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    params = {"symbol": "BTC/USD:USD", "timeframe": "1h", "limit": limit}
    if limit == 1:  # the one bar asked for is a bad print: refused, never replaced by an older bar
        with pytest.raises(DataQualityError, match="every record was quarantined"):
            await adapter.fetch(DataRequest(kind=RequestKind.BARS, params=params))
        await adapter.aclose()
        return
    resp = await adapter.fetch(DataRequest(kind=RequestKind.BARS, params=params))
    await adapter.aclose()
    bars = [b for b in resp.records if isinstance(b, Bar)]
    assert len(bars) == limit - 1  # the held bar leaves a hole in the window asked for
    assert max(b.close for b in bars) < 200  # the x50 latest bar is held, never shown
    assert len(resp.quarantined) == 1
    charts = next(r for r in sent if "/charts/" in r.url.path)
    span = int(charts.url.params["to"]) - int(charts.url.params["from"])
    assert span >= 30 * 3600 - 1  # at least 30 hourly bars of context were requested, whatever the limit


async def test_short_twelvedata_and_eia_windows_are_still_checked(ctx_factory: Any) -> None:
    days = sorted(
        d for d in (datetime(2026, 10, 20, tzinfo=UTC) - timedelta(days=k) for k in range(60)) if d.weekday() < 5
    )
    values = [
        {
            "datetime": d.strftime("%Y-%m-%d"),
            "open": "1.1",
            "high": "1.2",
            "low": "1.0",
            "close": f"{1.1 + 0.002 * math.sin(i)}",
        }
        for i, d in enumerate(days)
    ]
    for k in ("open", "high", "low", "close"):
        values[-1][k] = f"{float(values[-1][k]) * 100:.4f}"
    body = {"meta": {"symbol": "EUR/USD", "type": "Physical Currency"}, "values": values, "status": "ok"}
    ctx, rec = ctx_factory(lambda _r: httpx.Response(200, json=body), flags={"provider_twelvedata": True}, **SETTINGS)
    adapter = TwelveDataAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "outputsize": 5}))
    await adapter.aclose()
    assert int(rec.requests[0].url.params["outputsize"]) >= 30
    assert len(resp.records) == 4  # 5 asked for, the held latest bar leaves a hole
    assert all(isinstance(b, Bar) and b.close < 2 for b in resp.records)

    crude = _crude()
    crude[-1] = (crude[-1] or 0.0) * 0.5
    ctx, rec = ctx_factory(lambda _r: httpx.Response(200, json=_eia_body(crude)), **SETTINGS)
    eia = EiaAdapter(ctx)
    resp = await eia.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1", "length": 4}))
    await eia.aclose()
    assert int(rec.requests[0].url.params["length"]) >= 30
    assert len(resp.records) == 3  # 4 asked for, the held latest week leaves a hole
    assert len(resp.quarantined) == 1


async def test_short_cot_window_is_still_checked(ctx_factory: Any) -> None:
    request = _plan(CftcAdapter, RequestKind.COT, report_type="disaggregated", futures_only=None)
    rows = json.loads(Cassette.load(CASSETTES / "cftc" / cassette_name(request)).interactions[0].body)
    rows[0] = {
        k: (str(float(v) * 10) if ("positions" in k or k == "open_interest_all") else v) for k, v in rows[0].items()
    }
    ctx, rec = ctx_factory(
        lambda _r: httpx.Response(200, json=rows),
        now=Cassette.load(CASSETTES / "cftc" / cassette_name(request)).replay_time,
    )
    adapter = CftcAdapter(ctx)
    params = {"report_type": "disaggregated", "market_code": "067651", "weeks": 4}
    resp = await adapter.fetch(DataRequest(kind=RequestKind.COT, params=params))
    await adapter.aclose()
    assert int(rec.requests[0].url.params["$limit"]) >= 30
    assert len(resp.records) == 3  # 4 asked for, the held latest week leaves a hole
    assert len(resp.quarantined) == 1


def _treasury_csv(days: list[datetime], tenyr: list[float]) -> str:
    header = "Date," + ",".join(COLUMNS)
    rows = []
    for k, (d, ten) in enumerate(sorted(zip(days, tenyr, strict=True), reverse=True)):  # newest first, like Treasury
        base = 3.7 + 0.02 * math.cos(k)
        curve = [f"{base + 0.05 * i:.2f}" for i in range(len(COLUMNS))]
        curve[list(COLUMNS).index("10 Yr")] = f"{ten:.2f}"
        rows.append(d.strftime("%m/%d/%Y") + "," + ",".join(curve))
    return "\n".join([header, *rows]) + "\n"


async def test_early_year_treasury_file_gets_last_year_as_context(ctx_factory: Any) -> None:
    """In early January the year's file holds a few days; the previous year's file gives the QA its history."""
    nyse = ExchangeCalendar("XNYS")

    def sessions(start: datetime, end: datetime) -> list[datetime]:
        out, d = [], start.date()
        while d <= end.date():
            out.append(datetime.combine(d, datetime.min.time(), tzinfo=UTC))
            d = nyse.closes_after(d, 1)[0].date()
        return out

    last_year = sessions(datetime(2025, 10, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC))
    this_year = sessions(datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 9, tzinfo=UTC))
    level = [4.2 + 0.03 * math.sin(i) for i in range(len(last_year) + len(this_year))]
    level[-1] *= 2  # the latest 10Y doubled
    files = {
        "2025": _treasury_csv(last_year, level[: len(last_year)]),
        "2026": _treasury_csv(this_year, level[len(last_year) :]),
    }
    ctx, rec = ctx_factory(
        lambda r: httpx.Response(200, text=files[r.url.path.rsplit("/", 2)[-2]]),
        now=datetime(2026, 1, 9, 23, tzinfo=UTC),
    )
    adapter = TreasuryAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.TREASURY_CURVE, params={"year": 2026}))
    await adapter.aclose()
    assert [r.url.path.rsplit("/", 2)[-2] for r in rec.requests] == ["2026", "2025"]
    assert all(isinstance(p, TreasuryYieldPoint) and p.ts.year == 2026 for p in resp.records)  # context not returned
    held = [p for p in resp.quarantined if isinstance(p, TreasuryYieldPoint)]
    assert [(p.tenor, p.ts.date().isoformat()) for p in held] == [("10Y", "2026-01-09")]
    assert resp.as_of == datetime(2026, 1, 8, tzinfo=UTC)  # the newest day with a full, usable curve


async def test_exchange_error_bodies_and_corrupt_gzip_count_on_the_breaker(ctx_factory: Any) -> None:
    request = _plan(KrakenFuturesAdapter, RequestKind.CRYPTO_DERIVATIVES)
    markets = next(
        it.body
        for it in Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request)).interactions
        if "instruments" in it.url
    )

    def kraken(req: httpx.Request) -> httpx.Response:
        if "instruments" in req.url.path:
            return httpx.Response(200, text=markets)
        return httpx.Response(200, json={"result": "error", "error": "Unavailable"})

    ctx, _ = ctx_factory(kraken, cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    derivatives = DataRequest(kind=RequestKind.CRYPTO_DERIVATIVES, params={"symbols": "BTC/USD:USD"})
    for _ in range(5):
        with pytest.raises(ProviderDown, match="exchange error"):
            await adapter.fetch(derivatives)
    with pytest.raises(ProviderDown, match="circuit open"):
        await adapter.fetch(derivatives)
    await adapter.aclose()

    def garbage(_r: httpx.Request) -> httpx.Response:  # decoded only when read, like a real network response
        return httpx.Response(200, stream=httpx.ByteStream(b"not gzip at all"), headers={"content-encoding": "gzip"})

    ctx, _ = ctx_factory(garbage, cache=False)
    cftc = CftcAdapter(ctx)
    cftc.http._sleep = _record_sleep([])
    cot = DataRequest(kind=RequestKind.COT, params={"report_type": "tff", "market_code": "099741"})
    with pytest.raises(ProviderDown, match="DecodingError"):
        await cftc.fetch(cot)
    await cftc.aclose()
    assert (await ctx.breaker.status("cftc")).consecutive_failures >= 1


async def test_expiry_week_open_interest_drops_are_not_withheld() -> None:
    """S&P 500 open interest falls ~23% at the September roll: a genuine move, shown (flagged), not held."""
    request = _plan(CftcAdapter, RequestKind.COT, report_type="legacy", futures_only=None)
    resp = await _replay(CftcAdapter, request)
    assert resp.quarantined == ()
    dates = {r.as_of.isoformat() for r in resp.records if hasattr(r, "as_of")}
    assert {"2026-09-22", "2026-09-29"} <= dates


async def test_eia_only_weekly_and_unlisted_series_on_release_day(ctx_factory: Any) -> None:
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json={}), **SETTINGS)
    adapter = EiaAdapter(ctx)
    with pytest.raises(InvalidArgs, match="weekly"):
        await adapter.fetch(
            DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1", "frequency": "monthly"})
        )
    await adapter.aclose()
    body = _eia_body(_crude(), series="WTTSTUS1")
    wednesday = datetime(2026, 10, 21, 16, tzinfo=UTC)  # released this morning (10:30 ET)
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=body), now=wednesday, **SETTINGS)
    adapter = EiaAdapter(ctx)
    resp = await adapter.fetch(
        DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WTTSTUS1", "route": "petroleum/stoc/wstk"})
    )
    await adapter.aclose()
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.FRESH


async def test_vendor_error_codes_and_calendar_from_the_answer(ctx_factory: Any) -> None:
    async def twelve(body: dict[str, Any], now: datetime | None = None, **params: Any) -> DataResponse:
        ctx, _ = ctx_factory(
            lambda _r: httpx.Response(200, json=body), now=now, flags={"provider_twelvedata": True}, **SETTINGS
        )
        adapter = TwelveDataAdapter(ctx)
        try:
            return await adapter.fetch(DataRequest(kind=RequestKind.BARS, params=params))
        finally:
            await adapter.aclose()

    with pytest.raises(ProviderDown, match="server error 503"):
        await twelve({"status": "error", "code": 503, "message": "maintenance"}, symbol="EUR/USD")

    days = [datetime(2026, 10, 15, tzinfo=UTC) - timedelta(days=k) for k in range(40, -1, -1)]  # crypto: every day
    values = [
        {
            "datetime": d.strftime("%Y-%m-%d"),
            "open": "60000",
            "high": "61000",
            "low": "59000",
            "close": f"{60000 + 50 * math.sin(i)}",
        }
        for i, d in enumerate(days)
    ]
    body = {"meta": {"symbol": "BTC/USD", "type": "Digital Currency"}, "values": values, "status": "ok"}
    # Sunday 10-18: two crypto sessions (16th, 17th) have closed since the 15th. On an FX calendar it would be fresh.
    resp = await twelve(body, now=datetime(2026, 10, 18, 12, tzinfo=UTC), symbol="BTC/USD")
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.STALE

    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json={"error": "too many requests", "code": 429}), **SETTINGS)
    adapter = EiaAdapter(ctx)
    with pytest.raises(RateLimited):
        await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1"}))
    await adapter.aclose()


def test_weekly_equity_gaps_are_detected() -> None:
    mondays = [datetime(2026, 6, 1, tzinfo=UTC) + timedelta(weeks=k) for k in range(20) if k != 7]
    bars = [
        Bar(symbol="AAPL", interval="1w", ts=m, open=100, high=101, low=99, close=100 + 0.1 * i)
        for i, m in enumerate(mondays)
    ]
    res = check_interval_gaps(bars, timedelta(weeks=1), ExchangeCalendar("XNYS"))
    assert not res.passed
    assert "1 of 20" in res.detail


@pytest.mark.parametrize(
    "name",
    ["app_key", "appid", "subscription-key", "Ocp-Apim-Subscription-Key", "X-Amz-Signature", "X-Amz-Credential",
     "AWSAccessKeyId", "privatekey", "hmac"],
)  # fmt: skip
def test_round4_credential_names_are_redacted(name: str) -> None:
    assert redact_params({name: "v" * 12}) == {name: "REDACTED"}


# ------------------------------------------------------------------------------------- review round 5 (P1)


def _kraken_range_server(
    end: datetime, width: timedelta, *, bad_last: bool
) -> Callable[[httpx.Request], httpx.Response]:
    """A charts endpoint that honours from/to like the real API (500 bars of history up to ``end``)."""
    request = _plan(KrakenFuturesAdapter, RequestKind.BARS)
    markets = next(
        it.body
        for it in Cassette.load(CASSETTES / "kraken_futures" / cassette_name(request)).interactions
        if "instruments" in it.url
    )
    starts = [end - width * k for k in range(500, 0, -1)]

    def candle(i: int, t: datetime) -> dict[str, Any]:
        close = 100 + 0.3 * math.sin(i) + 0.1 * math.cos(3 * i)
        if bad_last and t == starts[-1]:
            close *= 50
        return {"time": int(t.timestamp() * 1000), "open": str(close), "high": str(close * 1.002),
                "low": str(close * 0.998), "close": str(close), "volume": "5"}  # fmt: skip

    def handler(req: httpx.Request) -> httpx.Response:
        if "instruments" in req.url.path:
            return httpx.Response(200, text=markets)
        lo, hi = int(req.url.params["from"]), int(req.url.params["to"])
        rows = [candle(i, t) for i, t in enumerate(starts) if lo <= t.timestamp() <= hi]
        return httpx.Response(200, json={"candles": rows, "more_candles": False})

    return handler


@pytest.mark.parametrize(("timeframe", "width"), [("1d", timedelta(days=1)), ("1h", timedelta(hours=1))])
@pytest.mark.parametrize("limit", [None, 25])
async def test_incremental_kraken_polls_since_the_last_bar_are_checked(
    ctx_factory: Any, timeframe: str, width: timedelta, limit: int | None
) -> None:
    """``since_ms`` = the last stored bar: only a few bars are new, but QA still sees 30 of history."""
    end = datetime(2026, 10, 21, tzinfo=UTC)
    now = end + timedelta(minutes=5)
    sent: list[httpx.Request] = []
    server = _kraken_range_server(end, width, bad_last=True)

    def recording(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return server(req)

    ctx, _ = ctx_factory(recording, now=now, cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    since = int((end - 5 * width).timestamp() * 1000)
    params: dict[str, Any] = {"symbol": "BTC/USD:USD", "timeframe": timeframe, "since_ms": since}
    if limit is not None:
        params["limit"] = limit
    resp = await adapter.fetch(DataRequest(kind=RequestKind.BARS, params=params))
    await adapter.aclose()
    bars = [b for b in resp.records if isinstance(b, Bar)]
    assert bars
    assert all(b.ts.timestamp() * 1000 >= since for b in bars)  # context is never returned
    assert max(b.close for b in bars) < 200
    assert len(resp.quarantined) == 1
    charts = next(r for r in sent if "/charts/" in r.url.path)
    assert int(charts.url.params["from"]) <= since // 1000 - 25 * int(width.total_seconds())
    assert int(charts.url.params["to"]) <= int(now.timestamp())  # the injected clock, not the wall clock


def _fred_daily(values: list[float]) -> tuple[str, str]:
    info, _ = _fred_bodies()
    days = [datetime(2026, 10, 20, tzinfo=UTC) - timedelta(days=k) for k in range(len(values) * 2)]
    days = sorted(d for d in days if d.weekday() < 5)[-len(values) :]
    vintage = {"realtime_start": "2026-10-21", "realtime_end": "2026-10-21"}
    obs = {
        "observations": [
            {**vintage, "date": d.strftime("%Y-%m-%d"), "value": f"{v:.2f}"} for d, v in zip(days, values, strict=True)
        ]
    }
    return info, json.dumps(obs)


@pytest.mark.parametrize("factor", [10.0, 0.1, 2.0])
async def test_impossible_latest_fred_daily_yield_is_held(ctx_factory: Any, factor: float) -> None:
    values = [4.08 + 0.03 * math.sin(k) for k in range(60)]
    values[-1] *= factor
    info, obs = _fred_daily(values)
    ctx, _ = ctx_factory(
        lambda r: httpx.Response(200, text=info if r.url.path.endswith("/series") else obs),
        flags={"provider_fred": True},
        **SETTINGS,
    )
    adapter = FredAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"}))
    await adapter.aclose()
    assert len(resp.quarantined) == 1
    assert all(isinstance(o, MacroObservation) and (o.value or 0) < 5 for o in resp.records)


async def test_rate_limited_probe_is_released_and_auth_failures_count(ctx_factory: Any) -> None:
    clock = [1_000.0]
    calls: list[int] = []
    answers = iter([httpx.Response(429), httpx.Response(200, json=[])])

    def handler(_r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return next(answers)

    ctx, _ = ctx_factory(handler, cache=False)
    ctx.breaker = CircuitBreaker(MemoryStateStore(clock=lambda: clock[0]), failure_threshold=1, cooldown_s=60)
    await ctx.breaker.record_failure("cftc", "HTTP 503")
    clock[0] += 61
    adapter = CftcAdapter(ctx)
    adapter.http._sleep = _record_sleep([])
    adapter.http._max_attempts = 1
    request = DataRequest(kind=RequestKind.COT, params={"report_type": "tff", "market_code": "099741"})
    with pytest.raises(RateLimited):
        await adapter.fetch(request)  # the probe is answered 429: no verdict, probe given back
    with pytest.raises(NotFound):  # one second later the next fetch may probe at once
        await adapter.fetch(request)
    await adapter.aclose()
    assert len(calls) == 2

    ctx, _ = ctx_factory(lambda _r: httpx.Response(403), **SETTINGS)
    eia = EiaAdapter(ctx)
    for _ in range(5):
        with pytest.raises(ProviderDown, match="authorization"):
            await eia.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1"}))
    await eia.aclose()
    assert (await ctx.breaker.status("eia")).state is CircuitState.OPEN  # a revoked key shows as down


async def test_cot_moderate_latest_moves_are_flagged_and_old_gaps_do_not_block(ctx_factory: Any) -> None:
    request = _plan(CftcAdapter, RequestKind.COT, report_type="disaggregated", futures_only=None)
    cassette = Cassette.load(CASSETTES / "cftc" / cassette_name(request))
    rows = json.loads(cassette.interactions[0].body)
    scaled = [dict(r) for r in rows]
    scaled[0] = {
        k: (str(float(v) * 2.5) if ("positions" in k or k == "open_interest_all") else v) for k, v in rows[0].items()
    }
    resp = await _replay(CftcAdapter, request, {0: lambda _b: json.dumps(scaled)})
    assert any("large_moves" in w for w in resp.quality.warnings)  # shown, but flagged for review

    gappy = [r for i, r in enumerate(rows) if i not in (20, 21)]  # two weeks missing ~5 months back
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=gappy), now=cassette.replay_time)
    adapter = CftcAdapter(ctx)
    params = {"report_type": "disaggregated", "market_code": "067651", "weeks": 4}
    resp = await adapter.fetch(DataRequest(kind=RequestKind.COT, params=params))
    await adapter.aclose()
    assert len(resp.records) == 4  # the gap is outside the weeks asked for: no block


async def test_unlisted_eia_series_hold_impossible_latest_and_kraken_overflow_is_typed(ctx_factory: Any) -> None:
    values = _crude()
    values[-1] = (values[-1] or 0.0) * 10
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=_eia_body(values, series="WTTSTUS1")), **SETTINGS)
    adapter = EiaAdapter(ctx)
    resp = await adapter.fetch(
        DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WTTSTUS1", "route": "petroleum/stoc/wstk"})
    )
    await adapter.aclose()
    assert len(resp.quarantined) == 1

    hour = datetime(2026, 10, 21, 10, tzinfo=UTC)
    candles = _hourly_candles(40, hour)
    candles[5]["time"] = 10**20  # beyond any representable date
    ctx, _ = ctx_factory(_kraken_handler(candles), now=hour + timedelta(minutes=5), cache=False)
    kraken = KrakenFuturesAdapter(ctx)
    with pytest.raises(DataQualityError, match="schema"):
        await kraken.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "BTC/USD:USD", "timeframe": "1h"}))
    await kraken.aclose()


# ------------------------------------------------------------------------------------- review round 6 (P1)


def _revenue_fact(data: dict[str, Any]) -> dict[str, Any]:
    facts = data["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    period = ("2025-01-01", "2025-06-30")
    same = [f for f in facts if (f.get("start"), f["end"]) == period and f.get("form") in ("10-Q", "10-K")]
    assert len(same) >= 2, "the recorded AMZN facts carry this half-year in two filings"
    return max(same, key=lambda f: f["filed"])


async def test_sec_scale_error_against_another_filing_is_quarantined() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)

    def x1000(data: dict[str, Any]) -> None:
        _revenue_fact(data)["val"] *= 1000

    resp = await _replay(SecEdgarAdapter, request, {0: _json_edit(x1000)})
    shown = [
        f
        for f in resp.records
        if isinstance(f, FundamentalFact)
        and f.concept == "RevenueFromContractWithCustomerExcludingAssessedTax"
        and f.period_end.isoformat() == "2025-06-30"
        and f.period_start is not None
        and f.period_start.isoformat() == "2025-01-01"
    ]
    assert shown
    assert max(f.value for f in shown) < 1e12  # the 323-trillion figure never reaches the user
    # held: the x1000 fact and its sibling in the same table (that filing's Q2-2025 comparative)...
    held = [f for f in resp.quarantined if isinstance(f, FundamentalFact)]
    assert {(f.accession, f.period_end.isoformat()) for f in held} == {("0001018724-26-000026", "2025-06-30")}
    assert len(held) == 2
    # ...while Q2 2025 is still shown, as reported by the other filings
    assert any(
        f.concept == "RevenueFromContractWithCustomerExcludingAssessedTax" and f.period_end.isoformat() == "2025-06-30"
        and f.period_start is not None and f.period_start.isoformat() == "2025-04-01"
        for f in resp.records if isinstance(f, FundamentalFact)
    )  # fmt: skip
    assert any("scale_consistency" in w for w in resp.quality.warnings)


async def test_sec_real_facts_trip_no_scale_quarantine() -> None:
    """The rule is calibrated on real data: 9,000 multi-filing periods, none quarantined (restatements kept)."""
    resp = await _replay(SecEdgarAdapter, _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS))
    assert resp.quarantined == ()


async def test_fred_record_percent_release_is_shown(ctx_factory: Any) -> None:
    """Argentina's 2023 inflation (133.5%) is a genuine record release: shown with a warning, never dropped."""
    info, _ = _fred_bodies(series="FPCPITOTLZGARG")
    meta = json.loads(info)
    meta["seriess"][0].update(frequency_short="A", units="Percent", units_short="%")
    years = list(range(1994, 2024))
    values = [10.0 + 5 * math.sin(k) for k in range(len(years) - 2)] + [72.4, 133.5]
    obs = {"observations": [{"date": f"{y}-01-01", "value": f"{v:.1f}"} for y, v in zip(years, values, strict=True)]}
    ctx, _ = ctx_factory(
        lambda r: httpx.Response(200, text=json.dumps(meta) if r.url.path.endswith("/series") else json.dumps(obs)),
        flags={"provider_fred": True},
        **SETTINGS,
    )
    adapter = FredAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "FPCPITOTLZGARG"}))
    await adapter.aclose()
    assert resp.as_of == datetime(2023, 1, 1, tzinfo=UTC)
    assert resp.quarantined == ()


async def test_fred_held_latest_makes_the_response_only_as_fresh_as_what_it_shows(ctx_factory: Any) -> None:
    values = [4.08 + 0.03 * math.sin(k) for k in range(60)]
    values[-1] *= 10
    info, obs = _fred_daily(values)
    ctx, _ = ctx_factory(
        lambda r: httpx.Response(200, text=info if r.url.path.endswith("/series") else obs),
        now=datetime(2026, 10, 23, 12, tzinfo=UTC),
        flags={"provider_fred": True},
        **SETTINGS,
    )
    adapter = FredAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"}))
    await adapter.aclose()
    assert resp.freshness is not None
    assert resp.freshness.as_of == resp.as_of  # judged from the newest value shown, not from last_updated


async def test_fred_projections_left_out_and_400_bodies_mapped(ctx_factory: Any) -> None:
    info, obs_text = _fred_bodies()
    obs = json.loads(obs_text)
    obs["observations"].append({"date": "2030-01-01", "value": "4.00"})  # a projection row
    ctx, _ = ctx_factory(
        lambda r: httpx.Response(200, text=info if r.url.path.endswith("/series") else json.dumps(obs)),
        flags={"provider_fred": True},
        **SETTINGS,
    )
    adapter = FredAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"}))
    await adapter.aclose()
    assert all(isinstance(o, MacroObservation) and o.ts.year < 2030 for o in resp.records)
    assert any("projections" in n for n in resp.notes)

    bad_key = {"error_code": 400, "error_message": "Bad Request.  The value for variable api_key is not registered."}
    ctx, _ = ctx_factory(lambda _r: httpx.Response(400, json=bad_key), flags={"provider_fred": True}, **SETTINGS)
    adapter = FredAdapter(ctx)
    with pytest.raises(ProviderDown, match="authorization"):
        await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"}))
    await adapter.aclose()
    assert (await ctx.breaker.status("fred")).consecutive_failures == 1

    missing = {"error_code": 400, "error_message": "Bad Request.  The series does not exist."}
    ctx, _ = ctx_factory(lambda _r: httpx.Response(400, json=missing), flags={"provider_fred": True}, **SETTINGS)
    adapter = FredAdapter(ctx)
    with pytest.raises(NotFound):
        await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "NOSUCH"}))
    await adapter.aclose()


async def test_vendor_plan_limits_and_exchange_bad_requests_do_not_trip_the_breaker(
    ctx_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"status": "error", "code": 403, "message": "This symbol is available starting with the Pro plan"}
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=body), flags={"provider_twelvedata": True}, **SETTINGS)
    adapter = TwelveDataAdapter(ctx)
    for _ in range(6):
        with pytest.raises(LicenceBlocked):
            await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "XAU/USD"}))
    await adapter.aclose()
    assert (await ctx.breaker.status("twelvedata")).state is CircuitState.CLOSED

    import ccxt.async_support as ccxt_async  # noqa: PLC0415 - only this test needs the CCXT exception types

    from rd_data.adapters import kraken_futures  # noqa: PLC0415

    request = _plan(KrakenFuturesAdapter, RequestKind.CRYPTO_DERIVATIVES)
    for exc, expected in (
        (ccxt_async.BadRequest("bad"), InvalidArgs),
        (ccxt_async.DDoSProtection("slow"), RateLimited),
    ):

        async def raising(self: Any, *_a: Any, _exc: Exception = exc, **_k: Any) -> Any:
            raise _exc

        monkeypatch.setattr(kraken_futures._RoutedKrakenFutures, "fetch_tickers", raising)
        with pytest.raises(expected):
            await _replay(KrakenFuturesAdapter, request)


async def test_schema_drift_shows_on_the_status_page_without_opening_the_circuit() -> None:
    request = _plan(TreasuryAdapter, RequestKind.TREASURY_CURVE)

    def rename(rows: list[list[str]]) -> None:
        rows[0][rows[0].index("10 Yr")] = "10 Year"

    cassette = Cassette.load(CASSETTES / "us_treasury" / cassette_name(request))
    cassette.interactions[0].body = _csv_edit(rename)(cassette.interactions[0].body)
    breaker = CircuitBreaker(MemoryStateStore())
    ctx = AdapterContext(
        settings=Settings(),
        limiter=LocalRateLimiter(),
        breaker=breaker,
        clock=FixedClock(cassette.replay_time),
        transport=ReplayTransport([cassette]),
    )
    adapter = TreasuryAdapter(ctx)
    with pytest.raises(DataQualityError):
        await adapter.fetch(request)
    await adapter.aclose()
    statuses = await provider_statuses({}, breaker, Settings())
    assert statuses["us_treasury"]["circuit"] == "closed"
    assert statuses["us_treasury"]["last_quality_error"] == "csv_header"  # check names only: the page is public


async def test_physically_impossible_weekly_storage_change_is_held(ctx_factory: Any) -> None:
    storage = [3_500.0]
    for k in range(59):  # realistic weekly builds and draws of up to ~130 Bcf: the MAD alone would let +700 pass
        storage.append(storage[-1] + 90 * math.sin(k * 1.3) + 40 * math.cos(k * 2.1))
    storage[-1] += 700  # +700 Bcf in one week: beyond any injection the system can make
    body = _eia_body(storage, series="NW2_EPG0_SWO_R48_BCF", unit="BCF")
    ctx, _ = ctx_factory(
        lambda _r: httpx.Response(200, json=body), now=datetime(2026, 10, 23, 12, tzinfo=UTC), **SETTINGS
    )
    adapter = EiaAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "NW2_EPG0_SWO_R48_BCF"}))
    await adapter.aclose()
    assert len(resp.quarantined) == 1
    assert any("max_weekly_change" in w for w in resp.quality.warnings)


async def test_kraken_since_poll_gaps_outside_the_window_do_not_block(ctx_factory: Any) -> None:
    end = datetime(2026, 10, 21, tzinfo=UTC)
    server = _kraken_range_server(end, timedelta(days=1), bad_last=False)

    def gappy(req: httpx.Request) -> httpx.Response:
        resp = server(req)
        if "/charts/" in req.url.path:
            data = json.loads(resp.content)
            data["candles"] = [c for i, c in enumerate(data["candles"]) if i not in (5, 6)]  # ~3 weeks back
            return httpx.Response(200, json=data)
        return resp

    ctx, _ = ctx_factory(gappy, now=end + timedelta(hours=1), cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    since = int((end - timedelta(days=3, hours=8)).timestamp() * 1000)
    resp = await adapter.fetch(
        DataRequest(kind=RequestKind.BARS, params={"symbol": "BTC/USD:USD", "timeframe": "1d", "since_ms": since})
    )
    await adapter.aclose()
    assert resp.records


# ------------------------------------------------------------------------------------- review round 7 (P1)

NEWEST_10Q = "0001018724-26-000026"


def _scale_filing(accession: str, factor: float, *, concept: str | None = None, end: str | None = None) -> Mutator:
    def edit(data: dict[str, Any]) -> None:
        for name, body in data["facts"]["us-gaap"].items():
            if concept is not None and name != concept:
                continue
            for facts in body["units"].values():
                for f in facts:
                    if f["accn"] == accession and (end is None or f["end"] == end):
                        f["val"] = f["val"] * factor

    return _json_edit(edit)


@pytest.mark.parametrize("factor", [1000.0, 0.001])
@pytest.mark.parametrize(
    "concept",
    [
        "RevenueFromContractWithCustomerExcludingAssessedTax",  # stable: judged against its neighbouring quarters
        # volatile flows, where /1000 stays within 10^2.5 of the neighbours: caught because H1 != Q1 + Q2
        "IncomeTaxExpenseBenefit",
        "ComprehensiveIncomeNetOfTax",
        "OtherNonoperatingIncomeExpense",
    ],
)
async def test_scale_error_in_the_newest_quarter_is_held(factor: float, concept: str) -> None:
    """The newest quarter exists in one filing only: it is judged against the same concept's neighbouring quarters
    and against the sum it belongs to (the half-year = the earlier filing's first quarter + the newest quarter)."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    resp = await _replay(
        SecEdgarAdapter, request, {0: _scale_filing(NEWEST_10Q, factor, concept=concept, end="2026-06-30")}
    )
    newest = [
        f
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.concept == concept and f.accession == NEWEST_10Q
        and f.period_end.isoformat() == "2026-06-30"
    ]  # fmt: skip
    assert newest == []  # neither the quarter nor the half-year at the wrong scale is shown
    held = [f for f in resp.quarantined if isinstance(f, FundamentalFact) and f.concept == concept]
    assert len(held) >= 2


def _fact(start: str, end: str, value: float, accession: str) -> FundamentalFact:
    return FundamentalFact(
        cik="0000000001", concept="NetIncomeLoss", taxonomy="us-gaap", unit="USD", value=value,
        period_start=date.fromisoformat(start), period_end=date.fromisoformat(end), form="10-Q",
        filed=date.fromisoformat(end) + timedelta(days=30), accession=accession,
    )  # fmt: skip


def test_rounded_or_restated_sums_are_not_scale_errors() -> None:
    """Values rounded to millions (2 != 1 + 2) and reclassified halves do not add up, but no power of 1000 explains
    them: shrinking one term into insignificance is not evidence (both seen in the recorded AMZN facts)."""
    rounded = [
        _fact("2014-01-01", "2014-06-30", 1e6, "a"),
        _fact("2014-07-01", "2014-09-30", 2e6, "b"),
        _fact("2014-01-01", "2014-09-30", 2e6, "b"),
    ]
    restated = [
        _fact("2020-01-01", "2020-03-31", 693e6, "a"),
        _fact("2020-04-01", "2020-06-30", 9_918e6, "b"),
        _fact("2020-01-01", "2020-06-30", 9_994e6, "b"),
    ]
    assert sec_scale.scale_errors(rounded)[0] == set()
    assert sec_scale.scale_errors(restated)[0] == set()


def test_scale_error_found_by_a_sum_is_pinned_on_the_side_far_from_its_neighbours() -> None:
    """H1 = Q1 + Q2 fails with the newest filing at /1000. Scaling that filing up or the earlier one down both repair
    the sum; the neighbouring periods, as reported by neither filing, decide which one is wrong."""
    history = [
        _fact("2024-01-01", "2024-06-30", 60e9, "h1"),
        _fact("2025-01-01", "2025-06-30", 62e9, "h2"),
        *(_fact(f"2025-{m:02d}-01", end, 31e9, "h2") for m, end in ((1, "2025-03-31"), (4, "2025-06-30"))),
        *(_fact(f"2025-{m:02d}-01", end, 30e9, "h3") for m, end in ((7, "2025-09-30"), (10, "2025-12-31"))),
    ]
    q1 = _fact("2026-01-01", "2026-03-31", 30.25e9, "old")
    q2 = _fact("2026-04-01", "2026-06-30", 62.65e6, "new")
    h1 = _fact("2026-01-01", "2026-06-30", 92.9e6, "new")
    records = [*history, q1, q2, h1]
    held, _ = sec_scale.scale_errors(records)
    assert {records[i] for i in held} == {q2, h1}
    # no history to decide by: nothing in the disagreeing sum is served
    assert sec_scale.scale_errors([q1, q2, h1])[0] == {0, 1, 2}
    # one filing alone cannot show its own scale error
    assert sec_scale.scale_errors([q2, h1, _fact("2026-01-01", "2026-03-31", 30.25e6, "new")])[0] == set()


async def test_a_mis_scaled_filing_is_held_whole() -> None:
    """Every fact of the newest 10-Q x1000: the whole filing is withheld, and what is left (the April filing) is more
    than 120 days old, so the response is honestly refused as stale rather than served with the wrong scale."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    with pytest.raises(Stale, match="2026-04-30"):
        await _replay(SecEdgarAdapter, request, {0: _scale_filing(NEWEST_10Q, 1000.0)})


async def test_undecidable_scale_disagreement_holds_both_filings() -> None:
    """Never serve the x1000 copy: when the neighbours cannot decide between two filings, both are held."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    concept = "AmortizationOfAcquiredIntangibleAssets"
    resp = await _replay(SecEdgarAdapter, request, {0: _scale_filing("0001193125-13-028520", 1000.0, concept=concept)})
    shown_2010 = [
        f.value
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.concept == concept and f.period_end.isoformat() == "2010-12-31"
        and f.period_start is not None and (f.period_end - f.period_start).days > 300
    ]  # fmt: skip
    # Two filings, each contradicting the other by x1000, and no third filing to side with either: nothing for this
    # concept is shown from either filing (a guess could pick the wrong scale), and the hold is reported.
    assert shown_2010 == []
    assert all(f.value < 1e9 for f in resp.records if isinstance(f, FundamentalFact) and f.concept == concept)
    assert any("scale_consistency" in w for w in resp.quality.warnings)


async def test_hole_at_the_start_of_a_since_window_is_reported(ctx_factory: Any) -> None:
    end = datetime(2026, 10, 21, 0, tzinfo=UTC)
    server = _kraken_range_server(end, timedelta(hours=1), bad_last=False)
    since_dt = end - timedelta(hours=6)

    def holed(req: httpx.Request) -> httpx.Response:
        resp = server(req)
        if "/charts/" not in req.url.path:
            return resp
        data = json.loads(resp.content)
        hole = {int((since_dt + timedelta(hours=h)).timestamp() * 1000) for h in range(3)}  # 18:00-20:00 missing
        data["candles"] = [c for c in data["candles"] if c["time"] not in hole]
        return httpx.Response(200, json=data)

    ctx, _ = ctx_factory(holed, now=end + timedelta(minutes=5), cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    params = {"symbol": "BTC/USD:USD", "timeframe": "1h", "since_ms": int(since_dt.timestamp() * 1000)}
    with pytest.raises(DataQualityError, match="calendar_gaps"):
        await adapter.fetch(DataRequest(kind=RequestKind.BARS, params=params))
    await adapter.aclose()


async def test_fred_trailing_holiday_row_and_wrong_shape(ctx_factory: Any) -> None:
    values = [4.08 + 0.03 * math.sin(k) for k in range(60)]
    values[-1] *= 10
    info, obs_text = _fred_daily(values)
    obs = json.loads(obs_text)
    obs["observations"].append({"date": "2026-10-21", "value": "."})  # holiday row after the held value
    ctx, _ = ctx_factory(
        lambda r: httpx.Response(200, text=info if r.url.path.endswith("/series") else json.dumps(obs)),
        now=datetime(2026, 10, 23, 12, tzinfo=UTC),
        flags={"provider_fred": True},
        **SETTINGS,
    )
    adapter = FredAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"}))
    await adapter.aclose()
    assert resp.freshness is not None
    assert resp.freshness.as_of == resp.as_of

    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json={}), flags={"provider_fred": True}, **SETTINGS)
    adapter = FredAdapter(ctx)
    with pytest.raises(ProviderDown, match="malformed"):
        await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"}))
    await adapter.aclose()


async def test_sec_wrong_shape_is_an_outage_and_undated_facts_are_set_aside() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    with pytest.raises(ProviderDown, match="malformed"):
        await _replay(SecEdgarAdapter, request, {0: lambda _b: "{}"})

    def undated(data: dict[str, Any]) -> None:
        _revenue_fact(data)["filed"] = "not-a-date"

    resp = await _replay(SecEdgarAdapter, request, {0: _json_edit(undated)})
    assert any("point_in_time" in w for w in resp.quality.warnings)  # one bad date never blocks the company


async def test_twelvedata_http_403_is_a_plan_limit(ctx_factory: Any) -> None:
    ctx, _ = ctx_factory(lambda _r: httpx.Response(403, json={}), flags={"provider_twelvedata": True}, **SETTINGS)
    adapter = TwelveDataAdapter(ctx)
    for _ in range(6):
        with pytest.raises(LicenceBlocked):
            await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "XAU/USD"}))
    await adapter.aclose()
    assert (await ctx.breaker.status("twelvedata")).state is CircuitState.CLOSED


def test_single_missing_bar_warns_instead_of_blocking() -> None:
    days = [date(2026, 10, 13) - timedelta(days=7 * i) for i in range(10) if i != 4]
    res = check_cadence(days, period=timedelta(days=7))
    assert not res.passed
    assert not res.blocking  # 1 of 10 weeks: shown with a warning, not refused


async def test_eia_weekly_change_hold_is_in_the_report(ctx_factory: Any) -> None:
    storage = [3_500.0]
    for k in range(59):
        storage.append(storage[-1] + 90 * math.sin(k * 1.3) + 40 * math.cos(k * 2.1))
    storage[-1] += 700
    body = _eia_body(storage, series="NW2_EPG0_SWO_R48_BCF", unit="BCF")
    ctx, _ = ctx_factory(
        lambda _r: httpx.Response(200, json=body), now=datetime(2026, 10, 23, 12, tzinfo=UTC), **SETTINGS
    )
    adapter = EiaAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "NW2_EPG0_SWO_R48_BCF"}))
    await adapter.aclose()
    assert len(resp.quality.quarantined) == len(resp.quarantined) == 1


# ------------------------------------------------------------------------------------- review round 8 (P1)

NEWEST_10K = "0001018724-26-000004"


@pytest.mark.parametrize("factor", [0.001, 1000.0])
@pytest.mark.parametrize(
    "concept", ["IncomeTaxExpenseBenefit", "ComprehensiveIncomeNetOfTax", "NonoperatingIncomeExpense"]
)
async def test_newest_10k_scale_error_is_held_against_the_same_years_nine_months(factor: float, concept: str) -> None:
    """The full year exists in the 10-K only, and annual neighbours are years apart: the year is judged against the
    same year's nine months (an earlier 10-Q), whose ratio to the year is steady across years."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    resp = await _replay(
        SecEdgarAdapter, request, {0: _scale_filing(NEWEST_10K, factor, concept=concept, end="2025-12-31")}
    )
    shown = [
        f.value
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.concept == concept and f.accession == NEWEST_10K
        and f.period_end.isoformat() == "2025-12-31"
    ]  # fmt: skip
    assert shown == []
    assert any("scale_consistency" in w for w in resp.quality.warnings)


@pytest.mark.parametrize("factor", [1000.0, 0.001])
async def test_a_mis_scaled_column_is_held_whole(factor: float) -> None:
    """Only the current-period column of the newest 10-Q is mis-scaled (comparatives intact): once most of it is
    held, the rest of it (cash, new concepts) is held too rather than served at the wrong scale."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    resp = await _replay(SecEdgarAdapter, request, {0: _scale_filing(NEWEST_10Q, factor, end="2026-06-30")})
    shown = [
        f
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.accession == NEWEST_10Q and f.period_end.isoformat() == "2026-06-30"
    ]
    assert shown == []
    assert any(f.period_end.isoformat() == "2025-06-30" for f in resp.records if isinstance(f, FundamentalFact))


async def test_empty_sec_ticker_map_is_an_outage() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.INSTRUMENTS)
    with pytest.raises(ProviderDown, match="empty ticker map"):
        await _replay(SecEdgarAdapter, request, {0: lambda _b: "{}"})


async def test_kraken_gaps_before_since_do_not_block_and_a_bad_first_bar_is_reported(ctx_factory: Any) -> None:
    end = datetime(2026, 10, 21, 0, tzinfo=UTC)
    width = timedelta(hours=1)
    since_dt = end - timedelta(hours=6)
    server = _kraken_range_server(end, width, bad_last=False)

    def edited(req: httpx.Request) -> httpx.Response:
        resp = server(req)
        if "/charts/" not in req.url.path:
            return resp
        data = json.loads(resp.content)
        before = {int((since_dt - timedelta(hours=h)).timestamp() * 1000) for h in (1, 2)}  # 16:00-17:00 missing
        data["candles"] = [c for c in data["candles"] if c["time"] not in before]
        for c in data["candles"]:
            if c["time"] == int(since_dt.timestamp() * 1000):  # the first bar asked for: a x50 print
                c.update({k: str(float(c[k]) * 50) for k in ("open", "high", "low", "close")})
        return httpx.Response(200, json=data)

    ctx, _ = ctx_factory(edited, now=end + timedelta(minutes=5), cache=False)
    adapter = KrakenFuturesAdapter(ctx)
    params = {"symbol": "BTC/USD:USD", "timeframe": "1h", "since_ms": int(since_dt.timestamp() * 1000)}
    resp = await adapter.fetch(DataRequest(kind=RequestKind.BARS, params=params))
    await adapter.aclose()
    assert [b.ts for b in resp.quarantined if isinstance(b, Bar)] == [since_dt]
    assert all(isinstance(b, Bar) and b.close < 200 for b in resp.records)
    assert not any("calendar_gaps" in w for w in resp.quality.warnings)


async def test_twelvedata_bad_print_at_the_start_of_the_window_is_reported(ctx_factory: Any) -> None:
    days = sorted(
        d for d in (datetime(2026, 10, 20, tzinfo=UTC) - timedelta(days=k) for k in range(60)) if d.weekday() < 5
    )
    values = [
        {"datetime": d.strftime("%Y-%m-%d"), "open": "1.1", "high": "1.2", "low": "1.0",
         "close": f"{1.1 + 0.002 * math.sin(i)}"}
        for i, d in enumerate(days)
    ]  # fmt: skip
    for k in ("open", "high", "low", "close"):
        values[-5][k] = f"{float(values[-5][k]) * 100:.4f}"
    body = {"meta": {"symbol": "EUR/USD", "type": "Physical Currency"}, "values": values, "status": "ok"}
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=body), flags={"provider_twelvedata": True}, **SETTINGS)
    adapter = TwelveDataAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "outputsize": 5}))
    await adapter.aclose()
    assert [b.ts for b in resp.quarantined if isinstance(b, Bar)] == [days[-5]]


# ------------------------------------------------------------------------------------- review round 9 (P1)


@pytest.mark.parametrize(
    ("accession", "concept", "end"),
    [
        (NEWEST_10K, "FederalIncomeTaxExpenseBenefitContinuingOperations", "2025-12-31"),  # volatile annual-only line
        (NEWEST_10Q, "DerivativeAssets", "2026-06-30"),  # first reported in this filing: no history at all
    ],
)
async def test_values_beyond_the_companys_total_assets_are_held(accession: str, concept: str, end: str) -> None:
    """x1e6 lands at 10^4-10^5 times total assets in the same response: impossible for any line item."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    resp = await _replay(SecEdgarAdapter, request, {0: _scale_filing(accession, 1e6, concept=concept, end=end)})
    assert not [
        f
        for f in resp.records
        if isinstance(f, FundamentalFact)
        and f.concept == concept
        and f.accession == accession
        and f.period_end.isoformat() == end
    ]
    assert any("scale_consistency" in w for w in resp.quality.warnings)


def test_off_balance_sheet_amounts_may_exceed_total_assets() -> None:
    def fact(concept: str, value: float, taxonomy: str = "us-gaap") -> FundamentalFact:
        return FundamentalFact(
            cik="0000000001", concept=concept, taxonomy=taxonomy, unit="USD", value=value,
            period_end=date(2025, 12, 31), form="10-K", filed=date(2026, 2, 1), accession="a",
        )  # fmt: skip

    records = [
        fact("Assets", 1e9),
        fact("DerivativeNotionalAmount", 5e11),  # 500x: a dealer's notional is not on its balance sheet
        fact("EntityPublicFloat", 3e11, taxonomy="dei"),  # a market value, not a line item
        fact("Liabilities", 5e11),  # 500x: impossible
    ]
    logs: list[float | None] = [math.log10(f.value) for f in records]
    assert sec_scale._beyond_assets(records, logs, set(), set()) == {3}


async def test_a_balance_that_jumps_by_orders_of_magnitude_is_flagged() -> None:
    """PP&E moves smoothly (largest clean step 10^0.4); /1000 in the newest 10-K is at least flagged."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    concept = "PropertyPlantAndEquipmentNet"

    def flagged(resp: DataResponse) -> int:
        check = next(c for c in resp.quality.checks if c.name == "scale_outliers")
        return len(check.indices)

    clean = await _replay(SecEdgarAdapter, request)
    seeded = await _replay(
        SecEdgarAdapter, request, {0: _scale_filing(NEWEST_10K, 0.001, concept=concept, end="2025-12-31")}
    )
    assert flagged(seeded) == flagged(clean) + 1


async def test_sec_ticker_map_with_no_usable_rows_is_an_outage() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.INSTRUMENTS)
    with pytest.raises(ProviderDown, match="no usable rows"):
        await _replay(SecEdgarAdapter, request, {0: lambda _b: json.dumps({"0": {"ticker": "X"}, "1": [1]})})


async def test_a_held_bar_leaves_a_hole_and_never_shifts_the_window(ctx_factory: Any) -> None:
    days = sorted(
        d for d in (datetime(2026, 10, 20, tzinfo=UTC) - timedelta(days=k) for k in range(60)) if d.weekday() < 5
    )
    values = [
        {"datetime": d.strftime("%Y-%m-%d"), "open": "1.1", "high": "1.2", "low": "1.0",
         "close": f"{1.1 + 0.002 * math.sin(i)}"}
        for i, d in enumerate(days)
    ]  # fmt: skip
    for k in ("open", "high", "low", "close"):
        values[-2][k] = f"{float(values[-2][k]) * 100:.4f}"
    body = {"meta": {"symbol": "EUR/USD", "type": "Physical Currency"}, "values": values, "status": "ok"}
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, json=body), flags={"provider_twelvedata": True}, **SETTINGS)
    adapter = TwelveDataAdapter(ctx)
    resp = await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "outputsize": 3}))
    await adapter.aclose()
    assert [b.ts for b in resp.records if isinstance(b, Bar)] == [days[-3], days[-1]]
    assert [b.ts for b in resp.quarantined if isinstance(b, Bar)] == [days[-2]]


# ------------------------------------------------------------------------------------- review round 10 (P1)

EARLIER_QUARTER = "0001018724-26-000014"


async def test_a_mis_scaled_older_filing_never_withholds_the_newest_figures() -> None:
    """The previous 10-Q's whole column /1000 is held, and must not shrink the total-assets yardstick: the newest
    quarter's genuine revenue, net income and assets are still served (round-10 regression)."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    resp = await _replay(SecEdgarAdapter, request, {0: _scale_filing(EARLIER_QUARTER, 0.001, end="2026-03-31")})
    newest = {
        f.concept
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.accession == NEWEST_10Q and f.period_end.isoformat() == "2026-06-30"
    }
    assert {"RevenueFromContractWithCustomerExcludingAssessedTax", "NetIncomeLoss", "Assets"} <= newest
    assert not [
        f
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.accession == EARLIER_QUARTER and f.period_end == date(2026, 3, 31)
    ]


def _scale_concepts(accession: str, factor: float, concepts: set[str]) -> Mutator:
    def edit(data: dict[str, Any]) -> None:
        for concept in concepts:
            for fact in data["facts"]["us-gaap"][concept]["units"]["USD"]:
                if fact["accn"] == accession:
                    fact["val"] *= factor

    return _json_edit(edit)


TAX_COMPONENTS = {
    "CurrentIncomeTaxExpenseBenefit", "DeferredIncomeTaxExpenseBenefit", "CurrentFederalTaxExpenseBenefit",
    "CurrentStateAndLocalTaxExpenseBenefit", "CurrentForeignTaxExpenseBenefit",
    "DeferredFederalIncomeTaxExpenseBenefit", "DeferredStateAndLocalIncomeTaxExpenseBenefit",
    "DeferredForeignIncomeTaxExpenseBenefit",
}  # fmt: skip


@pytest.mark.parametrize(
    ("concepts", "factor"),
    [
        ({"FederalIncomeTaxExpenseBenefitContinuingOperations"}, 1000.0),  # 11x total assets: within the bound
        ({"CurrentFederalTaxExpenseBenefit"}, 0.001),
        (TAX_COMPONENTS, 0.001),  # the whole current/deferred table, every year in the 10-K
        (TAX_COMPONENTS, 1000.0),
    ],
)
async def test_tax_lines_that_break_their_identities_are_held(concepts: set[str], factor: float) -> None:
    """total = current + deferred = federal + state + foreign: a mis-scaled group of lines is found by the identities
    and blamed by its own history; the genuine total is never the one withheld."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    resp = await _replay(SecEdgarAdapter, request, {0: _scale_concepts(NEWEST_10K, factor, concepts)})
    served = [
        f
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.accession == NEWEST_10K and f.concept in concepts
    ]
    assert served == []
    total = [
        f.value
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.concept == "IncomeTaxExpenseBenefit" and f.accession == NEWEST_10K
        and f.period_end.isoformat() == "2025-12-31"
    ]  # fmt: skip
    assert total == [19_087_000_000.0]


async def test_a_flagged_fact_ten_times_total_assets_is_held() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    concept = "CashAndCashEquivalentsAtCarryingValue"
    resp = await _replay(
        SecEdgarAdapter, request, {0: _scale_filing(NEWEST_10Q, 1000.0, concept=concept, end="2026-06-30")}
    )
    assert not [
        f
        for f in resp.records
        if isinstance(f, FundamentalFact)
        and f.concept == concept
        and f.accession == NEWEST_10Q
        and f.period_end.isoformat() == "2026-06-30"
    ]


def test_small_filers_and_cumulative_equity_are_not_bounded_by_assets() -> None:
    def fact(concept: str, value: float) -> FundamentalFact:
        return FundamentalFact(
            cik="0000000001", concept=concept, taxonomy="us-gaap", unit="USD", value=value,
            period_end=date(2025, 12, 31), form="10-K", filed=date(2026, 2, 1), accession="a",
        )  # fmt: skip

    shell = [fact("Assets", 2e4), fact("AdditionalPaidInCapital", 4e7), fact("Liabilities", 6e6)]
    biotech = [fact("Assets", 2e7), fact("RetainedEarningsAccumulatedDeficit", -2.5e9)]
    for records in (shell, biotech):
        logs: list[float | None] = [math.log10(abs(f.value)) for f in records]
        assert sec_scale._beyond_assets(records, logs, set(), set()) == set()


async def test_malformed_kraken_chart_bodies_are_outages(ctx_factory: Any) -> None:
    end = datetime(2026, 10, 21, tzinfo=UTC)
    server = _kraken_range_server(end, timedelta(hours=1), bad_last=False)
    for body in ({}, {"candles": None}, [1, 2, 3]):

        def broken(req: httpx.Request, body: Any = body) -> httpx.Response:
            return httpx.Response(200, json=body) if "/charts/" in req.url.path else server(req)

        ctx, _ = ctx_factory(broken, now=end + timedelta(minutes=5), cache=False)
        adapter = KrakenFuturesAdapter(ctx)
        with pytest.raises(ProviderDown, match="malformed"):
            await adapter.fetch(DataRequest(kind=RequestKind.BARS, params={"symbol": "BTC/USD:USD", "timeframe": "1h"}))
        await adapter.aclose()


async def test_a_few_malformed_ticker_rows_are_left_out_with_a_warning() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.INSTRUMENTS)

    def one_bad(data: dict[str, Any]) -> None:
        data["broken"] = {"ticker": "X"}

    resp = await _replay(SecEdgarAdapter, request, {0: _json_edit(one_bad)})
    assert len(resp.records) > 1000
    assert any("malformed row" in w for w in resp.quality.warnings)


# ------------------------------------------------------------------------------------- review round 11 (P1)


async def test_ticker_rows_without_a_ticker_and_truncated_maps_are_refused() -> None:
    request = _plan(SecEdgarAdapter, RequestKind.INSTRUMENTS)

    def null_ticker(data: dict[str, Any]) -> None:
        data["broken"] = {"cik_str": 1018724, "ticker": None, "title": "X"}

    resp = await _replay(SecEdgarAdapter, request, {0: _json_edit(null_ticker)})
    assert not any(isinstance(i, Instrument) and i.symbol == "NONE" for i in resp.records)

    def truncate(data: dict[str, Any]) -> None:
        for k in list(data)[100:]:
            del data[k]

    with pytest.raises(ProviderDown, match="truncated"):
        await _replay(SecEdgarAdapter, request, {0: _json_edit(truncate)})


@pytest.mark.parametrize("factor", [1000.0, 0.001])
async def test_a_newest_10k_line_off_its_own_comparatives_is_held(factor: float) -> None:
    """The research-credit line of the rate reconciliation has no other filing to compare with, but the same 10-K
    reports 2023 and 2024 beside it (2.196B, 2.644B): a 2025 cell a power of 1000 away is held."""
    concept = "IncomeTaxReconciliationTaxCreditsResearch"
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    resp = await _replay(
        SecEdgarAdapter, request, {0: _scale_filing(NEWEST_10K, factor, concept=concept, end="2025-12-31")}
    )
    shown = [
        f.period_end.year
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.concept == concept and f.accession == NEWEST_10K
    ]
    assert sorted(shown) == [2023, 2024]  # the comparatives stay; the mis-scaled 2025 cell does not


def _filing(facts: list[tuple[str, int, float]], *, accession: str = "a", unit: str = "USD") -> list[FundamentalFact]:
    """Annual facts of one 10-K: (concept, fiscal year, value); "<concept>@<year>" instants are written as year < 0."""
    out = []
    for concept, year, value in facts:
        instant = year < 0
        y = abs(year)
        out.append(
            FundamentalFact(
                cik="0000000001",
                concept=concept,
                taxonomy="us-gaap",
                unit="pure" if concept.startswith("Effective") else unit,
                value=value,
                period_start=None if instant else date(y, 1, 1),
                period_end=date(y, 12, 31),
                form="10-K",
                filed=date(2026, 2, 1),
                accession=accession,
            )
        )
    return out


def test_sga_statutory_tax_and_goodwill_change_relations_find_a_lone_scale_error() -> None:
    years = (2023, 2024, 2025)
    sga = [("SellingAndMarketingExpense", y, v) for y, v in zip(years, (18.26e9, 18.639e9, 19.524e12), strict=True)]
    sga += [("GeneralAndAdministrativeExpense", y, v) for y, v in zip(years, (6.672e9, 7.458e9, 8.077e9), strict=True)]
    sga += [
        ("SellingGeneralAndAdministrativeExpense", y, v)
        for y, v in zip(years, (24.932e9, 26.097e9, 27.601e9), strict=True)
    ]
    records = _filing(sga)
    held, _ = sec_scale.scale_errors(records)
    assert {(records[i].concept, records[i].period_end.year) for i in held} == {("SellingAndMarketingExpense", 2025)}

    pretax = "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
    statutory = "IncomeTaxReconciliationIncomeTaxExpenseBenefitAtFederalStatutoryIncomeTaxRate"
    tax = [(pretax, y, v) for y, v in zip(years, (100e9, 120e9, 165.934e9), strict=True)]
    tax += [("EffectiveIncomeTaxRateReconciliationAtFederalStatutoryIncomeTaxRate", y, 0.21) for y in years]
    tax += [(statutory, y, v) for y, v in zip(years, (21e9, 25.2e9, 34.846e12), strict=True)]
    records = _filing(tax)
    held, _ = sec_scale.scale_errors(records)
    assert {(records[i].concept, records[i].period_end.year) for i in held} == {(statutory, 2025)}

    goodwill = [("Goodwill", -y, v) for y, v in ((2023, 4.0e9), (2024, 4.4e9), (2025, 5.0e9))]
    goodwill += [("GoodwillPeriodIncreaseDecrease", y, v) for y, v in ((2024, 0.4e9), (2025, 0.6e12))]
    # last year's 10-K, as any filer has: the balances and changes before 2025, on scale
    earlier = [("Goodwill", -y, v) for y, v in ((2022, 3.7e9), (2023, 4.0e9), (2024, 4.4e9))]
    earlier += [("GoodwillPeriodIncreaseDecrease", y, v) for y, v in ((2023, 0.3e9), (2024, 0.4e9))]
    records = [*_filing(earlier, accession="earlier"), *_filing(goodwill)]
    held, _ = sec_scale.scale_errors(records)
    assert {(records[i].concept, records[i].period_end.year) for i in held} == {
        ("GoodwillPeriodIncreaseDecrease", 2025)
    }


def test_a_few_disputed_facts_never_hold_a_whole_filing() -> None:
    """Four mis-scaled debt-table cells in a filing that agrees with its peers on everything else (Tesla's FY2016
    10-K): the four are held, the other 40 shared facts are served."""
    concepts = [f"Line{k}" for k in range(11)]
    older = _filing([(c, y, 1e9 * (k + 1)) for k, c in enumerate(concepts) for y in (2014, 2015)], accession="old")
    newer = _filing([(c, y, 1e9 * (k + 1)) for k, c in enumerate(concepts) for y in (2014, 2015)], accession="new")
    for f in newer[:4]:
        object.__setattr__(f, "value", f.value * 1000)
    third = _filing([(c, y, 1e9 * (k + 1)) for k, c in enumerate(concepts) for y in (2013, 2016)], accession="third")
    records = [*older, *newer, *third]
    held, _ = sec_scale.scale_errors(records)
    assert {i for i in held if records[i].accession == "new"} == {len(older) + k for k in range(4)}


async def test_figures_superseded_by_a_held_filing_turn_stale_a_week_after_it() -> None:
    """The newest 10-Q (filed 2026-07-31) is held whole: on 2026-08-09 the April figures are not "fresh" just because
    a filing was not yet due; the next filing arrived and could not be used."""
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    resp = await _replay(
        SecEdgarAdapter, request, {0: _scale_filing(NEWEST_10Q, 1000.0)}, now=datetime(2026, 8, 9, 12, tzinfo=UTC)
    )
    assert str(resp.as_of)[:10] == "2026-04-30"
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.STALE
    assert any("newest filing (filed 2026-07-31) is held" in n for n in resp.notes)


def test_a_proven_rate_reconciliation_pins_a_lone_mis_scaled_line() -> None:
    """total tax = statutory + state + other - credits, closing in 2023 and 2024 of the same 10-K. The 2025 "other"
    line swings too much between years for its comparatives to judge, but x1000 it breaks the reconciliation, which one
    rescale of that line alone repairs; a 4M line below the 1% tolerance does not veto the repair."""
    statutory = "IncomeTaxReconciliationIncomeTaxExpenseBenefitAtFederalStatutoryIncomeTaxRate"
    rows = {
        "IncomeTaxExpenseBenefit": (7_120e6, 9_265e6, 19_087e6),
        statutory: (7_500e6, 10_000e6, 18_000e6),
        "IncomeTaxReconciliationStateAndLocalIncomeTaxes": (400e6, 500e6, 900e6),
        "IncomeTaxReconciliationOtherAdjustments": (-82e6, 1_165e6, 2_587e6 * 1000),
        "IncomeTaxReconciliationTaxCreditsResearch": (698e6, 2_400e6, 2_404e6),
        "IncomeTaxReconciliationTaxCreditsOther": (0.0, 0.0, 4e6),
    }
    records = _filing([(c, y, v) for c, vs in rows.items() for y, v in zip((2023, 2024, 2025), vs, strict=True)])
    # last year's 10-K, as any filer has: 2022-2024 on scale
    earlier = {
        c: (vs[0] * 0.9, vs[0], vs[1]) for c, vs in rows.items() if c != "IncomeTaxReconciliationOtherAdjustments"
    }
    records = [
        *_filing(
            [(c, y, v) for c, vs in earlier.items() for y, v in zip((2022, 2023, 2024), vs, strict=True)],
            accession="earlier",
        ),
        *records,
    ]
    held, _ = sec_scale.scale_errors(records)
    assert {(records[i].concept, records[i].period_end.year, records[i].accession) for i in held} == {
        ("IncomeTaxReconciliationOtherAdjustments", 2025, "a")
    }


@pytest.mark.parametrize("factor", [1000.0, 0.001])
async def test_a_cash_flow_section_total_off_the_change_in_cash_is_held(factor: float) -> None:
    """Financing cash flow swings too much for its history to judge, but cash change = operating + investing +
    financing + FX effect in the same 10-K, and only a rescale of financing closes it."""
    concept = "NetCashProvidedByUsedInFinancingActivities"
    request = _plan(SecEdgarAdapter, RequestKind.COMPANY_FACTS)
    resp = await _replay(
        SecEdgarAdapter, request, {0: _scale_filing(NEWEST_10K, factor, concept=concept, end="2025-12-31")}
    )
    assert not [
        f
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.concept == concept and f.accession == NEWEST_10K
        and f.period_end.isoformat() == "2025-12-31"
    ]  # fmt: skip
    operating = [
        f
        for f in resp.records
        if isinstance(f, FundamentalFact) and f.concept == "NetCashProvidedByUsedInOperatingActivities"
        and f.accession == NEWEST_10K and f.period_end.isoformat() == "2025-12-31"
        and f.period_start is not None and f.period_start.isoformat() == "2025-01-01"
    ]  # fmt: skip
    assert operating  # the genuine sections are still shown


def test_a_rollforward_with_filer_specific_signs_is_learned_from_its_other_years() -> None:
    """Unrecognized tax benefits: closing = opening + increases - decreases - settlements - lapses. The signs are
    learned from the 2023 and 2024 rows of the same 10-K (no fixed sign table), and a 2025 settlement line /1000 is
    held, alone."""
    balance = [
        ("UnrecognizedTaxBenefits", -y, v) for y, v in ((2022, 16.8e9), (2023, 19.5e9), (2024, 22.0e9), (2025, 25.1e9))
    ]
    moves = {
        "UnrecognizedTaxBenefitsIncreasesResultingFromCurrentPeriodTaxPositions": (2.5e9, 2.9e9, 3.3e9),
        "UnrecognizedTaxBenefitsIncreasesResultingFromPriorPeriodTaxPositions": (1.2e9, 0.9e9, 1.4e9),
        "UnrecognizedTaxBenefitsDecreasesResultingFromPriorPeriodTaxPositions": (0.6e9, 0.8e9, 0.5e9),
        "UnrecognizedTaxBenefitsDecreasesResultingFromSettlementsWithTaxingAuthorities": (0.3e9, 0.4e9, 0.9e9 / 1000),
        "UnrecognizedTaxBenefitsReductionsResultingFromLapseOfApplicableStatuteOfLimitations": (0.1e9, 0.1e9, 0.2e9),
    }
    flows = [(c, y, v) for c, vs in moves.items() for y, v in zip((2023, 2024, 2025), vs, strict=True)]
    earlier = [
        ("UnrecognizedTaxBenefits", -y, v) for y, v in ((2021, 15.0e9), (2022, 16.8e9), (2023, 19.5e9), (2024, 22.0e9))
    ]
    earlier += [
        (c, y, v)
        for c, vs in moves.items()
        for y, v in zip((2022, 2023, 2024), (vs[0] * 0.9, vs[0], vs[1]), strict=True)
    ]
    records = [*_filing(earlier, accession="earlier"), *_filing([*balance, *flows])]
    held, _ = sec_scale.scale_errors(records)
    assert {(records[i].concept, records[i].period_end.year, records[i].accession) for i in held} == {
        ("UnrecognizedTaxBenefitsDecreasesResultingFromSettlementsWithTaxingAuthorities", 2025, "a")
    }
