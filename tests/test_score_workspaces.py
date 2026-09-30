from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qwen_voice_lab.app import create_app
from qwen_voice_lab.config import Settings
from qwen_voice_lab.longform import LongFormManager
from qwen_voice_lab.models import (
    ProjectCreate,
    RevisionCreate,
    Take,
    TakeSelection,
    TakeStatus,
    Voice,
    VoiceKind,
)
from qwen_voice_lab.score_workspaces import (
    ScoreCollection,
    ScoreWorkspaces,
    revision_snapshot_sha256,
)
from qwen_voice_lab.storage import Store


def silent_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(b"\x00\x00" * 2400)
    return output.getvalue()


@pytest.fixture
def collection(tmp_path: Path):
    settings = Settings(_env_file=None, engine="mock", data_dir=tmp_path / "data")
    settings.prepare()
    store = Store(settings)
    store.save_voice(Voice(id="voice_example", name="Example", kind=VoiceKind.CLONE,
                           reference_file=str(tmp_path / "reference.wav"),
                           reference_sha256="0" * 64))
    manager = LongFormManager(settings, store, object(), threading.RLock())
    blocks = []
    projects = []
    takes = []
    for index, role in enumerate(("main", "variant")):
        project = manager.create_project(ProjectCreate(
            title=f"Example {index}", voice_id="voice_example", language="en",
            blocks=[{"id": "block_001", "text": f"Example speech {index}.",
                     "pause_after_ms": 1250 + index}], speech_speed=1.1 + index / 10,
        ))
        segment = project.segments[0]
        path = settings.projects_dir / project.id / "takes" / "block_001" / "speech.wav"
        path.parent.mkdir(parents=True)
        path.write_bytes(silent_wav())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        take = Take(
            id=f"take_example_{index}", project_id=project.id,
            revision_id=project.revision.id, segment_id="block_001", attempt=1, seed=1,
            status=TakeStatus.NEEDS_REVIEW if index == 0 else TakeStatus.PASS,
            raw_file=str(path), trimmed_file=str(path), raw_sha256=digest,
            trimmed_sha256=digest, duration_seconds=999, voice_id="voice_example",
            voice_reference_sha256="0" * 64, model="fixture",
            text_sha256=segment.text_sha256, sampling=project.sampling,
            baseline_speed=0.98 if index == 0 else 1.0, provenance={"source": "fixture"},
        )
        store.save_take(take)
        selected = manager.select_take(project.id, "block_001", take.id, TakeSelection(
            override=index == 0, reason="Editorial fixture" if index == 0 else None,
        ))
        revision = selected.revision
        blocks.append({
            "source_key": f"example_{index}", "role": role, "stage_id": f"stage_{index}",
            "stage_title": f"Example stage {index}", "order": index, "text": segment.text,
            "pause_after_ms": segment.pause_after_ms, "speech_speed": revision.speech_speed,
            "response_marker": "Wait for a response" if index == 0 else None,
            "source": {
                "project_id": project.id, "revision_id": revision.id,
                "revision_sha256": revision.source_sha256,
                "revision_snapshot_sha256": revision_snapshot_sha256(revision),
                "segment_id": "block_001", "take_id": take.id,
                "text_sha256": segment.text_sha256, "audio_sha256": digest,
            },
        })
        projects.append(selected)
        takes.append(take)
    manifest = {
        "schema_version": "score-workspace-collection-v1", "id": "example_score",
        "title": "Example score", "is_default": True, "editorial_mode": True,
        "lead_in_ms": 10_000, "blocks": blocks, "beacon": None,
    }
    service = ScoreWorkspaces(settings, store)
    service.collections.mkdir(parents=True)
    manifest_path = service.collections / "example_score.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return service, manager, manifest, manifest_path, projects, takes


def test_pinned_historical_sources_and_complete_script(collection):
    service, manager, _, _, projects, takes = collection
    detail = service.detail("example_score")
    assert detail["workspace"]["status"] == "ready"
    revision = detail["revision"]
    assert revision["lead_in_ms"] == 10_000
    assert [b["role"] for b in revision["blocks"]] == ["main"]
    assert [b["role"] for b in detail["catalog_blocks"]] == ["main", "variant"]
    assert {b["source"]["segment_id"] for b in revision["blocks"]} == {"block_001"}
    first = revision["blocks"][0]
    assert first["source"]["validation_status"] == "needs_review"
    assert first["source"]["selection_override_reason"] == "Editorial fixture"
    assert first["source"]["take_provenance"] == {"source": "fixture"}
    assert first["source"]["take_revision_id"] != first["source"]["revision_id"]
    assert first["source"]["take_revision_id"] == takes[0].revision_id
    assert first["baseline_speed"] == 0.98
    assert first["speech_speed"] == 1.1
    assert first["duration_seconds"] == 0.1  # Exact speech bytes, not stale take metadata.
    manager.add_revision(projects[0].id, RevisionCreate(
        blocks=[{"id": "block_001", "text": "A later script."}],
        expected_revision_id=projects[0].revision.id,
    ))
    assert service.detail("example_score") == detail
    restarted = ScoreWorkspaces(service.settings, service.store)
    assert restarted.detail("example_score") == detail
    listing = restarted.list()
    assert listing["default_workspace_id"] == "example_score"
    assert listing["editorial_mode"] is True
    assert listing["workspaces"][0]["main_block_count"] == 1
    assert listing["workspaces"][0]["variant_block_count"] == 1


@pytest.mark.parametrize("mutation", ["foreign_take", "text", "revision", "audio", "missing"])
def test_altered_sources_fail_honestly_without_latest(collection, mutation):
    service, _, manifest, manifest_path, projects, takes = collection
    if mutation == "foreign_take":
        manifest["blocks"][0]["source"]["take_id"] = takes[1].id
        manifest_path.write_text(json.dumps(manifest))
    elif mutation == "text":
        manifest["blocks"][0]["text"] = "Changed script."
        manifest_path.write_text(json.dumps(manifest))
    elif mutation == "revision":
        changed = projects[0].revision.model_copy(update={"markdown": "Changed markdown"})
        service.store.save_revision(changed)
    elif mutation == "audio":
        Path(takes[0].trimmed_file).write_bytes(b"changed audio")
    else:
        Path(takes[0].trimmed_file).unlink()
    detail = service.detail("example_score")
    assert detail["workspace"]["status"] == "unavailable"
    assert detail["revision"]["blocks"][0]["status"] == "unavailable"
    assert detail["catalog_blocks"][1]["status"] == "ready"
    assert len(detail["catalog_blocks"]) == 2
    assert not service.database_path.exists()  # Never freeze an unverified initial snapshot.
    assert str(service.settings.data_dir) not in json.dumps(detail)


def test_saved_snapshot_does_not_reinterpret_changed_catalog_or_metadata(collection):
    service, _, manifest, manifest_path, _, takes = collection
    original = service.detail("example_score")
    changed = takes[0].model_copy(update={"baseline_speed": 1.2, "status": TakeStatus.PASS})
    service.store.save_take(changed)
    altered = service.detail("example_score")
    assert altered["workspace"]["status"] == "unavailable"
    assert altered["revision"]["blocks"][0]["baseline_speed"] == 0.98
    assert altered["revision"]["blocks"][0]["source"]["validation_status"] == "needs_review"
    manifest["blocks"][0]["text"] = "Replacement catalogue speech."
    manifest_path.write_text(json.dumps(manifest))
    altered = service.detail("example_score")
    assert altered["revision"]["id"] == original["revision"]["id"]
    assert altered["revision"]["blocks"][0]["text"] == original["revision"]["blocks"][0]["text"]
    assert altered["workspace"]["status"] == "unavailable"


def test_manifest_schema_rejects_ambiguous_or_unsafe_sources(collection):
    _, _, manifest, _, _, _ = collection
    with pytest.raises(ValidationError):
        ScoreCollection.model_validate({**manifest, "extra": "unknown"})
    manifest["blocks"][1]["source_key"] = manifest["blocks"][0]["source_key"]
    with pytest.raises(ValidationError, match="unique"):
        ScoreCollection.model_validate(manifest)
    manifest["blocks"][1]["source_key"] = "../outside"
    with pytest.raises(ValidationError):
        ScoreCollection.model_validate(manifest)
    manifest["blocks"][1]["source_key"] = "example_1"
    manifest["blocks"][0]["speech_speed"] = float("nan")
    with pytest.raises(ValidationError):
        ScoreCollection.model_validate(manifest)


def test_missing_invalid_and_symlinked_manifests_are_unavailable(collection):
    service, _, _, manifest_path, _, _ = collection
    manifest_path.write_text("{invalid json")
    assert service.detail("example_score")["revision"] is None
    assert service.list()["workspaces"][0]["status"] == "unavailable"
    outside = service.root.parent / "outside.json"
    manifest_path.rename(outside)
    manifest_path.symlink_to(outside)
    assert service.detail("example_score")["revision"] is None
    with pytest.raises(KeyError):
        service.detail("../outside")
    with pytest.raises(KeyError):
        service.detail("unknown")


def test_empty_installation_and_authenticated_api_reading(collection, monkeypatch):
    service, _, _, _, _, _ = collection
    settings = service.settings.model_copy(update={"access_token": "x" * 32})
    app = create_app(settings)
    # A read-only collection endpoint cannot queue or render a model job.
    def forbidden(*args, **kwargs):
        raise AssertionError("Collection reads must never start synthesis")
    monkeypatch.setattr(app.state.manager.engine, "render_synthesis", forbidden)
    monkeypatch.setattr(app.state.manager.engine, "render_design", forbidden)
    client = TestClient(app)
    assert client.get("/api/score-workspaces").status_code == 401
    assert client.get("/api/score-workspaces/example_score").status_code == 401
    assert client.post("/api/auth/session", json={"token": "x" * 32}).status_code == 200
    assert client.get("/api/score-workspaces").json()["default_workspace_id"] == "example_score"
    response = client.get("/api/score-workspaces/example_score")
    assert response.status_code == 200
    assert response.json()["workspace"]["status"] == "ready"
    assert client.get("/api/score-workspaces/unknown").status_code == 404
    empty_settings = settings.model_copy(update={"data_dir": service.root / "empty"})
    empty_settings.prepare()
    empty = ScoreWorkspaces(empty_settings, Store(empty_settings))
    assert empty.list() == {"workspaces": [], "editorial_mode": False,
                            "default_workspace_id": None}
    assert not empty.root.exists()


def test_beacon_is_pinned_by_bytes_and_does_not_use_current_revision(collection):
    service, manager, manifest, manifest_path, projects, _ = collection
    directory = service.settings.projects_dir / projects[0].id / "beacon"
    directory.mkdir()
    audio = directory / "audio.wav"
    audio.write_bytes(silent_wav())
    manifest["beacon"] = {
        "project_id": projects[0].id,
        "asset_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "enabled": True, "offset_seconds": 2.5, "volume": 0.3,
    }
    manifest_path.write_text(json.dumps(manifest))
    original = service.detail("example_score")
    assert original["revision"]["beacon"]["status"] == "ready"
    manager.add_revision(projects[0].id, RevisionCreate(
        blocks=[{"id": "block_001", "text": "Later speech."}],
        expected_revision_id=projects[0].revision.id,
    ))
    assert service.detail("example_score") == original
    audio.write_bytes(b"Changed Beacon")
    changed = service.detail("example_score")
    assert changed["revision"]["beacon"]["status"] == "unavailable"
    assert changed["workspace"]["status"] == "unavailable"


def test_initial_snapshot_publication_is_atomic_across_connections(collection):
    service, _, _, _, _, _ = collection
    before = [(p.id, p.current_revision_id) for p in service.store.list_projects()]
    sources = [service.store.get_revision(p.current_revision_id).model_dump()
               for p in service.store.list_projects()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ScoreWorkspaces(service.settings, service.store)
                               .detail("example_score"), range(2)))
    assert results[0] == results[1]
    assert [(p.id, p.current_revision_id) for p in service.store.list_projects()] == before
    assert [service.store.get_revision(p.current_revision_id).model_dump()
            for p in service.store.list_projects()] == sources
    with sqlite3.connect(service.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM score_revisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM score_workspaces").fetchone()[0] == 1
    assert service.database_path.stat().st_mode & 0o777 == 0o600
