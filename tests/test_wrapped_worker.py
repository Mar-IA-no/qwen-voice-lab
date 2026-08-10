from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from qwen_voice_lab.app import create_app
from qwen_voice_lab.config import Settings


def wait_for(client: TestClient, job_id: str, timeout: float = 4) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"complete", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {job_id}")


def build_client(tmp_path: Path, monkeypatch, mode: str = "success") -> tuple[TestClient, Path]:
    runtime = Path(__file__).parent / "fixtures" / "fake_gpu_runtime.py"
    runtime.chmod(0o755)
    starts = tmp_path / "worker-starts.txt"
    monkeypatch.delenv("QVL_GPU_WRAPPED", raising=False)
    monkeypatch.setenv("QVL_FAKE_GPU_MODE", mode)
    monkeypatch.setenv("QVL_FAKE_GPU_STARTS", str(starts))
    monkeypatch.setenv("QVL_FAKE_GPU_UNIT", "fake-gpu-worker.service")
    settings = Settings(
        engine="qwen",
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        access_token="",
        require_gpu_wrapper=True,
        gpu_wrapper=str(runtime),
        gpu_cgroup_pattern=r"(fake-gpu-worker[.]service)",
        gpu_unit_prefix="",
        gpu_worker_command=f"{sys.executable} {runtime} worker",
        gpu_stop_command=f"{sys.executable} {runtime} stop {{unit}}",
        gpu_stop_all_command="true",
        gpu_worker_start_timeout_seconds=2,
        gpu_retry_cooldown_seconds=30,
    )
    return TestClient(create_app(settings)), starts


def submit(client: TestClient, title: str) -> dict:
    response = client.post(
        "/api/jobs",
        json={
            "title": title,
            "voice_id": "voice_amara_sol",
            "language": "es",
            "segments": [{"id": "p01", "text": "Prueba aislada."}],
            "seed": 42,
        },
    )
    assert response.status_code == 202, response.text
    return wait_for(client, response.json()["id"])


def test_wrapped_worker_is_lazy_and_reused_while_api_stays_resident(
    tmp_path: Path, monkeypatch
) -> None:
    client, starts = build_client(tmp_path, monkeypatch)
    with client:
        initial = client.get("/api/capabilities").json()
        assert initial["engine_ready"] is True
        assert initial["gpu_execution_mode"] == "wrapped-worker"
        assert initial["gpu_worker_state"] == "standby"
        assert not starts.exists()

        first = submit(client, "First")
        second = submit(client, "Second")
        assert first["status"] == second["status"] == "complete"
        assert starts.read_text() == "1"

        active = client.get("/api/capabilities").json()
        assert active["gpu_worker_state"] == "ready"
        assert active["gpu_wrapper_verified"] is True
        assert client.get("/api/health").status_code == 200


def test_preemption_fails_job_but_preserves_api_and_applies_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    client, starts = build_client(tmp_path, monkeypatch, mode="preempt")
    with client:
        first = submit(client, "Preempted")
        assert first["status"] == "failed"
        assert "preempted by the priority service" in first["error"]

        status = client.get("/api/capabilities").json()
        assert status["engine_ready"] is False
        assert status["gpu_worker_state"] == "cooldown"
        assert client.get("/api/health").json()["status"] == "ok"

        second = submit(client, "Cooldown")
        assert second["status"] == "failed"
        assert "retry in about" in second["error"]
        assert starts.read_text() == "1"


def test_idle_worker_preemption_is_visible_without_taking_down_api(
    tmp_path: Path, monkeypatch
) -> None:
    client, _ = build_client(tmp_path, monkeypatch, mode="preempt_idle")
    with client:
        assert submit(client, "Completes before preemption")["status"] == "complete"
        deadline = time.monotonic() + 2
        status = client.get("/api/capabilities").json()
        while status["gpu_worker_state"] != "cooldown" and time.monotonic() < deadline:
            time.sleep(0.02)
            status = client.get("/api/capabilities").json()
        assert status["gpu_worker_state"] == "cooldown"
        assert "preempted by the priority service" in status["gpu_worker_reason"]
        assert client.get("/api/health").json()["status"] == "ok"


def test_scheduler_busy_exit_is_reported_as_retryable_admission(
    tmp_path: Path, monkeypatch
) -> None:
    client, _ = build_client(tmp_path, monkeypatch, mode="deferred")
    with client:
        job = submit(client, "Scheduler occupied")
        assert job["status"] == "failed"
        assert "GPU admission deferred" in job["error"]
        assert "exit 1" in job["error"]
        assert "scheduler is occupied" in job["error"]


def test_missing_stop_placeholder_is_exposed_as_misconfiguration(
    tmp_path: Path, monkeypatch
) -> None:
    client, _ = build_client(tmp_path, monkeypatch)
    client.app.state.settings.gpu_stop_command = "true"
    with client:
        status = client.get("/api/capabilities").json()
        assert status["engine_ready"] is False
        assert status["gpu_worker_state"] == "misconfigured"
        assert "{unit} placeholder" in status["engine_reason"]
