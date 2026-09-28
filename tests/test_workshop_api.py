from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from qwen_voice_lab import longform
from qwen_voice_lab.app import create_app
from qwen_voice_lab.config import Settings
from qwen_voice_lab.longform import LongFormManager
from qwen_voice_lab.models import (
    AssemblyKind,
    BeaconSettings,
    ProjectCreate,
    RevisionCreate,
    RevisionRestore,
    Take,
    TakeSelection,
    TakeStatus,
    Voice,
    VoiceKind,
)
from qwen_voice_lab.storage import Store


def workshop(tmp_path: Path) -> tuple[LongFormManager, Store]:
    settings = Settings(engine="mock", data_dir=tmp_path / "data")
    settings.prepare()
    store = Store(settings)
    store.save_voice(Voice(
        id="voice_workshop", name="Workshop", kind=VoiceKind.CLONE,
        reference_file=str(tmp_path / "reference.wav"), reference_sha256="0" * 64,
    ))
    return LongFormManager(settings, store, object(), threading.RLock()), store


def source_project(manager: LongFormManager):
    return manager.create_project(ProjectCreate(
        title="Score", voice_id="voice_workshop", language="es",
        blocks=[
            {"id": "opening", "text": "Escucha.", "pause_after_ms": 120_000, "speed": 1.1},
            {"id": "closing", "text": "Respira.", "pause_after_ms": 0},
        ],
        provenance={"source": "bundle-1"},
    ))


def passing_take(manager: LongFormManager, store: Store, project, segment_id: str) -> Take:
    segment = next(row for row in project.segments if row.id == segment_id)
    path = manager.settings.projects_dir / project.id / "takes" / segment.id / "take_test"
    path.mkdir(parents=True)
    audio = path / "speech.wav"
    sf.write(audio, np.ones(2400, dtype=np.float32) * 0.1, 24_000, subtype="PCM_16")
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    take = Take(
        id=f"take_{segment_id}", project_id=project.id,
        revision_id=project.revision.id, segment_id=segment.id, attempt=1, seed=1,
        status=TakeStatus.PASS, raw_file=str(audio), trimmed_file=str(audio),
        raw_sha256=digest, trimmed_sha256=digest, duration_seconds=0.1,
        voice_id="voice_workshop", voice_reference_sha256="0" * 64,
        model="test", text_sha256=segment.text_sha256, sampling=project.sampling,
    )
    store.save_take(take)
    return take


def test_structured_input_and_snapshot_history(tmp_path: Path) -> None:
    manager, store = workshop(tmp_path)
    initial = source_project(manager)
    assert initial.revision.blocks[0].id == "opening"
    assert initial.revision.blocks[0].pause_after_ms == 120_000
    assert initial.revision.blocks[0].speed == 1.1
    assert "[120s]" in initial.revision.markdown
    with pytest.raises(ValidationError, match="exactly one"):
        ProjectCreate(title="Bad", voice_id="voice_workshop", language="es",
                      markdown="Hello", blocks=[{"id": "a", "text": "Hello"}])
    with pytest.raises(ValidationError, match="unique"):
        RevisionCreate(blocks=[{"id": "a", "text": "A"}, {"id": "a", "text": "B"}])
    with pytest.raises(ValidationError, match="host paths"):
        ProjectCreate(title="Bad", voice_id="voice_workshop", language="es",
                      blocks=[{"id": "a", "text": "A"}], provenance={"path": "/tmp/x"})

    take = passing_take(manager, store, initial, "opening")
    selected = manager.select_take(initial.id, "opening", take.id, TakeSelection(
        expected_revision_id=initial.revision.id
    ))
    assert selected.revision.id != initial.revision.id
    assert selected.segments[0].selected_take_id == take.id
    assert store.list_segments(initial.revision.id)[0].selected_take_id is None
    assert store.get_take(take.id).selected is False
    assert manager.list_takes(initial.id, "opening")[0].selected is True
    assert [row.number for row in manager.list_revisions(initial.id)] == [2, 1]
    with pytest.raises(ValueError, match="revision changed"):
        manager.add_revision(initial.id, RevisionCreate(
            blocks=[{"id": "opening", "text": "Escucha."}],
            expected_revision_id=initial.revision.id,
        ))
    with pytest.raises(ValueError, match="revision changed"):
        manager.select_take(initial.id, "opening", take.id, TakeSelection(
            expected_revision_id=initial.revision.id
        ))

    revised = manager.add_revision(initial.id, RevisionCreate(
        expected_revision_id=selected.revision.id,
        blocks=[{"id": "opening", "text": "Escucha.", "pause_after_ms": 121_000},
                {"id": "closing", "text": "Respira."}],
        lead_pause_ms=250, speech_speed=0.9,
    ))
    assert revised.segments[0].selected_take_id == take.id
    assert revised.revision.lead_pause_ms == 250
    assert revised.revision.speech_speed == 0.9
    assert revised.revision.blocks[0].selected_take_id == take.id
    restored = manager.restore_revision(initial.id, RevisionRestore(
        revision_id=initial.revision.id, expected_revision_id=revised.revision.id
    ))
    assert restored.revision.restored_from_revision_id == initial.revision.id
    assert restored.segments[0].selected_take_id is None
    assert store.list_segments(revised.revision.id)[0].selected_take_id == take.id


def test_historical_preview_bundle_and_verified_beacon(tmp_path: Path) -> None:
    manager, store = workshop(tmp_path)
    initial = source_project(manager)
    take = passing_take(manager, store, initial, "opening")
    selected = manager.select_take(initial.id, "opening", take.id, TakeSelection())
    revision = manager.add_revision(initial.id, RevisionCreate(
        blocks=[{"id": "opening", "text": "Escucha.", "pause_after_ms": 2_000,
                 "speed": 1.2}, {"id": "closing", "text": "Respira."}],
        expected_revision_id=selected.revision.id, lead_pause_ms=300,
        speech_speed=0.8,
    ))
    assembly = manager.assemble(initial.id, AssemblyKind.PREVIEW,
                                revision_id=revision.revision.id, segment_id="opening")
    assert assembly.revision_id == revision.revision.id
    assert assembly.segment_id == "opening"
    manifest = json.loads(Path(assembly.manifest_file).read_text())
    assert manifest["speech_speed"] == 0.8
    assert manifest["lead_pause_ms"] == 0
    assert manifest["timeline"][0]["speed"] == 1.2
    assert manifest["timeline"][0]["pause_samples"] == 48_000
    with pytest.raises(ValueError, match="partial previews"):
        manager.bundle(assembly.id)
    app = create_app(manager.settings)
    bundle_route = next(row.endpoint for row in app.routes if isinstance(row, APIRoute)
                        and row.path == "/api/assemblies/{assembly_id}/bundle")
    with pytest.raises(HTTPException) as rejected:
        bundle_route(assembly.id)
    assert rejected.value.status_code == 409
    closing_take = passing_take(manager, store, initial, "closing")
    complete = manager.select_take(initial.id, "closing", closing_take.id, TakeSelection())
    full = manager.assemble(initial.id, AssemblyKind.PREVIEW,
                            revision_id=complete.revision.id)
    assert full.segment_id is None
    _, bundle = manager.bundle(full.id)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        assert set(archive.namelist()) == {"preview.wav", "manifest.json", "score.md", "score.json"}
        assert json.loads(archive.read("score.json"))["id"] == complete.revision.id
        assert archive.read("preview.wav") == Path(full.output_file).read_bytes()
    Path(full.output_file).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        manager.bundle(full.id)

    beacon_dir = manager.settings.projects_dir / initial.id / "beacon"
    beacon_dir.mkdir()
    beacon = beacon_dir / "audio.wav"
    sf.write(beacon, np.zeros(2400, dtype=np.float32), 24_000)
    digest = hashlib.sha256(beacon.read_bytes()).hexdigest()
    (beacon_dir / "manifest.json").write_text(json.dumps({
        "sha256": digest, "mime_type": "audio/wav",
    }))
    enabled = manager.add_revision(initial.id, RevisionCreate(
        blocks=[{"id": "opening", "text": "Escucha.", "pause_after_ms": 2_000},
                {"id": "closing", "text": "Respira."}],
        expected_revision_id=complete.revision.id,
        beacon=BeaconSettings(enabled=True, asset_id=digest),
    ))
    assert enabled.revision.beacon.asset_id == digest
    assert manager.beacon_audio(initial.id) == (beacon.read_bytes(), "audio/wav")
    beacon.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        manager.beacon_audio(initial.id)


def test_generated_take_carries_project_baseline_without_changing_synthesis_request(
    tmp_path: Path, monkeypatch,
) -> None:
    manager, store = workshop(tmp_path)
    detail = manager.create_project(ProjectCreate(
        title="Imported timing", voice_id="voice_workshop", language="es",
        blocks=[{"id": "opening", "text": "Escucha."}],
        baseline_speed=0.98, provenance={"source": "timing-test"},
    ))
    observed = {}

    def fake_render(request, voice, output, progress, cancelled):
        observed["request"] = request
        output.parent.mkdir(parents=True)
        sf.write(output, np.ones(7200, dtype=np.float32) * 0.1, 24_000)
        return SimpleNamespace(model="mock")

    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(manager, "_render_locked", fake_render)
    monkeypatch.setattr(longform.asyncio, "to_thread", immediate)
    take, _ = asyncio.run(manager._render_take(
        store.get_project(detail.id), detail.revision, detail.segments[0],
        store.get_voice("voice_workshop"), 1,
    ))
    assert take.baseline_speed == 0.98
    assert take.provenance == {"source": "timing-test"}
    assert "baseline_speed" not in observed["request"].model_dump()
    assert store.get_take(take.id).baseline_speed == 0.98


def test_workshop_routes_publish_and_restore_snapshots(tmp_path: Path) -> None:
    app = create_app(Settings(engine="mock", data_dir=tmp_path / "api-data"))
    app.state.store.save_voice(Voice(
        id="voice_workshop", name="Workshop", kind=VoiceKind.CLONE,
        reference_file=str(tmp_path / "reference.wav"), reference_sha256="0" * 64,
    ))

    def route(path: str, method: str):
        return next(row.endpoint for row in app.routes if isinstance(row, APIRoute)
                    and row.path == path and method in row.methods)

    create = route("/api/projects", "POST")
    project = create(ProjectCreate(
        title="Score", voice_id="voice_workshop", language="es",
        blocks=[{"id": "opening", "text": "Escucha."}],
        lead_pause_ms=500, speech_speed=0.9,
    ))
    assert project.revision.lead_pause_ms == 500
    revisions = route("/api/projects/{project_id}/revisions", "GET")
    assert [row.id for row in revisions(project.id)] == [project.revision.id]
    save = route("/api/projects/{project_id}/revisions", "POST")
    updated = save(project.id, RevisionCreate(
        expected_revision_id=project.revision.id,
        blocks=[{"id": "opening", "text": "Escucha.", "speed": 1.1}],
    ))
    assert updated.revision.lead_pause_ms == 500
    assert updated.revision.speech_speed == 0.9
    restore = route("/api/projects/{project_id}/restore", "POST")
    restored = restore(project.id, RevisionRestore(
        revision_id=project.revision.id, expected_revision_id=updated.revision.id,
    ))
    assert restored.revision.restored_from_revision_id == project.revision.id
    assert len(revisions(project.id)) == 3
    assert route("/api/assemblies/{assembly_id}/bundle", "GET")
    assert route("/api/projects/{project_id}/beacon", "GET")
