from datetime import UTC, datetime, timedelta, timezone

import pytest

from rd_core.clock import FixedClock, SystemClock, ensure_utc, isoformat_z, to_display_tz


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 10, 21, 12, 0))  # noqa: DTZ001 - deliberately naive


def test_ensure_utc_converts() -> None:
    pkt = timezone(timedelta(hours=5))
    assert ensure_utc(datetime(2026, 10, 21, 17, 0, tzinfo=pkt)) == datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def test_display_conversion_and_format() -> None:
    ts = datetime(2026, 10, 21, 2, 41, 9, 123456, tzinfo=UTC)
    assert isoformat_z(ts) == "2026-10-21T02:41:09Z"
    assert to_display_tz(ts, "Asia/Karachi").hour == 7
    assert to_display_tz(ts, "America/New_York").utcoffset() == timedelta(hours=-4)


def test_fixed_clock() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    clock.advance(timedelta(minutes=5))
    assert clock.now() == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    clock.set(datetime(2026, 2, 1, tzinfo=UTC))
    assert clock.now().month == 2
    assert SystemClock().now().tzinfo is UTC
