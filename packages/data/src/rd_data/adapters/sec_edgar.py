"""SEC EDGAR: filings (submissions API), XBRL company facts and the ticker→CIK map (PROMPT §5.2).

Fair access: the SEC caps clients at 10 requests/second and requires a User-Agent naming the company and a contact
email (env ``SEC_EDGAR_USER_AGENT``). We stay at 8 r/s shared across workers. Fundamentals are point-in-time: each
fact carries its ``filed`` date and is usable only from then on (never by period end alone).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
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
from rd_data.quality import DataQualityReport, check_duplicates, check_monotonic, with_schema_rejections
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

    def _parse_filings(self, request: DataRequest, data: Mapping[str, Any]) -> ParseResult:
        cik = cik10(str(data.get("cik", request.params["cik"])))
        company = str(data.get("name", ""))
        recent = data.get("filings", {}).get("recent", {})
        forms = request.params.get("forms")
        wanted = {f.strip().upper() for f in str(forms).split(",")} if forms else None
        n = len(recent.get("accessionNumber", []))
        records: list[Filing] = []
        rejected = 0
        for i in range(n):
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
                        filed=date.fromisoformat(str(recent["filingDate"][i])),
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
                f"sec:filings:{cik}", (check_duplicates([f.accession for f in records]),), (), len(records)
            ),
            rejected,
        )
        as_of = records[-1].filed if records else None
        return ParseResult(tuple(records), quality, as_of, None, source_tz="America/New_York")

    # ------------------------------------------------------------------------------------------ facts

    def _parse_facts(self, request: DataRequest, data: Mapping[str, Any]) -> ParseResult:
        cik = cik10(str(data.get("cik", request.params["cik"])))
        concepts_param = request.params.get("concepts")
        wanted = {c.strip() for c in str(concepts_param).split(",")} if concepts_param else None
        records: list[FundamentalFact] = []
        rejected = 0
        for taxonomy, concepts in (data.get("facts") or {}).items():
            for concept, body in concepts.items():
                if wanted and concept not in wanted:
                    continue
                for unit, facts in (body.get("units") or {}).items():
                    for fact in facts:
                        form = str(fact.get("form", ""))
                        if form not in FUNDAMENTAL_FORMS:
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
        quality = with_schema_rejections(
            DataQualityReport(
                f"sec:facts:{cik}",
                (check_monotonic([f.filed for f in records]), check_duplicates(keys)),
                (),
                len(records),
            ),
            rejected,
        )
        as_of = records[-1].filed if records else None
        return ParseResult(
            tuple(records),
            quality,
            as_of,
            FreshnessSpec(DataType.FUNDAMENTALS),
            source_tz="America/New_York",
            notes=("Point-in-time by filing date: a fact is usable only on or after its 'filed' date.",),
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [
            DataRequest(kind=RequestKind.INSTRUMENTS),
            DataRequest(kind=RequestKind.FILINGS, params={"cik": "320193"}),
            DataRequest(kind=RequestKind.COMPANY_FACTS, params={"cik": "1018724"}),
        ]


def _parse_accepted(text: str) -> datetime | None:
    if not text:
        return None
    # EDGAR gives e.g. "2024-11-01T06:01:36.000Z"
    return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def _filing_url(cik: str, accession: str, primary: str | None) -> str:
    folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    return f"{folder}/{primary}" if primary else f"{folder}/{accession}-index.htm"
