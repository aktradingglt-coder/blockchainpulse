"""Integration fixtures: real Postgres/Timescale and Valkey from the dev stack (`make dev`) or CI services."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rd_core.settings import Settings
from rd_db.engine import make_engine
from rd_db.migrate import upgrade_to_head


@pytest.fixture(scope="session")
def settings() -> Settings:
    s = Settings()
    if s.database_url.hosts()[0].get("password") is None:
        raise RuntimeError("integration tests need DATABASE_URL/REDIS_URL from .env (make dev) or CI")
    return s


@pytest.fixture(scope="session", autouse=True)
def _migrated(settings: Settings) -> None:
    upgrade_to_head()


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(settings, pool_size=2)
    yield eng
    await eng.dispose()
