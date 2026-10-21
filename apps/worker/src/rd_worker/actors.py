"""Worker entry module: ``dramatiq rd_worker.actors``. Importing it configures the broker and registers actors."""

from __future__ import annotations

import dramatiq
import redis.asyncio as aioredis

from rd_core.bootstrap import bootstrap
from rd_core.logging import get_logger
from rd_worker.broker import ECHO_KEY_PREFIX, QUEUE_DEFAULT, make_broker
from rd_worker.heartbeat import HeartbeatMiddleware

SERVICE = "rd-worker"
ECHO_TTL_SECONDS = 300

_runtime = bootstrap(SERVICE)
broker = make_broker(_runtime.settings)
broker.add_middleware(HeartbeatMiddleware(_runtime.settings, service=SERVICE))
_log = get_logger(__name__)


@dramatiq.actor(queue_name=QUEUE_DEFAULT, max_retries=3)
async def system_echo(nonce: str) -> None:
    """End-to-end pipeline probe: API/CLI enqueues a nonce, the worker writes it back to Redis."""
    if not nonce or len(nonce) > 64 or not nonce.isalnum():
        raise ValueError("nonce must be 1-64 alphanumeric characters")
    client = aioredis.from_url(str(_runtime.settings.redis_url))
    try:
        await client.set(f"{ECHO_KEY_PREFIX}{nonce}", "ok", ex=ECHO_TTL_SECONDS)
    finally:
        await client.aclose()
    _log.info("system.echo", nonce=nonce)
