"""rd_db paths that need no database: CLI argument handling, error reporting, factories."""

import pytest
from alembic import command
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker

from rd_core.settings import Settings
from rd_db import health, healthcheck, migrate
from rd_db.engine import make_engine, make_session_factory


def test_session_factory() -> None:
    engine = make_engine(Settings(), pool_size=1)
    assert isinstance(make_session_factory(engine), async_sessionmaker)


def test_head_revision_requires_a_head(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoHead:
        def get_current_head(self) -> None:
            return None

    monkeypatch.setattr("alembic.script.ScriptDirectory.from_config", lambda _cfg: _NoHead())
    with pytest.raises(RuntimeError, match="no Alembic head"):
        health.head_revision()


def test_healthcheck_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["healthcheck"])
    with pytest.raises(SystemExit) as info:
        healthcheck.main()
    assert info.value.code == 2


@pytest.mark.parametrize(("outcome", "code"), [(True, 0), (False, 1)])
def test_healthcheck_exit_codes(monkeypatch: pytest.MonkeyPatch, outcome: bool, code: int) -> None:
    async def fake_check(_service: str, _host: str) -> bool:
        return outcome

    monkeypatch.setattr("sys.argv", ["healthcheck", "rd-worker"])
    monkeypatch.setattr(healthcheck, "check", fake_check)
    with pytest.raises(SystemExit) as info:
        healthcheck.main()
    assert info.value.code == code


def test_healthcheck_reports_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "Pw" + "9x8y7z6w5v"

    async def failing_check(_service: str, _host: str) -> bool:
        raise ConnectionError("cannot reach postgresql://u:" + secret + "@db:5432/x")

    monkeypatch.setattr("sys.argv", ["healthcheck", "rd-worker"])
    monkeypatch.setattr(healthcheck, "check", failing_check)
    with pytest.raises(SystemExit) as info:
        healthcheck.main()
    assert info.value.code == 1
    out = capsys.readouterr()
    assert "unhealthy: ConnectionError" in out.out
    assert secret not in out.out + out.err
    assert "Traceback" not in out.out + out.err


def test_migrate_retries_refused_connections_only(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes: list[BaseException | None] = [ConnectionRefusedError(111, "refused"), ConnectionResetError(), None]
    calls: list[str] = []
    sleeps: list[float] = []

    def upgrade(_cfg: object, rev: str) -> None:
        calls.append(rev)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(command, "upgrade", upgrade)
    migrate.upgrade_to_head(sleep=sleeps.append)
    assert calls == ["head", "head", "head"]
    assert sleeps == [migrate.CONNECT_PAUSE_S, migrate.CONNECT_PAUSE_S]

    def bad_sql(_cfg: object, _rev: str) -> None:
        raise ProgrammingError("ALTER TABLE x", {}, Exception("syntax error"))

    monkeypatch.setattr(command, "upgrade", bad_sql)
    sleeps.clear()
    with pytest.raises(ProgrammingError):
        migrate.upgrade_to_head(sleep=sleeps.append)
    assert sleeps == []  # a failing migration is never retried

    def always_down(_cfg: object, _rev: str) -> None:
        raise ConnectionRefusedError(111, "refused")

    monkeypatch.setattr(command, "upgrade", always_down)
    with pytest.raises(ConnectionRefusedError):
        migrate.upgrade_to_head(sleep=sleeps.append)
    assert len(sleeps) == migrate.CONNECT_ATTEMPTS - 1
