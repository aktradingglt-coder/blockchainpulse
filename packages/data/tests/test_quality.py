"""QA checks catch seeded bad data (P1 exit criterion)."""

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from rd_data.errors import DataQualityError
from rd_data.freshness import ExchangeCalendar, WeekdayCalendar
from rd_data.quality import (
    check_bars,
    check_cadence,
    check_positioning,
    check_price_spikes,
    check_provider_order,
    check_range,
    check_series,
    robust_z_scores,
    scan_spikes,
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


def _obs(values: Sequence[float | None]) -> list[MacroObservation]:
    t0 = datetime(2025, 1, 3, tzinfo=UTC)
    return [
        MacroObservation(series_id="S", ts=t0 + timedelta(days=7 * i), value=v, unit="u") for i, v in enumerate(values)
    ]


def _level(n: int = 40) -> list[float]:
    return [400_000.0 + 3_000 * math.sin(i / 3) for i in range(n)]


def test_series_checks() -> None:
    obs = _obs([float(i) for i in range(5)])
    report, usable = check_series(obs, subject="ok")
    assert report.passed
    assert usable == obs
    with_missing = _obs([*range(5), None])
    rep, _ = check_series(with_missing, subject="missing")
    assert rep.passed
    assert "1 observation" in next(c for c in rep.checks if c.name == "missing_values").detail
    with pytest.raises(ValidationError, match="finite"):  # canonical schemas refuse NaN/inf outright
        _obs([*range(5), float("inf")])
    smuggled = [*obs, MacroObservation.model_construct(series_id="S", ts=obs[-1].ts, value=math.nan, unit="u")]
    assert not check_series(smuggled, subject="nan")[0].passed  # and QA still blocks one built around them
    assert not check_series([obs[1], obs[0]], subject="order")[0].passed


@pytest.mark.parametrize("bad", [4.2e12, -5.0])
def test_seeded_impossible_inventory_value_is_quarantined(bad: float) -> None:
    """Crude stocks of 4.2 trillion or -5 thousand barrels never reach a report."""
    values = _level()
    values[-1] = bad
    report, usable = check_series(_obs(values), subject="eia", lo=0.0, hi=2_000_000.0)
    assert report.quarantined == (39,)
    assert all(o.value != bad for o in usable)
    assert any("value_range" in w for w in report.warnings)


def test_seeded_reverting_macro_outlier_is_quarantined() -> None:
    values = _level()
    values[20] = values[20] * 100  # unit error in one week, then back to normal
    report, usable = check_series(_obs(values), subject="eia")
    assert report.quarantined == (20,)
    assert len(usable) == 39


def test_extreme_latest_macro_release_is_kept_and_flagged() -> None:
    """A record jump in an official release (e.g. unemployment in April 2020) is shown, with a warning."""
    values = [3.5 + 0.05 * math.sin(i) for i in range(40)]
    values[-1] = 14.7
    report, usable = check_series(_obs(values), subject="unemployment", lo=0.0, hi=100.0)
    assert report.quarantined == ()
    assert usable[-1].value == 14.7
    assert any("large_moves" in w for w in report.warnings)


def test_value_range_helper() -> None:
    res = check_range([1.0, None, -1.0, 50.0], lo=0.0, hi=10.0, what="yield")
    assert res.indices == (2, 3)
    assert not res.blocking
    assert check_range([1.0], lo=None, hi=None, what="x").passed


def test_provider_order_and_cadence() -> None:
    days = [date(2026, 10, 13) - timedelta(days=7 * i) for i in range(30)]
    assert check_provider_order(days, descending=True).passed
    reversed_rows = check_provider_order(list(reversed(days)), descending=True)
    assert not reversed_rows.passed
    assert reversed_rows.blocking
    assert check_provider_order(list(reversed(days)), descending=False).passed
    assert check_cadence(days, period=timedelta(days=7)).passed
    one_missing = check_cadence([d for d in days if d != days[10]], period=timedelta(days=7))
    assert not one_missing.passed
    assert not one_missing.blocking  # 1 of 30 weeks: below the 5% blocking threshold
    assert days[10].isoformat() in one_missing.detail
    many_missing = check_cadence(days[::3], period=timedelta(days=7))
    assert many_missing.blocking
    holiday_shift = [*days[:5], days[5] + timedelta(days=1), *days[6:]]  # e.g. a Wednesday position date
    assert check_cadence(holiday_shift, period=timedelta(days=7)).passed


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


def test_robust_z_helpers() -> None:
    assert robust_z_scores([]) == []
    assert robust_z_scores([1.0, 1.0, 1.0]) == [0.0, 0.0, 0.0]
    # MAD is 0 when most values are equal: the mean absolute deviation takes over as the scale.
    assert robust_z_scores([1.0, 1.0, 1.0, 1.0, 5.0])[4] == pytest.approx(4 / (1.253314 * 0.8))
    skipped, _ = check_price_spikes([1.0, 2.0])
    assert "not evaluated" in skipped.detail
    assert not scan_spikes([1.0] * 5).evaluated


@pytest.mark.parametrize(
    ("seed", "quarantined"),
    [
        ({39: 50.0}, (39,)),  # bad print on the latest bar, the one reports use
        ({38: 50.0, 39: 50.0}, (38, 39)),  # two bad latest bars
        ({0: 50.0}, (0,)),  # bad first bar
        ({20: 50.0, 21: 50.0}, (20, 21)),  # two consecutive bad prints mid-series
        ({20: 0.01}, (20,)),  # a print 100x too low
    ],
)
def test_seeded_bad_prints_anywhere_are_quarantined(seed: dict[int, float], quarantined: tuple[int, ...]) -> None:
    closes = _walk(40)
    for i, factor in seed.items():
        closes[i] *= factor
    bars = [
        b.model_copy(update={"open": b.close, "high": b.close * 1.01, "low": b.close * 0.99}) for b in _bars(closes)
    ]
    report, usable = check_bars(bars, subject="bad-print")
    assert report.quarantined == quarantined
    assert all(b.close < 1_000 for b in usable)
    assert report.passed  # quarantined, reported, not deleted


def test_latest_level_shift_is_held_until_confirmed() -> None:
    """An extreme move on the last bars cannot be told from a bad print until more bars confirm it."""
    closes = _walk(40)
    shifted = closes[:38] + [c * 0.5 for c in closes[38:]]
    report, _ = check_bars(_bars(shifted), subject="unconfirmed")
    assert report.quarantined == (38, 39)
    confirmed = closes[:36] + [c * 0.5 for c in closes[36:]]
    report, usable = check_bars(_bars(confirmed), subject="confirmed")
    assert report.quarantined == ()
    assert len(usable) == 40
    assert any("large_moves" in w for w in report.warnings)
