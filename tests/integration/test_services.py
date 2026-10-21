"""Process-level integration: API readiness against real services and the worker pipeline end to end."""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rd_api.app import create_app
from rd_core.bootstrap import Runtime
from rd_core.config import load_config
from rd_core.settings import Settings
from rd_worker import smoke

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


def test_api_ready_against_real_services(settings: Settings) -> None:
    with TestClient(create_app(Runtime(settings=settings, config=load_config(settings.config_dir)))) as c:
        r = c.get("/readyz")
        assert r.status_code == 200, r.text
        assert r.json()["checks"]["database"]["timescaledb"]


@pytest.fixture
def worker_process() -> Iterator[subprocess.Popen[bytes]]:
    env = {**os.environ, "SERVICE_NAME": "rd-worker-test"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "rd_worker.main", "--processes", "1", "--threads", "2"],
        cwd=REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    yield proc
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_worker_processes_enqueued_probe(worker_process: subprocess.Popen[bytes]) -> None:
    assert worker_process.poll() is None, "worker exited during start-up"
    assert smoke.run(timeout_s=30)
