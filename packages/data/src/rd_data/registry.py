"""Builds the configured provider adapters for a process (API, worker, recorder)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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


async def provider_statuses(
    flags: Mapping[str, bool], breaker: CircuitBreaker, settings: Settings
) -> dict[str, dict[str, Any]]:
    """Per-provider state for the status page (PROMPT §5.1): feature flag, credentials, circuit breaker, licence.

    Read straight from the shared breaker state, without building adapters or opening HTTP clients.
    """
    out: dict[str, dict[str, Any]] = {}
    for cls in ADAPTERS:
        circuit = await breaker.status(cls.name)
        out[cls.name] = {
            "enabled": cls.feature_flag is None or bool(flags.get(cls.feature_flag, False)),
            "configured": cls.credential_setting is None or bool(getattr(settings, cls.credential_setting)),
            "circuit": circuit.state.value,
            "consecutive_failures": circuit.consecutive_failures,
            "last_error": circuit.last_error,
            "last_quality_error": circuit.last_quality_error,
            "terms_verified": cls.licence.terms_verified,
        }
    return out
