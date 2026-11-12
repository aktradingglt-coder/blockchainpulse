import asyncio

from rd_core.settings import Settings
from rd_scheduler.main import run


async def test_scheduler_survives_database_outage() -> None:
    """With Postgres unreachable the scheduler keeps running as standby instead of crashing."""
    settings = Settings(database_url="postgresql+asyncpg://nobody@127.0.0.1:1/none")
    stop = asyncio.Event()
    task = asyncio.create_task(run(stop, tick=0.05, service="rd-sched-unit", settings=settings))
    await asyncio.sleep(0.5)
    assert not task.done(), task.exception() if task.done() else None
    stop.set()
    await asyncio.wait_for(task, timeout=10)
