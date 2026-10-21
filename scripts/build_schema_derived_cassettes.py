"""Builds *schema-derived* cassettes for keyed vendors that CI cannot record without a key (D-011).

Each body follows the vendor's documented response format; every value is synthetic and the cassette says so in
``provenance`` and ``source``. Requests are produced by running the real adapter against a capturing transport, so
URLs and parameters match exactly what the adapter sends. Replace a schema-derived cassette by recording it in CI
(``record-cassettes`` workflow) once the vendor key is available; the P11 release gate fails while any remain.

Usage: ``uv run python scripts/build_schema_derived_cassettes.py``
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from rd_core.clock import FixedClock
from rd_core.settings import Settings
from rd_data.adapters.base import AdapterContext, BaseAdapter
from rd_data.adapters.eia import EiaAdapter
from rd_data.adapters.fred import FredAdapter
from rd_data.adapters.twelvedata import TwelveDataAdapter
from rd_data.cassette import Cassette, Interaction, json_shape
from rd_data.http import redact_params, scrub_body, secret_values
from rd_data.recording import DEFAULT_OUT, cassette_name
from rd_data.resilience import CircuitBreaker, LocalRateLimiter, MemoryStateStore

AS_OF = datetime(2026, 10, 21, 12, tzinfo=UTC)
SOURCE = (
    "schema-derived by the builder from the vendor's documented response format; all values are SYNTHETIC "
    "test values, not market data. Replace with a CI recording when a vendor key is configured (D-011)."
)


def _weeks(n: int, end: date) -> list[date]:
    return [end - timedelta(days=7 * k) for k in range(n)]


_GAS_DESC = "Weekly Lower 48 States Natural Gas Working Underground Storage (Billion Cubic Feet)"
_DGS10_TITLE = "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, Quoted on an Investment Basis"


def eia_body(request: httpx.Request) -> dict[str, Any]:
    series = dict(request.url.params)["facets[series][]"]
    unit, base, desc = (
        ("MBBL", 400_000.0, "U.S. Ending Stocks excluding SPR of Crude Oil (Thousand Barrels)")
        if series.startswith("W")
        else ("BCF", 3_500.0, _GAS_DESC)
    )
    rows = [
        {"period": d.isoformat(), "series": series, "series-description": desc, "value": base + 10 * k, "units": unit}
        for k, d in enumerate(_weeks(60, date(2026, 10, 16)))
    ]
    return {
        "response": {
            "total": len(rows),
            "dateFormat": "YYYY-MM-DD",
            "frequency": "weekly",
            "data": rows,
            "description": desc,
        },
        "request": {
            "command": f"/v2/{request.url.path.removeprefix('/v2/').removesuffix('/data/')}/data/",
            "params": {
                "frequency": dict(request.url.params)["frequency"],
                "data": ["value"],
                "facets": {"series": [series]},
                "sort": [{"column": "period", "direction": "desc"}],
                "offset": 0,
                "length": int(dict(request.url.params)["length"]),
                "api_key": dict(request.url.params)["api_key"],  # EIA echoes the key; the recorder scrubs it
            },
        },
        "apiVersion": "2.1.8",
        "ExcelAddInVersion": "2.1.0",
    }


def fred_body(request: httpx.Request) -> dict[str, Any]:
    if request.url.path.endswith("/observations"):
        days = [
            date(2026, 10, 20) - timedelta(days=k)
            for k in range(30)
            if (date(2026, 10, 20) - timedelta(days=k)).weekday() < 5
        ]
        obs = [
            {
                "realtime_start": "2026-10-21",
                "realtime_end": "2026-10-21",
                "date": d.isoformat(),
                "value": "." if k == 5 else f"{3.5 + k / 100:.2f}",
            }
            for k, d in enumerate(sorted(days))
        ]
        return {
            "realtime_start": "2026-10-21",
            "realtime_end": "2026-10-21",
            "observation_start": "1600-01-01",
            "observation_end": "9999-12-31",
            "units": "lin",
            "output_type": 1,
            "file_type": "json",
            "order_by": "observation_date",
            "sort_order": "asc",
            "count": len(obs),
            "offset": 0,
            "limit": 100000,
            "observations": obs,
        }
    return {
        "realtime_start": "2026-10-21",
        "realtime_end": "2026-10-21",
        "seriess": [
            {
                "id": "DGS10",
                "realtime_start": "2026-10-21",
                "realtime_end": "2026-10-21",
                "title": _DGS10_TITLE,
                "observation_start": "1962-01-02",
                "observation_end": "2026-10-20",
                "frequency": "Daily",
                "frequency_short": "D",
                "units": "Percent",
                "units_short": "%",
                "seasonal_adjustment": "Not Seasonally Adjusted",
                "seasonal_adjustment_short": "NSA",
                "last_updated": "2026-10-20 15:18:02-05",
                "popularity": 90,
                "notes": "Synthetic schema-derived notes (no copyright notice).",
            }
        ],
    }


def twelvedata_body(request: httpx.Request) -> dict[str, Any]:
    _ = request
    days = sorted(d for d in (date(2026, 10, 20) - timedelta(days=k) for k in range(45)) if d.weekday() < 5)[-30:]
    values = [
        {
            "datetime": d.isoformat(),
            "open": f"{1.08 + k / 1000:.5f}",
            "high": f"{1.085 + k / 1000:.5f}",
            "low": f"{1.075 + k / 1000:.5f}",
            "close": f"{1.081 + k / 1000:.5f}",
        }
        for k, d in enumerate(days)
    ]
    return {
        "meta": {
            "symbol": "EUR/USD",
            "interval": "1day",
            "currency_base": "Euro",
            "currency_quote": "US Dollar",
            "type": "Physical Currency",
        },
        "values": values,
        "status": "ok",
    }


def _capturing_handler(
    body: Callable[[httpx.Request], dict[str, Any]], captured: list[Interaction]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(req: httpx.Request) -> httpx.Response:
        payload = body(req)
        raw_params = dict(req.url.params)
        params = redact_params(raw_params)
        text = scrub_body(json.dumps(payload).encode(), secret_values(raw_params)).decode()
        url = str(req.url.copy_with(query=None))
        captured.append(
            Interaction(
                "GET",
                url,
                dict(sorted(params.items())),
                200,
                "application/json",
                text,
                json_shape(json.loads(text)),
            )
        )
        return httpx.Response(200, json=payload)

    return handler


async def build(
    cls: type[BaseAdapter],
    body: Callable[[httpx.Request], dict[str, Any]],
    out: Path,
    **settings: Any,  # noqa: ANN401 - Settings kwargs
) -> list[Path]:
    paths = []
    for request in cls.recording_plan():  # type: ignore[attr-defined]  # every schema-derived adapter defines it
        captured: list[Interaction] = []
        handler = _capturing_handler(body, captured)

        ctx = AdapterContext(
            settings=Settings(**settings),
            limiter=LocalRateLimiter(),
            breaker=CircuitBreaker(MemoryStateStore()),
            clock=FixedClock(AS_OF),
            transport=httpx.MockTransport(handler),
            flags={cls.feature_flag: True} if cls.feature_flag else {},
        )
        adapter = cls(ctx)
        response = await adapter.fetch(request)
        await adapter.aclose()
        assert response.quality.passed  # noqa: S101 - build-time sanity check
        replay_at = AS_OF.isoformat().replace("+00:00", "Z")
        cassette = Cassette(cls.name, "schema-derived", None, SOURCE, captured, replay_at=replay_at)
        path = out / cls.name / cassette_name(request)
        cassette.save(path)
        paths.append(path)
    return paths


async def main() -> None:
    fake_key = "schema-derived-" + "0" * 16
    out = DEFAULT_OUT
    written = []
    written += await build(EiaAdapter, eia_body, out, eia_api_key=fake_key)
    written += await build(FredAdapter, fred_body, out, fred_api_key=fake_key)
    written += await build(TwelveDataAdapter, twelvedata_body, out, twelvedata_api_key=fake_key)
    for p in written:
        print(p)


if __name__ == "__main__":
    asyncio.run(main())
