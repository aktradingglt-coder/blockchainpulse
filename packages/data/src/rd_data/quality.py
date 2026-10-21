"""Data-quality checks run on every ingestion (PROMPT §5.3). Blocking failures stop use, they do not just log.

Each check returns a :class:`CheckResult`; :func:`check_bars`, :func:`check_series` and :func:`check_positioning`
assemble a :class:`DataQualityReport` that is stored with the evidence it guards. Outliers are *quarantined*:
excluded from the usable series but kept, with their indices, in the report.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from rd_data.errors import DataQualityError
from rd_data.freshness import TradingCalendar
from rd_data.schemas import Bar, MacroObservation, PositioningReport

#: History every series adapter fetches at minimum, whatever window was asked for, so that bad-print and stock-move
#: checks always have enough context (they need 11 points); the response is trimmed to the request afterwards.
QA_CONTEXT = 30
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


def check_provider_order(times: Sequence[datetime | date], *, descending: bool) -> CheckResult:
    """The provider returned rows in the order we asked for (checked *before* any sorting on our side).

    Rows out of the requested order mean the API, pagination or a proxy mangled the response.
    """
    seq = list(reversed(times)) if descending else list(times)
    bad = tuple(i for i in range(1, len(seq)) if seq[i] < seq[i - 1])
    if bad:
        order = "descending" if descending else "ascending"
        return _fail("provider_order", f"{len(bad)} row(s) not in the requested {order} order", blocking=True)
    return _ok("provider_order")


def check_cadence(
    times: Sequence[datetime | date],
    *,
    period: timedelta,
    tolerance: timedelta = timedelta(days=1),
    max_gap_fraction: float = MAX_GAP_FRACTION,
) -> CheckResult:
    """Regular series (weekly COT and inventories): missing periods between the first and last observation.

    Holiday shifts within ``tolerance`` are fine; a longer gap counts the periods it skips. Blocking above
    ``max_gap_fraction`` of the expected periods, otherwise a warning that names the gaps.
    """
    ordered = sorted({_as_date(t) for t in times})
    if len(ordered) < 2:
        return _ok("cadence_gaps", "fewer than two observations")
    missing: list[date] = []
    for prev, cur in itertools.pairwise(ordered):
        step = cur - prev
        if step > period + tolerance:
            skipped = round(step / period) - 1
            missing.extend(prev + period * (k + 1) for k in range(max(skipped, 1)))
    expected = len(ordered) + len(missing)
    if not missing:
        return _ok("cadence_gaps", f"{len(ordered)} consecutive periods")
    sample = ", ".join(d.isoformat() for d in missing[:5])
    detail = f"{len(missing)} of {expected} expected periods missing (e.g. {sample})"
    return _fail("cadence_gaps", detail, blocking=len(missing) / expected > max_gap_fraction)


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


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
    """Robust z = 0.6745 * (x - median) / MAD.

    When more than half the values are identical the MAD is 0; the scale then falls back to the mean absolute
    deviation (z = (x - median) / (1.2533 * MeanAD)), the usual companion estimator. Only when every value equals
    the median is the scale 0: values equal to it score 0, any other ±inf.
    """
    if not values:
        return []
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    if mad > 0:
        return [0.6745 * (v - med) / mad for v in values]
    mean_ad = statistics.fmean(abs(v - med) for v in values)
    if mean_ad > 0:
        return [(v - med) / (1.253314 * mean_ad) for v in values]
    return [0.0 if v == med else math.copysign(math.inf, v - med) for v in values]


#: Longest run of consecutive points treated as a bad print when the series returns to its prior level afterwards.
MAX_BAD_RUN = 2
#: For an implausible move (beyond ``implausible`` in :func:`scan_spikes`, e.g. x10 in one bar) the run, and the
#: confirmation a genuine shift needs, extend to this many points: three bad ticks in a row are not a market move.
LONG_BAD_RUN = 5
#: A single-bar price move beyond x10 (or ÷10) is implausible for any instrument we cover (D-031).
IMPLAUSIBLE_LOG_MOVE = math.log(10.0)
#: A week-on-week open-interest move beyond x3 (or /3) is a corrupted row, not an expiry roll.
OI_IMPLAUSIBLE_LOG_MOVE = math.log(3.0)


@dataclass(frozen=True)
class SpikeScan:
    """Result of :func:`scan_spikes` on a level series (indices are positions in the series)."""

    evaluated: bool
    bad_prints: tuple[int, ...] = ()  # extreme move that reverses within MAX_BAD_RUN points
    unconfirmed: tuple[int, ...] = ()  # extreme move at either end of the series that cannot be confirmed yet
    jumps: tuple[int, ...] = ()  # extreme move confirmed by more than MAX_BAD_RUN later points


def scan_spikes(
    levels: Sequence[float], *, threshold: float = OUTLIER_Z, implausible: float | None = None
) -> SpikeScan:
    """Bad prints vs genuine moves, on point-to-point changes of ``levels`` scored by robust z (MAD).

    An extreme change is a *bad print* when the series comes back (within half the move) after at most
    :data:`MAX_BAD_RUN` points. One that does not come back is a genuine level shift once more than
    :data:`MAX_BAD_RUN` points sit at the new level; with fewer (at the end of the series), or when the shift is
    within the first :data:`MAX_BAD_RUN` points, it is *unconfirmed*: the move cannot be told apart from a bad print.
    Changes larger than ``implausible`` (absolute, in the units of ``levels``) use :data:`LONG_BAD_RUN` instead.
    """
    n = len(levels)
    if n < 11:
        return SpikeScan(evaluated=False)
    changes = [levels[i + 1] - levels[i] for i in range(n - 1)]
    extreme = [abs(z) > threshold for z in robust_z_scores(changes)]  # extreme[k]: the change into point k + 1
    bad: set[int] = set()
    unconfirmed: set[int] = set()
    jumps: list[int] = []
    return_legs: set[int] = set()
    for k, is_extreme in enumerate(extreme):
        if not is_extreme or k in return_legs:
            continue
        limit = LONG_BAD_RUN if implausible is not None and abs(changes[k]) > implausible else MAX_BAD_RUN
        run = _reversal_run(levels, changes, extreme, k, limit)
        if run:
            bad.update(range(k + 1, k + 1 + run))
            return_legs.add(k + run)
        elif k + 1 <= limit:
            unconfirmed.update(range(k + 1))  # the first point(s) may be the bad print: nothing before to compare
        elif n - 1 - k <= limit:
            unconfirmed.update(range(k + 1, n))  # too few points at the new level to confirm the move
        else:
            jumps.append(k + 1)
    return SpikeScan(
        evaluated=True,
        bad_prints=tuple(sorted(bad)),
        unconfirmed=tuple(sorted(unconfirmed - bad)),
        jumps=tuple(jumps),
    )


def _reversal_run(
    levels: Sequence[float], changes: Sequence[float], extreme: Sequence[bool], k: int, limit: int
) -> int:
    """Length of the run of points after change ``k`` from which the series comes back, or 0 if it does not."""
    n = len(levels)
    for run in range(1, limit + 1):
        back = k + run  # index of the change out of the candidate run
        if back > n - 2:
            return 0
        if run > 1 and extreme[back - 1]:
            return 0  # another extreme move inside the run: not a single bad print
        if extreme[back] and abs(levels[back + 1] - levels[k]) < 0.5 * abs(changes[k]):
            return run
    return 0


def check_price_spikes(closes: Sequence[float], *, threshold: float = OUTLIER_Z) -> tuple[CheckResult, CheckResult]:
    """Bad prints on log closes. Returns (price_spikes, large_moves).

    Bad prints and *unconfirmed* extreme moves at the ends of the series are quarantined: a bad print on the latest
    bar is the most damaging error a report can repeat, so an extreme last move is held back until later bars
    confirm it. Confirmed level shifts are kept and reported as a warning.
    """
    if len(closes) < 11 or any(c <= 0 for c in closes):
        skipped = _fail(
            "price_spikes", "fewer than 11 positive closes: bad prints could not be checked", blocking=False
        )
        return skipped, _ok("large_moves", "not evaluated", blocking=False)
    scan = scan_spikes([math.log(c) for c in closes], threshold=threshold, implausible=IMPLAUSIBLE_LOG_MOVE)
    quarantined = tuple(sorted({*scan.bad_prints, *scan.unconfirmed}))
    spike_res = (
        _fail(
            "price_spikes",
            f"{len(scan.bad_prints)} bad print(s) and {len(scan.unconfirmed)} unconfirmed extreme move(s) quarantined",
            blocking=False,
            indices=quarantined,
        )
        if quarantined
        else _ok("price_spikes", blocking=False)
    )
    jump_res = (
        _fail(
            "large_moves",
            f"{len(scan.jumps)} confirmed extreme move(s) kept for review",
            blocking=False,
            indices=scan.jumps,
        )
        if scan.jumps
        else _ok("large_moves", blocking=False)
    )
    return spike_res, jump_res


def check_field_spikes(bars: Sequence[Bar], *, threshold: float = OUTLIER_Z) -> CheckResult:
    """Bad prints in highs, lows and volume (closes are scanned by :func:`check_price_spikes`).

    A wick 50 times the close or a volume a million times its neighbours is a bad tick even when the close is
    sound; such bars are quarantined like a bad close.
    """
    flagged: dict[str, set[int]] = {}
    for name, values in (("high", [b.high for b in bars]), ("low", [b.low for b in bars])):
        if len(values) < 11 or any(v <= 0 for v in values):
            continue
        scan = scan_spikes([math.log(v) for v in values], threshold=threshold, implausible=IMPLAUSIBLE_LOG_MOVE)
        hits = {*scan.bad_prints, *scan.unconfirmed}
        if hits:
            flagged[name] = hits
    volumes = [b.volume for b in bars]
    if len(volumes) >= 11 and all(v is not None and v > 0 for v in volumes):
        # Volume bursts of 10x are ordinary; only a bar 100x away from the series' median volume is a bad tick.
        logs = [math.log(v) for v in volumes if v is not None]
        mid = statistics.median(logs)
        hits = {i for i, v in enumerate(logs) if abs(v - mid) > 2 * IMPLAUSIBLE_LOG_MOVE}
        if hits:
            flagged["volume"] = hits
    indices = tuple(sorted(set().union(*flagged.values()))) if flagged else ()
    if indices:
        detail = ", ".join(f"{name}: {len(idx)}" for name, idx in sorted(flagged.items()))
        return _fail(
            "field_spikes", f"bad high/low/volume prints quarantined ({detail})", blocking=False, indices=indices
        )
    return _ok("field_spikes", blocking=False)


def check_range(values: Sequence[float | None], *, lo: float | None, hi: float | None, what: str) -> CheckResult:
    """Values outside the physically possible range for the series (e.g. negative stocks, a 400% yield)."""
    bad = tuple(
        i
        for i, v in enumerate(values)
        if v is not None and ((lo is not None and v < lo) or (hi is not None and v > hi))
    )
    if bad:
        return _fail(
            "value_range",
            f"{len(bad)} {what} value(s) outside [{lo}, {hi}] quarantined",
            blocking=False,
            indices=bad,
        )
    return _ok("value_range", f"{what} within [{lo}, {hi}]", blocking=False)


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
    if b.volume is not None and (not math.isfinite(b.volume) or b.volume < 0):
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


def check_interval_gaps(
    bars: Sequence[Bar], width: timedelta, calendar: TradingCalendar, *, max_gap_fraction: float = MAX_GAP_FRACTION
) -> CheckResult:
    """Intraday/weekly bars: bar starts missing between consecutive bars while the market is open."""
    if len(bars) < 2:
        return _ok("calendar_gaps", "fewer than two bars")
    missing: list[datetime] = []
    for prev, cur in itertools.pairwise(bars):
        t, steps = prev.ts + width, 0
        while t < cur.ts and steps < 10_000:
            if _trades_during(calendar, t, width):
                missing.append(t)
            t, steps = t + width, steps + 1
    expected = len(bars) + len(missing)
    detail = f"{len(missing)} of {expected} expected {width} bars missing ({calendar.name})"
    if missing:
        detail += f" (e.g. {', '.join(m.isoformat() for m in missing[:3])})"
        return _fail("calendar_gaps", detail, blocking=len(missing) / expected > max_gap_fraction)
    return _ok("calendar_gaps", detail)


def _trades_during(calendar: TradingCalendar, start: datetime, width: timedelta) -> bool:
    """Whether a bar starting at ``start`` should exist: the market is open at its start (intraday), or a session
    falls inside it (bars a day or wider, e.g. a weekly bar labelled Monday 00:00 UTC for an exchange)."""
    if width < timedelta(days=1):
        return calendar.is_open(start)
    first_close = calendar.closes_after(start.date() - timedelta(days=1), 1)[0]
    return _session_of_close(first_close, calendar) < (start + width).date()


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


def check_splits(
    bars: Sequence[Bar], split_dates: Sequence[date] = (), *, tolerance: float = 0.03, blocking: bool = True
) -> CheckResult:
    """Unexplained close-to-close jumps matching a split ratio mean the history is not split-adjusted.

    Blocking only when ``split_dates`` comes from a corporate-actions source: without one, a genuine -33% or x2 day
    cannot be told from a split, and the jump is flagged for review instead of blocking the series (D-033).
    """
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
            blocking=blocking,
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
    corporate_actions_known: bool = False,
    bar_width: timedelta | None = None,
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
    fields = check_field_spikes(bars)
    checks.extend([spikes, jumps, fields])
    checks.append(check_stale_run(closes, market_open_at_end=market_open_at_end))
    if calendar is not None and bars and bars[0].interval == "1d":
        checks.append(check_gaps(bars, calendar))
    elif calendar is not None and bar_width is not None:
        checks.append(check_interval_gaps(bars, bar_width, calendar))
    if reference is not None:
        checks.append(check_reconciliation(bars, reference))
    if split_dates is not None:
        checks.append(check_splits(bars, split_dates, blocking=corporate_actions_known))
    quarantined = tuple(sorted({*spikes.indices, *fields.indices}))
    usable = [b for i, b in enumerate(bars) if i not in set(quarantined)]
    report = DataQualityReport(subject, tuple(checks), quarantined, len(bars))
    return report, usable


def check_series(
    observations: Sequence[MacroObservation],
    *,
    subject: str,
    lo: float | None = None,
    hi: float | None = None,
    stock: bool = False,
) -> tuple[DataQualityReport, list[MacroObservation]]:
    """Macro/inventory series: order, duplicates, finite values, physical range and bad prints.

    Out-of-range values and extreme moves that revert are quarantined. For *flow* statistics (rates, indices,
    releases) an extreme latest value is kept with a warning: official releases are revised rather than mis-printed,
    and hiding a genuine record release would misstate the data. For *stock* levels (``stock=True``, e.g. inventories)
    changes are judged relative to the level, and an extreme latest move is held like a price until later releases
    confirm it: a stock cannot halve or jump 50% in one week, so such a value is a decimal or unit error (D-031).
    Missing values are allowed and counted.
    """
    values = [o.value for o in observations]
    missing = sum(1 for v in values if v is None)
    nonfinite = tuple(i for i, v in enumerate(values) if v is not None and not math.isfinite(v))
    range_check = check_range(values, lo=lo, hi=hi, what="series")
    present = [(i, v) for i, v in enumerate(values) if v is not None and i not in range_check.indices]
    relative = stock and all(v > 0 for _, v in present)
    scan = scan_spikes([math.log(v) if relative else v for _, v in present])
    reverted = tuple(present[j][0] for j in scan.bad_prints)
    unconfirmed = tuple(present[j][0] for j in scan.unconfirmed)
    held = unconfirmed if stock else ()
    edge = tuple(sorted({*(() if stock else unconfirmed), *(present[j][0] for j in scan.jumps)}))
    checks = [
        check_monotonic([o.ts for o in observations]),  # callers pass rows sorted; raw order: check_provider_order
        check_duplicates([o.ts for o in observations]),
        _fail("finite_values", f"{len(nonfinite)} non-finite value(s)", blocking=True, indices=nonfinite)
        if nonfinite
        else _ok("finite_values"),
        range_check,
        _fail(
            "outliers",
            f"{len(reverted)} reverting outlier(s) and {len(held)} unconfirmed stock move(s) quarantined",
            blocking=False,
            indices=tuple(sorted({*reverted, *held})),
        )
        if reverted or held
        else _ok("outliers", "evaluated" if scan.evaluated else "fewer than 11 values: not evaluated", blocking=False),
        _fail("large_moves", f"{len(edge)} extreme move(s) kept for review", blocking=False, indices=edge)
        if edge
        else _ok("large_moves", blocking=False),
        _ok("missing_values", f"{missing} observation(s) reported as missing by the source", blocking=False),
    ]
    quarantined = tuple(sorted({*range_check.indices, *reverted, *held}))
    usable = [o for i, o in enumerate(observations) if i not in set(quarantined)]
    return DataQualityReport(subject, tuple(checks), quarantined, len(observations)), usable


def check_open_interest_moves(reports: Sequence[PositioningReport]) -> tuple[CheckResult, CheckResult]:
    """Open interest is a stock of contracts: judged on relative weekly changes like an inventory (D-031).

    Returns (quarantine, warning): reverting outliers and unconfirmed extreme latest weeks (e.g. every field x10, which
    still balances) are quarantined; confirmed extreme moves are flagged. ``reports`` must be in date order.
    """
    levels = [r.open_interest for r in reports]
    if any(v <= 0 for v in levels):
        return _ok("open_interest_outliers", "not evaluated", blocking=False), _ok("large_moves", blocking=False)
    logs = [math.log(v) for v in levels]
    scan = scan_spikes(logs, implausible=IMPLAUSIBLE_LOG_MOVE)

    def implausible(i: int) -> bool:
        # Quarterly expiry routinely moves open interest by 20-40% in a week (and it rebuilds afterwards); only a
        # move beyond x3 / /3 is a corruption rather than a roll.
        neighbours = [abs(logs[i] - logs[j]) for j in (i - 1, i + 1) if 0 <= j < len(logs)]
        return max(neighbours, default=0.0) > OI_IMPLAUSIBLE_LOG_MOVE

    held = tuple(sorted(i for i in {*scan.bad_prints, *scan.unconfirmed} if implausible(i)))
    quarantine = (
        _fail("open_interest_outliers", f"{len(held)} week(s) quarantined", blocking=False, indices=held)
        if held
        else _ok("open_interest_outliers", blocking=False)
    )
    moves = (
        _fail("large_moves", f"{len(scan.jumps)} confirmed extreme move(s) kept", blocking=False, indices=scan.jumps)
        if scan.jumps
        else _ok("large_moves", blocking=False)
    )
    return quarantine, moves


def check_positioning(
    reports: Sequence[PositioningReport], *, subject: str, tolerance: float = 0.01
) -> DataQualityReport:
    """COT rows: one row per market/date, and *each* side (incl. spreading) must sum to open interest.

    Every reportable and non-reportable position is counted once long and once short, so both sums equal open
    interest exactly in the published data; a mismatch on either side means a corrupted or misparsed field (and the
    net positioning built from it would be wrong).
    """
    keys = [(r.market_code, r.as_of, r.report_type, r.futures_only) for r in reports]

    def unbalanced(side: str) -> tuple[int, ...]:
        out = []
        for i, r in enumerate(reports):
            total = sum(getattr(c, side) + (c.spreading or 0.0) for c in r.categories.values())
            if r.open_interest > 0 and abs(total / r.open_interest - 1) > tolerance:
                out.append(i)
        return tuple(out)

    empty = tuple(i for i, r in enumerate(reports) if r.open_interest <= 0)
    checks = [
        check_duplicates(keys),
        _fail("positive_open_interest", f"{len(empty)} row(s) with no open interest", blocking=True, indices=empty)
        if empty
        else _ok("positive_open_interest"),
    ]
    for side in ("long", "short"):
        bad = unbalanced(side)
        name = f"{side}_side_equals_open_interest"
        checks.append(
            _fail(
                name,
                f"{len(bad)} row(s) where {side} positions do not sum to open interest within {tolerance:.0%}",
                blocking=True,
                indices=bad,
            )
            if bad
            else _ok(name)
        )
    return DataQualityReport(subject, tuple(checks), (), len(reports))
