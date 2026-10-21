"""``python -m rd_worker.smoke`` — enqueue a probe and wait until the worker has processed it."""

from __future__ import annotations

import secrets
import sys
import time

import redis

from rd_core.settings import get_settings
from rd_worker.broker import ECHO_KEY_PREFIX, QUEUE_DEFAULT, make_broker


def run(timeout_s: float = 30.0) -> bool:
    settings = get_settings()
    broker = make_broker(settings)
    nonce = secrets.token_hex(8)
    from dramatiq import Message  # noqa: PLC0415 - avoid importing the worker actor module (it boots a worker)

    broker.declare_queue(QUEUE_DEFAULT)
    broker.enqueue(Message(queue_name=QUEUE_DEFAULT, actor_name="system_echo", args=(nonce,), kwargs={}, options={}))
    client = redis.Redis.from_url(str(settings.redis_url))
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if client.get(f"{ECHO_KEY_PREFIX}{nonce}") == b"ok":
                return True
            time.sleep(0.25)
        return False
    finally:
        client.close()


def main() -> None:
    ok = run()
    sys.stdout.write("worker pipeline: ok\n" if ok else "worker pipeline: TIMEOUT\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
