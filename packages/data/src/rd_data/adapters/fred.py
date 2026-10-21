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
from rd_data.errors import InvalidArgs, NotFound, ProviderDown
from rd_data.licence import byo_key_only
from rd_data.quality import check_series, with_schema_rejections
from rd_data.resilience import RateLimit
from rd_data.schemas import AssetClass, DataType, MacroObservation

_SERIES = re.compile(r"^[A-Z0-9_]{1,30}$")
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

    def _validate(self, request: DataRequest) -> None:
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
        params = dict(common)
        if "start" in request.params:
            params["observation_start"] = str(request.params["start"])
        obs = await self.http.get("https://api.stlouisfed.org/fred/series/observations", params)
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
        series = str(meta.get("id", request.params["series"]))
        frequency = _FREQ.get(str(meta.get("frequency_short", "")), "daily")
        unit = str(meta.get("units_short") or meta.get("units") or "")
        obs: list[MacroObservation] = []
        rejected = 0
        for row in body.get("observations", []):
            try:
                ts = datetime.strptime(str(row["date"]), "%Y-%m-%d").replace(tzinfo=UTC)
                obs.append(
                    MacroObservation(
                        series_id=series, ts=ts, value=as_float(row.get("value")), unit=unit, frequency=frequency
                    )
                )
            except (KeyError, ValueError):
                rejected += 1
        quality = with_schema_rejections(check_series(obs, subject=f"fred:{series}"), rejected)
        notes = ["FRED terms restrict generative-AI use; enabled only behind provider_fred (D-010)."]
        override = None
        if self.copyrighted(raw):
            notes.append("Series notes carry a third-party copyright notice: internal use only.")
            override = self.licence.model_copy(update={"commercial_use_allowed": False})
        as_of = obs[-1].ts if obs else None
        return ParseResult(
            tuple(obs),
            quality,
            as_of,
            FreshnessSpec(DataType.MACRO_RELEASE, release_frequency=frequency),
            notes=tuple(notes),
            licence_override=override,
        )
