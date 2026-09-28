from __future__ import annotations

import copy
import hashlib
import json
import math
import struct
import sys
import wave
from pathlib import Path

import pytest

from qwen_voice_lab.bundle_import import import_project_bundle
from qwen_voice_lab.cli import main
from qwen_voice_lab.config import Settings
from qwen_voice_lab.models import ProjectStatus, TakeStatus
from qwen_voice_lab.storage import Store


def _wav(path: Path, sample_rate: int = 16000) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = [int(8000 * math.sin(2 * math.pi * 330 * i / sample_rate))
               for i in range(sample_rate)]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"".join(struct.pack("<hh", sample, sample) for sample in samples))
    return path.read_bytes()


def _bundle(root: Path) -> dict:
    reference = _wav(root / "voice.wav")
    spoken = _wav(root / "speech.wav")
    beacon = _wav(root / "beacon.wav")
    manifest = {
        "schema_version": "qvl-project-bundle-v1",
        "bundle_id": "bundle-1",
        "voice": {"name": "Synthetic Test", "reference_file": "voice.wav",
                  "reference_sha256": hashlib.sha256(reference).hexdigest(),
                  "reference_text": "Exact reference transcript."},
        "beacon": {"file": "beacon.wav", "sha256": hashlib.sha256(beacon).hexdigest()},
        "projects": [{
            "source_id": "source-1", "title": "Imported example", "language": "es",
            "baseline_speed": 0.98, "provenance": {"origin": "synthetic"},
            "blocks": [
                {"id": "first", "text": "Primera frase.", "pause_after_ms": 1234,
                 "speed": None, "audio_file": "speech.wav",
                 "audio_sha256": hashlib.sha256(spoken).hexdigest(),
                 "provenance": {"kind": "existing"}},
                {"id": "second", "text": "Segunda frase.", "pause_after_ms": 0,
                 "speed": None, "provenance": {"kind": "pending"}},
            ],
        }],
    }
    _write_manifest(root, manifest)
    return manifest


def _write_manifest(root: Path, manifest: dict) -> None:
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_import_preserves_original_and_review_state(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = _bundle(bundle)
    settings = Settings(data_dir=tmp_path / "data")
    result = import_project_bundle(bundle, settings)
    store = Store(settings)
    project = store.get_project(result["projects"][0]["project_id"])
    assert project is not None
    assert project.baseline_speed == 0.98
    assert project.status == ProjectStatus.NEEDS_REVIEW
    assert project.provenance["import"]["manifest_sha256"] == result["manifest_sha256"]
    voice = store.get_voice(result["voice_id"])
    assert voice is not None
    assert voice.reference_text == "Exact reference transcript."
    assert Path(voice.reference_file).read_bytes() == (bundle / "voice.wav").read_bytes()
    revision = store.get_revision(project.current_revision_id)
    assert revision is not None
    assert revision.blocks[0].selected_take_id
    assert revision.blocks[1].selected_take_id is None
    assert revision.beacon.asset_id == manifest["beacon"]["sha256"]
    segments = store.list_segments(revision.id)
    assert segments[0].pause_after_ms == 1234
    assert segments[1].selected_take_id is None
    take = store.get_take(segments[0].selected_take_id)
    assert take is not None
    assert take.status == TakeStatus.NEEDS_REVIEW
    assert take.baseline_speed == 1
    assert take.trim_start_ms == take.trim_end_ms == 0
    assert take.provenance == {"kind": "existing"}
    assert Path(take.raw_file).read_bytes() == (bundle / "speech.wav").read_bytes()
    with wave.open(take.trimmed_file, "rb") as canonical:
        assert canonical.getnchannels() == 1
        assert canonical.getframerate() == 24000
        assert canonical.getnframes() == 24000
    assert store.list_quality_reports(take.id) == []
    beacon_file = settings.projects_dir / project.id / "beacon" / "audio.wav"
    beacon_blob = settings.projects_dir / ".beacon-blobs" / manifest["beacon"]["sha256"]
    assert beacon_file.read_bytes() == (bundle / "beacon.wav").read_bytes()
    assert beacon_file.samefile(beacon_blob)
    assert not beacon_file.samefile(bundle / "beacon.wav")
    assert json.loads((beacon_file.parent / "manifest.json").read_text())["sha256"] == (
        manifest["beacon"]["sha256"]
    )
    assert result["projects"][0]["status"] == "created"
    again = import_project_bundle(bundle, settings)
    assert again["projects"][0] == {"source_id": "source-1", "project_id": project.id,
                                      "status": "existing"}
    assert len(store.list_projects()) == 1


def test_changed_manifest_creates_separate_project(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = _bundle(bundle)
    settings = Settings(data_dir=tmp_path / "data")
    first = import_project_bundle(bundle, settings)
    manifest["projects"][0]["title"] = "New edition"
    _write_manifest(bundle, manifest)
    second = import_project_bundle(bundle, settings)
    assert first["projects"][0]["project_id"] != second["projects"][0]["project_id"]
    store = Store(settings)
    assert store.get_project(first["projects"][0]["project_id"]).title == "Imported example"
    assert len(store.list_projects()) == 2


def test_beacon_is_one_readonly_blob_with_project_hardlinks(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = _bundle(bundle)
    second = copy.deepcopy(manifest["projects"][0])
    second["source_id"] = "es:CALDEAMIENTO:prompt"
    second["title"] = "Second imported example"
    manifest["projects"].append(second)
    _write_manifest(bundle, manifest)
    settings = Settings(data_dir=tmp_path / "data")
    result = import_project_bundle(bundle, settings)
    assert [row["source_id"] for row in result["projects"]] == [
        "source-1", "es:CALDEAMIENTO:prompt"
    ]
    assert all(":" not in row["project_id"] for row in result["projects"])
    blob = settings.projects_dir / ".beacon-blobs" / manifest["beacon"]["sha256"]
    beacons = [settings.projects_dir / row["project_id"] / "beacon" / "audio.wav"
               for row in result["projects"]]
    assert all(path.samefile(blob) for path in beacons)
    assert not blob.samefile(bundle / "beacon.wav")
    assert blob.stat().st_nlink == 3
    assert blob.stat().st_mode & 0o222 == 0
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == manifest["beacon"]["sha256"]
    assert len(list(blob.parent.iterdir())) == 1


def test_beacon_hardlink_failure_does_not_copy_per_project(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = _bundle(bundle)

    def no_hardlinks(*_args, **_kwargs):
        raise OSError("hardlinks unsupported")

    monkeypatch.setattr("qwen_voice_lab.bundle_import.os.link", no_hardlinks)
    settings = Settings(data_dir=tmp_path / "data")
    with pytest.raises(RuntimeError, match="hardlinks"):
        import_project_bundle(bundle, settings)
    assert not list(settings.projects_dir.glob("project_*"))
    blob = settings.projects_dir / ".beacon-blobs" / manifest["beacon"]["sha256"]
    assert not blob.exists()


def test_folder_prefers_bundle_json_and_explicit_path_is_idempotent(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _bundle(bundle)
    (bundle / "manifest.json").rename(bundle / "bundle.json")
    settings = Settings(data_dir=tmp_path / "data")
    first = import_project_bundle(bundle, settings)
    second = import_project_bundle(bundle / "bundle.json", settings)
    assert first["projects"][0]["status"] == "created"
    assert second["projects"][0]["status"] == "existing"
    assert first["projects"][0]["project_id"] == second["projects"][0]["project_id"]


def test_identical_dual_manifests_are_unambiguous(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _bundle(bundle)
    (bundle / "bundle.json").write_bytes((bundle / "manifest.json").read_bytes())
    result = import_project_bundle(bundle, Settings(data_dir=tmp_path / "data"))
    assert result["projects"][0]["status"] == "created"


@pytest.mark.parametrize("explicit", [False, True])
def test_differing_dual_manifests_fail_before_writing(tmp_path: Path, explicit: bool) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = _bundle(bundle)
    manifest["projects"][0]["title"] = "Conflicting edition"
    (bundle / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
    data_dir = tmp_path / "data"
    source = bundle / "bundle.json" if explicit else bundle
    with pytest.raises(ValueError, match="bundle.json and manifest.json differ"):
        import_project_bundle(source, Settings(data_dir=data_dir))
    assert not data_dir.exists()


@pytest.mark.parametrize("change", ["hash", "traversal", "symlink", "unlisted", "host_path"])
def test_invalid_bundle_writes_nothing(tmp_path: Path, change: str) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = _bundle(bundle)
    if change == "hash":
        manifest["projects"][0]["blocks"][0]["audio_sha256"] = "0" * 64
    elif change == "traversal":
        manifest["projects"][0]["blocks"][0]["audio_file"] = "../speech.wav"
    elif change == "symlink":
        (bundle / "speech.wav").unlink()
        (bundle / "speech.wav").symlink_to(bundle / "voice.wav")
    elif change == "unlisted":
        (bundle / "extra.wav").write_bytes(b"extra")
    else:
        manifest["projects"][0]["provenance"]["origin"] = "/home/private/recording.wav"
    _write_manifest(bundle, manifest)
    data_dir = tmp_path / "data"
    with pytest.raises(ValueError):
        import_project_bundle(bundle, Settings(data_dir=data_dir))
    assert not data_dir.exists()


def test_cli_requires_authorization_and_reports_ids(tmp_path: Path, monkeypatch, capsys) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _bundle(bundle)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(sys, "argv", ["qvl", "import-project-bundle", str(bundle),
                                     "--data-dir", str(data_dir)])
    with pytest.raises(SystemExit):
        main()
    assert not data_dir.exists()
    monkeypatch.setattr(sys, "argv", ["qvl", "import-project-bundle", str(bundle),
                                     "--data-dir", str(data_dir), "--confirm-authorized"])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["projects"][0]["status"] == "created"
    assert str(tmp_path) not in json.dumps(result)
