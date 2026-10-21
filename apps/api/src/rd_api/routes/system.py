"""Liveness, readiness, metrics and public metadata."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from rd_db.health import check_database
from rd_db.heartbeat import heartbeat_age, leader_of

router = APIRouter()
HEARTBEAT_MAX_AGE = timedelta(seconds=45)


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness: the process is up and serving."""
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """Readiness: database reachable with TimescaleDB and schema at head; Redis-protocol store reachable."""
    state = request.app.state.rd
    checks: dict[str, Any] = {}
    try:
        db = await check_database(state.engine)
        checks["database"] = {
            "ok": db.ok,
            "revision": db.current_revision,
            "head": db.head_revision,
            "timescaledb": db.timescaledb,
        }
    except Exception as exc:  # noqa: BLE001 - readiness reports, never raises
        checks["database"] = {"ok": False, "error": type(exc).__name__}
    try:
        checks["redis"] = {"ok": bool(await state.redis.ping())}
    except Exception as exc:  # noqa: BLE001 - readiness reports, never raises
        checks["redis"] = {"ok": False, "error": type(exc).__name__}
    ready = all(c["ok"] for c in checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks}, status_code=200 if ready else 503
    )


@router.get("/v1/status")
async def status(request: Request) -> dict[str, Any]:
    """Public pipeline status (feeds the status page): worker/scheduler heartbeats, queue reachability, leadership."""
    state = request.app.state.rd
    components: dict[str, Any] = {}
    for service in ("rd-worker", "rd-scheduler"):
        try:
            age = await heartbeat_age(state.engine, service)
            components[service] = {
                "ok": age is not None and age <= HEARTBEAT_MAX_AGE,
                "last_heartbeat_age_s": None if age is None else round(age.total_seconds(), 1),
            }
        except Exception as exc:  # noqa: BLE001 - status reports, never raises
            components[service] = {"ok": False, "error": type(exc).__name__}
    try:
        components["queue"] = {"ok": bool(await state.redis.ping())}
    except Exception as exc:  # noqa: BLE001 - status reports, never raises
        components["queue"] = {"ok": False, "error": type(exc).__name__}
    try:
        leader = await leader_of(state.engine, "rd-scheduler", HEARTBEAT_MAX_AGE)
        components["rd-scheduler"]["leader_elected"] = leader is not None
    except Exception as exc:  # noqa: BLE001 - status reports, never raises
        components["rd-scheduler"]["leader_elected"] = False
        components["rd-scheduler"]["leader_error"] = type(exc).__name__
    return {"components": components, "ok": all(c.get("ok", False) for c in components.values())}


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/v1/meta")
async def meta(request: Request) -> dict[str, Any]:
    """Public, non-sensitive product metadata read from config/product.yaml."""
    cfg = request.app.state.rd.runtime.config.product
    return {
        "product_name": cfg.identity.product_name,
        "working_identity": cfg.identity.working,
        "support_email": cfg.identity.support_email,
        "display_currency": cfg.defaults.display_currency,
        "trial_days": cfg.commercial.trial_days,
    }
