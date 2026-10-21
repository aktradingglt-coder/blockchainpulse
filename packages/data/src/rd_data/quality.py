"""Data-quality checks run on every ingestion (PROMPT §5.3). Blocking failures stop use, they do not just log.

Each check returns a :class:`CheckResult`; :func:`check_bars`, :func:`check_series` and :func:`check_positioning`
assemble a :class:`DataQualityReport` that is stored with the evidence it guards. Outliers are *quarantined*:
excluded from the usable series but kept, with their indices, in the report.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from rd_data.errors import DataQualityError
from rd_data.freshness import TradingCalendar
from rd_data.schemas import Bar, MacroObservation, PositioningReport

#: Robust z-score (median absolute deviation) above which a return is an outlier.
OUTLIER_Z = 10.0
#: Consecutive identical closes that make a quote stale while the market is open.
STALE_RUN = 5
#: Share of expected sessions that may be missing before gaps block use.
MAX_GAP_FRACTION = 0.05
#: Relative close difference tolerated between two sources.
RECONCILE_TOLERANCE = 0.005
#: Common split/reverse-split ratios checked for unexplained price jumps.
SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 10.0, 1.5, 20.0)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    blocking: bool
    detail: str
    indices: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "blocking": self.blocking,
            "detail": self.detail,
            "indices": list(self.indices),
        }


@dataclass(frozen=True)
class DataQualityReport:
    subject: str
    checks: tuple[CheckResult, ...]
    quarantined: tuple[int, ...] = ()
    record_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True when no *blocking* check failed (warnings do not block)."""
        return all(c.passed or not c.blocking for c in self.checks)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(f"{c.name}: {c.detail}" for c in self.checks if not c.passed and not c.blocking)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(f"{c.name}: {c.detail}" for c in self.checks if not c.passed and c.blocking)

    def require_passed(self, *, provider: str | None = None) -> None:
        if not self.passed:
            raise DataQualityError(f"{self.subject}: " + "; ".join(self.failures), provider=provider)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "passed": self.passed,
            "record_count": self.record_count,
            "checks": [c.as_dict() for c in self.checks],
            "quarantined": list(self.quarantined),
            "warnings": list(self.warnings),
            "failures": list(self.failures),
            **self.extra,
        }


def _ok(name: str, detail: str = "ok", *, blocking: bool = True) -> CheckResult:
    return CheckResult(name=name, passed=True, blocking=blocking, detail=detail)


def _fail(name: str, detail: str, *, blocking: bool, indices: tuple[int, ...] = ()) -> CheckResult:
    return CheckResult(name=name, passed=False, blocking=blocking, detail=detail, indices=indices)


def with_schema_rejections(report: DataQualityReport, rejected: int) -> DataQualityReport:
    """Adds the blocking ``schema`` check to a report (raw rows that failed normalisation)."""
    check = (
        _fail("schema", f"{rejected} raw row(s) failed schema validation", blocking=True) if rejected else _ok("schema")
    )
    return DataQualityReport(
        report.subject, (check, *report.checks), report.quarantined, report.record_count, report.extra
    )


# --------------------------------------------------------------------------------------------- generic checks


def check_monotonic(times: Sequence[datetime | date]) -> CheckResult:
    bad = tuple(i for i in range(1, len(times)) if times[i] < times[i - 1])
    if bad:
        return _fail("monotonic_timestamps", f"{len(bad)} timestamp(s) out of order", blocking=True, indices=bad)
    return _ok("monotonic_timestamps")


def check_duplicates(keys: Sequence[object]) -> CheckResult:
    seen: dict[object, int] = {}
    dups: list[int] = []
    for i, k in enumerate(keys):
        if k in seen:
            dups.append(i)
        else:
            seen[k] = i
    if dups:
        return _fail("no_duplicates", f"{len(dups)} duplicate key(s)", blocking=True, indices=tuple(dups))
    return _ok("no_duplicates")


def robust_z_scores(values: Sequence[float]) -> list[float]:
    """Robust z = 0.6745 * (x - median) / MAD. Zero MAD yields 0 for values equal to the median, else ±inf."""
    if not values:
        return []
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    if mad == 0:
        return [0.0 if v == med else math.copysign(math.inf, v - med) for v in values]
    return [0.6745 * (v - med) / mad for v in values]


def check_outliers(values: Sequence[float], *, threshold: float = OUTLIER_Z) -> CheckResult:
    """Flags robust-z outliers in a level series (e.g. macro values); non-blocking: the caller quarantines."""
    if len(values) < 10:
        return _ok("outliers", "fewer than 10 values: not evaluated", blocking=False)
    flagged = tuple(i for i, z in enumerate(robust_z_scores(values)) if abs(z) > threshold)
    if flagged:
        return _fail(
            "outliers",
            f"{len(flagged)} value(s) with |robust z| > {threshold:g} quarantined",
            blocking=False,
            indices=flagged,
        )
    return _ok("outliers", blocking=False)


def check_price_spikes(closes: Sequence[float], *, threshold: float = OUTLIER_Z) -> tuple[CheckResult, CheckResult]:
    """Bad prints vs genuine moves, on log returns scored by robust z.

    A *spike* — a bar whose incoming and outgoing returns are both extreme with opposite signs — is a bad print and
    is quarantined. A one-sided extreme return (a real jump or level shift) is kept and reported as a warning.
    Returns (spikes, jumps).
    """
    if len(closes) < 11 or any(c <= 0 for c in closes):
        skipped = _ok("price_spikes", "fewer than 11 positive closes: not evaluated", blocking=False)
        return skipped, _ok("large_moves", "not evaluated", blocking=False)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    z = robust_z_scores(rets)
    extreme = [abs(v) > threshold for v in z]  # extreme[k] is the return into bar k + 1
    spikes: list[int] = []
    for bar in range(1, len(closes)):
        into, out = bar - 1, bar
        if extreme[into] and out < len(rets) and extreme[out] and (rets[into] > 0) != (rets[out] > 0):
            spikes.append(bar)
    jumps = tuple(k + 1 for k, e in enumerate(extreme) if e and (k + 1) not in spikes and (k + 1) - 1 not in spikes)
    spike_res = (
        _fail("price_spikes", f"{len(spikes)} isolated spike(s) quarantined", blocking=False, indices=tuple(spikes))
        if spikes
        else _ok("price_spikes", blocking=False)
    )
    jump_res = (
        _fail("large_moves", f"{len(jumps)} extreme one-sided move(s) kept for review", blocking=False, indices=jumps)
        if jumps
        else _ok("large_moves", blocking=False)
    )
    return spike_res, jump_res


def check_stale_run(values: Sequence[float], *, run: int = STALE_RUN, market_open_at_end: bool = True) -> CheckResult:
    """A run of identical values reaching the end of the series while the market is open means a stale feed."""
    if len(values) < run:
        return _ok("stale_quotes", "series shorter than the stale-run length")
    tail = values[-run:]
    if market_open_at_end and all(v == tail[0] for v in tail):
        idx = tuple(range(len(values) - run, len(values)))
        return _fail("stale_quotes", f"last {run} values identical ({tail[0]})", blocking=True, indices=idx)
    return _ok("stale_quotes")


# ------------------------------------------------------------------------------------------------- bar checks


def _bar_is_sane(b: Bar, *, allow_non_positive: bool) -> bool:
    prices = (b.open, b.high, b.low, b.close)
    if any(math.isnan(x) or math.isinf(x) for x in prices):
        return False
    if not (b.low <= min(b.open, b.close) and max(b.open, b.close) <= b.high):
        return False
    if b.volume is not None and b.volume < 0:
        return False
    return allow_non_positive or min(prices) > 0


def check_ohlc(bars: Sequence[Bar], *, allow_non_positive: bool = False) -> CheckResult:
    bad = tuple(i for i, b in enumerate(bars) if not _bar_is_sane(b, allow_non_positive=allow_non_positive))
    if bad:
        return _fail(
            "ohlc_sanity",
            f"{len(bad)} bar(s) violate low <= open/close <= high, volume >= 0 or positive finite prices",
            blocking=True,
            indices=bad,
        )
    return _ok("ohlc_sanity")


def check_gaps(
    bars: Sequence[Bar], calendar: TradingCalendar, *, max_gap_fraction: float = MAX_GAP_FRACTION
) -> CheckResult:
    """Daily bars against the instrument's trading calendar: expected sessions vs bars present."""
    if len(bars) < 2:
        return _ok("calendar_gaps", "fewer than two bars")
    first, last = bars[0].ts.date(), bars[-1].ts.date()
    expected: list[date] = [first]
    probe = first
    while probe < last:
        nxt = calendar.closes_after(probe, 1)[0]
        session = _session_of_close(nxt, calendar)
        if session > last:
            break
        expected.append(session)
        probe = session
    have = {b.ts.date() for b in bars}
    missing = [d for d in expected if d not in have]
    fraction = len(missing) / len(expected)
    detail = f"{len(missing)} of {len(expected)} expected {calendar.name} sessions missing"
    if missing:
        sample = ", ".join(d.isoformat() for d in missing[:5])
        detail += f" (e.g. {sample})"
        return _fail("calendar_gaps", detail, blocking=fraction > max_gap_fraction)
    return _ok("calendar_gaps", detail)


def _session_of_close(close: datetime, calendar: TradingCalendar) -> date:
    """Session date for a close: exchange closes fall on the session day, 00:00 UTC closes on the day after."""
    if close.hour == 0 and close.minute == 0 and calendar.name in ("24/7", "weekdays"):
        return (close - timedelta(days=1)).date()
    return close.date()


def check_reconciliation(
    bars: Sequence[Bar], reference: Sequence[Bar], *, tolerance: float = RECONCILE_TOLERANCE
) -> CheckResult:
    """Cross-source check on closes at matching timestamps; blocking only if most points disagree."""
    ref = {b.ts: b.close for b in reference}
    compared, off = 0, []
    for i, b in enumerate(bars):
        if b.ts in ref and ref[b.ts] != 0:
            compared += 1
            if abs(b.close / ref[b.ts] - 1) > tolerance:
                off.append(i)
    if compared == 0:
        return _fail("cross_source", "no overlapping timestamps with the reference source", blocking=False)
    if off:
        return _fail(
            "cross_source",
            f"{len(off)} of {compared} closes differ by more than {tolerance:.2%}",
            blocking=len(off) > compared / 2,
            indices=tuple(off),
        )
    return _ok("cross_source", f"{compared} closes within {tolerance:.2%}", blocking=False)


def check_splits(bars: Sequence[Bar], split_dates: Sequence[date] = (), *, tolerance: float = 0.03) -> CheckResult:
    """Unexplained close-to-close jumps matching a split ratio mean the history is not split-adjusted."""
    known = set(split_dates)
    hits = []
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1].close, bars[i].close
        if prev <= 0 or cur <= 0 or bars[i].ts.date() in known:
            continue
        ratio = prev / cur if prev > cur else cur / prev
        if any(abs(ratio / r - 1) <= tolerance for r in SPLIT_RATIOS):
            hits.append(i)
    if hits:
        return _fail(
            "corporate_actions",
            f"{len(hits)} jump(s) match a split ratio with no recorded corporate action",
            blocking=True,
            indices=tuple(hits),
        )
    return _ok("corporate_actions")


def check_bars(
    bars: Sequence[Bar],
    *,
    subject: str,
    calendar: TradingCalendar | None = None,
    market_open_at_end: bool = True,
    reference: Sequence[Bar] | None = None,
    split_dates: Sequence[date] | None = None,
    allow_non_positive: bool = False,
    rejected_rows: int = 0,
) -> tuple[DataQualityReport, list[Bar]]:
    """Full bar QA. Returns the report and the usable bars (quarantined outliers removed)."""
    checks: list[CheckResult] = [
        _fail("schema", f"{rejected_rows} raw row(s) failed schema validation", blocking=True)
        if rejected_rows
        else _ok("schema"),
        check_monotonic([b.ts for b in bars]),
        check_duplicates([b.ts for b in bars]),
        check_ohlc(bars, allow_non_positive=allow_non_positive),
    ]
    closes = [b.close for b in bars]
    spikes, jumps = check_price_spikes(closes)
    checks.extend([spikes, jumps])
    checks.append(check_stale_run(closes, market_open_at_end=market_open_at_end))
    if calendar is not None and bars and bars[0].interval == "1d":
        checks.append(check_gaps(bars, calendar))
    if reference is not None:
        checks.append(check_reconciliation(bars, reference))
    if split_dates is not None:
        checks.append(check_splits(bars, split_dates))
    quarantined = spikes.indices
    usable = [b for i, b in enumerate(bars) if i not in set(quarantined)]
    report = DataQualityReport(subject, tuple(checks), quarantined, len(bars))
    return report, usable


def check_series(observations: Sequence[MacroObservation], *, subject: str) -> DataQualityReport:
    """Macro/inventory series: ordering, duplicates, finite values; missing values are allowed but counted."""
    values = [o.value for o in observations]
    missing = sum(1 for v in values if v is None)
    nonfinite = tuple(i for i, v in enumerate(values) if v is not None and not math.isfinite(v))
    checks = [
        check_monotonic([o.ts for o in observations]),
        check_duplicates([o.ts for o in observations]),
        _fail("finite_values", f"{len(nonfinite)} non-finite value(s)", blocking=True, indices=nonfinite)
        if nonfinite
        else _ok("finite_values"),
        _ok("missing_values", f"{missing} observation(s) reported as missing by the source", blocking=False),
    ]
    return DataQualityReport(subject, tuple(checks), (), len(observations))


def check_positioning(
    reports: Sequence[PositioningReport], *, subject: str, tolerance: float = 0.01
) -> DataQualityReport:
    """COT rows: one row per market/date, and the long side (incl. spreading) must sum to open interest."""
    keys = [(r.market_code, r.as_of, r.report_type, r.futures_only) for r in reports]
    unbalanced = []
    for i, r in enumerate(reports):
        long_total = sum(c.long + (c.spreading or 0.0) for c in r.categories.values())
        if r.open_interest > 0 and abs(long_total / r.open_interest - 1) > tolerance:
            unbalanced.append(i)
    checks = [
        check_duplicates(keys),
        _fail(
            "long_side_equals_open_interest",
            f"{len(unbalanced)} row(s) where long positions do not sum to open interest within {tolerance:.0%}",
            blocking=True,
            indices=tuple(unbalanced),
        )
        if unbalanced
        else _ok("long_side_equals_open_interest"),
    ]
    return DataQualityReport(subject, tuple(checks), (), len(reports))
