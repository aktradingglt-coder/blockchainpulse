"""Database readiness: connectivity, TimescaleDB extension present, schema at Alembic head."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


@dataclass(frozen=True)
class DbHealth:
    ok: bool
    current_revision: str | None
    head_revision: str
    timescaledb: str | None
    detail: str


def head_revision() -> str:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        raise RuntimeError("no Alembic head revision found")
    return head


async def check_database(engine: AsyncEngine) -> DbHealth:
    head = head_revision()
    async with engine.connect() as conn:
        tsdb = (
            await conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"))
        ).scalar_one_or_none()
        has_table = (await conn.execute(text("SELECT to_regclass('public.alembic_version')"))).scalar_one_or_none()
        current = None
        if has_table is not None:
            current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    ok = current == head and tsdb is not None
    detail = "ok" if ok else f"revision={current} head={head} timescaledb={tsdb}"
    return DbHealth(ok=ok, current_revision=current, head_revision=head, timescaledb=tsdb, detail=detail)
