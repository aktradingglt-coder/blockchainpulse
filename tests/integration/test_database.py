import asyncio
import json
import subprocess
import sys
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from rd_core.settings import Settings
from rd_db.health import check_database, head_revision
from rd_db.heartbeat import beat, heartbeat_age, is_fresh, is_fresh_for_host, leader_of
from rd_db.migrate import upgrade_to_head

pytestmark = pytest.mark.integration


async def test_schema_at_head_with_timescale(engine: AsyncEngine) -> None:
    health = await check_database(engine)
    assert health.ok, health.detail
    assert health.current_revision == head_revision()
    assert health.timescaledb is not None


async def test_migrations_are_idempotent(engine: AsyncEngine) -> None:
    await asyncio.to_thread(upgrade_to_head)  # alembic's env runs its own event loop
    assert (await check_database(engine)).ok


async def test_database_session_timezone_is_utc(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("SHOW timezone"))).scalar_one() == "UTC"


async def test_heartbeats(engine: AsyncEngine) -> None:
    service = f"rd-test-{uuid.uuid4().hex[:8]}"
    assert await heartbeat_age(engine, service) is None
    assert not await is_fresh(engine, service, timedelta(seconds=30))
    await beat(engine, service=service, instance_id="hostA:1", role="leader")
    await beat(engine, service=service, instance_id="hostB:2", role="standby")
    await beat(engine, service=service, instance_id="hostA:1", role="leader")  # upsert, not duplicate
    assert await is_fresh(engine, service, timedelta(seconds=30))
    assert await is_fresh_for_host(engine, service, "hostA", timedelta(seconds=30))
    assert not await is_fresh_for_host(engine, service, "hostC", timedelta(seconds=30))
    assert not await is_fresh_for_host(engine, service, "host%", timedelta(seconds=30))  # LIKE metachars escaped
    assert await leader_of(engine, service, timedelta(seconds=30)) == "hostA:1"
    async with engine.begin() as conn:
        n = await conn.execute(text("SELECT count(*) FROM service_heartbeats WHERE service = :s"), {"s": service})
        assert n.scalar_one() == 2
        await conn.execute(
            text("UPDATE service_heartbeats SET last_seen_at = now() - interval '10 minutes' WHERE service = :s"),
            {"s": service},
        )
    assert not await is_fresh(engine, service, timedelta(seconds=30))
    assert await leader_of(engine, service, timedelta(seconds=30)) is None
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM service_heartbeats WHERE service = :s"), {"s": service})


async def test_health_reports_unmigrated_database(settings: Settings) -> None:
    """A database without Alembic history is reported not ready (never ok by accident)."""
    name = f"rd_probe_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(str(settings.database_url), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        probe = create_async_engine(str(settings.database_url).rsplit("/", 1)[0] + f"/{name}")
        try:
            health = await check_database(probe)
            assert not health.ok
            assert health.current_revision is None
            assert "revision=None" in health.detail
        finally:
            await probe.dispose()
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


def test_migrate_cli_exits_cleanly() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "rd_db.migrate"], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    events = [json.loads(line)["event"] for line in proc.stdout.splitlines() if line.startswith("{")]
    assert "migrate.done" in events


async def test_sessions_carry_timeouts_and_keepalives(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("SHOW statement_timeout"))).scalar_one() == "1min"
        assert (await conn.execute(text("SHOW tcp_keepalives_idle"))).scalar_one() == "30"
