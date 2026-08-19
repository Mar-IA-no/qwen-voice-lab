from __future__ import annotations

import time
from pathlib import Path

import soundfile as sf
from fastapi.testclient import TestClient

from qwen_voice_lab.app import create_app
from qwen_voice_lab.config import Settings
from qwen_voice_lab.longform import deterministic_seed
from qwen_voice_lab.models import QualityReport
from qwen_voice_lab.quality import ValidationResult, content_metrics


def client_for(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                engine="mock",
                data_dir=tmp_path / "data",
                host="127.0.0.1",
                access_token="",
            )
        )
    )


def wait_run(client: TestClient, run_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = client.get(f"/api/project-runs/{run_id}").json()
        if payload["status"] in {"complete", "needs_review", "failed"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("project run did not finish")


def create_project(client: TestClient) -> dict:
    response = client.post(
        "/api/projects",
        json={
            "title": "Meditation",
            "voice_id": "voice_amara_sol",
            "language": "it",
            "project_seed": 17,
            "sampling": {
                "temperature": 0.7,
                "top_p": 0.75,
                "top_k": 40,
                "repetition_penalty": 1.08,
                "max_new_tokens": 1024,
            },
            "markdown": "Ascolta con calma.\n\n[1.2s]\n\nResta con il suono.\n",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_project_pipeline_persists_takes_qc_exact_pauses_and_final_audit(
    tmp_path: Path,
) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        run_response = client.post(f"/api/projects/{project['id']}/runs")
        assert run_response.status_code == 202
        run = wait_run(client, run_response.json()["id"])
        assert run["status"] == "complete", run

        detail = client.get(f"/api/projects/{project['id']}").json()
        assert detail["status"] == "ready"
        assert all(row["selected_take_id"] for row in detail["segments"])
        first = detail["segments"][0]
        takes = client.get(f"/api/projects/{project['id']}/segments/{first['id']}/takes").json()
        assert len(takes) == 1
        assert "raw_file" not in takes[0]
        assert "trimmed_file" not in takes[0]
        assert takes[0]["seed"] == deterministic_seed(17, first["id"], 1)
        assert takes[0]["sampling"]["temperature"] == 0.7
        assert {row["validator"] for row in takes[0]["quality_reports"]} == {
            "technical-audio-v1",
            "mock-content-oracle-v1",
            "mock-acoustic-window-v1",
        }
        assert client.get(f"/api/takes/{takes[0]['id']}/audio").status_code == 200
        assert client.get(f"/api/takes/{takes[0]['id']}/audio?raw=true").status_code == 200

        preview = client.post(f"/api/projects/{project['id']}/preview", json={}).json()
        assert preview["kind"] == "preview"
        assert "output_file" not in preview
        assert "manifest_file" not in preview
        manifest = client.get(f"/api/assemblies/{preview['id']}/manifest").json()
        assert manifest["timeline"][0]["pause_samples"] == round(1.2 * manifest["sample_rate"])
        info = sf.info(
            tmp_path
            / "data"
            / "projects"
            / project["id"]
            / "assemblies"
            / preview["id"]
            / "audio.wav"
        )
        assert info.frames == manifest["total_samples"]

        final = client.post(f"/api/projects/{project['id']}/assemblies", json={}).json()
        assert final["audit_status"] == "pass"
        assert final["audit"]["wer"] == 0
        assert client.get(f"/api/assemblies/{final['id']}/download").status_code == 200

        manual = client.post(
            f"/api/projects/{project['id']}/segments/{first['id']}/takes"
        ).json()
        assert wait_run(client, manual["id"])["status"] == "complete"
        reviewed = client.get(
            f"/api/projects/{project['id']}/segments/{first['id']}/takes"
        ).json()
        assert len(reviewed) == 2
        assert next(row for row in reviewed if row["attempt"] == 1)["selected"] is True
        assert next(row for row in reviewed if row["attempt"] == 2)["selected"] is False
        assert client.get(f"/api/projects/{project['id']}").json()["status"] == "ready"


def test_pause_only_revision_reuses_selected_audio_without_new_tts(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        run = client.post(f"/api/projects/{project['id']}/runs").json()
        assert wait_run(client, run["id"])["status"] == "complete"
        before = client.get(f"/api/projects/{project['id']}").json()
        take_ids = [row["selected_take_id"] for row in before["segments"]]

        revised = client.post(
            f"/api/projects/{project['id']}/revisions",
            json={"markdown": "Ascolta con calma.\n\n[2s]\n\nResta con il suono.\n"},
        )
        assert revised.status_code == 201
        after = revised.json()
        assert [row["id"] for row in after["segments"]] == [row["id"] for row in before["segments"]]
        assert [row["selected_take_id"] for row in after["segments"]] == take_ids
        assert after["status"] == "ready"
        assert client.post(f"/api/projects/{project['id']}/runs").status_code == 409
        preview = client.post(f"/api/projects/{project['id']}/preview", json={}).json()
        manifest = client.get(f"/api/assemblies/{preview['id']}/manifest").json()
        assert manifest["timeline"][0]["pause_samples"] == 2 * manifest["sample_rate"]


def test_invalid_editorial_body_returns_line_diagnostic(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        response = client.post(
            "/api/projects",
            json={
                "title": "Bad cues",
                "voice_id": "voice_amara_sol",
                "language": "en",
                "markdown": "Listen. ^",
            },
        )
        assert response.status_code == 422
        assert "line 1" in response.json()["detail"]


def test_content_metrics_detect_missing_suffix_and_reordered_words() -> None:
    missing = content_metrics("one two three four", "one two three")
    reordered = content_metrics("one two three four", "three four one two")
    assert missing["suffix_coverage"] < 0.8
    assert missing["wer"] > 0.12
    assert reordered["wer"] > 0.12


def test_retry_orchestration_uses_generation_and_validation_waves(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        calls: list[int] = []

        def fake_batch(items, *, mock=False):
            calls.append(len(items))
            verdict = "retry" if len(calls) == 1 else "pass"
            return [
                ValidationResult(
                    content=QualityReport(
                        id=f"qc_fake_{len(calls)}_{index}",
                        take_id="pending",
                        validator="fake-asr",
                        verdict=verdict,
                        transcript=expected if verdict == "pass" else "missing",
                        reasons=[] if verdict == "pass" else ["missing content"],
                    )
                )
                for index, (_, expected, _, _) in enumerate(items)
            ]

        client.app.state.projects.validator.validate_batch = fake_batch
        run = client.post(f"/api/projects/{project['id']}/runs").json()
        assert wait_run(client, run["id"])["status"] == "complete"
        assert calls == [2, 2]
        detail = client.get(f"/api/projects/{project['id']}").json()
        for segment in detail["segments"]:
            takes = client.get(
                f"/api/projects/{project['id']}/segments/{segment['id']}/takes"
            ).json()
            assert [take["attempt"] for take in takes] == [2, 1]
            assert takes[0]["selected"] is True
            assert takes[1]["status"] == "retry"


def test_calibrated_identity_outlier_retries_and_requires_review(tmp_path: Path) -> None:
    calibration_id = ""
    with client_for(tmp_path) as client:
        project = create_project(client)
        calibration = client.post(
            "/api/voices/voice_amara_sol/identity-calibrations",
            json={
                "language": "it",
                "min_window_score": 1.0,
                "min_median_score": 1.0,
                "notes": "Strict test-only boundary from controlled fixtures.",
            },
        )
        assert calibration.status_code == 201
        run = client.post(f"/api/projects/{project['id']}/runs").json()
        assert wait_run(client, run["id"])["status"] == "needs_review"
        detail = client.get(f"/api/projects/{project['id']}").json()
        first = detail["segments"][0]
        takes = client.get(
            f"/api/projects/{project['id']}/segments/{first['id']}/takes"
        ).json()
        assert len(takes) == 3
        identity = next(
            report
            for report in takes[0]["quality_reports"]
            if report["validator"] == "mock-acoustic-window-v1"
        )
        assert identity["verdict"] == "retry"
        assert identity["calibration_id"] == calibration.json()["id"]
        calibration_id = calibration.json()["id"]
    with client_for(tmp_path) as restarted:
        calibrations = restarted.get(
            "/api/voices/voice_amara_sol/identity-calibrations"
        ).json()
        assert [row["id"] for row in calibrations] == [calibration_id]
