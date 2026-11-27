import random

import pytest

from rd_data.errors import ProviderDown, RateLimited
from rd_data.resilience import (
    CircuitBreaker,
    CircuitState,
    LocalRateLimiter,
    MemoryStateStore,
    RateLimit,
    ResponseCache,
    backoff_delays,
    request_fingerprint,
    retry_after_seconds,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_rate_limit_buckets() -> None:
    assert RateLimit().buckets() == []
    assert [b[0] for b in RateLimit(per_second=2, per_minute=60, per_day=1000).buckets()] == ["s", "m", "d"]


async def test_local_token_bucket_never_exceeds_rate() -> None:
    clock = FakeClock()
    limiter = LocalRateLimiter(clock, sleep=clock.sleep)
    limit = RateLimit(per_second=5)
    start = clock.t
    for _ in range(25):
        await limiter.acquire("p", limit)
    # 5 burst tokens, then 5 per second: 25 requests need at least 4 seconds.
    assert clock.t - start == pytest.approx(4.0, abs=1e-6)


async def test_local_limiter_respects_tightest_bucket_and_timeout() -> None:
    clock = FakeClock()
    limiter = LocalRateLimiter(clock, sleep=clock.sleep)
    limit = RateLimit(per_second=10, per_minute=3)
    for _ in range(3):
        await limiter.acquire("p", limit)
    assert limiter.try_acquire("p", limit) == pytest.approx(20.0)  # minute bucket: 1 token per 20 s
    with pytest.raises(RateLimited) as info:
        await limiter.acquire("p", limit, timeout_s=5)
    assert info.value.retry_after_s == pytest.approx(20.0)
    await limiter.acquire("other", limit)  # buckets are per provider


async def test_circuit_breaker_lifecycle() -> None:
    clock = FakeClock()
    store = MemoryStateStore(clock)
    cb = CircuitBreaker(store, failure_threshold=3, cooldown_s=60)
    await cb.before_call("p")
    for _ in range(2):
        await cb.record_failure("p", "HTTP 503")
    assert (await cb.status("p")).state is CircuitState.CLOSED
    await cb.record_failure("p", "HTTP 503")
    status = await cb.status("p")
    assert status.state is CircuitState.OPEN
    assert status.last_error == "HTTP 503"
    with pytest.raises(ProviderDown, match="circuit open"):
        await cb.before_call("p")
    clock.t += 61
    assert (await cb.status("p")).state is CircuitState.HALF_OPEN
    await cb.before_call("p")  # the single probe
    with pytest.raises(ProviderDown):
        await cb.before_call("p")  # second caller during the probe is refused
    await cb.record_failure("p", "HTTP 500")  # probe failed: re-open with a fresh cool-down
    assert (await cb.status("p")).state is CircuitState.OPEN
    clock.t += 61
    await cb.before_call("p")
    await cb.record_success("p")
    status = await cb.status("p")
    assert status.state is CircuitState.CLOSED
    assert status.consecutive_failures == 0


async def test_memory_store_ttl_and_cache() -> None:
    clock = FakeClock()
    store = MemoryStateStore(clock)
    assert await store.set("k", "v", ttl_s=10)
    assert not await store.set("k", "w", only_if_absent=True)
    clock.t += 11
    assert await store.get("k") is None
    assert await store.now() == clock.t
    cache = ResponseCache(store)
    await cache.put("fp", "payload", ttl_s=0)  # TTL 0 = do not cache
    assert await cache.get("fp") is None
    await cache.put("fp", "payload", ttl_s=30)
    assert await cache.get("fp") == "payload"
    await store.delete()  # no keys is a no-op


def test_fingerprint_is_order_insensitive() -> None:
    a = request_fingerprint("p", "bars", {"symbol": "X", "limit": 5})
    b = request_fingerprint("p", "bars", {"limit": 5, "symbol": "X"})
    assert a == b
    assert a != request_fingerprint("p", "bars", {"symbol": "Y", "limit": 5})


def test_backoff_and_retry_after() -> None:
    delays = list(backoff_delays(5, base_s=1, cap_s=4, rng=random.Random(7)))
    assert len(delays) == 5
    assert all(0 <= d <= min(4, 2**k) for k, d in enumerate(delays))
    assert delays == list(backoff_delays(5, base_s=1, cap_s=4, rng=random.Random(7)))  # deterministic with a seed
    assert retry_after_seconds(None) is None
    assert retry_after_seconds("7") == 7.0
    assert retry_after_seconds("-3") == 0.0
    assert retry_after_seconds("9999") == 120.0
    assert retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT") is None
