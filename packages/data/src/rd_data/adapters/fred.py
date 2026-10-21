"""FRED (Federal Reserve Bank of St. Louis) — built and tested, OFF by default (D-010).

FRED's terms restrict use of its content with generative AI, and many series are third-party copyrighted. Until
counsel clears it, the adapter is behind the ``provider_fred`` flag, its terms are unverified, and any series whose
notes carry a copyright notice is treated as internal-only even where FRED is enabled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
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
    as_float,
    require_param,
)
from rd_data.errors import DataQualityError, InvalidArgs, NotFound, ProviderDown
from rd_data.licence import byo_key_only
from rd_data.quality import DataQualityReport, check_provider_order, check_series, with_schema_rejections
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, DataType, MacroObservation

_SERIES = re.compile(r"^[A-Z0-9_]{1,30}$")
#: Plausible range for a series measured in percent (rates, yields, unemployment); outside it is a unit error.
PERCENT_RANGE = (-50.0, 100.0)
_FREQ = {"D": "daily", "W": "weekly", "BW": "weekly", "M": "monthly", "Q": "quarterly", "SA": "annual", "A": "annual"}


class FredAdapter(BaseAdapter):
    name: ClassVar[str] = "fred"
    asset_classes = frozenset({AssetClass.MACRO, AssetClass.RATES})
    licence = byo_key_only("Source: Federal Reserve Bank of St. Louis (FRED); see each series' copyright notes")
    rate_limit = RateLimit(per_minute=100)
    allowed_hosts = frozenset({"api.stlouisfed.org"})
    supported = frozenset({RequestKind.MACRO_SERIES})
    feature_flag = "provider_fred"
    cache_ttl_s: ClassVar[Mapping[RequestKind, int]] = {RequestKind.MACRO_SERIES: 3600}
    params_allowed: ClassVar[Mapping[RequestKind, frozenset[str]]] = {
        RequestKind.MACRO_SERIES: frozenset({"series", "start"})
    }
    credential_setting = "fred_api_key"

    def _validate(self, request: DataRequest) -> None:
        if "start" in request.params and _start_of(request) is None:
            raise InvalidArgs("fred: start must be a date (YYYY-MM-DD)", provider=self.name)
        if self.ctx.settings.fred_api_key is None:
            raise ProviderDown("fred: FRED_API_KEY is not configured", provider=self.name)
        series = str(require_param(request, "series", self.name))
        if not _SERIES.match(series):
            raise InvalidArgs(f"fred: invalid series id {series!r}", provider=self.name)

    async def _download(self, request: DataRequest) -> RawPayload:
        key = self.ctx.settings.fred_api_key
        assert key is not None  # checked in _validate  # noqa: S101
        series = str(request.params["series"])
        common: dict[str, Any] = {"series_id": series, "api_key": key.get_secret_value(), "file_type": "json"}
        info = await self.http.get("https://api.stlouisfed.org/fred/series", common)
        # The whole history is fetched (no observation_start) so the earliest requested value is checked against
        # what came before it; ``start`` trims the response after QA.
        obs = await self.http.get("https://api.stlouisfed.org/fred/series/observations", common)
        return RawPayload.from_results([info, obs], self.ctx.clock.now())

    def copyrighted(self, raw: RawPayload) -> bool:
        info = orjson.loads(raw.bodies[0])
        notes = " ".join(str(s.get("notes", "")) for s in info.get("seriess", []))
        return "copyright" in notes.lower()

    def _parse(self, request: DataRequest, raw: RawPayload) -> ParseResult:
        info = orjson.loads(raw.bodies[0]).get("seriess") or []
        body = orjson.loads(raw.bodies[1])
        if not info:
            raise NotFound(f"fred: unknown series {request.params['series']}", provider=self.name)
        meta = info[0]
        series = str(request.params["series"])
        if str(meta.get("id")) != series:
            raise DataQualityError(
                f"fred: response describes series {meta.get('id')!r}, request was {series!r}", provider=self.name
            )
        frequency = _FREQ.get(str(meta.get("frequency_short", "")), "daily")
        unit = str(meta.get("units_short") or meta.get("units") or "")
        obs: list[MacroObservation] = []
        rejected = 0
        for row in body.get("observations", []):
            try:
                ts = datetime.strptime(str(row["date"]), "%Y-%m-%d").replace(tzinfo=UTC)
                if ts > raw.retrieved_at:
                    rejected += 1  # an observation dated after we fetched it cannot be real
                    continue
                obs.append(
                    MacroObservation(
                        series_id=series, ts=ts, value=as_float(row.get("value")), unit=unit, frequency=frequency
                    )
                )
            except (KeyError, TypeError, ValueError):
                rejected += 1
        order = check_provider_order([o.ts for o in obs], descending=False)  # FRED returns ascending dates
        obs.sort(key=lambda o: o.ts)
        percent = unit in ("%", "Percent")
        report, usable = check_series(
            obs,
            subject=f"fred:{series}",
            lo=PERCENT_RANGE[0] if percent else None,
            hi=PERCENT_RANGE[1] if percent else None,
            # Daily FRED series are market levels (yields, FX, commodity prices): the price rule applies (D-031).
            level_like=frequency == "daily",
        )
        quality = with_schema_rejections(
            DataQualityReport(report.subject, (*report.checks, order), report.quarantined, report.record_count),
            rejected,
        )
        notes = ["FRED terms restrict generative-AI use; enabled only behind provider_fred (D-010)."]
        override = None
        if self.copyrighted(raw):
            notes.append("Series notes carry a third-party copyright notice: internal use only.")
            override = self.licence.model_copy(update={"commercial_use_allowed": False})
        start = _start_of(request)
        held = tuple(o for o in obs if o not in usable and (start is None or o.ts >= start))
        if start is not None:
            usable = [o for o in usable if o.ts >= start]
        reported = [o for o in usable if o.value is not None]
        if usable and not reported:
            raise DataQualityError(
                f"{self.name}: every observation is reported as missing; nothing to date the series by",
                provider=self.name,
            )
        as_of = reported[-1].ts if reported else None  # FRED marks missing days with "."; never date data by one
        # Freshness is judged from the series' last update (its latest release), not from the observation date:
        # a monthly value dated the 1st is released weeks later, and judging it from the 1st would block it on
        # release day.
        updated = _last_updated(meta.get("last_updated"))
        return ParseResult(
            tuple(usable),
            quality,
            as_of,
            FreshnessSpec(DataType.MACRO_RELEASE, release_frequency=frequency),
            quarantined=held,
            notes=tuple(notes),
            licence_override=override,
            freshness_as_of=updated or as_of,
        )

    @staticmethod
    def recording_plan() -> list[DataRequest]:
        return [DataRequest(kind=RequestKind.MACRO_SERIES, params={"series": "DGS10"})]


def _last_updated(text: object) -> datetime | None:
    """FRED's ``last_updated``, e.g. ``2026-10-21 15:18:02-05`` (offset in hours)."""
    if not isinstance(text, str) or not text:
        return None
    value = text.strip()
    if len(value) >= 3 and value[-3] in "+-" and value[-2:].isdigit():
        value += ":00"
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _start_of(request: DataRequest) -> datetime | None:
    try:
        return datetime.strptime(str(request.params["start"]), "%Y-%m-%d").replace(tzinfo=UTC)
    except (KeyError, ValueError):
        return None
