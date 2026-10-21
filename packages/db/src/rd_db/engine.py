"""Async engine and session factories (asyncpg)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from rd_core.settings import Settings


def make_engine(settings: Settings, *, pool_size: int = 5) -> AsyncEngine:
    return create_async_engine(
        str(settings.database_url),
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args={"server_settings": {"timezone": "UTC", "application_name": settings.service_name}},
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
