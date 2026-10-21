"""QA checks catch seeded bad data (P1 exit criterion)."""

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from rd_data.errors import DataQualityError
from rd_data.freshness import ExchangeCalendar, WeekdayCalendar
from rd_data.quality import (
    check_bars,
    check_outliers,
    check_positioning,
    check_price_spikes,
    check_series,
    robust_z_scores,
)
from rd_data.schemas import Bar, MacroObservation, PositionCounts, PositioningReport

NYSE = ExchangeCalendar("XNYS")


def _sessions(start: date, n: int) -> list[date]:
    out = [start]
    while len(out) < n:
        nxt = NYSE.closes_after(out[-1], 1)[0].date()
        out.append(nxt)
    return out


def _bars(closes: list[float], start: date = date(2026, 6, 1)) -> list[Bar]:
    days = _sessions(start, len(closes))
    return [
        Bar(
            symbol="T",
            interval="1d",
            ts=datetime(d.year, d.month, d.day, tzinfo=UTC),
            open=c,
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            volume=1000,
        )
        for d, c in zip(days, closes, strict=True)
    ]


def _walk(n: int = 60) -> list[float]:
    return [100 * (1 + 0.004 * math.sin(i) + 0.001 * i) for i in range(n)]


def test_clean_series_passes() -> None:
    report, usable = check_bars(_bars(_walk()), subject="clean", calendar=NYSE, split_dates=[])
    assert report.passed, report.as_dict()
    assert len(usable) == 60
    assert not report.warnings


def test_seeded_duplicate_and_out_of_order_block() -> None:
    bars = _bars(_walk(30))
    seeded = [*bars[:10], bars[9], *bars[10:]]
    report, _ = check_bars(seeded, subject="dup")
    assert not report.passed
    assert any("no_duplicates" in f for f in report.failures)
    swapped = [*bars[:5], bars[6], bars[5], *bars[7:]]
    report, _ = check_bars(swapped, subject="order")
    assert any("monotonic_timestamps" in f for f in report.failures)
    with pytest.raises(DataQualityError, match="order"):
        report.require_passed(provider="test")


def test_seeded_ohlc_violation_and_negative_volume_block() -> None:
    bars = _bars(_walk(20))
    bad = bars[5].model_copy(update={"low": bars[5].close * 1.5})
    neg = bars[6].model_copy(update={"volume": -1.0})
    report, _ = check_bars([*bars[:5], bad, neg, *bars[7:]], subject="ohlc")
    assert any("ohlc_sanity" in f for f in report.failures)
    assert next(c for c in report.checks if c.name == "ohlc_sanity").indices == (5, 6)


def test_seeded_spike_is_quarantined_not_deleted() -> None:
    closes = _walk(40)
    closes[20] = closes[20] * 3  # bad print: up then straight back down
    report, usable = check_bars(_bars(closes), subject="spike")
    assert report.passed  # quarantine is not a blocking failure
    assert report.quarantined == (20,)
    assert len(usable) == 39
    assert any("price_spikes" in w for w in report.warnings)


def test_genuine_jump_is_kept_and_flagged() -> None:
    closes = _walk(40)
    closes = closes[:25] + [c * 0.6 for c in closes[25:]]  # a real 40% level shift
    report, usable = check_bars(_bars(closes), subject="jump")
    assert report.quarantined == ()
    assert len(usable) == 40
    assert any("large_moves" in w for w in report.warnings)


def test_seeded_stale_feed_blocks_while_market_open() -> None:
    closes = _walk(20)[:15] + [111.11] * 5
    report, _ = check_bars(_bars(closes), subject="stale")
    assert any("stale_quotes" in f for f in report.failures)
    report_closed, _ = check_bars(_bars(closes), subject="stale-closed", market_open_at_end=False)
    assert report_closed.passed


def test_seeded_calendar_gaps() -> None:
    bars = _bars(_walk(60))
    few_missing = bars[:30] + bars[31:]  # 1 of 60 missing: warning
    report, _ = check_bars(few_missing, subject="gap", calendar=NYSE)
    assert report.passed
    assert any("calendar_gaps" in w for w in report.warnings)
    many_missing = bars[:10] + bars[20:]  # 10 of 60 missing: blocks
    report, _ = check_bars(many_missing, subject="gaps", calendar=NYSE)
    assert any("calendar_gaps" in f for f in report.failures)


def test_gaps_respect_holidays_and_weekday_calendar() -> None:
    # Thanksgiving 2026-11-26 is not a NYSE session, so its absence is not a gap.
    days = [date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]
    bars = [
        Bar(
            symbol="T",
            interval="1d",
            ts=datetime(d.year, d.month, d.day, tzinfo=UTC),
            open=1,
            high=1.1,
            low=0.9,
            close=1,
        )
        for d in days
    ]
    report, _ = check_bars(bars, subject="holiday", calendar=NYSE, market_open_at_end=False)
    assert next(c for c in report.checks if c.name == "calendar_gaps").passed
    fx_days = [date(2026, 10, 16), date(2026, 10, 19)]  # Friday then Monday
    fx = [
        Bar(
            symbol="EURUSD",
            interval="1d",
            ts=datetime(d.year, d.month, d.day, tzinfo=UTC),
            open=1,
            high=1.1,
            low=0.9,
            close=1 + i / 100,
        )
        for i, d in enumerate(fx_days)
    ]
    report, _ = check_bars(fx, subject="fx", calendar=WeekdayCalendar())
    assert next(c for c in report.checks if c.name == "calendar_gaps").passed


def test_cross_source_reconciliation() -> None:
    bars = _bars(_walk(20))
    ref = [b.model_copy(update={"close": b.close * 1.001}) for b in bars]
    report, _ = check_bars(bars, subject="xs", reference=ref)
    assert next(c for c in report.checks if c.name == "cross_source").passed
    off = [b.model_copy(update={"close": b.close * (1.05 if i in (3, 4) else 1.0)}) for i, b in enumerate(bars)]
    report, _ = check_bars(bars, subject="xs-off", reference=off)
    assert report.passed
    assert any("cross_source" in w for w in report.warnings)
    all_off = [b.model_copy(update={"close": b.close * 1.2}) for b in bars]
    report, _ = check_bars(bars, subject="xs-bad", reference=all_off)
    assert any("cross_source" in f for f in report.failures)
    report, _ = check_bars(bars, subject="xs-none", reference=[])
    assert any("no overlapping" in w for w in report.warnings)


def test_seeded_unadjusted_split_blocks_unless_recorded() -> None:
    closes = _walk(30)
    closes = closes[:15] + [c / 4 for c in closes[15:]]  # 4:1 split not adjusted
    bars = _bars(closes)
    report, _ = check_bars(bars, subject="split", split_dates=[])
    assert any("corporate_actions" in f for f in report.failures)
    report, _ = check_bars(bars, subject="split-known", split_dates=[bars[15].ts.date()])
    assert next(c for c in report.checks if c.name == "corporate_actions").passed


def test_schema_rejections_block() -> None:
    report, _ = check_bars(_bars(_walk(5)), subject="schema", rejected_rows=2)
    assert any("schema" in f for f in report.failures)


def test_series_checks() -> None:
    t = [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=7 * i) for i in range(5)]
    obs = [MacroObservation(series_id="S", ts=ts, value=float(i), unit="u") for i, ts in enumerate(t)]
    assert check_series(obs, subject="ok").passed
    with_missing = [*obs, MacroObservation(series_id="S", ts=t[-1] + timedelta(days=7), value=None, unit="u")]
    rep = check_series(with_missing, subject="missing")
    assert rep.passed
    assert "1 observation" in next(c for c in rep.checks if c.name == "missing_values").detail
    bad = [*obs, MacroObservation(series_id="S", ts=t[-1] + timedelta(days=7), value=float("inf"), unit="u")]
    assert not check_series(bad, subject="inf").passed
    assert not check_series([obs[1], obs[0]], subject="order").passed


def test_positioning_balance_check() -> None:
    good = PositioningReport(
        market_code="1",
        market_name="M",
        report_type="legacy",
        futures_only=True,
        as_of=date(2026, 10, 13),
        open_interest=1000,
        categories={
            "noncommercial": PositionCounts(long=300, short=200, spreading=100),
            "commercial": PositionCounts(long=500, short=700),
            "nonreportable": PositionCounts(long=100, short=0),
        },
    )
    assert check_positioning([good], subject="cot").passed
    bad = good.model_copy(update={"open_interest": 2000})
    rep = check_positioning([good, bad, good], subject="cot-bad")
    assert not rep.passed
    assert any("long_side" in f for f in rep.failures)
    assert any("no_duplicates" in f for f in rep.failures)


def test_robust_z_and_outlier_helpers() -> None:
    assert robust_z_scores([]) == []
    assert robust_z_scores([1.0, 1.0, 1.0]) == [0.0, 0.0, 0.0]
    assert robust_z_scores([1.0, 1.0, 5.0])[2] == math.inf
    values = [1.0 + i * 0.01 for i in range(20)] + [100.0]
    res = check_outliers(values)
    assert res.indices == (20,)
    assert check_outliers([1.0] * 5).passed
    skipped, _ = check_price_spikes([1.0, 2.0])
    assert "not evaluated" in skipped.detail
