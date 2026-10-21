"""Worker paths that need no running services."""

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from rd_core.settings import Settings
from rd_worker import actors, smoke
from rd_worker.heartbeat import HeartbeatMiddleware


def test_echo_nonce_validation() -> None:
    assert actors.validate_nonce("abc123") == "abc123"
    for bad in ("", "x" * 65, "not alnum!"):
        with pytest.raises(ValueError, match="nonce"):
            actors.validate_nonce(bad)


def test_heartbeat_skips_beats_while_threads_are_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    beats: list[str] = []

    async def fake_beat(_engine: Any, **kw: Any) -> None:
        beats.append(kw["instance_id"])

    monkeypatch.setattr("rd_worker.heartbeat.beat", fake_beat)
    mw = HeartbeatMiddleware(Settings(), service="rd-unit", interval=0.05)
    dead = threading.Thread(target=lambda: None)
    mw.after_worker_boot(None, SimpleNamespace(consumers={"q": dead}, workers=[dead]))  # type: ignore[arg-type]  # duck-typed worker
    threading.Event().wait(0.3)
    mw.before_worker_shutdown(None, None)  # type: ignore[arg-type]  # hook arguments unused
    assert beats == []


def test_heartbeat_survives_beat_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def failing_beat(_engine: Any, **_kw: Any) -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError("db down")

    monkeypatch.setattr("rd_worker.heartbeat.beat", failing_beat)
    mw = HeartbeatMiddleware(Settings(), service="rd-unit", interval=0.05)
    mw.after_worker_boot(None, None)  # type: ignore[arg-type]  # hook arguments unused
    threading.Event().wait(0.3)
    mw.before_worker_shutdown(None, None)  # type: ignore[arg-type]  # hook arguments unused
    assert calls >= 2


def test_shutdown_before_boot_is_safe() -> None:
    HeartbeatMiddleware(Settings(), service="rd-unit").before_worker_shutdown(None, None)  # type: ignore[arg-type]  # unused


class _FakeBroker:
    def declare_queue(self, _name: str) -> None:
        return None

    def enqueue(self, _message: object) -> None:
        return None


class _FakeRedis:
    def __init__(self, value: bytes | None) -> None:
        self.value = value

    def get(self, _key: str) -> bytes | None:
        return self.value

    def close(self) -> None:
        return None


@pytest.mark.parametrize(("stored", "expected"), [(b"ok", True), (None, False)])
def test_smoke_run_outcomes(monkeypatch: pytest.MonkeyPatch, stored: bytes | None, expected: bool) -> None:
    monkeypatch.setattr(smoke, "make_broker", lambda _s: _FakeBroker())
    monkeypatch.setattr("redis.Redis.from_url", lambda _url: _FakeRedis(stored))
    assert smoke.run(timeout_s=0.3) is expected


@pytest.mark.parametrize(("ok", "code", "text"), [(True, 0, "ok"), (False, 1, "TIMEOUT")])
def test_smoke_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ok: bool, code: int, text: str
) -> None:
    monkeypatch.setattr(smoke, "run", lambda: ok)
    with pytest.raises(SystemExit) as info:
        smoke.main()
    assert info.value.code == code
    assert text in capsys.readouterr().out
