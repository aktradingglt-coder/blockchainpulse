"""Adapter parsing and pipeline behaviour on small synthetic payloads (unit fixtures, not provider recordings)."""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from rd_core.settings import DeploymentMode
from rd_data.adapters.base import DataRequest, RequestKind
from rd_data.adapters.cftc import CftcAdapter
from rd_data.adapters.eia import EiaAdapter
from rd_data.adapters.fred import FredAdapter
from rd_data.adapters.sec_edgar import SecEdgarAdapter, cik10
from rd_data.adapters.treasury import TreasuryAdapter
from rd_data.adapters.twelvedata import TwelveDataAdapter
from rd_data.errors import (
    DataQualityError,
    FeatureDisabled,
    InvalidArgs,
    NotFound,
    ProviderDown,
    RateLimited,
    Stale,
)
from rd_data.freshness import Freshness
from rd_data.licence import LicenceTag
from rd_data.schemas import Filing, FundamentalFact, MacroObservation, PositioningReport, TreasuryYieldPoint

UA = {"sec_edgar_user_agent": "Research Desk test ops@example.com"}


def of_type[T](records: Sequence[object], cls: type[T]) -> list[T]:
    """Narrow a response's records to one canonical type (and prove they all are)."""
    out = [r for r in records if isinstance(r, cls)]
    assert len(out) == len(records)
    return out


def _json(body: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(status, json=body)


def _tff_row(day: str, oi: float = 1000.0) -> dict[str, str]:
    return {
        "report_date_as_yyyy_mm_dd": f"{day}T00:00:00.000",
        "cftc_contract_market_code": "099741",
        "market_and_exchange_names": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
        "open_interest_all": str(oi),
        "dealer_positions_long_all": "100",
        "dealer_positions_short_all": "200",
        "dealer_positions_spread_all": "50",
        "asset_mgr_positions_long": "400",
        "asset_mgr_positions_short": "100",
        "asset_mgr_positions_spread": "50",
        "lev_money_positions_long": "200",
        "lev_money_positions_short": "300",
        "lev_money_positions_spread": "50",
        "other_rept_positions_long": "50",
        "other_rept_positions_short": "50",
        "other_rept_positions_spread": "0",
        "nonrept_positions_long_all": "100",
        "nonrept_positions_short_all": "200",
    }


TFF = DataRequest(kind=RequestKind.COT, params={"report_type": "tff", "market_code": "099741", "weeks": 2})


async def test_cftc_parse_cache_freshness_and_licence(ctx_factory: Any) -> None:
    ctx, rec = ctx_factory(_json([_tff_row("2026-10-13"), _tff_row("2026-10-06")]))
    adapter = CftcAdapter(ctx)
    resp = await adapter.fetch(TFF)
    reports = of_type(resp.records, PositioningReport)
    assert [r.as_of for r in reports] == [date(2026, 10, 6), date(2026, 10, 13)]  # sorted ascending
    rec0 = reports[1]
    assert rec0.categories["leveraged_funds"].net == -100
    assert rec0.exchange == "CHICAGO MERCANTILE EXCHANGE"
    assert resp.quality.passed
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.FRESH  # Wednesday after the Friday release of the 13 Oct report
    assert resp.freshness.as_of == datetime(2026, 10, 16, 20, 30, tzinfo=UTC)
    assert resp.licence_tag is LicenceTag.DISPLAY_OK
    assert resp.content_hash.startswith("sha256:")
    q = dict(httpx.QueryParams(rec.requests[0].url.query))
    assert q["$where"] == "cftc_contract_market_code='099741'"
    again = await adapter.fetch(TFF)
    assert again.from_cache
    assert len(rec.requests) == 1
    await adapter.aclose()


async def test_cftc_blocking_conditions(ctx_factory: Any) -> None:
    bad = _tff_row("2026-10-13", oi=5000)  # long side no longer sums to open interest
    ctx, _ = ctx_factory(_json([bad]))
    with pytest.raises(DataQualityError, match="long_side"):
        await CftcAdapter(ctx).fetch(TFF)
    broken = {k: v for k, v in _tff_row("2026-10-13").items() if k != "dealer_positions_long_all"}
    ctx, _ = ctx_factory(_json([broken, _tff_row("2026-10-06")]))
    with pytest.raises(DataQualityError, match="schema"):
        await CftcAdapter(ctx).fetch(TFF)
    ctx, _ = ctx_factory(_json([]))
    with pytest.raises(NotFound):
        await CftcAdapter(ctx).fetch(TFF)
    ctx, _ = ctx_factory(_json([_tff_row("2026-09-01")]))  # released 2026-09-04: blocked 15 days after release
    with pytest.raises(Stale):
        await CftcAdapter(ctx).fetch(TFF)


@pytest.mark.parametrize(
    "params",
    [
        {"report_type": "weekly", "market_code": "1"},
        {"report_type": "tff", "market_code": "x' OR 1=1"},
        {"report_type": "tff", "market_code": "099741", "weeks": 0},
        {"market_code": "099741"},
    ],
)
async def test_cftc_rejects_bad_params_before_any_request(ctx_factory: Any, params: dict[str, Any]) -> None:
    ctx, rec = ctx_factory(_json([]))
    with pytest.raises(InvalidArgs):
        await CftcAdapter(ctx).fetch(DataRequest(kind=RequestKind.COT, params=params))
    assert rec.requests == []
    with pytest.raises(InvalidArgs, match="does not serve"):
        await CftcAdapter(ctx).fetch(DataRequest(kind=RequestKind.BARS, params={}))


def _facts() -> dict[str, Any]:
    return {
        "cik": 1018724,
        "entityName": "X",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2026-01-01",
                                "end": "2026-03-31",
                                "val": 100,
                                "accn": "a1",
                                "fy": 2026,
                                "fp": "Q1",
                                "form": "10-Q",
                                "filed": "2026-05-01",
                            },
                            {
                                "start": "2026-04-01",
                                "end": "2026-06-30",
                                "val": 120,
                                "accn": "a2",
                                "fy": 2026,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": "2026-08-01",
                            },
                            {"end": "2026-06-30", "val": 1, "accn": "a3", "form": "8-K", "filed": "2026-08-02"},
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            {"end": "2026-06-30", "val": "bad", "accn": "a2", "form": "10-Q", "filed": "2026-08-01"}
                        ]
                    }
                },
            }
        },
    }


async def test_sec_requires_fair_access_user_agent(ctx_factory: Any) -> None:
    ctx, rec = ctx_factory(_json({}))
    with pytest.raises(ProviderDown, match="SEC_EDGAR_USER_AGENT"):
        await SecEdgarAdapter(ctx).fetch(DataRequest(kind=RequestKind.INSTRUMENTS))
    assert rec.requests == []


async def test_sec_facts_are_point_in_time_and_validated(ctx_factory: Any) -> None:
    ctx, rec = ctx_factory(_json(_facts()), **UA)
    with pytest.raises(DataQualityError, match="schema"):  # the malformed Assets value blocks use
        await SecEdgarAdapter(ctx).fetch(DataRequest(kind=RequestKind.COMPANY_FACTS, params={"cik": "1018724"}))
    assert rec.requests[0].headers["user-agent"] == UA["sec_edgar_user_agent"]
    ctx, _ = ctx_factory(_json(_facts()), **UA)
    resp = await SecEdgarAdapter(ctx).fetch(
        DataRequest(kind=RequestKind.COMPANY_FACTS, params={"cik": 1018724, "concepts": "Revenues"})
    )
    facts = [r for r in resp.records if isinstance(r, FundamentalFact)]
    assert [f.filed for f in facts] == [date(2026, 5, 1), date(2026, 8, 1)]  # 8-K excluded, ordered by filing
    assert resp.as_of == date(2026, 8, 1)
    assert "Point-in-time" in resp.notes[0]


async def test_sec_filings_and_tickers(ctx_factory: Any) -> None:
    recent = {  # EDGAR lists the newest filing first
        "accessionNumber": ["0001-26-000002", "0001-26-000001"],
        "filingDate": ["2026-10-15", "2026-10-01"],
        "reportDate": ["", "2026-09-30"],
        "acceptanceDateTime": ["", "2026-10-01T20:01:02.000Z"],
        "form": ["8-K", "10-Q"],
        "primaryDocument": ["", "q.htm"],
    }
    ctx, rec = ctx_factory(_json({"cik": "320193", "name": "Apple Inc.", "filings": {"recent": recent}}), **UA)
    resp = await SecEdgarAdapter(ctx).fetch(DataRequest(kind=RequestKind.FILINGS, params={"cik": "320193"}))
    filings = of_type(resp.records, Filing)
    assert [f.form for f in filings] == ["10-Q", "8-K"]
    assert filings[0].url == "https://www.sec.gov/Archives/edgar/data/320193/000126000001/q.htm"
    assert str(rec.requests[0].url) == "https://data.sec.gov/submissions/CIK0000320193.json"
    ctx, _ = ctx_factory(_json({"cik": "320193", "name": "A", "filings": {"recent": recent}}), **UA)
    with pytest.raises(NotFound, match="10-K"):  # no 10-K among the recent filings: reported, never invented
        await SecEdgarAdapter(ctx).fetch(
            DataRequest(kind=RequestKind.FILINGS, params={"cik": "320193", "forms": "10-K"})
        )
    tickers = {
        "0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."},
        "1": {"cik_str": "x", "ticker": "B", "title": "Bad"},
    }
    ctx, _ = ctx_factory(_json(tickers), **UA)
    with pytest.raises(DataQualityError):
        await SecEdgarAdapter(ctx).fetch(DataRequest(kind=RequestKind.INSTRUMENTS))
    assert cik10(320193) == "0000320193"
    with pytest.raises(InvalidArgs):
        cik10("12a")


async def test_sec_empty_filings_not_found(ctx_factory: Any) -> None:
    ctx, _ = ctx_factory(_json({"cik": "1", "name": "A", "filings": {"recent": {"accessionNumber": []}}}), **UA)
    with pytest.raises(NotFound):
        await SecEdgarAdapter(ctx).fetch(DataRequest(kind=RequestKind.FILINGS, params={"cik": "1"}))
    with pytest.raises(InvalidArgs):
        await SecEdgarAdapter(ctx).fetch(DataRequest(kind=RequestKind.FILINGS, params={"cik": 1.5}))


CSV = (
    "﻿Date,1 Mo,1.5 Month,2 Mo,3 Mo,4 Mo,6 Mo,1 Yr,2 Yr,3 Yr,5 Yr,7 Yr,10 Yr,20 Yr,30 Yr\n"
    "10/20/2026,4.10,4.08,4.05,4.00,,3.90,3.70,3.50,3.45,3.50,3.65,3.85,4.30,4.40\n"
    "10/19/2026,4.11,4.09,4.06,4.01,3.98,3.91,3.71,3.51,3.46,3.51,3.66,3.86,4.31,4.41\n"
    "not a date,1,1,1,1,1,1,1,1,1,1,1,1,1,1\n"
)


async def test_treasury_curve(ctx_factory: Any) -> None:
    ctx, rec = ctx_factory(
        lambda _r: httpx.Response(200, text=CSV.replace("not a date,1,1,1,1,1,1,1,1,1,1,1,1,1,1\n", ""))
    )
    resp = await TreasuryAdapter(ctx).fetch(DataRequest(kind=RequestKind.TREASURY_CURVE, params={"year": 2026}))
    assert len(resp.records) == 27  # 4M missing on 2026-10-20
    tenyr = [p for p in of_type(resp.records, TreasuryYieldPoint) if p.tenor == "10Y"]
    assert [p.yield_pct for p in tenyr] == [3.86, 3.85]
    assert resp.as_of == datetime(2026, 10, 20, tzinfo=UTC)
    assert "field_tdr_date_value=2026" in str(rec.requests[0].url)
    ctx, _ = ctx_factory(lambda _r: httpx.Response(200, text=CSV))
    with pytest.raises(DataQualityError):
        await TreasuryAdapter(ctx).fetch(DataRequest(kind=RequestKind.TREASURY_CURVE, params={"year": 2026}))
    with pytest.raises(InvalidArgs):
        await TreasuryAdapter(ctx).fetch(DataRequest(kind=RequestKind.TREASURY_CURVE, params={"year": "2026"}))


def _eia_body() -> dict[str, Any]:
    rows = [
        {
            "period": f"2026-10-{d:02d}",
            "series": "WCESTUS1",
            "value": str(420 + d),
            "units": "MBBL",
            "series-description": "U.S. Ending Stocks excluding SPR of Crude Oil",
        }
        for d in (2, 9, 16)
    ]
    return {"response": {"frequency": "weekly", "data": list(reversed(rows))}}


async def test_eia_requires_key_then_parses(ctx_factory: Any) -> None:
    req = DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "WCESTUS1", "length": 3})
    ctx, rec = ctx_factory(_json(_eia_body()))
    with pytest.raises(ProviderDown, match="EIA_API_KEY"):
        await EiaAdapter(ctx).fetch(req)
    key = "eia-test-" + "k" * 20
    ctx, rec = ctx_factory(_json(_eia_body()), eia_api_key=key)
    resp = await EiaAdapter(ctx).fetch(req)
    assert [o.value for o in of_type(resp.records, MacroObservation)] == [422.0, 429.0, 436.0]
    assert resp.freshness is not None
    assert resp.freshness.status is Freshness.FRESH
    assert key not in " ".join(resp.source_urls)
    assert dict(httpx.QueryParams(rec.requests[0].url.query))["facets[series][]"] == "WCESTUS1"
    ctx, _ = ctx_factory(_json({"error": "invalid series"}), eia_api_key=key)
    with pytest.raises(InvalidArgs, match="invalid series"):
        await EiaAdapter(ctx).fetch(req)
    ctx, _ = ctx_factory(_json({"response": {"data": []}}), eia_api_key=key)
    with pytest.raises(NotFound):
        await EiaAdapter(ctx).fetch(req)
    with pytest.raises(InvalidArgs, match="route"):
        await EiaAdapter(ctx).fetch(DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "UNKNOWN1"}))


def _fred_handler(notes: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/observations"):
            return httpx.Response(
                200,
                json={"observations": [{"date": "2026-10-19", "value": "3.86"}, {"date": "2026-10-20", "value": "."}]},
            )
        return httpx.Response(
            200, json={"seriess": [{"id": "DGS10", "frequency_short": "D", "units_short": "%", "notes": notes}]}
        )

    return handler


async def test_fred_is_off_by_default_and_respects_copyright(ctx_factory: Any) -> None:
    req = DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"})
    ctx, rec = ctx_factory(_fred_handler("Public domain."))
    with pytest.raises(FeatureDisabled):
        await FredAdapter(ctx).fetch(req)
    assert rec.requests == []
    on = {"provider_fred": True}
    ctx, _ = ctx_factory(_fred_handler("Public domain."), flags=on)
    with pytest.raises(ProviderDown, match="FRED_API_KEY"):
        await FredAdapter(ctx).fetch(req)
    ctx, _ = ctx_factory(
        _fred_handler("Public domain."), flags=on, fred_api_key="f" * 32, mode=DeploymentMode.SELF_HOSTED
    )
    resp = await FredAdapter(ctx).fetch(req)
    assert [o.value for o in of_type(resp.records, MacroObservation)] == [3.86, None]  # "." is missing, not zero
    assert resp.licence_tag is LicenceTag.DISPLAY_OK  # self-hosted, buyer's key
    ctx, _ = ctx_factory(
        _fred_handler("Copyright, 2026, Some Vendor."), flags=on, fred_api_key="f" * 32, mode=DeploymentMode.SELF_HOSTED
    )
    resp = await FredAdapter(ctx).fetch(req)
    assert resp.licence_tag is LicenceTag.INTERNAL_ONLY
    assert any("copyright" in n.lower() for n in resp.notes)


async def test_twelvedata_flag_key_and_errors(ctx_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    req = DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "interval": "1d", "outputsize": 3})
    ctx, _ = ctx_factory(_json({}))
    with pytest.raises(FeatureDisabled):
        await TwelveDataAdapter(ctx).fetch(req)
    on = {"provider_twelvedata": True}
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)
    ctx, _ = ctx_factory(_json({}), flags=on)
    with pytest.raises(ProviderDown, match="TWELVEDATA_API_KEY"):
        await TwelveDataAdapter(ctx).fetch(req)
    monkeypatch.setenv("TWELVEDATA_API_KEY", "td-" + "x" * 30)
    values = [
        {"datetime": f"2026-10-{d}", "open": "1.10", "high": "1.11", "low": "1.09", "close": f"1.10{d}"}
        for d in (16, 19, 20)
    ]
    ctx, _ = ctx_factory(_json({"meta": {"symbol": "EUR/USD"}, "values": values, "status": "ok"}), flags=on)
    resp = await TwelveDataAdapter(ctx).fetch(req)
    assert len(resp.records) == 3
    assert resp.licence_tag is LicenceTag.INTERNAL_ONLY  # hosted, no contract
    assert "apikey=REDACTED" in resp.source_urls[0] or "[REDACTED]" in resp.source_urls[0]
    for body, err in [
        ({"status": "error", "code": 429, "message": "limit"}, RateLimited),
        ({"status": "error", "code": 404, "message": "symbol not found"}, NotFound),
        ({"status": "error", "code": 400, "message": "bad"}, InvalidArgs),
    ]:
        ctx, _ = ctx_factory(_json(body), flags=on)
        with pytest.raises(err):
            await TwelveDataAdapter(ctx).fetch(req)
    bad = DataRequest(kind=RequestKind.BARS, params={"symbol": "EUR/USD", "interval": "2d"})
    with pytest.raises(InvalidArgs):
        await TwelveDataAdapter(ctx).fetch(bad)
    _ = json


async def test_health_reports_flag_and_circuit(ctx_factory: Any) -> None:
    ctx, _ = ctx_factory(_json([]))
    health = await FredAdapter(ctx).health()
    assert not health.enabled
    assert not health.licence_verified
    assert (await CftcAdapter(ctx).health()).circuit.state.value == "closed"
