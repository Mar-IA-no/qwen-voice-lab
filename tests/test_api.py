from __future__ import annotations

import hashlib
import io
import json
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qwen_voice_lab.app import create_app
from qwen_voice_lab.config import Settings


def wav_bytes(seconds: float = 1.2) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * round(16_000 * seconds))
    return output.getvalue()


def wait_for(client: TestClient, job_id: str, timeout: float = 3) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = client.get(f"/api/jobs/{job_id}").json()
        if row["status"] in {"complete", "failed", "cancelled"}:
            return row
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {job_id}")


def build_client(tmp_path: Path, with_prosody: bool = False) -> TestClient:
    settings = Settings(
        engine="mock",
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        access_token="",
        require_gpu_wrapper=True,
        max_upload_mib=2,
    )
    if with_prosody:
        settings.prepare()
        references = {}
        for index, function in enumerate(("T", "S", "D", "R"), start=1):
            payload = wav_bytes(1 + index / 10)
            relative = Path("test_prosody") / f"{function}.wav"
            path = settings.data_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            references[function] = {
                "reference_file": str(relative),
                "reference_text": f"Referencia exacta para {function}.",
                "reference_sha256": hashlib.sha256(payload).hexdigest(),
                "provenance": "test fixture",
            }
        manifest = {
            "schema_version": "qwen-voice-lab-prosody-profile-v1",
            "id": "test-profile-v1",
            "identity_tags": ["profiled"],
            "status": "experimental",
            "languages": ["es", "en"],
            "references": references,
            "notes": ["test only"],
        }
        (settings.prosody_profiles_dir / "test-profile-v1.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    return TestClient(create_app(settings))


def import_voice(
    client: TestClient, name: str = "Test voice", tags: str = "test,bilingual"
) -> dict:
    response = client.post(
        "/api/voices",
        data={
            "name": name,
            "description": "Local fixture",
            "language_hint": "multilingual",
            "reference_text": "Esta es una referencia autorizada.",
            "tags": tags,
            "consent_confirmed": "true",
        },
        files={"file": ("reference.wav", wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_capabilities_are_local_and_free(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        payload = client.get("/api/capabilities").json()
        assert payload["engine"] == "mock"
        assert payload["engine_ready"] is True
        assert payload["paid_providers"] == []
        assert payload["languages"] == ["es", "en"]
        assert payload["gpu_wrapper_verified"] is False


def test_remote_binding_requires_authentication_or_explicit_override(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Remote binding requires"):
        Settings(engine="mock", data_dir=tmp_path, host="192.0.2.10", access_token="")


def test_malformed_retryable_exit_codes_fail_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="comma-separated integers"):
        Settings(
            engine="mock",
            data_dir=tmp_path,
            host="127.0.0.1",
            access_token="",
            gpu_retryable_exit_codes="busy,75",
        )


def test_remote_authentication_protects_catalog_and_mutations(tmp_path: Path) -> None:
    settings = Settings(
        engine="mock",
        data_dir=tmp_path / "data",
        host="192.0.2.10",
        access_token="a" * 48,
    )
    with TestClient(create_app(settings)) as client:
        assert Path(client.app.state.frontend_path).name == "static"
        assert client.get("/api/health").status_code == 200
        page = client.get("/")
        assert page.status_code == 200
        assert len(page.content) == int(page.headers["content-length"])
        assert b"Qwen Voice Lab" in page.content
        assert client.get("/api/auth/status").json() == {
            "required": True,
            "authenticated": False,
        }
        assert client.get("/api/voices").status_code == 401
        assert client.post("/api/auth/session", json={"token": "wrong"}).status_code == 401
        login = client.post("/api/auth/session", json={"token": "a" * 48})
        assert login.status_code == 200
        assert login.cookies.get("qvl_session")
        assert "HttpOnly" in login.headers["set-cookie"]
        assert "SameSite=strict" in login.headers["set-cookie"]
        assert client.get("/api/voices").status_code == 200
        assert client.delete("/api/auth/session").status_code == 200
        assert client.get("/api/voices").status_code == 401


def test_private_archive_lists_and_serves_audio_without_paths(tmp_path: Path) -> None:
    archive = tmp_path / "data" / "archive" / "collection"
    archive.mkdir(parents=True)
    (archive / "sample.wav").write_bytes(wav_bytes())
    with build_client(tmp_path) as client:
        response = client.get("/api/archive")
        assert response.status_code == 200
        assets = response.json()
        assert len(assets) == 1
        assert assets[0]["relative_path"] == "collection/sample.wav"
        assert "/tmp/" not in response.text
        audio = client.get(f"/api/archive/{assets[0]['id']}/audio")
        assert audio.status_code == 200
        assert audio.headers["content-type"].startswith("audio/")
        assert client.get("/api/archive/not-an-id/audio").status_code == 404


def test_bundled_amara_sol_is_seeded_without_human_reference(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        voices = client.get("/api/voices").json()
        amara = next(voice for voice in voices if voice["id"] == "voice_amara_sol")
        assert amara["name"] == "Amara Sol"
        assert amara["kind"] == "designed"
        assert amara["reference_sha256"] == (
            "60a5788e3bd9f23a6ff10a684dc4b0c9d618eb9ea00163e6e7158954f915ea38"
        )
        assert "reference_file" not in amara
        audio = client.get("/api/voices/voice_amara_sol/audio")
        assert audio.status_code == 200
        assert audio.content.startswith(b"RIFF")


def test_voice_import_requires_consent_and_hashes_reference(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        denied = client.post(
            "/api/voices",
            data={"name": "Denied", "consent_confirmed": "false"},
            files={"file": ("reference.wav", wav_bytes(), "audio/wav")},
        )
        assert denied.status_code == 422
        voice = import_voice(client)
        assert len(voice["reference_sha256"]) == 64
        assert voice["duration_seconds"] >= 1
        assert "reference_file" not in voice
        assert client.get(f"/api/voices/{voice['id']}/audio").status_code == 200


def test_scored_synthesis_completes_with_metrics(tmp_path: Path) -> None:
    with build_client(tmp_path, with_prosody=True) as client:
        voice = import_voice(client, tags="test,bilingual,profiled")
        listed = client.get("/api/voices").json()[0]
        assert listed["prosody_profile"]["functions"] == ["T", "S", "D", "R"]
        response = client.post(
            "/api/jobs",
            json={
                "title": "Scored ES",
                "voice_id": voice["id"],
                "language": "es",
                "seed": 42,
                "segments": [
                    {"id": "p01", "text": "Primera frase.", "pause_after_ms": 750, "prosody": "T"},
                    {"id": "p02", "text": "Segunda frase.", "pause_after_ms": 0, "prosody": "D"},
                ],
            },
        )
        assert response.status_code == 202
        job = wait_for(client, response.json()["id"])
        assert job["status"] == "complete", job
        assert job["metrics"]["duration_seconds"] > 2
        assert job["metrics"]["rtf"] >= 0
        assert len(job["metrics"]["output_sha256"]) == 64
        assert "output_file" not in job
        audio = client.get(f"/api/jobs/{job['id']}/audio")
        assert audio.headers["content-type"].startswith("audio/wav")
        assert audio.headers.get("content-disposition", "").startswith("inline")
        download = client.get(f"/api/jobs/{job['id']}/download")
        assert download.status_code == 200
        assert download.content == audio.content
        assert download.headers["content-disposition"].startswith("attachment")
        assert "Scored%20ES.wav" in download.headers["content-disposition"]


def test_prosody_changes_reference_and_unsupported_voice_is_rejected(tmp_path: Path) -> None:
    with build_client(tmp_path, with_prosody=True) as client:
        profiled = import_voice(client, name="Profiled voice", tags="profiled,test")
        unsupported = import_voice(client, name="Otra voz", tags="test")
        base = {
            "title": "Function comparison",
            "voice_id": profiled["id"],
            "language": "es",
            "seed": 99,
        }
        hashes = []
        for function in ("T", "D"):
            response = client.post(
                "/api/jobs",
                json={
                    **base,
                    "segments": [
                        {"id": "p01", "text": "El mismo texto.", "prosody": function}
                    ],
                },
            )
            assert response.status_code == 202
            hashes.append(wait_for(client, response.json()["id"])["metrics"]["output_sha256"])
        assert hashes[0] != hashes[1]

        rejected = client.post(
            "/api/jobs",
            json={
                **base,
                "voice_id": unsupported["id"],
                "segments": [{"id": "p01", "text": "No fingir.", "prosody": "S"}],
            },
        )
        assert rejected.status_code == 409
        assert "no active prosody profile" in rejected.json()["detail"]


def test_design_requires_explicit_idempotent_promotion(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.post(
            "/api/designs",
            json={
                "name": "Luz",
                "description": "Original fixture",
                "instruction": "A warm and luminous adult voice with calm, clear articulation.",
                "sample_text": "This sample becomes the canonical local reference.",
                "language": "en",
                "seed": 7,
            },
        )
        job = wait_for(client, response.json()["id"])
        assert job["status"] == "complete", job
        assert job["result_voice_id"] is None
        voices_before = client.get("/api/voices").json()
        assert [voice["id"] for voice in voices_before] == ["voice_amara_sol"]
        promoted = client.post(f"/api/jobs/{job['id']}/promote")
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["name"] == "Luz"
        assert promoted.json()["kind"] == "designed"
        repeated = client.post(f"/api/jobs/{job['id']}/promote")
        assert repeated.status_code == 200
        assert repeated.json()["id"] == promoted.json()["id"]
        voices = client.get("/api/voices").json()
        assert {voice["id"] for voice in voices} == {
            "voice_amara_sol",
            promoted.json()["id"],
        }
        updated_job = client.get(f"/api/jobs/{job['id']}").json()
        assert updated_job["result_voice_id"] == promoted.json()["id"]


def test_comparison_uses_same_text_and_seed_for_each_voice(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        first = import_voice(client, "First")
        second = import_voice(client, "Second")
        response = client.post(
            "/api/comparisons",
            json={
                "title": "Controlled",
                "voice_ids": [first["id"], second["id"]],
                "language": "en",
                "text": "Exactly the same comparison text.",
                "seed": 99,
            },
        )
        assert response.status_code == 202, response.text
        comparison = response.json()
        assert len(comparison["job_ids"]) == 2
        jobs = [wait_for(client, job_id) for job_id in comparison["job_ids"]]
        assert {job["request"]["seed"] for job in jobs} == {99}
        assert {job["request"]["segments"][0]["text"] for job in jobs} == {
            "Exactly the same comparison text."
        }
        assert all(job["status"] == "complete" for job in jobs)
