"""Scale-error checks for SEC company facts (D-036).

XBRL facts are entered with a scale (units, thousands, millions); a wrong one makes a value exactly a power of 1000 off.
Nothing in a filing marks such a value as wrong, so it is found from the rest of the same EDGAR response: the same
period in other filings, neighbouring periods, sums across periods, the same year's shorter year-to-date, exact
accounting identities, the company's own size, and the filing's own comparatives.
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from collections.abc import Sequence
from datetime import date, timedelta

from rd_data.schemas import FundamentalFact

#: How close (in log10) two filings' values must be to a ratio of 10^3, 10^6, ... to count as a scale error.
SCALE_TOLERANCE = 0.02
#: Neighbouring periods (same concept, unit and duration) a fact is compared with.
NEIGHBOURS = 4
#: Orders of magnitude from the neighbours beyond which a fact is flagged (genuine one-offs reach ~3 on real data).
FLAG_DISTANCE = 2.5
#: Flags (not holds) are raised only on the most recent filings, so years-old one-offs do not clutter every response.
FLAG_FILINGS = 4
#: Orders of magnitude from the neighbours beyond which a fact is held whatever the concept (none on real data).
HOLD_DISTANCE = 3.5
#: A concept whose facts usually sit within 10^0.15 (x1.4) of their neighbours is "stable": there, a fact about a power
#: of 1000 (+/- 10^0.5) from its neighbours is held, not just flagged.
STABLE_SPREAD = 0.15
NEAR_POWER = 0.5
#: How much closer (orders of magnitude) to its neighbours the reference filing must be to decide a disagreement.
DECISIVE_MARGIN = 1.0
#: Share of an accession's comparative facts that must be a power of 1000 from other filings to hold the whole filing.
FILING_SCALE_SHARE = 0.5
#: Relative tolerance for "January-June = January-March + April-June" (each value is rounded where it was reported).
ADDITIVITY_TOLERANCE = 0.01
_CURRENCY = re.compile(r"^[A-Z]{3}$")
#: Held facts needed (with FILING_SCALE_SHARE of the column) before a filing's whole column for one date is held.
COLUMN_MIN_HELD = 3
#: A currency fact larger than this multiple of the company's own total assets is impossible. On the recorded AMZN
#: facts the largest ratio is 5.2 (public float, dei) and 2.3 for us-gaap facts; derivative notionals at the largest
#: dealer banks stay below ~30x and are exempt anyway.
MAX_ASSETS_MULTIPLE = 100.0
#: Amounts that are not the company's own and legitimately exceed its balance sheet (derivative notionals, client
#: assets under management or custody), cumulative equity accounts (a long-loss-making company's accumulated deficit
#: and paid-in capital can dwarf what it has left) and the dei taxonomy (public float is a market value) are exempt.
_NOT_BOUNDED_BY_ASSETS = re.compile(
    r"Notional|UnderManagement|UnderCustody|Custod|Fiduciary|PaidInCapital|RetainedEarnings|AccumulatedDeficit"
    r"|StockholdersEquity|TreasuryStock|AccumulatedOtherComprehensive"
)
#: Below this total (in the reporting currency) a company's assets say little about the size of its line items.
MIN_ASSETS_FOR_BOUND = 1e7
#: A fact already flagged as unusual against its own history is held from this multiple of total assets.
FLAGGED_ASSETS_MULTIPLE = 10.0
#: Largest identity system (terms sharing identities, e.g. the 12-line income-tax table) solved by search.
MAX_IDENTITY_TERMS = 14
#: A group of facts this close to its own history (orders of magnitude) is taken as correctly scaled.
CONSISTENT_DISTANCE = 1.0
#: Exact US-GAAP identities, checked within one filing and period: total = sum of parts. Applied only where every term
#: is reported; on the recorded AMZN facts all 261 instances hold to within 1%.
IDENTITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("IncomeTaxExpenseBenefit", ("CurrentIncomeTaxExpenseBenefit", "DeferredIncomeTaxExpenseBenefit")),
    (
        "CurrentIncomeTaxExpenseBenefit",
        ("CurrentFederalTaxExpenseBenefit", "CurrentStateAndLocalTaxExpenseBenefit", "CurrentForeignTaxExpenseBenefit"),
    ),
    (
        "DeferredIncomeTaxExpenseBenefit",
        (
            "DeferredFederalIncomeTaxExpenseBenefit",
            "DeferredStateAndLocalIncomeTaxExpenseBenefit",
            "DeferredForeignIncomeTaxExpenseBenefit",
        ),
    ),
    (
        "IncomeTaxExpenseBenefit",
        (
            "FederalIncomeTaxExpenseBenefitContinuingOperations",
            "StateAndLocalIncomeTaxExpenseBenefitContinuingOperations",
            "ForeignIncomeTaxExpenseBenefitContinuingOperations",
        ),
    ),
    (
        "FederalIncomeTaxExpenseBenefitContinuingOperations",
        ("CurrentFederalTaxExpenseBenefit", "DeferredFederalIncomeTaxExpenseBenefit"),
    ),
    (
        "StateAndLocalIncomeTaxExpenseBenefitContinuingOperations",
        ("CurrentStateAndLocalTaxExpenseBenefit", "DeferredStateAndLocalIncomeTaxExpenseBenefit"),
    ),
    (
        "ForeignIncomeTaxExpenseBenefitContinuingOperations",
        ("CurrentForeignTaxExpenseBenefit", "DeferredForeignIncomeTaxExpenseBenefit"),
    ),
    (
        "IncomeTaxesPaidNet",
        (
            "IncomeTaxPaidFederalAfterRefundReceived",
            "IncomeTaxPaidStateAndLocalAfterRefundReceived",
            "IncomeTaxPaidForeignAfterRefundReceived",
        ),
    ),
    (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign",
        ),
    ),
    ("LiabilitiesAndStockholdersEquity", ("Assets",)),
)
#: A balance (instant fact) in a recent filing that moved more than 10^INSTANT_FLAG_STEP from the previous period end
#: is flagged: balances move smoothly (3 genuine moves that large in 17 years of AMZN balances).
INSTANT_FLAG_STEP = 2.0
#: Year-to-date ratio checks need this many other years, whose ratios sit within 10^PREFIX_SPREAD of their median.
PREFIX_HISTORY = 3
PREFIX_SPREAD = 0.3


def _scale_like(log_ratio: float, tolerance: float = SCALE_TOLERANCE) -> bool:
    k = round(log_ratio / 3)
    return k != 0 and abs(log_ratio - 3 * k) < tolerance


def _duration(f: FundamentalFact) -> int | None:
    return None if f.period_start is None else round((f.period_end - f.period_start).days / 91)


def scale_errors(records: list[FundamentalFact]) -> tuple[set[int], set[int]]:
    """XBRL scale errors (thousands entered as units, millions as thousands): (quarantine, flag). See D-036.

    Each fact is compared with the nearest NEIGHBOURS periods of the same concept, unit and duration. Measured on the
    recorded AMZN company facts (28,891 facts, 9,052 periods filed more than once): legitimate restatements reach a
    cross-filing ratio of 41 (a 20:1 split), and genuine one-offs sit up to ~10^3 from their neighbours, so:

    * held — within one period, filings a power of 1000 from the filing closest to the neighbours; a fact ~a power of
      1000 from its neighbours on a stable concept; any fact more than 10^HOLD_DISTANCE from its neighbours; and every
      fact of a filing whose comparatives are mostly a power of 1000 from the other filings (a mis-scaled filing);
    * held — facts that break "a period = its two halves" (e.g. January-June = January-March + April-June) when
      scaling one filing's facts in that sum by a power of 1000 makes it add up;
    * held — a year-to-date fact whose ratio to the same year's shorter year-to-date (from other filings: the year
      against its nine months, nine months against the half) is about a power of 1000 off the ratio other years show;
    * held — a held fact's siblings: the same filing's other facts of that concept and period end (a scale is set per
      table, so e.g. the trailing-twelve-month figure beside a mis-scaled quarter shares its error); and the whole
      column (one filing, one period end) once most of it is held;
    * flagged — any other fact more than 10^FLAG_DISTANCE from its neighbours (kept, for review).

    Whenever two filings are weighed against each other, neighbours reported by either of them are left out, so a
    mis-scaled filing never vouches for itself or against the other.
    """
    logs = [math.log10(abs(f.value)) if f.value else None for f in records]
    by_period: dict[_PeriodKey, list[int]] = {}
    for i, f in enumerate(records):
        if logs[i] is not None:
            by_period.setdefault((f.concept, f.unit, _duration(f), f.period_end), []).append(i)
    neighbours = _Neighbours(records, logs, by_period)
    distance = {
        i: d
        for idx in by_period.values()
        for i in idx
        if (d := neighbours.distance(i, frozenset({records[i].accession}))) is not None
    }
    held, flagged = _fact_holds(records, distance)
    held |= _filing_disagreements(records, logs, by_period, neighbours)
    held |= _additivity_errors(records, neighbours)
    held |= _prefix_errors(records, logs, neighbours)
    held |= _identity_errors(records, held, neighbours)
    recent = _recent_accessions(records)
    for i, f in enumerate(records):
        step = neighbours.previous_step(i) if f.period_start is None and f.accession in recent else None
        if step is not None and step > INSTANT_FLAG_STEP:
            flagged.add(i)
    held |= _beyond_assets(records, logs, held, flagged)
    tables = {(records[i].concept, records[i].unit, records[i].accession, records[i].period_end) for i in held}
    held |= {i for i, f in enumerate(records) if (f.concept, f.unit, f.accession, f.period_end) in tables}
    # A filing's column (its facts for one period end) that is mostly held is held whole: the scale was set for the
    # column, and the facts that slipped through the other checks share it.
    column: dict[tuple[str, date], list[int]] = {}
    for i, f in enumerate(records):
        column.setdefault((f.accession, f.period_end), []).append(i)
    for idx in column.values():
        hits = sum(i in held for i in idx)
        if hits >= COLUMN_MIN_HELD and hits / len(idx) >= FILING_SCALE_SHARE:
            held.update(idx)
    held |= _identity_errors(records, held, neighbours)  # tables that add up around a held term share its scale
    return held, flagged - held


_SeriesKey = tuple[str, str, int | None]
_PeriodKey = tuple[str, str, int | None, date]


class _Neighbours:
    """Orders of magnitude between a fact and the nearest NEIGHBOURS periods of its series (same concept, unit and
    duration), leaving out the values reported by the given filings."""

    def __init__(
        self, records: list[FundamentalFact], logs: list[float | None], by_period: dict[_PeriodKey, list[int]]
    ) -> None:
        self.records = records
        self.logs = logs
        self.series: dict[_SeriesKey, dict[date, list[tuple[str, float]]]] = {}
        for (concept, unit, dur, end), idx in by_period.items():
            self.series.setdefault((concept, unit, dur), {})[end] = [
                (records[i].accession, v) for i in idx if (v := logs[i]) is not None
            ]
        self._nearest: dict[tuple[_SeriesKey, date], list[date]] = {}

    def distance(self, i: int, exclude: frozenset[str]) -> float | None:
        f = self.records[i]
        value = self.logs[i]
        key = (f.concept, f.unit, _duration(f))
        periods = self.series.get(key, {})
        nearest = self._nearest.get((key, f.period_end))
        if nearest is None:
            nearest = sorted((e for e in periods if e != f.period_end), key=lambda e: abs((e - f.period_end).days))
            self._nearest[(key, f.period_end)] = nearest
        levels: list[float] = []
        for e in nearest:
            values = [v for acc, v in periods[e] if acc not in exclude]
            if values:
                levels.append(statistics.median(values))
            if len(levels) == NEIGHBOURS:
                break
        if len(levels) < 2 or value is None:
            return None
        return abs(value - statistics.median(levels))

    def previous_step(self, i: int) -> float | None:
        """Orders of magnitude between a fact and the previous period end of its series, as other filings report it."""
        f = self.records[i]
        value = self.logs[i]
        periods = self.series.get((f.concept, f.unit, _duration(f)), {})
        earlier = sorted((e for e in periods if e < f.period_end), reverse=True)
        for e in earlier:
            values = [v for acc, v in periods[e] if acc != f.accession]
            if values and value is not None:
                return abs(value - statistics.median(values))
        return None

    def mean_distance(self, part: Sequence[int], exclude: frozenset[str]) -> float | None:
        found = [d for i in part if (d := self.distance(i, exclude)) is not None]
        return statistics.mean(found) if len(found) == len(part) else None


def _recent_accessions(records: list[FundamentalFact]) -> set[str]:
    """The FLAG_FILINGS most recently filed accessions: the filings reports actually use."""
    filed_on: dict[str, date] = {}
    for f in records:
        filed_on[f.accession] = max(filed_on.get(f.accession, f.filed), f.filed)
    return set(sorted(filed_on, key=filed_on.__getitem__)[-FLAG_FILINGS:])


def _beyond_assets(
    records: list[FundamentalFact], logs: list[float | None], held: set[int], flagged: set[int]
) -> set[int]:
    """Currency facts too large for the company: more than MAX_ASSETS_MULTIPLE times its total assets (same currency),
    or FLAGGED_ASSETS_MULTIPLE times when the fact is also unusual against its own history. Total assets is the median
    of the NEIGHBOURS nearest period ends, each as reported by other filings where there are any, leaving out facts
    already held: one mis-scaled filing cannot shrink the yardstick and hold the newest genuine figures."""
    assets: dict[tuple[str, date], list[tuple[str, float]]] = {}
    for i, f in enumerate(records):
        if f.concept == "Assets" and f.period_start is None and i not in held and (v := logs[i]) is not None:
            assets.setdefault((f.unit, f.period_end), []).append((f.accession, v))
    ends: dict[str, list[date]] = {}
    for unit, end in assets:
        ends.setdefault(unit, []).append(end)
    floor = math.log10(MIN_ASSETS_FOR_BOUND)
    out: set[int] = set()
    for i, f in enumerate(records):
        value = logs[i]
        if value is None or f.unit not in ends or f.taxonomy == "dei" or _NOT_BOUNDED_BY_ASSETS.search(f.concept):
            continue
        nearest = sorted(ends[f.unit], key=lambda e: abs((e - f.period_end).days))[:NEIGHBOURS]
        levels = []
        for e in nearest:
            reported = assets[(f.unit, e)]
            others = [v for acc, v in reported if acc != f.accession] or [v for _, v in reported]
            levels.append(statistics.median(others))
        reference = statistics.median(levels)
        if reference < floor:
            continue
        limit = math.log10(FLAGGED_ASSETS_MULTIPLE if i in flagged else MAX_ASSETS_MULTIPLE)
        if value - reference > limit:
            out.add(i)
    return out


def _identity_errors(records: list[FundamentalFact], held: set[int], neighbours: _Neighbours) -> set[int]:
    """IDENTITIES within one filing and period, solved together: identities that share terms form one system. A broken
    system is repaired by rescaling a group of its terms by a power of 1000 so that every identity holds again (each
    repaired identity needing every term). Scaling a group up is the same as scaling the rest down, so the two sides
    are weighed against their own earlier periods (_blame); when that does not decide, the whole system is held.
    An identity that holds as reported but has a held term is held whole: its terms were tabled, and mis-scaled,
    together."""
    wanted = {c for total, parts in IDENTITIES for c in (total, *parts)}
    index: dict[tuple[str, str, date | None, date], dict[str, int]] = {}
    for i, f in enumerate(records):
        if f.concept in wanted and _CURRENCY.match(f.unit):
            index.setdefault((f.accession, f.unit, f.period_start, f.period_end), {})[f.concept] = i
    out: set[int] = set()
    consistent: list[list[int]] = []
    for terms in index.values():
        present = [
            [terms[total], *(terms[p] for p in parts)]
            for total, parts in IDENTITIES
            if total in terms and all(p in terms for p in parts)
        ]
        broken = [idx for idx in present if not _balances([records[k].value for k in idx])]
        consistent += [idx for idx in present if idx not in broken]
        for system in _systems(present):
            if any(idx in broken for idx in system):
                out |= _repair(records, system, [idx for idx in system if idx in broken], neighbours)
    known = held | out
    for _ in range(len(IDENTITIES)):  # spread through nested identities (total -> current -> current federal)
        spread = {k for idx in consistent if any(k2 in known for k2 in idx) for k in idx} - known
        if not spread:
            break
        known |= spread
        out |= spread
    return out


def _systems(identities: list[list[int]]) -> list[list[list[int]]]:
    """Group identities that share terms."""
    systems: list[list[list[int]]] = []
    for idx in identities:
        joined = [s for s in systems if any(set(idx) & set(other) for other in s)]
        merged = [idx, *(other for s in joined for other in s)]
        systems = [s for s in systems if s not in joined] + [merged]
    return systems


def _repair(
    records: list[FundamentalFact], system: list[list[int]], broken: list[list[int]], neighbours: _Neighbours
) -> set[int]:
    terms = sorted({k for idx in system for k in idx})
    if len(terms) > MAX_IDENTITY_TERMS:
        return set(terms)  # too large to solve here: hold rather than guess
    wrong: set[int] = set()
    undecided = False
    for size in range(1, len(terms) // 2 + 1):
        for group in itertools.combinations(terms, size):
            rest = [k for k in terms if k not in group]
            if size * 2 == len(terms) and rest < list(group):
                continue  # an even split is the same pair seen from the other side
            if not any(_repaired(records, system, broken, set(group), 1000.0**k) for k in (-2, -1, 1, 2)):
                continue
            blamed = _blame(neighbours, list(group), rest, frozenset({records[terms[0]].accession}))
            if blamed is None:
                undecided = True
            else:
                wrong |= set(blamed)
    if undecided:
        return set(terms)
    return wrong


def _repaired(
    records: list[FundamentalFact], system: list[list[int]], broken: list[list[int]], group: set[int], factor: float
) -> bool:
    def value(k: int) -> float:
        return records[k].value * factor if k in group else records[k].value

    return all((_balances_with_every_term if idx in broken else _balances)([value(k) for k in idx]) for idx in system)


def _blame(neighbours: _Neighbours, one: list[int], other: list[int], own: frozenset[str]) -> list[int] | None:
    """Which of two groups is mis-scaled, judged by how far each sits from its own earlier periods (as other filings
    report them; members with no history are left out of the mean). A group with no history at all is blamed when the
    other side sits well within its history (under 10^CONSISTENT_DISTANCE), and cleared when the other side is far."""
    a, b = _known_mean(neighbours, one, own), _known_mean(neighbours, other, own)
    if a is not None and b is not None:
        if abs(a - b) >= DECISIVE_MARGIN:
            return one if a > b else other
        return None
    if a is None and b is None:
        return None
    known, unknown, score = (one, other, a) if a is not None else (other, one, b)
    assert score is not None  # noqa: S101 - one side is known here
    if score < CONSISTENT_DISTANCE:
        return unknown
    if score > FLAG_DISTANCE:
        return known
    return None


def _known_mean(neighbours: _Neighbours, group: list[int], own: frozenset[str]) -> float | None:
    found = [d for i in group if (d := neighbours.distance(i, own)) is not None]
    return statistics.mean(found) if found else None


def _balances(values: Sequence[float]) -> bool:
    total, *parts = values
    return abs(total - sum(parts)) <= ADDITIVITY_TOLERANCE * max(abs(v) for v in values)


def _balances_with_every_term(values: Sequence[float]) -> bool:
    """The identity holds and needs every term: rescaling one term into insignificance proves nothing."""
    return _balances(values) and not any(
        _balances([0.0 if j == k else v for j, v in enumerate(values)]) for k in range(len(values))
    )


def _fact_holds(records: list[FundamentalFact], distance: dict[int, float]) -> tuple[set[int], set[int]]:
    """Per fact: held when far beyond what its series ever does, flagged (recent filings only) when unusual."""
    spread: dict[_SeriesKey, list[float]] = {}
    for i, d in distance.items():
        f = records[i]
        spread.setdefault((f.concept, f.unit, _duration(f)), []).append(d)
    typical = {k: statistics.median(v) for k, v in spread.items()}
    recent = _recent_accessions(records)
    held: set[int] = set()
    flagged: set[int] = set()
    for i, d in distance.items():
        f = records[i]
        stable = typical[(f.concept, f.unit, _duration(f))] < STABLE_SPREAD
        if d > HOLD_DISTANCE or (d > FLAG_DISTANCE and stable and _scale_like(d, NEAR_POWER)):
            held.add(i)
        elif d > FLAG_DISTANCE and f.accession in recent:
            flagged.add(i)  # history is not re-flagged on every response: only the filings reports actually use
    return held, flagged


def _clear_pick(
    neighbours: _Neighbours, groups: Sequence[Sequence[int]], exclude: frozenset[str], *, nearest: bool
) -> int | None:
    """Index of the group of facts clearly nearest (or furthest from) its neighbouring periods, by DECISIVE_MARGIN,
    judged without the disputing filings' own values; None when the neighbours do not decide."""
    scores = [neighbours.mean_distance(group, exclude) for group in groups]
    known = [d for d in scores if d is not None]
    if len(known) < len(scores):
        return None
    order = sorted(range(len(groups)), key=lambda g: known[g] if nearest else -known[g])
    if len(order) == 1 or abs(known[order[1]] - known[order[0]]) >= DECISIVE_MARGIN:
        return order[0]
    return None


def _filing_disagreements(
    records: list[FundamentalFact],
    logs: list[float | None],
    by_period: dict[_PeriodKey, list[int]],
    neighbours: _Neighbours,
) -> set[int]:
    """Periods filed more than once: filings a power of 1000 from the reference filing (the one closest to its
    independent neighbours) are held. When the neighbours do not clearly side with one filing, the disagreeing
    filings' facts of that concept are all held (a guess could pick the wrong scale). A filing whose comparatives
    mostly disagree by a power of 1000 is held whole."""
    held: set[int] = set()
    off_by_accession: dict[str, list[bool]] = {}
    ambiguous: set[tuple[str, str, str]] = set()
    for idx in by_period.values():
        accessions = frozenset(records[i].accession for i in idx)
        if len(accessions) < 2:
            continue
        values = [v for i in idx if (v := logs[i]) is not None]
        if not any(_scale_like(a - b) for a in values for b in values):
            continue  # filings agree up to restatements: nothing to decide
        # Group the facts by scale; the group clearly nearest the neighbouring periods is kept.
        groups: list[list[int]] = []
        for i in idx:
            log_i = logs[i]
            group = next((g for g in groups if (v := logs[g[0]]) is not None and log_i is not None
                          and abs(log_i - v) < 1.5), None)  # fmt: skip
            if group is None:
                groups.append([i])
            else:
                group.append(i)
        keep = _clear_pick(neighbours, groups, accessions, nearest=True)
        if keep is None:
            ambiguous.update((records[i].concept, records[i].unit, records[i].accession) for i in idx)
            continue
        keep_log = statistics.median(v for i in groups[keep] if (v := logs[i]) is not None)
        wrong = [i for i in idx if (v := logs[i]) is not None and _scale_like(v - keep_log)]
        held.update(wrong)
        for i in idx:
            off_by_accession.setdefault(records[i].accession, []).append(i in wrong)
    held.update(i for i, f in enumerate(records) if (f.concept, f.unit, f.accession) in ambiguous)
    bad_filings = {
        acc for acc, offs in off_by_accession.items() if sum(offs) >= 3 and sum(offs) / len(offs) >= FILING_SCALE_SHARE
    }
    held.update(i for i, f in enumerate(records) if f.accession in bad_filings)
    return held


def _additivity_errors(records: list[FundamentalFact], neighbours: _Neighbours) -> set[int]:
    """Currency flows add up across periods: a fact for a period equals the facts for its two consecutive parts
    (year-to-date = earlier year-to-date + latest quarter). The parts usually come from different filings, so a
    mis-scaled filing breaks the sum even when the newest values have no other filing to be compared with."""
    spans: dict[tuple[str, str, date, date], list[int]] = {}
    for i, f in enumerate(records):
        if f.period_start is not None and _CURRENCY.match(f.unit):
            spans.setdefault((f.concept, f.unit, f.period_start, f.period_end), []).append(i)
    starts: dict[tuple[str, str, date], list[date]] = {}
    for concept, unit, start, end in spans:
        starts.setdefault((concept, unit, end), []).append(start)
    held: set[int] = set()
    for (concept, unit, start, end), whole in spans.items():
        for split in starts[(concept, unit, end)]:
            first = spans.get((concept, unit, start, split - timedelta(days=1)))
            if split > start and first:
                for combo in itertools.product(whole, first, spans[(concept, unit, split, end)]):
                    held |= _misscaled_in(records, combo, neighbours)
    return held


def _prefix_errors(records: list[FundamentalFact], logs: list[float | None], neighbours: _Neighbours) -> set[int]:
    """Year-to-date facts against the same year's previous year-to-date (same start, earlier end; the full year
    against its nine months). Their ratio follows the business (a full year is usually about 4/3 of nine months) and
    is checked against the ratio the concept shows in other years, so the newest annual figure is checked against
    numbers from the same year rather than against years-old annual values only."""
    spans: dict[tuple[str, str, date, date], list[int]] = {}
    for i, f in enumerate(records):
        if f.period_start is not None and logs[i] is not None:
            spans.setdefault((f.concept, f.unit, f.period_start, f.period_end), []).append(i)
    ends: dict[tuple[str, str, date], list[date]] = {}
    for concept, unit, start, end in spans:
        ends.setdefault((concept, unit, start), []).append(end)
    chain: list[tuple[tuple[str, str, date, date], tuple[str, str, date, date]]] = []
    history: dict[tuple[str, str, int, int], list[tuple[date, float]]] = {}
    for (concept, unit, start), dates in ends.items():
        dates.sort()
        for shorter, longer in itertools.pairwise(dates):
            whole, part = (concept, unit, start, longer), (concept, unit, start, shorter)
            chain.append((whole, part))
            key = (concept, unit, _quarters(start, longer), _quarters(start, shorter))
            ratio = statistics.median(_known(logs, spans[whole])) - statistics.median(_known(logs, spans[part]))
            history.setdefault(key, []).append((start, ratio))
    held: set[int] = set()
    for whole, part in chain:
        concept, unit, start, longer = whole
        others = [
            r for s, r in history[(concept, unit, _quarters(start, longer), _quarters(start, part[3]))] if s != start
        ]
        if len(others) < PREFIX_HISTORY:
            continue
        expected = statistics.median(others)
        if statistics.median(abs(r - expected) for r in others) > PREFIX_SPREAD:
            continue  # the ratio swings from year to year (e.g. financing flows): no usual ratio to judge by
        for i in spans[whole]:
            prefix = [k for k in spans[part] if records[k].accession != records[i].accession]
            log_i = logs[i]
            if not prefix or log_i is None:
                continue  # one filing alone cannot show its own scale error
            off = log_i - statistics.median(_known(logs, prefix)) - expected
            if abs(off) > FLAG_DISTANCE and _scale_like(off, NEAR_POWER):
                exclude = frozenset({records[i].accession, *(records[k].accession for k in prefix)})
                # the prefix is judged as a "whole" in its own pair; here only this fact can be held, unless the
                # neighbours clearly blame the prefix
                if _clear_pick(neighbours, [[i], prefix], exclude, nearest=False) != 1:
                    held.add(i)
    return held


def _quarters(start: date, end: date) -> int:
    return round((end - start).days / 91)


def _known(logs: list[float | None], idx: Sequence[int]) -> list[float]:
    return [v for i in idx if (v := logs[i]) is not None]


def _adds_up(values: Sequence[float]) -> bool:
    whole, first, second = values
    return abs(whole - first - second) <= ADDITIVITY_TOLERANCE * max(abs(v) for v in values)


def _adds_up_with_every_term(values: Sequence[float]) -> bool:
    """The sum holds and needs all three terms: rescaling one term into insignificance proves nothing."""
    return _adds_up(values) and not any(
        _adds_up([0.0 if j == k else v for j, v in enumerate(values)]) for k in range(3)
    )


def _misscaled_in(records: list[FundamentalFact], combo: tuple[int, ...], neighbours: _Neighbours) -> set[int]:
    """The facts of one ``whole = first + second`` sum that a power-of-1000 scale error in one filing explains."""
    values = [records[i].value for i in combo]
    accessions = frozenset(records[i].accession for i in combo)
    if len(accessions) < 2 or _adds_up(values):
        return set()  # one filing alone cannot show its own scale error; a sum that adds up shows none
    fixes: list[list[int]] = []
    for acc in sorted(accessions):
        part = [i for i in combo if records[i].accession == acc]
        for k in (-2, -1, 1, 2):
            scaled = [v * 1000.0**k if records[i].accession == acc else v for i, v in zip(combo, values, strict=True)]
            if _adds_up_with_every_term(scaled):
                fixes.append(part)
    if not fixes:
        return set()  # not a scale error (e.g. a restatement): left to the other checks
    # Scaling one filing up is the same as scaling the other down: the side far from its neighbouring periods is the
    # mis-scaled one. Without a clear answer every candidate is held (a guess could serve the wrong scale).
    wrong = _clear_pick(neighbours, fixes, accessions, nearest=False)
    return set(fixes[wrong]) if wrong is not None else {i for part in fixes for i in part}
