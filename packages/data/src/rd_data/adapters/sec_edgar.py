"""SEC EDGAR: filings (submissions API), XBRL company facts and the ticker→CIK map (PROMPT §5.2).

Fair access: the SEC caps clients at 10 requests/second and requires a User-Agent naming the company and a contact
email (env ``SEC_EDGAR_USER_AGENT``). We stay at 8 r/s shared across workers. Fundamentals are point-in-time: each
fact carries its ``filed`` date and is usable only from then on (never by period end alone).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
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
from rd_data.adapters.sec_scale import scale_errors, superseded_filings
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
        if not data:
            # The SEC map lists every registrant (over 10,000); an empty one is a broken response, never cached.
            raise ProviderDown("sec: empty ticker map", provider=self.name)
        records: list[Instrument] = []
        rejected = 0
        for row in data.values():
            instrument = _instrument(row)
            if instrument is None:
                rejected += 1
            else:
                records.append(instrument)
        if not records:
            raise ProviderDown(f"sec: ticker map with no usable rows ({rejected} malformed)", provider=self.name)
        if rejected > len(records) * TICKER_REJECT_SHARE:
            quality = with_schema_rejections(DataQualityReport("sec:company_tickers", (), (), len(records)), rejected)
            return ParseResult(tuple(records), quality, None, None)  # blocked by the schema check
        if len(records) < MIN_TICKER_ROWS:
            # A cut-off download: serving (and caching) it would make most companies "not found" for a day.
            raise ProviderDown(f"sec: ticker map truncated ({len(records)} rows)", provider=self.name)
        # A ticker listed for two registrants would resolve to whichever row came first; neither is served.
        ciks: dict[str, set[str | None]] = {}
        for r in records:
            ciks.setdefault(r.symbol, set()).add(r.provider_symbol)
        ambiguous = sorted(t for t, c in ciks.items() if len(c) > 1)
        seen: set[str] = set()
        unique = []
        for r in records:
            if r.symbol not in seen and r.symbol not in ambiguous:
                seen.add(r.symbol)
                unique.append(r)
        # A few bad rows in a map of 10,000+ registrants are left out with a warning, never blocking every lookup.
        checks: list[CheckResult] = []
        if rejected:
            checks.append(
                CheckResult("schema", passed=False, blocking=False, detail=f"{rejected} malformed row(s) left out")
            )
        if ambiguous:
            checks.append(
                CheckResult(
                    "unique_ticker",
                    passed=False,
                    blocking=False,
                    detail=f"{len(ambiguous)} ticker(s) on several CIKs left out: {', '.join(ambiguous[:5])}",
                )
            )
        return ParseResult(
            tuple(unique), DataQualityReport("sec:company_tickers", tuple(checks), (), len(unique)), None, None
        )

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
        held, flagged = scale_errors(records)
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
                detail=f"{len(flagged)} recent fact(s) unusually far from their neighbouring periods, kept for review"
                if flagged
                else "ok",
                indices=tuple(sorted(flagged)),
            ),
        )
        quality = with_schema_rejections(
            DataQualityReport(f"sec:facts:{cik}", checks, tuple(sorted(held)), len(records)), rejected
        )
        usable = [f for i, f in enumerate(records) if i not in held]
        # The response is as fresh as the newest filing whose own figures (its current-period column) are shown; a
        # filing whose current column is held only contributes comparatives and does not make old figures current.
        superseded = superseded_filings(records, held)
        current = [f for f in usable if f.accession not in superseded]
        as_of = current[-1].filed if current else (usable[-1].filed if usable else None)
        notes = ["Point-in-time by filing date: a fact is usable only on or after its 'filed' date."]
        superseded_at = None
        later = sorted({f.filed for f in records if as_of is not None and f.filed > as_of})
        if later and as_of is not None:
            superseded_at = datetime.combine(later[0], datetime.min.time(), tzinfo=UTC)
            notes.append(
                f"The current-period figures of the filing of {later[0].isoformat()} are held for scale errors; the "
                f"latest figures shown are from the filing of {as_of.isoformat()} and are judged stale a week after "
                "the newer filing arrived."
            )
        return ParseResult(
            tuple(usable),
            quality,
            as_of,
            FreshnessSpec(DataType.FUNDAMENTALS, next_filing_at=superseded_at),
            source_tz="America/New_York",
            quarantined=tuple(f for i, f in enumerate(records) if i in held),
            notes=tuple(notes),
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [
            DataRequest(kind=RequestKind.INSTRUMENTS),
            DataRequest(kind=RequestKind.FILINGS, params={"cik": "320193"}),
            DataRequest(kind=RequestKind.COMPANY_FACTS, params={"cik": "1018724"}),
        ]


#: Share of malformed rows the ticker map tolerates (left out with a warning) before the whole map is refused.
TICKER_REJECT_SHARE = 0.01
#: The SEC map lists every registrant with a ticker (10,443 rows when recorded); far fewer means a cut-off download.
MIN_TICKER_ROWS = 5000


def _instrument(row: Any) -> Instrument | None:  # noqa: ANN401 - provider rows are untyped
    """One ticker-map row, or None when it is malformed (a missing or empty ticker never becomes "NONE")."""
    try:
        ticker, title = row["ticker"], row["title"]
        if not isinstance(ticker, str) or not ticker.strip() or not isinstance(title, str):
            return None
        return Instrument(
            symbol=ticker.strip().upper(),
            asset_class=AssetClass.EQUITY,
            name=title,
            exchange=None,
            currency="USD",
            provider_symbol=cik10(row["cik_str"]),
        )
    except (KeyError, ValueError, InvalidArgs, TypeError):
        return None


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
