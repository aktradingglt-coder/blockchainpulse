from datetime import UTC, date, datetime, timedelta

import pytest

from rd_data.errors import InvalidArgs, Stale
from rd_data.freshness import (
    ContinuousCalendar,
    ExchangeCalendar,
    Freshness,
    WeekdayCalendar,
    classify,
)
from rd_data.schemas import DataType

T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("data_type", "age", "expected"),
    [
        (DataType.INTRADAY_REALTIME, timedelta(minutes=1), Freshness.FRESH),
        (DataType.INTRADAY_REALTIME, timedelta(minutes=2), Freshness.STALE),
        (DataType.INTRADAY_REALTIME, timedelta(minutes=15), Freshness.BLOCKED),
        (DataType.CRYPTO_DERIVATIVES, timedelta(minutes=59), Freshness.FRESH),
        (DataType.CRYPTO_DERIVATIVES, timedelta(hours=1), Freshness.STALE),
        (DataType.CRYPTO_DERIVATIVES, timedelta(hours=6), Freshness.BLOCKED),
        (DataType.COT, timedelta(days=7), Freshness.FRESH),
        (DataType.COT, timedelta(days=8), Freshness.STALE),
        (DataType.COT, timedelta(days=15), Freshness.BLOCKED),
        (DataType.NEWS, timedelta(hours=23), Freshness.FRESH),
        (DataType.NEWS, timedelta(hours=24), Freshness.STALE),
        (DataType.NEWS, timedelta(days=7), Freshness.BLOCKED),
    ],
)
def test_fixed_windows(data_type: DataType, age: timedelta, expected: Freshness) -> None:
    assert classify(data_type, T0 - age, T0).status is expected


def test_delayed_feed() -> None:
    assert (
        classify(DataType.INTRADAY_DELAYED, T0 - timedelta(minutes=19), T0, delay_minutes=15).status is Freshness.FRESH
    )
    assert (
        classify(DataType.INTRADAY_DELAYED, T0 - timedelta(minutes=20), T0, delay_minutes=15).status is Freshness.STALE
    )
    assert (
        classify(DataType.INTRADAY_DELAYED, T0 - timedelta(minutes=45), T0, delay_minutes=15).status
        is Freshness.BLOCKED
    )


def test_daily_bars_count_exchange_sessions_not_calendar_days() -> None:
    nyse = ExchangeCalendar("XNYS")
    friday_bar = date(2026, 10, 16)
    sunday = datetime(2026, 10, 18, 22, tzinfo=UTC)
    assert classify(DataType.DAILY_BAR, friday_bar, sunday, calendar=nyse).status is Freshness.FRESH
    tuesday = datetime(2026, 10, 20, 12, tzinfo=UTC)  # Monday's session closed
    assert classify(DataType.DAILY_BAR, friday_bar, tuesday, calendar=nyse).status is Freshness.STALE
    thursday = datetime(2026, 10, 22, 21, tzinfo=UTC)  # Mon, Tue, Wed, Thu closed
    res = classify(DataType.DAILY_BAR, friday_bar, thursday, calendar=nyse)
    assert res.status is Freshness.BLOCKED
    with pytest.raises(Stale):
        res.require_usable(provider="x")


def test_daily_bars_skip_exchange_holidays() -> None:
    nyse = ExchangeCalendar("XNYS")
    # Wed 2026-11-25; Thanksgiving Thu 26 closed; Fri 27 half day. Monday 30 morning: only Friday has closed.
    res = classify(DataType.DAILY_BAR, date(2026, 11, 25), datetime(2026, 11, 30, 14, tzinfo=UTC), calendar=nyse)
    assert res.status is Freshness.STALE
    assert "1 completed" in res.reason


def test_crypto_and_fx_calendars() -> None:
    crypto = ContinuousCalendar()
    assert (
        classify(DataType.DAILY_BAR, date(2026, 10, 17), datetime(2026, 10, 18, 23, tzinfo=UTC), calendar=crypto).status
        is Freshness.FRESH
    )
    assert (
        classify(
            DataType.DAILY_BAR, date(2026, 10, 17), datetime(2026, 10, 19, 0, 1, tzinfo=UTC), calendar=crypto
        ).status
        is Freshness.STALE
    )
    fx = WeekdayCalendar()
    assert (
        classify(DataType.DAILY_BAR, date(2026, 10, 16), datetime(2026, 10, 19, 23, tzinfo=UTC), calendar=fx).status
        is Freshness.FRESH
    )


def test_macro_release_windows() -> None:
    nr = datetime(2026, 11, 13, 13, 30, tzinfo=UTC)
    as_of = datetime(2026, 10, 15, tzinfo=UTC)
    assert (
        classify(DataType.MACRO_RELEASE, as_of, nr + timedelta(hours=23), next_release_at=nr).status is Freshness.FRESH
    )
    assert classify(DataType.MACRO_RELEASE, as_of, nr + timedelta(days=1), next_release_at=nr).status is Freshness.STALE
    assert (
        classify(DataType.MACRO_RELEASE, as_of, nr + timedelta(days=7), next_release_at=nr).status is Freshness.BLOCKED
    )
    weekly = classify(DataType.MACRO_RELEASE, as_of, as_of + timedelta(days=8), release_frequency="weekly")
    assert weekly.status is Freshness.STALE
    with pytest.raises(InvalidArgs):
        classify(DataType.MACRO_RELEASE, as_of, T0)


def test_fundamentals_windows() -> None:
    filed = date(2026, 5, 1)
    assert classify(DataType.FUNDAMENTALS, filed, datetime(2026, 8, 1, tzinfo=UTC)).status is Freshness.FRESH
    assert classify(DataType.FUNDAMENTALS, filed, datetime(2026, 8, 13, tzinfo=UTC)).status is Freshness.STALE
    assert classify(DataType.FUNDAMENTALS, filed, datetime(2026, 8, 29, tzinfo=UTC)).status is Freshness.BLOCKED
    custom = classify(
        DataType.FILINGS,
        filed,
        datetime(2026, 6, 1, tzinfo=UTC),
        next_filing_expected_at=datetime(2026, 5, 20, tzinfo=UTC),
    )
    assert custom.status is Freshness.STALE


def test_guards() -> None:
    with pytest.raises(InvalidArgs, match="future"):
        classify(DataType.NEWS, T0 + timedelta(hours=1), T0)
    with pytest.raises(InvalidArgs, match="calendar"):
        classify(DataType.DAILY_BAR, date(2026, 10, 16), T0)
    with pytest.raises(InvalidArgs, match="unknown trading calendar"):
        ExchangeCalendar("NOPE")


def test_interval_bars_are_judged_from_their_close() -> None:
    close = datetime(2026, 10, 21, 10, tzinfo=UTC)  # the 09:00 1h bar
    hour = timedelta(hours=1)
    assert (
        classify(DataType.INTERVAL_BAR, close, close + timedelta(minutes=5), bar_width=hour).status is Freshness.FRESH
    )
    # the 10:00 bar is due at 11:00; five minutes later it is overdue
    assert classify(DataType.INTERVAL_BAR, close, close + hour + timedelta(minutes=6), bar_width=hour).status is (
        Freshness.STALE
    )
    assert classify(DataType.INTERVAL_BAR, close, close + 3 * hour + timedelta(minutes=31), bar_width=hour).status is (
        Freshness.BLOCKED
    )
    four = timedelta(hours=4)
    four_close = datetime(2026, 10, 21, 8, tzinfo=UTC)  # the 04:00 4h bar
    assert classify(
        DataType.INTERVAL_BAR, four_close, datetime(2026, 10, 21, 10, 5, tzinfo=UTC), bar_width=four
    ).status is (Freshness.FRESH)
    delayed = classify(
        DataType.INTERVAL_BAR, close, close + hour + timedelta(minutes=10), bar_width=hour, delay_minutes=15
    )
    assert delayed.status is Freshness.FRESH  # a 15-minute delayed feed gets its delay on top
    with pytest.raises(InvalidArgs, match="bar_width"):
        classify(DataType.INTERVAL_BAR, close, close)


def test_market_open_state_per_calendar() -> None:
    saturday = datetime(2026, 10, 17, 15, tzinfo=UTC)
    tuesday_noon_ny = datetime(2026, 10, 20, 16, tzinfo=UTC)
    tuesday_night = datetime(2026, 10, 21, 3, tzinfo=UTC)
    assert ContinuousCalendar().is_open(saturday)
    assert not WeekdayCalendar().is_open(saturday)
    assert WeekdayCalendar().is_open(tuesday_night)
    nyse = ExchangeCalendar("XNYS")
    assert nyse.is_open(tuesday_noon_ny)
    assert not nyse.is_open(tuesday_night)
    assert not nyse.is_open(saturday)
    assert not nyse.is_open(datetime(1950, 1, 3, 15, tzinfo=UTC))  # outside the calendar's range
