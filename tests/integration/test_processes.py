"""Scheduler loop, worker heartbeat middleware and container healthcheck against real Postgres."""

import asyncio
import socket
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rd_core.settings import Settings
from rd_db import healthcheck
from rd_db.heartbeat import heartbeat_age, leader_of
from rd_scheduler.main import run
from rd_worker.heartbeat import HeartbeatMiddleware

pytestmark = pytest.mark.integration
TEST_LOCK_KEY = 9_100_000_000_456


async def _rows(engine: AsyncEngine, service: str) -> dict[str, str]:
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT instance_id, role FROM service_heartbeats WHERE service = :s"), {"s": service}
        )
        return {r.instance_id: r.role for r in res}


async def _cleanup(engine: AsyncEngine, service: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM service_heartbeats WHERE service = :s"), {"s": service})


async def test_two_schedulers_elect_exactly_one_leader_and_fail_over(engine: AsyncEngine) -> None:
    service = f"rd-sched-test-{uuid.uuid4().hex[:6]}"
    stop_a, stop_b = asyncio.Event(), asyncio.Event()
    task_a = asyncio.create_task(run(stop_a, tick=0.2, service=service, lock_key=TEST_LOCK_KEY, instance_suffix="-a"))
    await asyncio.sleep(1.0)
    task_b = asyncio.create_task(run(stop_b, tick=0.2, service=service, lock_key=TEST_LOCK_KEY, instance_suffix="-b"))
    try:
        await asyncio.sleep(1.5)
        roles = await _rows(engine, service)
        assert sorted(roles.values()) == ["leader", "standby"], roles
        assert roles[next(k for k in roles if k.endswith("-a"))] == "leader"
        stop_a.set()
        await asyncio.wait_for(task_a, timeout=10)  # leader exits and releases the lock
        await asyncio.sleep(1.5)
        roles = await _rows(engine, service)
        assert roles[next(k for k in roles if k.endswith("-b"))] == "leader", roles
        leader = await leader_of(engine, service, timedelta(seconds=1))
        assert leader is not None
        assert leader.endswith("-b")
    finally:
        stop_a.set()
        stop_b.set()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        await _cleanup(engine, service)


async def test_worker_heartbeat_middleware_and_healthcheck(settings: Settings, engine: AsyncEngine) -> None:
    service = f"rd-worker-test-{uuid.uuid4().hex[:6]}"
    mw = HeartbeatMiddleware(settings, service=service, interval=0.2)
    assert not await healthcheck.check(service, socket.gethostname())
    mw.after_worker_boot(None, None)  # type: ignore[arg-type]  # broker/worker are unused by this hook
    try:
        for _ in range(50):
            if await heartbeat_age(engine, service) is not None:
                break
            await asyncio.sleep(0.1)
        assert await healthcheck.check(service, socket.gethostname())
        assert not await healthcheck.check(service, "some-other-host")
    finally:
        await asyncio.to_thread(mw.before_worker_shutdown, None, None)  # type: ignore[arg-type]  # unused hook args
        await _cleanup(engine, service)
