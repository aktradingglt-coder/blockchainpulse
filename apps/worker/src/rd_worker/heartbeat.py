"""Dramatiq middleware that records a heartbeat row for this worker process every few seconds."""

from __future__ import annotations

import asyncio
import os
import socket
import threading

from dramatiq import Broker, Middleware, Worker

from rd_core.logging import get_logger
from rd_core.settings import Settings
from rd_db.engine import make_engine
from rd_db.heartbeat import beat

HEARTBEAT_INTERVAL_SECONDS = 10.0


def instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def worker_threads_alive(worker: Worker | None) -> bool:
    """True when every consumer and worker thread of a booted Dramatiq worker is running."""
    if worker is None:
        return True  # no worker object (unit use): liveness of the process is all we can claim
    consumers = list(worker.consumers.values())
    threads = list(worker.workers)
    return bool(consumers) and bool(threads) and all(t.is_alive() for t in [*consumers, *threads])


def broker_reachable(broker: Broker | None) -> bool:
    """A PING round trip on the broker's own Redis client; brokers without a client (tests) count as reachable."""
    client = getattr(broker, "client", None)
    if client is None:
        return True
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001 - any connection error means the broker is unreachable
        return False


class HeartbeatMiddleware(Middleware):
    """Beats only while consumer/worker threads are alive and the broker answers PING, so a wedged or disconnected
    worker turns unhealthy (container healthcheck and /v1/status)."""

    def __init__(self, settings: Settings, *, service: str, interval: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
        self._settings = settings
        self._service = service
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker: Worker | None = None
        self._broker: Broker | None = None
        self._log = get_logger(__name__)

    def after_worker_boot(self, broker: Broker, worker: Worker) -> None:
        self._worker = worker
        self._broker = broker
        self._thread = threading.Thread(target=self._run, name="rd-heartbeat", daemon=True)
        self._thread.start()

    def before_worker_shutdown(self, broker: Broker, worker: Worker) -> None:  # noqa: ARG002 - dramatiq hook signature
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def _run(self) -> None:
        asyncio.run(self._loop())

    async def _loop(self) -> None:
        engine = make_engine(self._settings, pool_size=1)
        ident = instance_id()
        try:
            while not self._stop.is_set():
                if not worker_threads_alive(self._worker):
                    self._log.error("worker.threads_dead", service=self._service)
                    await asyncio.to_thread(self._stop.wait, self._interval)
                    continue
                if not await asyncio.to_thread(broker_reachable, self._broker):
                    self._log.error("worker.broker_unreachable", service=self._service)
                    await asyncio.to_thread(self._stop.wait, self._interval)
                    continue
                try:
                    await beat(engine, service=self._service, instance_id=ident)
                except Exception as exc:  # noqa: BLE001 - a failed beat must never kill the worker
                    self._log.warning("heartbeat.failed", error=type(exc).__name__)
                await asyncio.to_thread(self._stop.wait, self._interval)
        finally:
            await engine.dispose()
