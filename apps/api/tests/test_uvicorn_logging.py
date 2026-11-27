"""Runs the real ASGI entry point under uvicorn and checks that every log line is JSON and secrets never appear."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_uvicorn_output_is_json_and_redacted(tmp_path: Path) -> None:
    port = _free_port()
    secret = "sk-" + "ant-" + "api03-" + "Q" * 40
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql+asyncpg://nobody@127.0.0.1:1/none",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "LOG_FORMAT": "json",
        "SERVICE_NAME": "rd-api",
    }
    log_path = tmp_path / "uvicorn.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, test-only
            [
                sys.executable,
                "-m",
                "uvicorn",
                "rd_api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            cwd=REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    urllib.request.urlopen(f"{base}/healthz", timeout=1)  # noqa: S310 - local test server
                    break
                except OSError:
                    time.sleep(0.1)
            with urllib.request.urlopen(f"{base}/v1/meta?api_key={secret}&token={secret}", timeout=5) as r:  # noqa: S310
                assert r.status == 200
        finally:
            proc.terminate()
            proc.wait(timeout=20)
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    assert lines, "uvicorn produced no output"
    assert secret not in log_path.read_text()
    records = [json.loads(ln) for ln in lines]  # every line must be JSON
    assert any(r.get("event") == "http.request" and r.get("route") == "/v1/meta" for r in records)
    assert any(
        "Uvicorn running" in str(r.get("event")) or "Application startup" in str(r.get("event")) for r in records
    )
    assert all(r.get("service") == "rd-api" for r in records)
