"""Time utilities. All timestamps are timezone-aware UTC; conversion happens only at display edges."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FixedClock:
    """Deterministic clock for tests and simulations."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set(self, value: datetime) -> None:
        self._now = ensure_utc(value)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` in UTC. Naive datetimes are rejected rather than guessed."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return value.astimezone(UTC)


def to_display_tz(value: datetime, tz_name: str) -> datetime:
    """Convert an aware timestamp to a reader's IANA timezone for display."""
    return ensure_utc(value).astimezone(ZoneInfo(tz_name))


def isoformat_z(value: datetime) -> str:
    """ISO-8601 with a trailing ``Z`` and second precision, e.g. ``2026-10-21T02:41:09Z``."""
    return ensure_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")
