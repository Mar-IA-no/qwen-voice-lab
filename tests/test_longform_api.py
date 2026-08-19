from __future__ import annotations

import asyncio
import io
import os
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from qwen_voice_lab import audio_pipeline
from qwen_voice_lab.app import create_app
from qwen_voice_lab.config import Settings
from qwen_voice_lab.longform import deterministic_seed
from qwen_voice_lab.models import (
    ProjectRun,
    ProjectStatus,
    QualityReport,
    RevisionCreate,
    RunStatus,
)
from qwen_voice_lab.quality import (
    MOCK_IDENTITY_MODEL_SHA256,
    MOCK_IDENTITY_VALIDATOR,
    ValidationResult,
    content_metrics,
)


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
        assert takes[0]["trim_threshold_db"] == -48
        assert takes[0]["trim_padding_ms"] == 80
        assert {row["validator"] for row in takes[0]["quality_reports"]} == {
            "technical-audio-v1",
            "mock-content-oracle-v1",
            "mock-acoustic-window-v1",
        }
        assert client.get(f"/api/takes/{takes[0]['id']}/audio").status_code == 200
        assert client.get(f"/api/takes/{takes[0]['id']}/audio?raw=true").status_code == 200
        download = client.get(f"/api/takes/{takes[0]['id']}/download")
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment;")
        raw_download = client.get(f"/api/takes/{takes[0]['id']}/download?raw=true")
        assert "-raw.wav" in raw_download.headers["content-disposition"]

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
        partial = client.get(
            f"/api/assemblies/{final['id']}/audio", headers={"Range": "bytes=0-99"}
        )
        assert partial.status_code == 206
        assert len(partial.content) == 100
        assert partial.headers["content-range"].startswith("bytes 0-99/")
        assert partial.headers["accept-ranges"] == "bytes"

        manual = client.post(f"/api/projects/{project['id']}/segments/{first['id']}/takes").json()
        assert wait_run(client, manual["id"])["status"] == "complete"
        reviewed = client.get(f"/api/projects/{project['id']}/segments/{first['id']}/takes").json()
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
        compatible = client.get(
            f"/api/projects/{project['id']}/segments/{after['segments'][0]['id']}/takes"
        ).json()
        assert [row["id"] for row in compatible] == [take_ids[0]]
        assert client.post(f"/api/projects/{project['id']}/runs").status_code == 409
        manual = client.post(
            f"/api/projects/{project['id']}/segments/{after['segments'][0]['id']}/takes"
        ).json()
        assert wait_run(client, manual["id"])["status"] == "complete"
        compatible = client.get(
            f"/api/projects/{project['id']}/segments/{after['segments'][0]['id']}/takes"
        ).json()
        assert [row["attempt"] for row in compatible] == [2, 1]
        assert compatible[0]["seed"] == deterministic_seed(17, after["segments"][0]["id"], 2)
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
        assert client.get("/api/projects").json() == []


def test_content_metrics_detect_missing_suffix_and_reordered_words() -> None:
    missing = content_metrics("one two three four", "one two three")
    reordered = content_metrics("one two three four", "three four one two")
    assert missing["suffix_coverage"] < 0.8
    assert missing["wer"] > 0.12
    assert reordered["wer"] > 0.12
    repeated_elsewhere = content_metrics("end one middle end one two", "end one middle two")
    assert repeated_elsewhere["suffix_coverage"] < 0.8


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
                        transcript=item.expected if verdict == "pass" else "missing",
                        reasons=[] if verdict == "pass" else ["missing content"],
                    )
                )
                for index, item in enumerate(items)
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
                "validator": MOCK_IDENTITY_VALIDATOR,
                "validator_model_sha256": MOCK_IDENTITY_MODEL_SHA256,
            },
        )
        assert calibration.status_code == 201
        run = client.post(f"/api/projects/{project['id']}/runs").json()
        assert wait_run(client, run["id"])["status"] == "needs_review"
        detail = client.get(f"/api/projects/{project['id']}").json()
        first = detail["segments"][0]
        takes = client.get(f"/api/projects/{project['id']}/segments/{first['id']}/takes").json()
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
        calibrations = restarted.get("/api/voices/voice_amara_sol/identity-calibrations").json()
        assert [row["id"] for row in calibrations] == [calibration_id]


def test_duplicate_active_run_is_rejected_atomically(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        blocker = ProjectRun(
            id="run_blocker",
            project_id=project["id"],
            revision_id=project["revision"]["id"],
            segment_ids=[project["segments"][0]["id"]],
        )
        client.app.state.store.save_run(blocker)
        response = client.post(f"/api/projects/{project['id']}/runs")
        assert response.status_code == 409
        assert len(client.app.state.store.list_runs(project["id"])) == 1


def test_run_creation_rejects_a_revision_that_became_stale(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        stale_run = ProjectRun(
            id="run_stale_revision",
            project_id=project["id"],
            revision_id=project["revision"]["id"],
            segment_ids=[project["segments"][0]["id"]],
        )
        revised = client.post(
            f"/api/projects/{project['id']}/revisions",
            json={"markdown": "Testo completamente cambiato.\n"},
        )
        assert revised.status_code == 201

        with pytest.raises(ValueError, match="project revision changed"):
            client.app.state.store.create_run_if_idle(stale_run)
        assert client.app.state.store.list_runs(project["id"]) == []
        assert (
            client.get(f"/api/projects/{project['id']}").json()["revision"]["id"]
            == revised.json()["revision"]["id"]
        )


def test_revision_transaction_rejects_an_active_run(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        blocker = ProjectRun(
            id="run_revision_blocker",
            project_id=project["id"],
            revision_id=project["revision"]["id"],
            segment_ids=[project["segments"][0]["id"]],
        )
        client.app.state.store.create_run_if_idle(blocker)

        response = client.post(
            f"/api/projects/{project['id']}/revisions",
            json={"markdown": "Testo completamente cambiato.\n"},
        )
        assert response.status_code == 422
        detail = client.get(f"/api/projects/{project['id']}").json()
        assert detail["revision"]["id"] == project["revision"]["id"]
        assert len(client.app.state.store.list_revisions(project["id"])) == 1


def test_revision_save_and_run_creation_are_one_atomic_exclusion_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        store = client.app.state.store
        manager = client.app.state.projects
        stale_run = ProjectRun(
            id="run_atomic_race",
            project_id=project["id"],
            revision_id=project["revision"]["id"],
            segment_ids=[project["segments"][0]["id"]],
        )
        barrier = threading.Barrier(2)
        original_revision_save = store.save_project_revision_if_idle
        original_run_create = store.create_run_if_idle

        def synchronized_revision_save(*args, **kwargs):
            barrier.wait(timeout=2)
            return original_revision_save(*args, **kwargs)

        def synchronized_run_create(*args, **kwargs):
            barrier.wait(timeout=2)
            return original_run_create(*args, **kwargs)

        monkeypatch.setattr(store, "save_project_revision_if_idle", synchronized_revision_save)
        monkeypatch.setattr(store, "create_run_if_idle", synchronized_run_create)
        successes: list[str] = []
        failures: list[Exception] = []

        def save_revision() -> None:
            try:
                manager.add_revision(
                    project["id"], RevisionCreate(markdown="Testo completamente cambiato.\n")
                )
                successes.append("revision")
            except Exception as exc:
                failures.append(exc)

        def create_run() -> None:
            try:
                store.create_run_if_idle(stale_run)
                successes.append("run")
            except Exception as exc:
                failures.append(exc)

        revision_thread = threading.Thread(target=save_revision)
        run_thread = threading.Thread(target=create_run)
        revision_thread.start()
        run_thread.start()
        revision_thread.join(timeout=3)
        run_thread.join(timeout=3)
        assert not revision_thread.is_alive()
        assert not run_thread.is_alive()
        assert len(successes) == 1
        assert len(failures) == 1

        detail = manager.get_project(project["id"])
        runs = store.list_runs(project["id"])
        if successes == ["revision"]:
            assert detail.revision.id != project["revision"]["id"]
            assert runs == []
            assert "revision changed" in str(failures[0])
        else:
            assert detail.revision.id == project["revision"]["id"]
            assert [row.id for row in runs] == [stale_run.id]
            assert "active project run" in str(failures[0])


def test_shutdown_marks_an_active_long_form_run_failed(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    worker_cancelled = False

    async def blocked_render(*args, **kwargs):
        await asyncio.sleep(3600)

    def cancel_active() -> None:
        nonlocal worker_cancelled
        worker_cancelled = True

    client.app.state.projects._render_take = blocked_render
    client.app.state.projects.engine.cancel_active = cancel_active
    with client as active:
        project = create_project(active)
        run = active.post(f"/api/projects/{project['id']}/runs").json()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            persisted = client.app.state.store.get_run(run["id"])
            if persisted and persisted.status == RunStatus.RUNNING:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("project run did not start")

    persisted = client.app.state.store.get_run(run["id"])
    assert persisted is not None
    assert persisted.status == RunStatus.FAILED
    assert persisted.error == "The process stopped before this run completed."
    assert worker_cancelled is True
    assert client.app.state.store.get_project(project["id"]).status == ProjectStatus.NEEDS_REVIEW


def test_take_assets_fail_closed_after_tamper_or_symlink(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        run = client.post(f"/api/projects/{project['id']}/runs").json()
        assert wait_run(client, run["id"])["status"] == "complete"
        detail = client.get(f"/api/projects/{project['id']}").json()
        take_id = detail["segments"][0]["selected_take_id"]
        take = client.app.state.store.get_take(take_id)
        assert take is not None
        speech = Path(take.trimmed_file)
        original = speech.read_bytes()
        speech.write_bytes(original + b"tampered")
        assert client.get(f"/api/takes/{take_id}/audio").status_code == 404
        assert client.post(f"/api/projects/{project['id']}/preview", json={}).status_code == 409

        speech.write_bytes(original)
        outside = tmp_path / "outside.wav"
        outside.write_bytes(original)
        speech.unlink()
        os.symlink(outside, speech)
        assert client.get(f"/api/takes/{take_id}/audio").status_code == 404
        assert client.post(f"/api/projects/{project['id']}/preview", json={}).status_code == 409


def test_assembly_assets_fail_closed_after_tamper(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        run = client.post(f"/api/projects/{project['id']}/runs").json()
        assert wait_run(client, run["id"])["status"] == "complete"
        assembly = client.post(f"/api/projects/{project['id']}/preview", json={}).json()
        persisted = client.app.state.store.get_assembly(assembly["id"])
        assert persisted is not None

        output = Path(persisted.output_file)
        original_output = output.read_bytes()
        output.write_bytes(original_output + b"tampered")
        assert client.get(f"/api/assemblies/{assembly['id']}/audio").status_code == 404
        assert client.get(f"/api/assemblies/{assembly['id']}/download").status_code == 404

        output.write_bytes(original_output)
        manifest = Path(persisted.manifest_file)
        manifest.write_bytes(manifest.read_bytes() + b"tampered")
        assert client.get(f"/api/assemblies/{assembly['id']}/manifest").status_code == 404


def test_assembly_decodes_the_same_authenticated_bytes_it_certifies(
    tmp_path: Path, monkeypatch
) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        run = client.post(f"/api/projects/{project['id']}/runs").json()
        assert wait_run(client, run["id"])["status"] == "complete"
        detail = client.get(f"/api/projects/{project['id']}").json()
        take = client.app.state.store.get_take(detail["segments"][0]["selected_take_id"])
        assert take is not None
        speech = Path(take.trimmed_file)
        original_bytes = speech.read_bytes()
        original_audio, rate = sf.read(io.BytesIO(original_bytes), dtype="float32")
        replacement = tmp_path / "replacement.wav"
        sf.write(replacement, np.full(len(original_audio), 0.5, dtype=np.float32), rate)
        replacement_bytes = replacement.read_bytes()

        original_read = audio_pipeline.sf.read

        def replace_path_before_decode(source, *args, **kwargs):
            if isinstance(source, io.BytesIO):
                speech.write_bytes(replacement_bytes)
            return original_read(source, *args, **kwargs)

        monkeypatch.setattr(audio_pipeline.sf, "read", replace_path_before_decode)
        preview_response = client.post(f"/api/projects/{project['id']}/preview", json={})
        assert preview_response.status_code == 201, preview_response.text
        preview = preview_response.json()
        manifest = client.get(f"/api/assemblies/{preview['id']}/manifest").json()
        assert manifest["timeline"][0]["take_sha256"] == take.trimmed_sha256
        assembled, _ = original_read(
            tmp_path
            / "data"
            / "projects"
            / project["id"]
            / "assemblies"
            / preview["id"]
            / "audio.wav",
            dtype="float32",
        )
        np.testing.assert_allclose(assembled[: len(original_audio)], original_audio, atol=1e-6)

def test_blank_audit_and_calibration_notes_are_rejected(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        assert (
            client.post(
                f"/api/projects/{project['id']}/preview", json={"override_reason": "   "}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/voices/voice_amara_sol/identity-calibrations",
                json={
                    "language": "it",
                    "min_window_score": 0,
                    "min_median_score": 0,
                    "notes": "   ",
                    "validator": MOCK_IDENTITY_VALIDATOR,
                    "validator_model_sha256": MOCK_IDENTITY_MODEL_SHA256,
                },
            ).status_code
            == 422
        )


def test_calibration_with_wrong_provenance_remains_advisory(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        response = client.post(
            "/api/voices/voice_amara_sol/identity-calibrations",
            json={
                "language": "it",
                "min_window_score": 1,
                "min_median_score": 1,
                "notes": "Different frozen model.",
                "validator": MOCK_IDENTITY_VALIDATOR,
                "validator_model_sha256": "f" * 64,
            },
        )
        assert response.status_code == 201
        run = client.post(f"/api/projects/{project['id']}/runs").json()
        assert wait_run(client, run["id"])["status"] == "complete"
        detail = client.get(f"/api/projects/{project['id']}").json()
        reports = client.get(
            f"/api/projects/{project['id']}/segments/{detail['segments'][0]['id']}/takes"
        ).json()[0]["quality_reports"]
        identity = next(row for row in reports if row["validator"] == MOCK_IDENTITY_VALIDATOR)
        assert identity["calibration_id"] is None
        assert identity["verdict"] == "pass"
        assert "provenance does not match" in identity["reasons"][0]


def test_startup_reconciles_interrupted_project_status(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        row = client.app.state.store.get_project(project["id"])
        assert row is not None
        row.status = ProjectStatus.GENERATING
        client.app.state.store.save_project(row)
        client.app.state.store.save_run(
            ProjectRun(
                id="run_interrupted",
                project_id=project["id"],
                revision_id=project["revision"]["id"],
                segment_ids=[project["segments"][0]["id"]],
            )
        )
    with client_for(tmp_path) as restarted:
        detail = restarted.get(f"/api/projects/{project['id']}").json()
        assert detail["status"] == "needs_review"
        run = restarted.get("/api/project-runs/run_interrupted").json()
        assert run["status"] == "failed"


def test_startup_reconciles_generating_project_with_already_terminal_run(
    tmp_path: Path,
) -> None:
    with client_for(tmp_path) as client:
        project = create_project(client)
        row = client.app.state.store.get_project(project["id"])
        assert row is not None
        row.status = ProjectStatus.GENERATING
        client.app.state.store.save_project(row)
        client.app.state.store.save_run(
            ProjectRun(
                id="run_already_failed",
                project_id=project["id"],
                revision_id=project["revision"]["id"],
                segment_ids=[project["segments"][0]["id"]],
                status=RunStatus.FAILED,
                error="crash window fixture",
            )
        )
    with client_for(tmp_path) as restarted:
        detail = restarted.get(f"/api/projects/{project['id']}").json()
        assert detail["status"] == "needs_review"
        run = restarted.get("/api/project-runs/run_already_failed").json()
        assert run["status"] == "failed"
        assert run["error"] == "crash window fixture"


def test_terminal_run_and_project_state_roll_back_as_one_transaction(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        created = create_project(client)
        store = client.app.state.store
        project = store.get_project(created["id"])
        assert project is not None
        run = ProjectRun(
            id="run_atomic_terminal",
            project_id=project.id,
            revision_id=created["revision"]["id"],
            segment_ids=[created["segments"][0]["id"]],
        )
        store.save_run(run)
        run.status = RunStatus.FAILED
        project.status = ProjectStatus.NEEDS_REVIEW
        with sqlite3.connect(client.app.state.settings.database_path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_project_update BEFORE UPDATE ON projects "
                "BEGIN SELECT RAISE(ABORT, 'injected project write failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected project write failure"):
            store.save_terminal_run_and_project(run, project)
        assert store.get_run(run.id).status == RunStatus.QUEUED
        assert store.get_project(project.id).status == ProjectStatus.DRAFT
        with sqlite3.connect(client.app.state.settings.database_path) as connection:
            connection.execute("DROP TRIGGER reject_project_update")
