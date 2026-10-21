"""Builds the configured provider adapters for a process (API, worker, recorder)."""

from __future__ import annotations

from collections.abc import Mapping

import redis.asyncio as aioredis

from rd_core.config import AppConfig
from rd_core.settings import Settings
from rd_data.adapters.base import AdapterContext, BaseAdapter
from rd_data.adapters.cftc import CftcAdapter
from rd_data.adapters.eia import EiaAdapter
from rd_data.adapters.fred import FredAdapter
from rd_data.adapters.kraken_futures import KrakenFuturesAdapter
from rd_data.adapters.sec_edgar import SecEdgarAdapter
from rd_data.adapters.treasury import TreasuryAdapter
from rd_data.adapters.twelvedata import TwelveDataAdapter
from rd_data.resilience import CircuitBreaker, RedisRateLimiter, RedisStateStore, ResponseCache

ADAPTERS: tuple[type[BaseAdapter], ...] = (
    CftcAdapter,
    SecEdgarAdapter,
    TreasuryAdapter,
    EiaAdapter,
    KrakenFuturesAdapter,
    FredAdapter,
    TwelveDataAdapter,
)


def effective_flags(config: AppConfig, settings: Settings) -> dict[str, bool]:
    flags = dict(config.feature_flags.flags)
    flags.update(settings.feature_flags)
    return flags


def build_context(settings: Settings, config: AppConfig, redis_client: aioredis.Redis) -> AdapterContext:
    store = RedisStateStore(redis_client)
    return AdapterContext(
        settings=settings,
        limiter=RedisRateLimiter(redis_client),
        breaker=CircuitBreaker(store),
        cache=ResponseCache(store),
        flags=effective_flags(config, settings),
    )


def build_adapters(ctx: AdapterContext) -> Mapping[str, BaseAdapter]:
    return {cls.name: cls(ctx) for cls in ADAPTERS}
