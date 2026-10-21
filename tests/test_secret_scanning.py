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
    found = shutil.which("gitleaks") or (
        str(REPO / ".tools/bin/gitleaks") if (REPO / ".tools/bin/gitleaks").exists() else None
    )
    if found is None:
        pytest.fail("gitleaks not installed: run `make tools` (CI installs it with a pinned checksum)")
    return found


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
