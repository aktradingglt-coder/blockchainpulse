"""``python -m rd_data.recording`` — record cassettes from live providers, or check them for schema drift.

Runs where the network is open (the ``record-cassettes`` GitHub workflow; the build sandbox cannot reach vendors,
D-011). For each adapter's ``recording_plan()`` it performs the real requests through the normal adapter pipeline
(rate limits, retries, normalisation, QA), writes one cassette per request with credentials redacted, and prints
one JSON summary line per request. ``--check-drift`` re-fetches live and compares response shapes with the stored
cassettes instead of writing (nightly live smoke test).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rd_core.clock import isoformat_z
from rd_core.config import load_config
from rd_core.logging import configure_logging
from rd_core.redaction import redact_text
from rd_core.settings import Settings
from rd_data.adapters.base import AdapterContext, BaseAdapter, DataRequest
from rd_data.cassette import Cassette, RecordingTransport, shape_drift
from rd_data.errors import DataError
from rd_data.registry import ADAPTERS, effective_flags
from rd_data.resilience import CircuitBreaker, LocalRateLimiter, MemoryStateStore

DEFAULT_OUT = Path("packages/data/tests/cassettes")


def cassette_name(request: DataRequest) -> str:
    """Stable, readable file name: kind plus a short hash of the parameters."""
    digest = hashlib.sha256(json.dumps(request.params, sort_keys=True).encode()).hexdigest()[:10]
    return f"{request.kind.value}-{digest}.json"


def _context(settings: Settings, transport: RecordingTransport, flags: dict[str, bool]) -> AdapterContext:
    return AdapterContext(
        settings=settings,
        limiter=LocalRateLimiter(),
        breaker=CircuitBreaker(MemoryStateStore()),
        cache=None,
        transport=transport,
        flags=flags,
    )


async def _run_one(
    cls: type[BaseAdapter],
    request: DataRequest,
    settings: Settings,
    flags: dict[str, bool],
    out: Path,
    *,
    check_drift: bool,
    source: str,
) -> dict[str, Any]:
    transport = RecordingTransport()
    adapter = cls(_context(settings, transport, {**flags, **({cls.feature_flag: True} if cls.feature_flag else {})}))
    summary: dict[str, Any] = {"provider": cls.name, "kind": request.kind.value, "params": request.params}
    try:
        response = await adapter.fetch(request)
        summary.update(
            ok=True,
            records=len(response.records),
            quarantined=len(response.quarantined),
            quality_passed=response.quality.passed,
            warnings=list(response.quality.warnings),
            freshness=response.freshness_label,
            as_of=str(response.as_of),
            licence_tag=response.licence_tag.value,
        )
    except DataError as exc:
        summary.update(ok=False, error=type(exc).__name__, detail=str(exc)[:500])
    except Exception as exc:  # noqa: BLE001 - one failing request must not abort the whole recording run
        summary.update(ok=False, error=type(exc).__name__, detail=redact_text(str(exc))[:500], unexpected=True)
    finally:
        await adapter.aclose()
    summary["http"] = [f"{i.status} {i.url}" for i in transport.interactions]
    summary["bytes"] = sum(len(i.body) for i in transport.interactions)
    path = out / cls.name / cassette_name(request)
    if check_drift:
        if path.exists():
            stored = Cassette.load(path)
            drift = []
            for old, new in zip(stored.interactions, transport.interactions, strict=False):
                if old.shape is not None and new.shape is not None:
                    drift.extend(shape_drift(old.shape, new.shape))
            summary["drift"] = drift
        else:
            summary["drift"] = ["no stored cassette"]
    elif transport.interactions:
        cassette = transport.to_cassette(cls.name, source)
        cassette.save(path)
        summary["cassette"] = str(path)
    return summary


async def run(providers: Sequence[str], out: Path, *, check_drift: bool, source: str) -> list[dict[str, Any]]:
    settings = Settings()
    flags = effective_flags(load_config(settings.config_dir), settings)
    selected = [cls for cls in ADAPTERS if cls.name in providers]
    unknown = set(providers) - {cls.name for cls in ADAPTERS}
    if unknown:
        raise SystemExit(f"unknown provider(s): {sorted(unknown)}")
    results = []
    for cls in selected:
        plan = getattr(cls, "recording_plan", None)
        if plan is None:
            results.append({"provider": cls.name, "ok": False, "error": "no recording_plan"})
            continue
        for request in plan():
            results.append(await _run_one(cls, request, settings, flags, out, check_drift=check_drift, source=source))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", required=True, help="comma-separated provider names")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check-drift", action="store_true")
    parser.add_argument("--source", default="manual run")
    args = parser.parse_args(argv)
    configure_logging(service="rd-recorder", level="WARNING", fmt="json")
    results = asyncio.run(
        run(
            [p.strip() for p in args.providers.split(",") if p.strip()],
            args.out,
            check_drift=args.check_drift,
            source=args.source,
        )
    )
    for r in results:
        sys.stdout.write("RECORDING " + json.dumps(r, default=str, sort_keys=True) + "\n")
    sys.stdout.write(f"RECORDING_DONE at={isoformat_z(datetime.now(tz=UTC))}\n")
    failed = [r for r in results if not r.get("ok") or r.get("drift")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
