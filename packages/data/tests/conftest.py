"""Shared helpers for adapter tests. Synthetic payloads here are *unit fixtures* exercising parsing branches; contract
tests against real recorded responses live in test_contracts.py (cassettes/)."""

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from rd_core.clock import FixedClock
from rd_core.settings import DeploymentMode, Settings
from rd_data.adapters.base import AdapterContext
from rd_data.resilience import CircuitBreaker, LocalRateLimiter, MemoryStateStore, ResponseCache

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def make_ctx(
    handler: Handler,
    *,
    now: datetime | None = None,
    mode: DeploymentMode = DeploymentMode.HOSTED,
    flags: dict[str, bool] | None = None,
    cache: bool = True,
    **settings: object,
) -> tuple[AdapterContext, Recorder]:
    rec = Recorder(handler)
    store = MemoryStateStore()
    ctx = AdapterContext(
        settings=Settings(deployment_mode=mode, **settings),  # type: ignore[arg-type]  # test-only passthrough
        limiter=LocalRateLimiter(),
        breaker=CircuitBreaker(store),
        cache=ResponseCache(store) if cache else None,
        clock=FixedClock(now or datetime(2026, 10, 21, 12, tzinfo=UTC)),
        transport=httpx.MockTransport(rec),
        flags=flags or {},
    )
    return ctx, rec


@pytest.fixture
def ctx_factory() -> Callable[..., tuple[AdapterContext, Recorder]]:
    return make_ctx
