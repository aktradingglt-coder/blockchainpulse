import threading
from types import SimpleNamespace

from rd_worker.heartbeat import worker_threads_alive


def _thread(alive: bool) -> threading.Thread:
    t = threading.Thread(target=lambda: None)
    if alive:
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
    return t


def test_threads_alive_rules() -> None:
    assert worker_threads_alive(None)
    alive = SimpleNamespace(consumers={"default": _thread(True)}, workers=[_thread(True)])
    assert worker_threads_alive(alive)  # type: ignore[arg-type]  # duck-typed stand-in for dramatiq.Worker
    dead_consumer = SimpleNamespace(consumers={"default": _thread(False)}, workers=[_thread(True)])
    assert not worker_threads_alive(dead_consumer)  # type: ignore[arg-type]  # duck-typed stand-in
    no_threads = SimpleNamespace(consumers={}, workers=[])
    assert not worker_threads_alive(no_threads)  # type: ignore[arg-type]  # duck-typed stand-in
