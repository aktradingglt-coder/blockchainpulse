"""``python -m rd_scheduler.main`` — the single-leader scheduler process.

P0 provides leadership, heartbeats and a clean shutdown; job definitions are registered from P8 onwards
(``rd_scheduler.jobs``), each fired with a unique key so it can never run twice (D-006).
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket

from rd_core.bootstrap import bootstrap
from rd_core.logging import get_logger
from rd_core.settings import Settings
from rd_db.engine import make_engine
from rd_db.heartbeat import beat
from rd_scheduler.leader import SCHEDULER_LOCK_KEY, LeaderLock

SERVICE = "rd-scheduler"
TICK_SECONDS = 5.0


async def run(
    stop: asyncio.Event,
    *,
    tick: float = TICK_SECONDS,
    service: str = SERVICE,
    lock_key: int = SCHEDULER_LOCK_KEY,
    instance_suffix: str = "",
    settings: Settings | None = None,
) -> None:
    runtime = bootstrap(service, settings)
    log = get_logger(__name__)
    engine = make_engine(runtime.settings, pool_size=2)
    lock = LeaderLock(engine, lock_key)
    ident = f"{socket.gethostname()}:{os.getpid()}{instance_suffix}"
    was_leader = False
    try:
        while not stop.is_set():
            try:
                leader = await lock.try_acquire()
            except Exception as exc:  # noqa: BLE001 - database outage: stay standby and retry next tick
                log.warning("scheduler.lock_unavailable", error=type(exc).__name__)
                leader = False
            if leader != was_leader:
                log.info("scheduler.leadership", leader=leader, instance=ident)
                was_leader = leader
            try:
                await beat(engine, service=service, instance_id=ident, role="leader" if leader else "standby")
            except Exception as exc:  # noqa: BLE001 - heartbeat failure must not stop scheduling
                log.warning("heartbeat.failed", error=type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick)
            except TimeoutError:
                continue
    finally:
        await lock.release()
        await engine.dispose()
        log.info("scheduler.stopped", instance=ident)


def main() -> None:
    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
