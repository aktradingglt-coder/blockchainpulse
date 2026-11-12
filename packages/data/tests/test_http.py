import random
from collections.abc import Callable

import httpx
import pytest

from rd_data.errors import InvalidArgs, NotFound, ProviderDown, RateLimited
from rd_data.http import HttpFetcher, display_url, redact_params
from rd_data.resilience import CircuitBreaker, CircuitState, LocalRateLimiter, MemoryStateStore, RateLimit

HOST = "api.example.gov"


def _fetcher(
    handler: Callable[[httpx.Request], httpx.Response], *, attempts: int = 3
) -> tuple[HttpFetcher, list[float], CircuitBreaker]:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    breaker = CircuitBreaker(MemoryStateStore(), failure_threshold=2, cooldown_s=60)
    f = HttpFetcher(
        provider="p",
        allowed_hosts=frozenset({HOST}),
        limiter=LocalRateLimiter(),
        rate_limit=RateLimit(),
        breaker=breaker,
        transport=httpx.MockTransport(handler),
        max_attempts=attempts,
        rng=random.Random(1),
        sleep=fake_sleep,
    )
    return f, slept, breaker


async def test_success_returns_body_and_redacted_url() -> None:
    f, _, _ = _fetcher(lambda r: httpx.Response(200, json={"ok": True}))
    res = await f.get(f"https://{HOST}/v1/x", {"api_key": "SECRETVALUE123", "q": "a"})
    assert res.json() == {"ok": True}
    assert "SECRETVALUE123" not in res.url
    assert "api_key=[REDACTED]" in res.url
    await f.aclose()


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, NotFound), (400, InvalidArgs), (422, InvalidArgs), (401, ProviderDown), (403, ProviderDown)],
)
async def test_client_errors_map_to_typed_errors(status: int, error: type[Exception]) -> None:
    f, slept, _ = _fetcher(lambda r: httpx.Response(status))
    with pytest.raises(error):
        await f.get(f"https://{HOST}/x")
    assert slept == []  # never retried


async def test_retry_then_success_honours_retry_after() -> None:
    calls = iter([httpx.Response(429, headers={"retry-after": "5"}), httpx.Response(503), httpx.Response(200, json=[])])
    f, slept, breaker = _fetcher(lambda r: next(calls), attempts=4)
    res = await f.get(f"https://{HOST}/x")
    assert res.status == 200
    assert slept[0] >= 5.0  # Retry-After respected
    assert len(slept) == 2
    assert (await breaker.status("p")).consecutive_failures == 0  # success resets


async def test_rate_limited_after_retries() -> None:
    f, slept, _ = _fetcher(lambda r: httpx.Response(429, headers={"retry-after": "2"}), attempts=2)
    with pytest.raises(RateLimited) as info:
        await f.get(f"https://{HOST}/x")
    assert info.value.retry_after_s == 2.0
    assert len(slept) == 1


async def test_server_errors_exhaust_and_open_circuit() -> None:
    f, _, breaker = _fetcher(lambda r: httpx.Response(502), attempts=2)
    with pytest.raises(ProviderDown, match="unavailable after 2"):
        await f.get(f"https://{HOST}/x")
    assert (await breaker.status("p")).state is CircuitState.OPEN
    with pytest.raises(ProviderDown, match="circuit open"):
        await f.get(f"https://{HOST}/x")


async def test_transport_errors_retry_then_fail() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    f, slept, _ = _fetcher(boom, attempts=3)
    with pytest.raises(ProviderDown, match="ConnectError"):
        await f.get(f"https://{HOST}/x")
    assert len(slept) == 2


@pytest.mark.parametrize(
    "url",
    ["http://api.example.gov/x", "https://evil.example.com/x", "https://169.254.169.254/latest", "file:///etc/passwd"],
)
async def test_ssrf_guard_refuses_non_allow_listed_targets(url: str) -> None:
    f, _, _ = _fetcher(lambda r: httpx.Response(200))
    with pytest.raises(InvalidArgs, match="non-allow-listed"):
        await f.get(url)


def test_param_redaction() -> None:
    assert redact_params({"apikey": "x", "token": "y", "series": "Z"}) == {
        "apikey": "REDACTED",
        "token": "REDACTED",
        "series": "Z",
    }
    assert display_url("https://h/x", {}) == "https://h/x"
    assert display_url("https://h/x", {"b": 2, "a": 1}) == "https://h/x?a=1&b=2"


async def test_invalid_json_is_provider_down() -> None:
    f, _, _ = _fetcher(lambda r: httpx.Response(200, content=b"<html>"))
    res = await f.get(f"https://{HOST}/x")
    with pytest.raises(ProviderDown, match="invalid JSON"):
        res.json()
    assert res.text == "<html>"
