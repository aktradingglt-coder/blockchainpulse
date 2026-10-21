"""Shared-state resilience primitives for provider calls (PROMPT §5.1).

* :class:`RedisRateLimiter` — token buckets shared by every worker through one atomic Lua script that uses the Redis
  server clock, so N workers together never exceed a provider's published limit.
* :class:`CircuitBreaker` — opens after N consecutive failures, admits one probe after a cool-down (half-open), and
  exposes its state for the status page.
* :class:`ResponseCache` — raw provider bodies keyed by the normalised request, TTL by data type.
* :func:`backoff_delays` — exponential backoff with full jitter for 429/5xx retries.

Each has an in-memory twin with identical semantics for unit tests and single-process tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import redis.asyncio as aioredis

from rd_data.errors import ProviderDown, RateLimited

# --------------------------------------------------------------------------------------------- rate limiting


@dataclass(frozen=True)
class RateLimit:
    """Published provider limits; ``None`` means no limit on that window."""

    per_second: float | None = None
    per_minute: float | None = None
    per_day: float | None = None

    def buckets(self) -> list[tuple[str, float, float]]:
        """(window name, capacity, refill tokens per second).

        Capacity is at least one token, so a fractional limit (``per_second=0.5``) means one call every two seconds
        rather than a bucket that can never hold a whole token.
        """
        out = []
        if self.per_second:
            out.append(("s", max(1.0, self.per_second), self.per_second))
        if self.per_minute:
            out.append(("m", max(1.0, self.per_minute), self.per_minute / 60.0))
        if self.per_day:
            out.append(("d", max(1.0, self.per_day), self.per_day / 86_400.0))
        return out


class RateLimiter(Protocol):
    async def acquire(self, provider: str, limit: RateLimit, *, timeout_s: float = 30.0) -> None:
        """Wait for a token in every bucket; raise :class:`RateLimited` if none is available within the timeout."""
        ...


# KEYS: bucket keys; ARGV: [n, cap1, rate1, cap2, rate2, ...]. Returns 0 if tokens were taken, else wait in ms.
_BUCKET_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local n = tonumber(ARGV[1])
local wait = 0
local state = {}
for i = 1, n do
  local cap = tonumber(ARGV[2 * i])
  local rate = tonumber(ARGV[2 * i + 1])
  local v = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
  local tokens = tonumber(v[1]) or cap
  local ts = tonumber(v[2]) or now
  tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
  state[i] = tokens
  if tokens < 0.999999 then
    local w = math.max(0.001, (1 - tokens) / rate)
    if w > wait then wait = w end
  end
end
if wait > 0 then
  for i = 1, n do
    redis.call('HSET', KEYS[i], 'tokens', state[i], 'ts', now)
  end
  return math.ceil(wait * 1000)
end
for i = 1, n do
  local cap = tonumber(ARGV[2 * i])
  local rate = tonumber(ARGV[2 * i + 1])
  redis.call('HSET', KEYS[i], 'tokens', state[i] - 1, 'ts', now)
  redis.call('EXPIRE', KEYS[i], math.ceil(cap / rate) + 60)
end
return 0
"""


class RedisRateLimiter:
    def __init__(self, client: aioredis.Redis, *, namespace: str = "rdesk:rl") -> None:
        self._client = client
        self._ns = namespace
        self._script = client.register_script(_BUCKET_LUA)

    async def try_acquire(self, provider: str, limit: RateLimit) -> float:
        """Take one token from every bucket; returns 0.0 on success, else seconds to wait."""
        buckets = limit.buckets()
        if not buckets:
            return 0.0
        keys = [f"{self._ns}:{provider}:{name}" for name, _, _ in buckets]
        args: list[float] = [len(buckets)]
        for _, cap, rate in buckets:
            args.extend([cap, rate])
        wait_ms = int(await self._script(keys=keys, args=args))
        return wait_ms / 1000.0

    async def acquire(self, provider: str, limit: RateLimit, *, timeout_s: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            wait = await self.try_acquire(provider, limit)
            if wait == 0:
                return
            if time.monotonic() + wait > deadline:
                raise RateLimited(f"local rate limit for {provider} exhausted", provider=provider, retry_after_s=wait)
            await asyncio.sleep(wait)


_EPSILON = 1e-6
_MIN_WAIT_S = 0.001


class LocalRateLimiter:
    """Single-process token buckets with an injectable clock (same maths as the Lua script)."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], object] | None = None
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._state: dict[str, tuple[float, float]] = {}

    def try_acquire(self, provider: str, limit: RateLimit) -> float:
        now = self._clock()
        wait = 0.0
        refreshed: dict[str, float] = {}
        for name, cap, rate in limit.buckets():
            key = f"{provider}:{name}"
            tokens, ts = self._state.get(key, (cap, now))
            tokens = min(cap, tokens + max(0.0, now - ts) * rate)
            refreshed[key] = tokens
            if tokens < 1 - _EPSILON:  # epsilon: float refill must not stall just below one token
                wait = max(wait, _MIN_WAIT_S, (1 - tokens) / rate)
        for key, tokens in refreshed.items():
            self._state[key] = (max(0.0, tokens - 1) if wait == 0 else tokens, now)
        return wait

    async def acquire(self, provider: str, limit: RateLimit, *, timeout_s: float = 30.0) -> None:
        waited = 0.0
        while True:
            wait = self.try_acquire(provider, limit)
            if wait == 0:
                return
            if waited + wait > timeout_s:
                raise RateLimited(f"local rate limit for {provider} exhausted", provider=provider, retry_after_s=wait)
            waited += wait
            if self._sleep is not None:
                self._sleep(wait)
            else:
                await asyncio.sleep(wait)


# --------------------------------------------------------------------------------------------- circuit breaker


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitStatus:
    provider: str
    state: CircuitState
    consecutive_failures: int
    opened_at: float | None
    last_error: str | None


class StateStore(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, *, ttl_s: int | None = None, only_if_absent: bool = False) -> bool: ...
    async def incr(self, key: str) -> int: ...
    async def delete(self, *keys: str) -> None: ...
    async def now(self) -> float: ...


class RedisStateStore:
    def __init__(self, client: aioredis.Redis) -> None:
        self._c = client

    async def get(self, key: str) -> str | None:
        value = await self._c.get(key)
        return value.decode() if isinstance(value, bytes) else value

    async def set(self, key: str, value: str, *, ttl_s: int | None = None, only_if_absent: bool = False) -> bool:
        return bool(await self._c.set(key, value, ex=ttl_s, nx=only_if_absent))

    async def incr(self, key: str) -> int:
        return int(await self._c.incr(key))

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._c.delete(*keys)

    async def now(self) -> float:
        sec, usec = await self._c.time()
        return float(sec) + float(usec) / 1e6


class MemoryStateStore:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._data: dict[str, tuple[str, float | None]] = {}

    def _live(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and self._clock() >= expires:
            del self._data[key]
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def set(self, key: str, value: str, *, ttl_s: int | None = None, only_if_absent: bool = False) -> bool:
        if only_if_absent and self._live(key) is not None:
            return False
        self._data[key] = (value, self._clock() + ttl_s if ttl_s else None)
        return True

    async def incr(self, key: str) -> int:
        value = int(self._live(key) or 0) + 1
        expires = self._data.get(key, ("", None))[1]
        self._data[key] = (str(value), expires)
        return value

    async def delete(self, *keys: str) -> None:
        for k in keys:
            self._data.pop(k, None)

    async def now(self) -> float:
        return self._clock()


#: The half-open probe held by the current task. An adapter sets it for a whole fetch, so every HTTP call of that
#: fetch (FRED: series + observations; Kraken: instruments + data) belongs to the one probe it was admitted with.
PROBE_TOKEN: ContextVar[str | None] = ContextVar("rd_probe_token", default=None)
#: Upper bound on one fetch: the 60 s retry-wait budget plus per-attempt timeouts.
PROBE_MAX_FETCH_S = 300


class CircuitBreaker:
    """Consecutive-failure breaker shared through a :class:`StateStore`."""

    def __init__(
        self, store: StateStore, *, failure_threshold: int = 5, cooldown_s: float = 60.0, namespace: str = "rdesk:cb"
    ) -> None:
        self._store = store
        self._threshold = failure_threshold
        self._cooldown = cooldown_s
        self._ns = namespace

    def _k(self, provider: str, part: str) -> str:
        return f"{self._ns}:{provider}:{part}"

    async def status(self, provider: str) -> CircuitStatus:
        fails = int(await self._store.get(self._k(provider, "fails")) or 0)
        opened_raw = await self._store.get(self._k(provider, "opened_at"))
        last_error = await self._store.get(self._k(provider, "last_error"))
        opened_at = float(opened_raw) if opened_raw else None
        state = CircuitState.CLOSED
        if opened_at is not None:
            now = await self._store.now()
            state = CircuitState.HALF_OPEN if now - opened_at >= self._cooldown else CircuitState.OPEN
        return CircuitStatus(provider, state, fails, opened_at, last_error)

    async def before_call(self, provider: str) -> None:
        """Raise :class:`ProviderDown` while open; in half-open, admit exactly one probe per cool-down."""
        status = await self.status(provider)
        if status.state is CircuitState.CLOSED:
            return
        if status.state is CircuitState.HALF_OPEN:
            token = PROBE_TOKEN.get()
            key = self._k(provider, "probe")
            if token is not None and await self._store.get(key) == token:
                return  # a later call of the fetch that already holds the probe
            # The probe outlives the slowest possible fetch (retry budget + timeouts), so no second probe is admitted
            # while the first is still running.
            ttl = int(self._cooldown + PROBE_MAX_FETCH_S)
            got_probe = await self._store.set(key, token or "probe", ttl_s=ttl, only_if_absent=True)
            if got_probe:
                return
        raise ProviderDown(f"circuit open for {provider} (last error: {status.last_error})", provider=provider)

    async def release_probe(self, provider: str, token: str) -> None:
        """Give back a half-open probe that ended without a verdict (rate-limited, bad request): the next fetch may
        probe at once instead of waiting for the probe to expire. A success or failure has already settled it."""
        key = self._k(provider, "probe")
        if await self._store.get(key) == token:
            await self._store.delete(key)

    async def record_success(self, provider: str) -> None:
        await self._store.delete(
            self._k(provider, "fails"),
            self._k(provider, "opened_at"),
            self._k(provider, "probe"),
            self._k(provider, "last_error"),
        )

    async def record_failure(self, provider: str, error: str) -> None:
        await self._store.set(self._k(provider, "last_error"), error[:200])
        fails = await self._store.incr(self._k(provider, "fails"))
        status = await self.status(provider)
        if fails >= self._threshold and status.state is not CircuitState.OPEN:
            # (Re)open: a failed half-open probe restarts the cool-down.
            await self._store.set(self._k(provider, "opened_at"), str(await self._store.now()))
            await self._store.delete(self._k(provider, "probe"))


# ------------------------------------------------------------------------------------------------- caching


def request_fingerprint(provider: str, kind: str, params: Mapping[str, object]) -> str:
    """Stable key for a normalised request: provider + kind + params sorted, canonical JSON, SHA-256."""
    canonical = json.dumps(
        {"p": provider, "k": kind, "a": dict(sorted(params.items()))},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ResponseCache:
    def __init__(self, store: StateStore, *, namespace: str = "rdesk:cache") -> None:
        self._store = store
        self._ns = namespace

    async def get(self, fingerprint: str) -> str | None:
        return await self._store.get(f"{self._ns}:{fingerprint}")

    async def drop(self, fingerprint: str) -> None:
        """Remove an entry that turned out to be unusable (corrupt or malformed), so the next call refetches."""
        await self._store.delete(f"{self._ns}:{fingerprint}")

    async def put(self, fingerprint: str, payload: str, ttl_s: int) -> None:
        if ttl_s > 0:
            await self._store.set(f"{self._ns}:{fingerprint}", payload, ttl_s=ttl_s)


# ------------------------------------------------------------------------------------------------- retries

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def backoff_delays(
    attempts: int, *, base_s: float = 0.5, cap_s: float = 30.0, rng: random.Random | None = None
) -> Iterator[float]:
    """Full-jitter exponential backoff: delay_k ~ U(0, min(cap, base * 2**k))."""
    r = rng or random.Random()  # noqa: S311 - jitter, not cryptography
    for k in range(attempts):
        yield r.uniform(0, min(cap_s, base_s * (2**k)))


def retry_after_seconds(header: str | None, *, cap_s: float = 120.0) -> float | None:
    """Parse a numeric ``Retry-After`` header (HTTP-date forms are ignored: use backoff instead)."""
    if header is None:
        return None
    try:
        value = float(header)
    except ValueError:
        return None
    return min(max(value, 0.0), cap_s)
