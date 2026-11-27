import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rd_scheduler.leader import LeaderLock

pytestmark = pytest.mark.integration
KEY = 9_100_000_000_123  # distinct from the production key so a running dev scheduler does not interfere


async def test_only_one_leader_and_handover(engine: AsyncEngine) -> None:
    a, b = LeaderLock(engine, KEY), LeaderLock(engine, KEY)
    try:
        assert await a.try_acquire()
        assert await a.try_acquire()  # re-entrant check, still leader
        assert not await b.try_acquire()
        assert await a.still_held()
        await a.release()
        assert not a.held
        assert await b.try_acquire()
        assert not await a.try_acquire()
    finally:
        await a.release()
        await b.release()


async def test_leadership_lost_when_connection_dies(engine: AsyncEngine) -> None:
    a, b = LeaderLock(engine, KEY), LeaderLock(engine, KEY)
    try:
        assert await a.try_acquire()
        async with engine.begin() as admin:
            pids = (
                (
                    await admin.execute(
                        text(
                            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
                            "AND objid::bigint = (:k & 4294967295) AND classid::bigint = (:k >> 32)"
                        ),
                        {"k": KEY},
                    )
                )
                .scalars()
                .all()
            )
            assert len(pids) == 1
            await admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pids[0]})
        assert not await a.still_held()
        assert not a.held
        assert await b.try_acquire()
    finally:
        await a.release()
        await b.release()
