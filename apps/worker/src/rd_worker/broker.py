"""Dramatiq broker configuration shared by the worker and by producers (API, scheduler)."""

from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import AgeLimit, Callbacks, Pipelines, Retries, ShutdownNotifications, TimeLimit
from dramatiq.middleware.asyncio import AsyncIO

from rd_core.settings import Settings

QUEUE_DEFAULT = "default"
#: Redis key prefix for the end-to-end pipeline probe (``system_echo``).
ECHO_KEY_PREFIX = "rdesk:echo:"
#: Retry policy: exponential backoff from 1 s to 5 min, then the message lands in the dead-letter set.
MAX_RETRIES = 5
MIN_BACKOFF_MS = 1_000
MAX_BACKOFF_MS = 300_000
#: Dead-lettered messages are kept for 14 days for inspection and replay.
DEAD_MESSAGE_TTL_MS = 14 * 24 * 3600 * 1000


def make_broker(settings: Settings) -> RedisBroker:
    broker = RedisBroker(
        url=str(settings.redis_url),
        namespace="rdesk",
        dead_message_ttl=DEAD_MESSAGE_TTL_MS,
        middleware=[
            AgeLimit(),
            TimeLimit(time_limit=15 * 60 * 1000),
            ShutdownNotifications(),
            Callbacks(),
            Pipelines(),
            Retries(max_retries=MAX_RETRIES, min_backoff=MIN_BACKOFF_MS, max_backoff=MAX_BACKOFF_MS),
            AsyncIO(),
        ],
    )
    dramatiq.set_broker(broker)
    return broker
