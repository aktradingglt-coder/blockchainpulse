"""Async engine and session factories (asyncpg)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from rd_core.settings import Settings

#: Server-side keepalives detect dead peers (e.g. the scheduler's leader-lock connection) within about a minute.
_KEEPALIVE = {"tcp_keepalives_idle": "30", "tcp_keepalives_interval": "10", "tcp_keepalives_count": "3"}


def make_engine(settings: Settings, *, pool_size: int = 5, statement_timeout_ms: int = 60_000) -> AsyncEngine:
    return create_async_engine(
        str(settings.database_url),
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
        connect_args={
            "timeout": 10,
            "server_settings": {
                "timezone": "UTC",
                "application_name": settings.service_name,
                "statement_timeout": str(statement_timeout_ms),
                "idle_in_transaction_session_timeout": "300000",
                **_KEEPALIVE,
            },
        },
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
