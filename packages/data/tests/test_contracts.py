"""Contract tests: every adapter against recorded provider responses (PROMPT §15.2, P1 exit criterion).

Cassettes under ``cassettes/`` are either *recorded* from the live provider by the ``record-cassettes`` workflow or
*schema-derived* (keyed vendors without a key in CI, D-011). Provenance is checked honestly: the schema-derived set
is pinned below and must shrink to empty before release (P11 gate).
"""

from __future__ import annotations

import itertools
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rd_core.clock import FixedClock
from rd_core.settings import Settings
from rd_data.adapters.base import AdapterContext, BaseAdapter, DataRequest, DataResponse
from rd_data.cassette import Cassette, ReplayTransport
from rd_data.recording import cassette_name
from rd_data.registry import ADAPTERS
from rd_data.resilience import CircuitBreaker, LocalRateLimiter, MemoryStateStore
from rd_data.schemas import (
    Bar,
    CryptoDerivativeMetric,
    Filing,
    FundamentalFact,
    Instrument,
    MacroObservation,
    PositioningReport,
    TreasuryYieldPoint,
)

CASSETTES = Path(__file__).parent / "cassettes"
#: Keyed vendors whose cassettes are schema-derived until a key exists in CI. Shrinks to empty before release.
SCHEMA_DERIVED_ALLOWED = {"eia", "fred", "twelvedata"}

PLAN: list[tuple[type[BaseAdapter], DataRequest]] = [
    (cls, req) for cls in ADAPTERS for req in getattr(cls, "recording_plan", list)()
]


def _id(item: tuple[type[BaseAdapter], DataRequest]) -> str:
    return f"{item[0].name}:{cassette_name(item[1])}"


async def _replay(cls: type[BaseAdapter], request: DataRequest) -> tuple[DataResponse, Cassette]:
    path = CASSETTES / cls.name / cassette_name(request)
    assert path.exists(), f"missing cassette {path}; run the record-cassettes workflow"
    cassette = Cassette.load(path)
    recorded = datetime.fromisoformat(cassette.recorded_at.replace("Z", "+00:00"))
    ctx = AdapterContext(
        settings=Settings(
            sec_edgar_user_agent="Research Desk test ops@example.com", eia_api_key="k" * 20, fred_api_key="k" * 20
        ),
        limiter=LocalRateLimiter(),
        breaker=CircuitBreaker(MemoryStateStore()),
        cache=None,
        clock=FixedClock(recorded),
        transport=ReplayTransport([cassette]),
        flags={cls.feature_flag: True} if cls.feature_flag else {},
    )
    adapter = cls(ctx)
    try:
        return await adapter.fetch(request), cassette
    finally:
        await adapter.aclose()


@pytest.fixture(autouse=True)
def _vendor_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWELVEDATA_API_KEY", "k" * 20)


@pytest.mark.parametrize("item", PLAN, ids=_id)
async def test_adapter_contract(item: tuple[type[BaseAdapter], DataRequest]) -> None:
    cls, request = item
    response, cassette = await _replay(cls, request)
    assert response.records, "a contract cassette must produce records"
    assert response.quality.passed, response.quality.as_dict()
    if cassette.provenance == "schema-derived":
        assert cls.name in SCHEMA_DERIVED_ALLOWED, f"{cls.name} must be recorded, not schema-derived"
    else:
        assert cassette.provenance == "recorded"
        assert "GitHub Actions run" in cassette.source
    assert all(i.status == 200 for i in cassette.interactions)
    for interaction in cassette.interactions:
        for key, value in interaction.params.items():
            if key.lower() in ("api_key", "apikey", "token", "key"):
                assert value == "REDACTED"


def test_schema_derived_set_is_exactly_the_allowed_one() -> None:
    derived = {p.parent.name for p in CASSETTES.glob("*/*.json") if Cassette.load(p).provenance == "schema-derived"}
    assert derived <= SCHEMA_DERIVED_ALLOWED
    recorded = {p.parent.name for p in CASSETTES.glob("*/*.json") if Cassette.load(p).provenance == "recorded"}
    assert {"cftc", "sec_edgar", "us_treasury", "kraken_futures"} <= recorded


# --------------------------------------------------------------------------- domain invariants on real data


def _records(cls_name: str, kind: str) -> list[tuple[DataRequest, type[BaseAdapter]]]:
    return [(r, c) for c, r in PLAN if c.name == cls_name and r.kind.value == kind]


async def test_cftc_real_rows() -> None:
    for request, cls in _records("cftc", "cot"):
        response, _ = await _replay(cls, request)
        reports = [r for r in response.records if isinstance(r, PositioningReport)]
        assert len(reports) == 60
        assert all(r.market_code == request.params["market_code"] for r in reports)
        tuesdays = sum(1 for r in reports if r.as_of.weekday() == 1)
        assert tuesdays >= 55  # positions are as of Tuesday except in holiday weeks
        gaps = [(b.as_of - a.as_of).days for a, b in itertools.pairwise(reports)]
        assert all(5 <= g <= 9 for g in gaps), gaps  # weekly cadence, no missing weeks
        assert response.freshness is not None


async def test_sec_real_filings_and_facts_are_point_in_time() -> None:
    ((req_f, cls),) = _records("sec_edgar", "filings")
    filings, _ = await _replay(cls, req_f)
    f = [r for r in filings.records if isinstance(r, Filing)]
    assert any(x.form == "10-K" for x in f)
    assert all(x.url.startswith("https://www.sec.gov/Archives/edgar/data/320193/") for x in f)
    ((req_c, cls),) = _records("sec_edgar", "company_facts")
    facts, _ = await _replay(cls, req_c)
    ff = [r for r in facts.records if isinstance(r, FundamentalFact)]
    assert ff
    assert all(x.filed >= x.period_end for x in ff), "a fact cannot be filed before its period ends"
    assert {x.form for x in ff} <= {"10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F"}
    ((req_t, cls),) = _records("sec_edgar", "instruments")
    tickers, _ = await _replay(cls, req_t)
    syms = {r.symbol: r for r in tickers.records if isinstance(r, Instrument)}
    assert syms["AAPL"].provider_symbol == "0000320193"
    assert syms["AMZN"].provider_symbol == "0001018724"


async def test_treasury_real_curve() -> None:
    ((request, cls),) = _records("us_treasury", "treasury_curve")
    response, _ = await _replay(cls, request)
    pts = [r for r in response.records if isinstance(r, TreasuryYieldPoint)]
    assert all(-1.0 < p.yield_pct < 25.0 for p in pts)
    latest = max(p.ts for p in pts)
    tenors = {p.tenor for p in pts if p.ts == latest}
    assert {"3M", "2Y", "10Y", "30Y"} <= tenors


async def test_kraken_real_bars_and_derivatives() -> None:
    ((req_b, cls),) = _records("kraken_futures", "bars")
    bars, _ = await _replay(cls, req_b)
    b = [r for r in bars.records if isinstance(r, Bar)]
    assert len(b) == 60
    assert all(x.ts.hour == 0 and x.ts.minute == 0 for x in b)  # daily bars at 00:00 UTC
    assert all((y.ts - x.ts) == timedelta(days=1) for x, y in itertools.pairwise(b))  # weekends present
    assert bars.freshness is None  # historical window
    ((req_d, cls),) = _records("kraken_futures", "crypto_derivatives")
    deriv, _ = await _replay(cls, req_d)
    m = [r for r in deriv.records if isinstance(r, CryptoDerivativeMetric)]
    by: dict[tuple[str, str], CryptoDerivativeMetric] = {(x.symbol, x.metric): x for x in m}
    for sym in ("BTC/USD:USD", "ETH/USD:USD"):
        fr = by[(sym, "funding_rate")]
        assert abs(fr.value) <= 0.25
        assert fr.funding_interval_hours == 1.0
        assert by[(sym, "mark_price")].value > 0
        assert by[(sym, "open_interest")].value >= 0
        mark, index = by[(sym, "mark_price")].value, by[(sym, "index_price")].value
        assert math.isclose(mark, index, rel_tol=0.02)  # perp mark tracks the index


async def test_schema_derived_macro_series_parse() -> None:
    for name in ("eia", "fred"):
        for request, cls in _records(name, "macro_series"):
            response, cassette = await _replay(cls, request)
            assert cassette.provenance == "schema-derived"
            obs = [r for r in response.records if isinstance(r, MacroObservation)]
            assert obs == sorted(obs, key=lambda o: o.ts)
