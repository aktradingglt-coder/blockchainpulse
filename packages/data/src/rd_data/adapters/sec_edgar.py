"""SEC EDGAR: filings (submissions API), XBRL company facts and the ticker→CIK map (PROMPT §5.2).

Fair access: the SEC caps clients at 10 requests/second and requires a User-Agent naming the company and a contact
email (env ``SEC_EDGAR_USER_AGENT``). We stay at 8 r/s shared across workers. Fundamentals are point-in-time: each
fact carries its ``filed`` date and is usable only from then on (never by period end alone).
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

import orjson

from rd_core.clock import ensure_utc
from rd_data.adapters.base import (
    BaseAdapter,
    DataRequest,
    FreshnessSpec,
    ParseResult,
    RawPayload,
    RequestKind,
    require_param,
)
from rd_data.errors import InvalidArgs, NotFound, ProviderDown
from rd_data.licence import public_domain
from rd_data.quality import (
    CheckResult,
    DataQualityReport,
    check_duplicates,
    check_provider_order,
    with_schema_rejections,
)
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, DataType, Filing, FundamentalFact, Instrument

_CIK = re.compile(r"^\d{1,10}$")
_UA = re.compile(r"^\S.{2,}\s\S+@\S+\.\S+$")
#: Forms whose facts are used for point-in-time fundamentals.
FUNDAMENTAL_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F"})


def cik10(value: str | int) -> str:
    text = str(value).strip()
    if not _CIK.match(text):
        raise InvalidArgs(f"sec: invalid CIK {value!r}", provider="sec_edgar")
    return text.zfill(10)


class SecEdgarAdapter(BaseAdapter):
    name: ClassVar[str] = "sec_edgar"
    asset_classes = frozenset({AssetClass.EQUITY})
    licence = public_domain("Source: U.S. Securities and Exchange Commission (EDGAR)")
    rate_limit = RateLimit(per_second=8)
    allowed_hosts = frozenset({"data.sec.gov", "www.sec.gov"})
    supported = frozenset({RequestKind.FILINGS, RequestKind.COMPANY_FACTS, RequestKind.INSTRUMENTS})
    cache_ttl_s: ClassVar[Mapping[RequestKind, int]] = {
        RequestKind.FILINGS: 15 * 60,
        RequestKind.COMPANY_FACTS: 6 * 3600,
        RequestKind.INSTRUMENTS: 24 * 3600,
    }
    params_allowed: ClassVar[Mapping[RequestKind, frozenset[str]]] = {
        RequestKind.FILINGS: frozenset({"cik", "forms"}),
        RequestKind.COMPANY_FACTS: frozenset({"cik", "concepts"}),
        RequestKind.INSTRUMENTS: frozenset(),
    }
    credential_setting = "sec_edgar_user_agent"

    def user_agent(self) -> str:
        ua = self.ctx.settings.sec_edgar_user_agent
        return ua or ""

    def _validate(self, request: DataRequest) -> None:
        ua = self.ctx.settings.sec_edgar_user_agent
        if not ua or not _UA.match(ua):
            raise ProviderDown(
                "sec: SEC_EDGAR_USER_AGENT must be set to 'Company Name contact@domain' (SEC fair access policy)",
                provider=self.name,
            )
        if request.kind in (RequestKind.FILINGS, RequestKind.COMPANY_FACTS):
            cik = require_param(request, "cik", self.name)
            if isinstance(cik, (bool, float)):
                raise InvalidArgs("sec: cik must be a string or integer", provider=self.name)
            cik10(cik)

    async def _download(self, request: DataRequest) -> RawPayload:
        if request.kind is RequestKind.INSTRUMENTS:
            result = await self.http.get("https://www.sec.gov/files/company_tickers.json")
        elif request.kind is RequestKind.FILINGS:
            result = await self.http.get(
                f"https://data.sec.gov/submissions/CIK{cik10(str(request.params['cik']))}.json"
            )
        else:
            result = await self.http.get(
                f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(str(request.params['cik']))}.json"
            )
        return RawPayload.from_results([result], self.ctx.clock.now())

    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        data = orjson.loads(raw.bodies[0])
        shape = {RequestKind.FILINGS: "filings", RequestKind.COMPANY_FACTS: "facts"}.get(request.kind)
        if not isinstance(data, dict) or (shape is not None and (shape not in data or "cik" not in data)):
            # Not an EDGAR payload of the requested kind (e.g. {} or another document): an outage, never "not found"
            raise ProviderDown(
                f"sec: malformed response (expected an EDGAR {shape or 'ticker'} payload)", provider=self.name
            )
        if request.kind is RequestKind.INSTRUMENTS:
            return self._parse_tickers(data)
        if request.kind is RequestKind.FILINGS:
            return self._parse_filings(request, data)
        return self._parse_facts(request, data)

    # ------------------------------------------------------------------------------------------ tickers

    def _parse_tickers(self, data: Mapping[str, Any]) -> ParseResult:
        records: list[Instrument] = []
        rejected = 0
        for row in data.values():
            try:
                records.append(
                    Instrument(
                        symbol=str(row["ticker"]).upper(),
                        asset_class=AssetClass.EQUITY,
                        name=str(row["title"]),
                        exchange=None,
                        currency="USD",
                        provider_symbol=cik10(row["cik_str"]),
                    )
                )
            except (KeyError, ValueError, InvalidArgs):
                rejected += 1
        quality = with_schema_rejections(DataQualityReport("sec:company_tickers", (), (), len(records)), rejected)
        return ParseResult(tuple(records), quality, None, None)

    # ------------------------------------------------------------------------------------------ filings

    def _subject_check(self, request: DataRequest, data: Mapping[str, Any]) -> CheckResult:
        """The response is about the company we asked for (EDGAR keys files by CIK; a mismatch is a wrong body)."""
        asked = cik10(str(request.params["cik"]))
        got = data.get("cik")
        try:
            ok = got is not None and cik10(str(got)) == asked  # EDGAR always names the company; absent = wrong body
        except InvalidArgs:
            ok = False
        return CheckResult(
            "subject_matches_request",
            passed=ok,
            blocking=True,
            detail="ok" if ok else f"response is for CIK {got}, request was CIK {asked}",
        )

    def _parse_filings(self, request: DataRequest, data: Mapping[str, Any]) -> ParseResult:
        subject = self._subject_check(request, data)
        cik = cik10(str(request.params["cik"]))
        latest_allowed = self.ctx.clock.now().date() + timedelta(days=1)
        company = str(data.get("name", ""))
        recent = data.get("filings", {}).get("recent", {})
        forms = request.params.get("forms")
        wanted = {f.strip().upper() for f in str(forms).split(",")} if forms else None
        n = len(recent.get("accessionNumber", []))
        records: list[Filing] = []
        raw_dates: list[date] = []
        rejected = 0
        for i in range(n):
            filed = _date_or_none(recent.get("filingDate", []), i)
            if filed is None or filed > latest_allowed:
                rejected += 1  # missing, unparseable or future-dated filing date
                continue
            raw_dates.append(filed)
            try:
                form = str(recent["form"][i])
                if wanted and form.upper() not in wanted:
                    continue
                accession = str(recent["accessionNumber"][i])
                primary = str(recent["primaryDocument"][i]) or None
                accepted_raw = str(recent.get("acceptanceDateTime", [""] * n)[i] or "")
                report_raw = str(recent.get("reportDate", [""] * n)[i] or "")
                records.append(
                    Filing(
                        cik=cik,
                        company=company,
                        form=form,
                        filed=filed,
                        accepted_at=_parse_accepted(accepted_raw),
                        accession=accession,
                        primary_document=primary,
                        report_date=date.fromisoformat(report_raw) if report_raw else None,
                        url=_filing_url(cik, accession, primary),
                    )
                )
            except (KeyError, IndexError, ValueError):
                rejected += 1
        if not records and not rejected:
            raise NotFound(
                f"sec: no filings for CIK {cik}" + (f" with forms {sorted(wanted)}" if wanted else ""),
                provider=self.name,
            )
        records.sort(key=lambda f: (f.filed, f.accession))
        quality = with_schema_rejections(
            DataQualityReport(
                f"sec:filings:{cik}",
                (
                    subject,
                    check_provider_order(raw_dates, descending=True),  # EDGAR lists the newest filing first
                    check_duplicates([f.accession for f in records]),
                ),
                (),
                len(records),
            ),
            rejected,
        )
        as_of = records[-1].filed if records else None
        return ParseResult(tuple(records), quality, as_of, None, source_tz="America/New_York")

    # ------------------------------------------------------------------------------------------ facts

    def _parse_facts(self, request: DataRequest, data: Mapping[str, Any]) -> ParseResult:
        subject = self._subject_check(request, data)
        cik = cik10(str(request.params["cik"]))
        latest_allowed = self.ctx.clock.now().date() + timedelta(days=1)
        concepts_param = request.params.get("concepts")
        wanted = {c.strip() for c in str(concepts_param).split(",")} if concepts_param else None
        records: list[FundamentalFact] = []
        rejected = 0
        impossible = 0
        for taxonomy, concepts in (data.get("facts") or {}).items():
            for concept, body in concepts.items():
                if wanted and concept not in wanted:
                    continue
                for unit, facts in (body.get("units") or {}).items():
                    for fact in facts:
                        form = str(fact.get("form", ""))
                        if form not in FUNDAMENTAL_FORMS:
                            continue
                        filed = _date_or_none([fact.get("filed")], 0)
                        end = _date_or_none([fact.get("end")], 0)
                        if filed is None or end is None or filed > latest_allowed or filed < end:
                            # filed in the future, or before the period it reports ended: not point-in-time correct.
                            # One such fact is set aside (reported) rather than blocking the company's other facts.
                            impossible += 1
                            continue
                        try:
                            records.append(
                                FundamentalFact(
                                    cik=cik,
                                    concept=concept,
                                    taxonomy=taxonomy,
                                    unit=unit,
                                    value=float(fact["val"]),
                                    period_start=date.fromisoformat(fact["start"]) if fact.get("start") else None,
                                    period_end=date.fromisoformat(fact["end"]),
                                    fiscal_year=fact.get("fy"),
                                    fiscal_period=fact.get("fp"),
                                    form=form,
                                    filed=date.fromisoformat(fact["filed"]),
                                    accession=str(fact["accn"]),
                                    frame=fact.get("frame"),
                                )
                            )
                        except (KeyError, ValueError, TypeError):
                            rejected += 1
        if not records and not rejected:
            raise NotFound(f"sec: no XBRL facts for CIK {cik}", provider=self.name)
        records.sort(key=lambda f: (f.filed, f.concept, f.period_end, f.unit, f.accession))
        keys = [(f.concept, f.unit, f.period_start, f.period_end, f.accession, f.frame) for f in records]
        held, flagged = _scale_errors(records)
        checks = (
            subject,
            check_duplicates(keys),
            CheckResult(
                "scale_consistency",
                passed=not held,
                blocking=False,
                detail=f"{len(held)} fact(s) with a likely scale error (x1000) quarantined" if held else "ok",
                indices=tuple(sorted(held)),
            ),
            CheckResult(
                "point_in_time",
                passed=not impossible,
                blocking=False,
                detail=f"{impossible} fact(s) undated, filed in the future or before their period ended, left out"
                if impossible
                else "ok",
            ),
            CheckResult(
                "scale_outliers",
                passed=not flagged,
                blocking=False,
                detail=f"{len(flagged)} recent fact(s) over 300x from their neighbouring periods, kept for review"
                if flagged
                else "ok",
                indices=tuple(sorted(flagged)),
            ),
        )
        quality = with_schema_rejections(
            DataQualityReport(f"sec:facts:{cik}", checks, tuple(sorted(held)), len(records)), rejected
        )
        usable = [f for i, f in enumerate(records) if i not in held]
        as_of = usable[-1].filed if usable else None
        return ParseResult(
            tuple(usable),
            quality,
            as_of,
            FreshnessSpec(DataType.FUNDAMENTALS),
            source_tz="America/New_York",
            quarantined=tuple(f for i, f in enumerate(records) if i in held),
            notes=("Point-in-time by filing date: a fact is usable only on or after its 'filed' date.",),
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [
            DataRequest(kind=RequestKind.INSTRUMENTS),
            DataRequest(kind=RequestKind.FILINGS, params={"cik": "320193"}),
            DataRequest(kind=RequestKind.COMPANY_FACTS, params={"cik": "1018724"}),
        ]


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


def _scale_like(log_ratio: float, tolerance: float = SCALE_TOLERANCE) -> bool:
    k = round(log_ratio / 3)
    return k != 0 and abs(log_ratio - 3 * k) < tolerance


def _duration(f: FundamentalFact) -> int | None:
    return None if f.period_start is None else round((f.period_end - f.period_start).days / 91)


def _scale_errors(records: list[FundamentalFact]) -> tuple[set[int], set[int]]:
    """XBRL scale errors (thousands entered as units, millions as thousands): (quarantine, flag). See D-036.

    Each fact is compared with the nearest NEIGHBOURS periods of the same concept, unit and duration. Measured on the
    recorded AMZN company facts (28,891 facts, 9,052 periods filed more than once): legitimate restatements reach a
    cross-filing ratio of 41 (a 20:1 split), and genuine one-offs sit up to ~10^3 from their neighbours, so:

    * held — within one period, filings a power of 1000 from the filing closest to the neighbours; a fact ~a power of
      1000 from its neighbours on a stable concept; any fact more than 10^HOLD_DISTANCE from its neighbours; and every
      fact of a filing whose comparatives are mostly a power of 1000 from the other filings (a mis-scaled filing);
    * held — facts that break "a period = its two halves" (e.g. January-June = January-March + April-June) when
      scaling one filing's facts in that sum by a power of 1000 makes it add up;
    * flagged — any other fact more than 10^FLAG_DISTANCE from its neighbours (kept, for review).
    """
    logs = [math.log10(abs(f.value)) if f.value else None for f in records]
    by_period: dict[_PeriodKey, list[int]] = {}
    for i, f in enumerate(records):
        if logs[i] is not None:
            by_period.setdefault((f.concept, f.unit, _duration(f), f.period_end), []).append(i)
    distance = _neighbour_distances(records, logs, by_period)
    held, flagged = _fact_holds(records, distance)
    held |= _filing_disagreements(records, logs, by_period, distance)
    held |= _additivity_errors(records, distance)
    return held, flagged - held


_SeriesKey = tuple[str, str, int | None]
_PeriodKey = tuple[str, str, int | None, date]


def _neighbour_distances(
    records: list[FundamentalFact], logs: list[float | None], by_period: dict[_PeriodKey, list[int]]
) -> dict[int, float]:
    """Orders of magnitude between each fact and the nearest NEIGHBOURS periods of its series, as reported by *other*
    filings (a mis-scaled filing cannot vouch for itself)."""
    series: dict[_SeriesKey, dict[date, list[tuple[str, float]]]] = {}
    for (concept, unit, dur, end), idx in by_period.items():
        series.setdefault((concept, unit, dur), {})[end] = [
            (records[i].accession, v) for i in idx if (v := logs[i]) is not None
        ]
    distance: dict[int, float] = {}
    for (concept, unit, dur, end), idx in by_period.items():
        periods = sorted((e for e in series[(concept, unit, dur)] if e != end), key=lambda e: abs((e - end).days))
        for i in idx:
            levels: list[float] = []
            for e in periods:
                values = [v for acc, v in series[(concept, unit, dur)][e] if acc != records[i].accession]
                if values:
                    levels.append(statistics.median(values))
                if len(levels) == NEIGHBOURS:
                    break
            value = logs[i]
            if len(levels) >= 2 and value is not None:
                distance[i] = abs(value - statistics.median(levels))
    return distance


def _fact_holds(records: list[FundamentalFact], distance: dict[int, float]) -> tuple[set[int], set[int]]:
    """Per fact: held when far beyond what its series ever does, flagged (recent filings only) when unusual."""
    spread: dict[_SeriesKey, list[float]] = {}
    for i, d in distance.items():
        f = records[i]
        spread.setdefault((f.concept, f.unit, _duration(f)), []).append(d)
    typical = {k: statistics.median(v) for k, v in spread.items()}
    filed_on: dict[str, date] = {}
    for f in records:
        filed_on[f.accession] = max(filed_on.get(f.accession, f.filed), f.filed)
    recent = set(sorted(filed_on, key=filed_on.__getitem__)[-FLAG_FILINGS:])
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


def _filing_disagreements(
    records: list[FundamentalFact],
    logs: list[float | None],
    by_period: dict[_PeriodKey, list[int]],
    distance: dict[int, float],
) -> set[int]:
    """Periods filed more than once: filings a power of 1000 from the reference filing (the one closest to its
    independent neighbours) are held. When the neighbours do not clearly side with one filing, the disagreeing
    filings' facts of that concept are all held (a guess could pick the wrong scale). A filing whose comparatives
    mostly disagree by a power of 1000 is held whole."""
    held: set[int] = set()
    off_by_accession: dict[str, list[bool]] = {}
    ambiguous: set[tuple[str, str, str]] = set()
    for idx in by_period.values():
        if len({records[i].accession for i in idx}) < 2:
            continue
        ref = min(idx, key=lambda i: distance.get(i, math.inf))
        ref_log = logs[ref]
        offs = [i for i in idx if (v := logs[i]) is not None and ref_log is not None and _scale_like(v - ref_log)]
        if not offs:
            continue
        margin = min(distance.get(i, math.inf) for i in offs) - distance.get(ref, math.inf)
        if not margin >= DECISIVE_MARGIN:
            ambiguous.update((records[i].concept, records[i].unit, records[i].accession) for i in idx)
            continue
        held.update(offs)
        for i in idx:
            if i != ref:
                off_by_accession.setdefault(records[i].accession, []).append(i in offs)
    held.update(i for i, f in enumerate(records) if (f.concept, f.unit, f.accession) in ambiguous)
    bad_filings = {
        acc for acc, offs in off_by_accession.items() if sum(offs) >= 3 and sum(offs) / len(offs) >= FILING_SCALE_SHARE
    }
    held.update(i for i, f in enumerate(records) if f.accession in bad_filings)
    return held


def _additivity_errors(records: list[FundamentalFact], distance: dict[int, float]) -> set[int]:
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
                    held |= _misscaled_in(records, combo, distance)
    return held


def _adds_up(values: Sequence[float]) -> bool:
    whole, first, second = values
    return abs(whole - first - second) <= ADDITIVITY_TOLERANCE * max(abs(v) for v in values)


def _adds_up_with_every_term(values: Sequence[float]) -> bool:
    """The sum holds and needs all three terms: rescaling one term into insignificance proves nothing."""
    return _adds_up(values) and not any(
        _adds_up([0.0 if j == k else v for j, v in enumerate(values)]) for k in range(3)
    )


def _misscaled_in(records: list[FundamentalFact], combo: tuple[int, ...], distance: dict[int, float]) -> set[int]:
    """The facts of one ``whole = first + second`` sum that a power-of-1000 scale error in one filing explains."""
    values = [records[i].value for i in combo]
    accessions = {records[i].accession for i in combo}
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

    def far(part: list[int]) -> float:
        return statistics.mean(distance.get(i, math.nan) for i in part)

    # Scaling one filing up is the same as scaling the other down: the side far from its neighbouring periods is the
    # mis-scaled one. Without a clear answer every candidate is held (a guess could serve the wrong scale).
    ranked = sorted(fixes, key=lambda part: -far(part) if not math.isnan(far(part)) else math.inf)
    if len(ranked) == 1 or far(ranked[0]) - far(ranked[1]) >= DECISIVE_MARGIN:
        return set(ranked[0])
    return {i for part in fixes for i in part}


def _date_or_none(values: Any, i: int) -> date | None:  # noqa: ANN401 - provider arrays are untyped
    try:
        return date.fromisoformat(str(values[i]))
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _parse_accepted(text: str) -> datetime | None:
    if not text:
        return None
    # EDGAR gives e.g. "2024-11-01T06:01:36.000Z"
    return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def _filing_url(cik: str, accession: str, primary: str | None) -> str:
    folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    return f"{folder}/{primary}" if primary else f"{folder}/{accession}-index.htm"
