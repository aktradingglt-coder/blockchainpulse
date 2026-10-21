"""Freshness policy (PROMPT §5.4), enforced in code.

| Data type                    | Stale after                          | Blocked after                      |
| ---------------------------- | ------------------------------------ | ---------------------------------- |
| Intraday price, real-time    | 2 min                                | 15 min                             |
| Intraday price, delayed      | delay + 5 min                        | delay + 30 min                     |
| Daily bars                   | 1 missed trading session             | 3 missed trading sessions          |
| Crypto funding / OI          | 1 h                                  | 6 h                                |
| COT                          | 8 days                               | 15 days                            |
| Inventories / macro releases | next scheduled release + 1 day       | next scheduled release + 7 days    |
| Fundamentals                 | next expected filing + 7 days        | 120 days after filing              |
| News ("latest" sections)     | 24 h                                 | 7 days                             |

Daily bars count *sessions* of the instrument's trading calendar, so a Friday bar is still fresh on Sunday.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Protocol

import exchange_calendars as xcals
import pandas as pd

from rd_core.clock import ensure_utc
from rd_data.errors import InvalidArgs, Stale
from rd_data.schemas import DataType


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class FreshnessResult:
    status: Freshness
    as_of: datetime
    evaluated_at: datetime
    stale_after: datetime
    blocked_after: datetime
    reason: str

    @property
    def usable(self) -> bool:
        return self.status is not Freshness.BLOCKED

    def require_usable(self, *, provider: str | None = None) -> None:
        if not self.usable:
            raise Stale(f"data as of {self.as_of.isoformat()} is blocked: {self.reason}", provider=provider)


class TradingCalendar(Protocol):
    name: str

    def closes_after(self, session: date, count: int) -> list[datetime]:
        """UTC closes of the next ``count`` sessions strictly after ``session``."""
        ...


class ContinuousCalendar:
    """Crypto: a session every calendar day, closing at 00:00 UTC of the next day (PROMPT §3.2 crypto rule)."""

    name = "24/7"

    def closes_after(self, session: date, count: int) -> list[datetime]:
        return [
            datetime.combine(session + timedelta(days=k + 1), time(0, tzinfo=UTC)) + timedelta(days=1)
            for k in range(count)
        ]


class WeekdayCalendar:
    """FX-style: Monday to Friday sessions closing at 00:00 UTC of the next day; no holidays."""

    name = "weekdays"

    def closes_after(self, session: date, count: int) -> list[datetime]:
        out: list[datetime] = []
        day = session
        while len(out) < count:
            day += timedelta(days=1)
            if day.weekday() < 5:
                out.append(datetime.combine(day + timedelta(days=1), time(0, tzinfo=UTC)))
        return out


class ExchangeCalendar:
    """Exchange sessions, early closes and holidays from ``exchange_calendars`` (e.g. ``XNYS``, ``CMES``)."""

    def __init__(self, code: str) -> None:
        self.name = code
        self._cal = _load_calendar(code)

    def closes_after(self, session: date, count: int) -> list[datetime]:
        start = pd.Timestamp(session) + pd.Timedelta(days=1)
        end = min(start + pd.Timedelta(days=30 + 3 * count), self._cal.last_session)
        if start > end:
            raise InvalidArgs(f"{self.name} calendar does not cover {session.isoformat()}")
        sessions = self._cal.sessions_in_range(start, end)[:count]
        return [ensure_utc(self._cal.session_close(s).to_pydatetime()) for s in sessions]


@lru_cache(maxsize=16)
def _load_calendar(code: str) -> xcals.ExchangeCalendar:
    try:
        return xcals.get_calendar(code)
    except Exception as exc:  # exchange_calendars raises its own InvalidCalendarName
        raise InvalidArgs(f"unknown trading calendar {code!r}") from exc


_FIXED: dict[DataType, tuple[timedelta, timedelta]] = {
    DataType.INTRADAY_REALTIME: (timedelta(minutes=2), timedelta(minutes=15)),
    DataType.CRYPTO_DERIVATIVES: (timedelta(hours=1), timedelta(hours=6)),
    DataType.COT: (timedelta(days=8), timedelta(days=15)),
    DataType.NEWS: (timedelta(hours=24), timedelta(days=7)),
}
#: Default gap to the next release when the caller has no release calendar entry.
RELEASE_INTERVALS: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=31),
    "quarterly": timedelta(days=92),
    "annual": timedelta(days=366),
}
#: 10-Q/10-K cadence: the next filing is expected within ~95 days of the last one.
NEXT_FILING_DEFAULT = timedelta(days=95)
FUNDAMENTALS_BLOCK = timedelta(days=120)


def _as_datetime(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value)
    return datetime.combine(value, time(0, tzinfo=UTC))


def classify(
    data_type: DataType,
    as_of: datetime | date,
    now: datetime,
    *,
    delay_minutes: int = 0,
    calendar: TradingCalendar | None = None,
    next_release_at: datetime | None = None,
    release_frequency: str | None = None,
    next_filing_expected_at: datetime | None = None,
) -> FreshnessResult:
    """Classify data as fresh, stale (label it) or blocked (do not use)."""
    as_of_dt = _as_datetime(as_of)
    now = ensure_utc(now)
    if as_of_dt > now + timedelta(minutes=5):
        raise InvalidArgs(f"as_of {as_of_dt.isoformat()} is in the future (clock skew or bad data)")

    if data_type in _FIXED:
        stale_d, block_d = _FIXED[data_type]
        stale_after, blocked_after = as_of_dt + stale_d, as_of_dt + block_d
        reason = f"{data_type.value}: stale after {stale_d}, blocked after {block_d}"
    elif data_type is DataType.INTRADAY_DELAYED:
        delay = timedelta(minutes=delay_minutes)
        stale_after = as_of_dt + delay + timedelta(minutes=5)
        blocked_after = as_of_dt + delay + timedelta(minutes=30)
        reason = f"delayed feed ({delay_minutes} min): stale after delay+5 min, blocked after delay+30 min"
    elif data_type is DataType.DAILY_BAR:
        return _classify_daily(as_of_dt, now, calendar)
    elif data_type is DataType.MACRO_RELEASE:
        if next_release_at is None:
            if release_frequency not in RELEASE_INTERVALS:
                raise InvalidArgs("macro freshness needs next_release_at or a known release_frequency")
            next_release_at = as_of_dt + RELEASE_INTERVALS[release_frequency]
        nr = ensure_utc(next_release_at)
        stale_after, blocked_after = nr + timedelta(days=1), nr + timedelta(days=7)
        reason = f"next release {nr.isoformat()}: stale after +1 day, blocked after +7 days"
    elif data_type in (DataType.FUNDAMENTALS, DataType.FILINGS):
        nf = ensure_utc(next_filing_expected_at) if next_filing_expected_at else as_of_dt + NEXT_FILING_DEFAULT
        stale_after = nf + timedelta(days=7)
        blocked_after = as_of_dt + FUNDAMENTALS_BLOCK
        reason = f"next filing expected {nf.date().isoformat()}: stale after +7 days, blocked 120 days after filing"
    else:  # pragma: no cover - exhaustive over DataType
        raise InvalidArgs(f"no freshness rule for {data_type}")

    status = Freshness.FRESH
    if now >= blocked_after:
        status = Freshness.BLOCKED
    elif now >= stale_after:
        status = Freshness.STALE
    return FreshnessResult(status, as_of_dt, now, stale_after, blocked_after, reason)


def _classify_daily(as_of: datetime, now: datetime, calendar: TradingCalendar | None) -> FreshnessResult:
    if calendar is None:
        raise InvalidArgs("daily-bar freshness needs the instrument's trading calendar")
    closes = calendar.closes_after(as_of.date(), 3)
    missed = sum(1 for c in closes if c <= now)
    status = Freshness.FRESH if missed == 0 else Freshness.STALE if missed < 3 else Freshness.BLOCKED
    reason = f"{missed} completed {calendar.name} session(s) since the bar; stale at 1, blocked at 3"
    return FreshnessResult(status, as_of, now, closes[0], closes[2], reason)
