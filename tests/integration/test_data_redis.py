"""Data-layer resilience against real Valkey: shared token buckets, circuit breaker, response cache, registry."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis

from rd_core.config import load_config
from rd_core.settings import Settings
from rd_data.errors import ProviderDown, RateLimited
from rd_data.registry import ADAPTERS, build_adapters, build_context, effective_flags
from rd_data.resilience import (
    CircuitBreaker,
    CircuitState,
    RateLimit,
    RedisRateLimiter,
    RedisStateStore,
    ResponseCache,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[aioredis.Redis]:
    c = aioredis.Redis.from_url(str(settings.redis_url))
    yield c
    await c.aclose()


@pytest.fixture
def ns() -> str:
    return f"rdtest:{uuid.uuid4().hex}"


@pytest.fixture
async def _cleanup(client: aioredis.Redis, ns: str) -> AsyncIterator[None]:
    yield
    keys = [k async for k in client.scan_iter(f"{ns}*")]
    if keys:
        await client.delete(*keys)


@pytest.mark.usefixtures("_cleanup")
async def test_concurrent_acquisitions_never_exceed_the_bucket(client: aioredis.Redis, ns: str) -> None:
    limiter = RedisRateLimiter(client, namespace=ns)
    limit = RateLimit(per_second=5, per_minute=100)
    waits = await asyncio.gather(*(limiter.try_acquire("p", limit) for _ in range(20)))
    granted = [w for w in waits if w == 0]
    assert len(granted) == 5  # burst capacity of the per-second bucket, atomically shared
    assert all(0 < w <= 1.0 for w in waits if w != 0)
    # A refused call takes no token from any bucket: the minute bucket lost only the 5 granted.
    tokens = float(await client.hget(f"{ns}:p:m", "tokens") or 0)
    assert 94.0 <= tokens <= 95.5


@pytest.mark.usefixtures("_cleanup")
async def test_acquire_waits_for_refill_then_times_out(client: aioredis.Redis, ns: str) -> None:
    limiter = RedisRateLimiter(client, namespace=ns)
    limit = RateLimit(per_second=10)
    for _ in range(10):
        await limiter.acquire("p", limit)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await limiter.acquire("p", limit)  # waits ~0.1 s for one token
    assert loop.time() - started >= 0.05
    slow = RateLimit(per_minute=1)
    await limiter.acquire("q", slow)
    with pytest.raises(RateLimited) as exc:
        await limiter.acquire("q", slow, timeout_s=0.2)
    assert exc.value.retry_after_s is not None
    assert exc.value.retry_after_s > 0.2


async def test_empty_rate_limit_is_free(client: aioredis.Redis, ns: str) -> None:
    assert await RedisRateLimiter(client, namespace=ns).try_acquire("p", RateLimit()) == 0.0


@pytest.mark.usefixtures("_cleanup")
async def test_circuit_breaker_shared_through_valkey(client: aioredis.Redis, ns: str) -> None:
    store = RedisStateStore(client)
    a = CircuitBreaker(store, failure_threshold=2, cooldown_s=1, namespace=ns)
    b = CircuitBreaker(store, failure_threshold=2, cooldown_s=1, namespace=ns)  # another worker
    await a.record_failure("p", "HTTP 503")
    assert (await b.status("p")).state is CircuitState.CLOSED
    await a.record_failure("p", "HTTP 503")
    with pytest.raises(ProviderDown, match="HTTP 503"):
        await b.before_call("p")
    await asyncio.sleep(1.1)
    assert (await b.status("p")).state is CircuitState.HALF_OPEN
    results = await asyncio.gather(a.before_call("p"), b.before_call("p"), return_exceptions=True)
    assert sum(r is None for r in results) == 1  # exactly one half-open probe across workers
    await b.record_success("p")
    status = await a.status("p")
    assert status.state is CircuitState.CLOSED
    assert status.consecutive_failures == 0


@pytest.mark.usefixtures("_cleanup")
async def test_state_store_and_cache_round_trip(client: aioredis.Redis, ns: str) -> None:
    store = RedisStateStore(client)
    assert await store.get(f"{ns}:missing") is None
    assert await store.set(f"{ns}:k", "v", only_if_absent=True)
    assert not await store.set(f"{ns}:k", "w", only_if_absent=True)
    assert await store.get(f"{ns}:k") == "v"
    assert await store.incr(f"{ns}:n") == 1
    await store.delete()
    await store.delete(f"{ns}:k")
    assert await store.get(f"{ns}:k") is None
    assert await store.now() > 1.7e9

    cache = ResponseCache(store, namespace=ns)
    await cache.put("fp", "payload", ttl_s=60)
    await cache.put("never", "payload", ttl_s=0)
    assert await cache.get("fp") == "payload"
    assert await cache.get("never") is None
    assert 0 < await client.ttl(f"{ns}:fp") <= 60


async def test_registry_builds_every_adapter_on_valkey(client: aioredis.Redis, settings: Settings) -> None:
    config = load_config(settings.config_dir)
    ctx = build_context(settings, config, client)
    adapters = build_adapters(ctx)
    assert set(adapters) == {cls.name for cls in ADAPTERS}
    assert ctx.flags == effective_flags(config, settings)
    assert ctx.flags["provider_fred"] is False  # D-010: FRED stays off unless the operator opts in
