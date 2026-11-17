"""Process-level integration: API readiness against real services and the worker pipeline end to end."""

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from fastapi.testclient import TestClient

from rd_api.app import create_app
from rd_core.bootstrap import Runtime
from rd_core.config import load_config
from rd_core.settings import Settings
from rd_data.resilience import CircuitBreaker, RedisStateStore
from rd_worker import smoke

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


def test_api_ready_against_real_services(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings=settings, config=load_config(settings.config_dir)))) as c:
        r = c.get("/readyz")
        assert r.status_code == 200, r.text
        assert r.json()["checks"]["database"]["timescaledb"]


def test_open_circuit_shows_on_the_status_page(settings: Settings) -> None:
    """PROMPT §5.1: a provider's circuit breaker shows on the status page."""

    async def trip() -> None:
        client = aioredis.Redis.from_url(str(settings.redis_url))
        breaker = CircuitBreaker(RedisStateStore(client), failure_threshold=1)
        await breaker.record_failure("cftc", "HTTP 503")
        await client.aclose()

    async def reset() -> None:
        client = aioredis.Redis.from_url(str(settings.redis_url))
        await CircuitBreaker(RedisStateStore(client)).record_success("cftc")
        await client.aclose()

    asyncio.run(trip())
    try:
        with TestClient(create_app(Runtime(settings=settings, config=load_config(settings.config_dir)))) as c:
            body = c.get("/v1/status").json()
        assert body["providers"]["cftc"]["circuit"] == "open"
        assert body["providers"]["cftc"]["last_error"] == "HTTP 503"
        assert "cftc" in body["degraded_providers"]
        assert body["providers"]["fred"]["enabled"] is False  # D-010
        assert body["providers"]["sec_edgar"]["circuit"] == "closed"
    finally:
        asyncio.run(reset())


@pytest.fixture
def worker_process() -> Iterator[subprocess.Popen[bytes]]:
    env = {**os.environ, "SERVICE_NAME": "rd-worker-test"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "rd_worker.main", "--processes", "1", "--threads", "2"],
        cwd=REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_worker_processes_enqueued_probe(worker_process: subprocess.Popen[bytes]) -> None:
    assert worker_process.poll() is None, "worker exited during start-up"
    assert smoke.run(timeout_s=30)
