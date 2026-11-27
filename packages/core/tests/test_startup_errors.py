"""Start-up failures must never echo secrets (PROMPT §1.5): bad URLs in env vars are reported redacted, as JSON."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("var", "value", "secret"),
    [
        ("REDIS_URL", "redis://:" + "Sup3rS3cretPassw0rdXYZ" + "@valkey:99999/0", "Sup3rS3cretPassw0rdXYZ"),
        ("DATABASE_URL", "postgresql+asyncpg://rdesk:" + "Tr0ub4dor&3xyz" + "@db:54321x/rdesk", "Tr0ub4dor&3xyz"),
    ],
)
@pytest.mark.parametrize("module", ["rd_scheduler.main", "rd_db.migrate"])
def test_invalid_url_setting_does_not_leak_password(var: str, value: str, secret: str, module: str) -> None:
    env = {**os.environ, var: value}
    proc = subprocess.run(  # noqa: S603 - fixed argv, test-only
        [sys.executable, "-m", module], cwd=REPO, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    for fragment in (secret, secret[-6:], secret[:6]):
        assert fragment not in output, output


def test_bootstrap_reports_invalid_settings_as_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rd_core import bootstrap as bootstrap_module  # noqa: PLC0415 - import after env patching
    from rd_core.settings import get_settings  # noqa: PLC0415

    monkeypatch.setenv("REDIS_URL", "redis://:" + "AnotherSecretValue99" + "@valkey:99999/0")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as info:
            bootstrap_module.bootstrap("rd-test")
        assert info.value.code == 2
        err = capsys.readouterr().err.strip().splitlines()[-1]
        record = json.loads(err)
        assert record["event"] == "service.startup_failed"
        assert record["error"] == "ValidationError"
        assert "AnotherSecretValue99" not in err
    finally:
        get_settings.cache_clear()
