"""Proves the secret scanner is active: a planted key is caught, and the committed tree is clean."""

import json
import os
import secrets
import shutil
import string
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _gitleaks() -> str:
    """Always the pinned, checksum-verified binary from `make tools`, never whatever is on PATH."""
    pinned = REPO / ".tools/bin/gitleaks"
    if not pinned.exists():
        pytest.fail("pinned gitleaks missing: run `make tools` (make test does this automatically)")
    return str(pinned)


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)  # noqa: S603, S607


def _random(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


@pytest.mark.parametrize(
    "planted",
    [
        lambda: "gh" + "p_" + _random(36),
        lambda: "sk-" + "ant-" + "api03-" + _random(40),
        lambda: "postgresql://admin:" + _random(20) + "@db.internal:5432/prod",
    ],
    ids=["github-pat", "anthropic-key", "url-password"],
)
def test_planted_secret_is_detected(tmp_path: Path, planted: object) -> None:
    assert callable(planted)
    _git(tmp_path, "init", "-q")
    (tmp_path / "settings.py").write_text(f'TOKEN = "{planted()}"\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "plant")
    result = subprocess.run(  # noqa: S603
        [_gitleaks(), "git", "--config", str(REPO / ".gitleaks.toml"), "--no-banner", "--redact", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_committable_tree_is_clean(tmp_path: Path) -> None:
    """Scan exactly what git would commit (tracked + untracked-but-not-ignored), never .env or caches."""
    listing = (
        subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],  # noqa: S607
            cwd=REPO,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .split("\0")
    )
    files = [f for f in listing if f and (REPO / f).is_file()]
    assert ".env" not in files
    for rel in files:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dst)
    result = subprocess.run(  # noqa: S603
        [_gitleaks(), "dir", "--config", str(REPO / ".gitleaks.toml"), "--no-banner", "--redact", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_empty_username_url_password_is_detected(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    scheme = "redis" + "://:"  # split so this file itself never contains a URL-with-password literal
    (tmp_path / "cfg.env").write_text("REDIS_URL=" + scheme + _random(24) + "@valkey:6379/0\n")
    (tmp_path / "client.py").write_text('r = from_url("' + scheme + _random(24) + '@cache.internal:6379/0")\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "plant")
    result = subprocess.run(  # noqa: S603
        [
            _gitleaks(),
            "git",
            "--config",
            str(REPO / ".gitleaks.toml"),
            "--no-banner",
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(tmp_path / "report.json"),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    findings = json.loads((tmp_path / "report.json").read_text())
    assert {f["File"] for f in findings} == {"cfg.env", "client.py"}


@pytest.mark.parametrize(
    "content",
    [
        lambda: "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/" + "1234567890123" + "/" + _random(60),
        lambda: "SENTRY_DSN=https://" + secrets.token_hex(16) + "@o450000.ingest.example.io/4500001",
        lambda: "REDIS_URL=" + "redis" + "://default:" + _random(6) + "/" + _random(10) + "@host:6379/0",
        lambda: "TELEGRAM_BOT_TOKEN=" + "123456789:" + _random(35),
    ],
    ids=["discord-webhook", "sentry-dsn", "url-password-with-slash", "telegram-token"],
)
def test_section19_secret_types_detected(tmp_path: Path, content: object) -> None:
    assert callable(content)
    _git(tmp_path, "init", "-q")
    (tmp_path / "deploy.env").write_text(content() + "\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "plant")
    result = subprocess.run(  # noqa: S603
        [_gitleaks(), "git", "--config", str(REPO / ".gitleaks.toml"), "--no-banner", "--redact", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize(
    "content",
    [
        lambda: "BILLING_API_KEY_ALT = '" + "pdl_" + "sdbx_apikey_" + _random(40) + "'",
        lambda: "x = '" + "pdl_" + "ntfset_" + _random(32) + "'",
        lambda: "DATABASE_URL=" + "postgresql" + "://admin:" + _random(6) + "$" + _random(8) + "@db.internal:5432/x",
        lambda: "DATABASE_URL=" + "postgresql" + "://admin:" + _random(6) + "@" + _random(8) + "@db.internal:5432/x",
    ],
    ids=["paddle-api-key", "paddle-webhook-secret", "url-password-with-dollar", "url-password-with-at"],
)
def test_round3_secret_shapes_detected(tmp_path: Path, content: object) -> None:
    assert callable(content)
    _git(tmp_path, "init", "-q")
    (tmp_path / "settings.py").write_text(content() + "\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "plant")
    result = subprocess.run(  # noqa: S603
        [_gitleaks(), "git", "--config", str(REPO / ".gitleaks.toml"), "--no-banner", "--redact", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_compose_placeholders_are_not_flagged(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "compose.yaml").write_text(
        "DATABASE_URL: postgresql+asyncpg://rdesk:${POSTGRES_PASSWORD:?run make dev}@db:5432/rdesk\n"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "placeholder")
    result = subprocess.run(  # noqa: S603
        [_gitleaks(), "git", "--config", str(REPO / ".gitleaks.toml"), "--no-banner", "--redact", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "content",
    [
        lambda: 'headers = {"Authorization": "Basic ' + _random(24) + '"}',
        lambda: "url = 'https://api.example.com/v1?api_key=" + secrets.token_hex(16) + "'",
        lambda: 'headers = {"x-api-key": "' + secrets.token_hex(16) + '"}',
    ],
    ids=["basic-auth", "hex-api-key-query", "hex-api-key-header"],
)
def test_round4_secret_shapes_detected(tmp_path: Path, content: object) -> None:
    assert callable(content)
    _git(tmp_path, "init", "-q")
    (tmp_path / "client.py").write_text(content() + "\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "plant")
    result = subprocess.run(  # noqa: S603
        [_gitleaks(), "git", "--config", str(REPO / ".gitleaks.toml"), "--no-banner", "--redact", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
