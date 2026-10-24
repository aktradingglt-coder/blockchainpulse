"""Single-leader election with a Postgres session-level advisory lock (D-006).

The lock lives on one dedicated connection; if that connection dies, Postgres releases the lock and another
scheduler instance takes over on its next attempt.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

#: Arbitrary constant identifying the scheduler leadership lock (stable across releases).
SCHEDULER_LOCK_KEY = 7_420_221_001


class LeaderLock:
    def __init__(self, engine: AsyncEngine, key: int = SCHEDULER_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._conn: AsyncConnection | None = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    async def try_acquire(self) -> bool:
        if self._conn is not None:
            return await self.still_held()
        conn = await self._engine.connect()
        got = bool((await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key})).scalar_one())
        await conn.commit()
        if got:
            self._conn = conn
            return True
        await conn.close()
        return False

    async def still_held(self) -> bool:
        """Verify the lock is still ours (the connection may have been dropped)."""
        if self._conn is None:
            return False
        try:
            row = await self._conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                    "AND pid = pg_backend_pid() AND objid::bigint = ((:k)::bigint & 4294967295) "
                    "AND classid::bigint = ((:k)::bigint >> 32) AND objsubid = 1"
                ),
                {"k": self._key},
            )
            held = bool(row.scalar_one())
            await self._conn.commit()
        except Exception:  # noqa: BLE001 - any connection failure means leadership is lost
            await self._discard()
            return False
        if not held:
            await self._discard()
        return held

    async def release(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
            await self._conn.commit()
        finally:
            await self._discard()

    async def _discard(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 - closing a broken connection may itself fail
                await conn.invalidate()
