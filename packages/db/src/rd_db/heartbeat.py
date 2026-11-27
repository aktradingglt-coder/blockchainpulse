"""Process heartbeats stored in Postgres (status page, health checks).

``instance_id`` is ``<hostname>:<pid>``; host-scoped checks match on the ``<hostname>:`` prefix so a container's
healthcheck only passes when a process inside that container is alive.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Interval, func, select, type_coerce
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from rd_db.models import ServiceHeartbeat


async def beat(engine: AsyncEngine, *, service: str, instance_id: str, role: str = "member") -> None:
    stmt = insert(ServiceHeartbeat).values(service=service, instance_id=instance_id, role=role)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ServiceHeartbeat.service, ServiceHeartbeat.instance_id],
        set_={"last_seen_at": func.now(), "role": role},
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)


async def heartbeat_age(engine: AsyncEngine, service: str, host: str | None = None) -> timedelta | None:
    """Age of the newest heartbeat for ``service`` (optionally only from ``host``), by the database clock."""
    newest = func.max(ServiceHeartbeat.last_seen_at)
    q = select(type_coerce(func.now() - newest, Interval())).where(ServiceHeartbeat.service == service)
    if host is not None:
        q = q.where(ServiceHeartbeat.instance_id.startswith(f"{host}:", autoescape=True))
    async with engine.connect() as conn:
        age: timedelta | None = (await conn.execute(q)).scalar_one_or_none()
    return age


async def is_fresh(engine: AsyncEngine, service: str, max_age: timedelta) -> bool:
    age = await heartbeat_age(engine, service)
    return age is not None and age <= max_age


async def is_fresh_for_host(engine: AsyncEngine, service: str, host: str, max_age: timedelta) -> bool:
    age = await heartbeat_age(engine, service, host)
    return age is not None and age <= max_age


async def leader_of(engine: AsyncEngine, service: str, max_age: timedelta) -> str | None:
    """Instance currently reporting the ``leader`` role with a fresh heartbeat, if any."""
    q = (
        select(ServiceHeartbeat.instance_id)
        .where(
            ServiceHeartbeat.service == service,
            ServiceHeartbeat.role == "leader",
            ServiceHeartbeat.last_seen_at >= func.now() - max_age,
        )
        .order_by(ServiceHeartbeat.last_seen_at.desc())
        .limit(1)
    )
    async with engine.connect() as conn:
        ident: str | None = (await conn.execute(q)).scalar_one_or_none()
    return ident


__all__ = ["beat", "heartbeat_age", "is_fresh", "is_fresh_for_host", "leader_of"]
