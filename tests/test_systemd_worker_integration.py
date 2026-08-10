from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwen_voice_lab.app import create_app
from qwen_voice_lab.config import Settings

RUN_SYSTEMD = os.getenv("QVL_RUN_SYSTEMD_TESTS") == "1" and os.geteuid() == 0
pytestmark = pytest.mark.skipif(not RUN_SYSTEMD, reason="requires explicit root systemd test")


def build_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path]:
    runtime = Path(__file__).parent / "fixtures" / "fake_gpu_runtime.py"
    runtime.chmod(0o755)
    unit_file = tmp_path / "unit.txt"
    monkeypatch.delenv("QVL_GPU_WRAPPED", raising=False)
    monkeypatch.setenv("QVL_FAKE_SYSTEMD", "1")
    monkeypatch.setenv("QVL_FAKE_GPU_MODE", "slow")
    monkeypatch.setenv("QVL_FAKE_GPU_UNIT_FILE", str(unit_file))
    settings = Settings(
        engine="qwen",
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        access_token="",
        require_gpu_wrapper=True,
        gpu_wrapper=str(runtime),
        gpu_cgroup_pattern=r"(qvl-test-worker-[0-9]+[.]service)",
        gpu_unit_prefix="qvl-test-worker-",
        gpu_worker_command=f"{sys.executable} {runtime} worker",
        gpu_stop_command=f"{sys.executable} {runtime} stop {{unit}}",
        gpu_stop_all_command="true",
        gpu_worker_start_timeout_seconds=5,
        gpu_retry_cooldown_seconds=2,
    )
    return TestClient(create_app(settings)), unit_file


def submit(client: TestClient) -> str:
    response = client.post(
        "/api/jobs",
        json={
            "title": "Systemd cleanup",
            "voice_id": "voice_amara_sol",
            "language": "es",
            "segments": [{"id": "p01", "text": "Worker lento."}],
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def wait_running(client: TestClient, job_id: str, unit_file: Path) -> str:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "running" and job["progress"] > 0 and unit_file.exists():
            return unit_file.read_text()
        time.sleep(0.05)
    job = client.get(f"/api/jobs/{job_id}").json()
    raise AssertionError(f"systemd worker did not start: {job}")


def assert_unit_inactive(unit: str) -> None:
    status = subprocess.run(
        ["systemctl", "is-active", unit], capture_output=True, text=True, check=False
    )
    assert status.stdout.strip() not in {"active", "activating", "deactivating"}


def test_cancel_stops_real_transient_unit_and_preserves_api(tmp_path: Path, monkeypatch) -> None:
    client, unit_file = build_client(tmp_path, monkeypatch)
    with client:
        job_id = submit(client)
        unit = wait_running(client, job_id, unit_file)
        cancelled = client.delete(f"/api/jobs/{job_id}")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert_unit_inactive(unit)
        assert client.get("/api/health").status_code == 200


def test_api_shutdown_stops_running_transient_unit(tmp_path: Path, monkeypatch) -> None:
    client, unit_file = build_client(tmp_path, monkeypatch)
    client.__enter__()
    unit = wait_running(client, submit(client), unit_file)
    started = time.monotonic()
    client.__exit__(None, None, None)
    assert time.monotonic() - started < 15
    assert_unit_inactive(unit)
