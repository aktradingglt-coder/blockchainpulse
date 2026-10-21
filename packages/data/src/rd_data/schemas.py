"""Canonical schemas every provider response is normalised into (PROMPT §5.1).

All timestamps are timezone-aware UTC; the provider's own timezone is recorded in ``source_tz`` on the response.
Records are frozen: once normalised, a value never changes (evidence immutability starts here).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from rd_core.clock import ensure_utc

UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]


class AssetClass(StrEnum):
    FX = "fx"
    COMMODITY = "commodity"
    FUTURE = "future"
    EQUITY = "equity"
    CRYPTO = "crypto"
    MACRO = "macro"
    RATES = "rates"


class DataType(StrEnum):
    """Freshness classes from PROMPT §5.4."""

    INTRADAY_REALTIME = "intraday_realtime"
    INTRADAY_DELAYED = "intraday_delayed"
    DAILY_BAR = "daily_bar"
    CRYPTO_DERIVATIVES = "crypto_derivatives"
    COT = "cot"
    MACRO_RELEASE = "macro_release"
    FUNDAMENTALS = "fundamentals"
    FILINGS = "filings"
    NEWS = "news"


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Instrument(_Record):
    symbol: str = Field(min_length=1)
    asset_class: AssetClass
    name: str | None = None
    exchange: str | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3,5}$")
    provider_symbol: str | None = None
    contract_month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    tick_size: float | None = Field(default=None, gt=0)
    tick_value: float | None = Field(default=None, gt=0)
    multiplier: float | None = Field(default=None, gt=0)


class Bar(_Record):
    """One OHLCV bar; ``ts`` is the bar's opening time in UTC."""

    symbol: str
    interval: Literal["1m", "5m", "15m", "1h", "4h", "1d", "1w"]
    ts: UtcDatetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    open_interest: float | None = None


class Quote(_Record):
    symbol: str
    ts: UtcDatetime
    bid: float | None = None
    ask: float | None = None
    last: float | None = None


class FundamentalFact(_Record):
    """One XBRL fact, point-in-time: usable only from ``filed`` onwards (never by period end alone)."""

    cik: str = Field(pattern=r"^\d{10}$")
    concept: str
    taxonomy: str
    unit: str
    value: float
    period_start: date | None = None
    period_end: date
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    form: str
    filed: date
    accession: str
    frame: str | None = None


class Filing(_Record):
    cik: str = Field(pattern=r"^\d{10}$")
    company: str
    form: str
    filed: date
    accepted_at: UtcDatetime | None = None
    accession: str
    primary_document: str | None = None
    report_date: date | None = None
    url: str


class MacroObservation(_Record):
    series_id: str
    ts: UtcDatetime
    value: float | None
    unit: str
    frequency: str | None = None
    note: str | None = None


class PositionCounts(_Record):
    long: float = Field(ge=0)
    short: float = Field(ge=0)
    spreading: float | None = Field(default=None, ge=0)

    @property
    def net(self) -> float:
        return self.long - self.short


class PositioningReport(_Record):
    """One CFTC Commitments of Traders row for one market and report date (positions as of Tuesday)."""

    market_code: str
    market_name: str
    report_type: Literal["legacy", "disaggregated", "tff"]
    futures_only: bool
    as_of: date
    open_interest: float = Field(ge=0)
    categories: Mapping[str, PositionCounts]
    exchange: str | None = None


class TermStructurePoint(_Record):
    root: str
    contract_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    ts: UtcDatetime
    price: float
    expiry: date | None = None


class CryptoDerivativeMetric(_Record):
    exchange: str
    symbol: str
    metric: Literal["funding_rate", "open_interest", "mark_price", "index_price", "basis", "volume_24h"]
    value: float
    ts: UtcDatetime
    unit: str
    funding_interval_hours: float | None = Field(default=None, gt=0)


class NewsItem(_Record):
    """Untrusted text: summaries and titles are data, never instructions (PROMPT §6.4)."""

    item_id: str
    source: str
    title: str
    url: str
    published_at: UtcDatetime
    summary: str | None = None
    untrusted: Literal[True] = True


class CalendarEvent(_Record):
    event_id: str
    name: str
    region: str
    scheduled_at: UtcDatetime
    importance: Literal["low", "medium", "high"]
    source: str


class TreasuryYieldPoint(_Record):
    """One point on the US Treasury daily par yield curve (percent)."""

    ts: UtcDatetime
    tenor: Literal["1M", "1.5M", "2M", "3M", "4M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]
    yield_pct: float


CanonicalRecord = (
    Instrument
    | Bar
    | Quote
    | FundamentalFact
    | Filing
    | MacroObservation
    | PositioningReport
    | TermStructurePoint
    | CryptoDerivativeMetric
    | NewsItem
    | CalendarEvent
    | TreasuryYieldPoint
)


def record_time(record: CanonicalRecord) -> datetime | date | None:
    """The as-of time a record describes (used for freshness and ordering)."""
    for attr in ("ts", "as_of", "filed", "period_end", "published_at", "scheduled_at"):
        value = getattr(record, attr, None)
        if isinstance(value, (datetime, date)):
            return value
    return None
