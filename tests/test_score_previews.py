from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
import test_score_workspaces as workspace_fixtures
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qwen_voice_lab import score_previews
from qwen_voice_lab.app import create_app
from qwen_voice_lab.models import RevisionCreate
from qwen_voice_lab.score_previews import (
    SAMPLE_RATE,
    ScorePreviewBusy,
    ScorePreviewCreate,
    ScorePreviews,
)
from qwen_voice_lab.score_workspaces import ScoreRevisionCreate


@pytest.fixture
def collection(tmp_path: Path):
    return workspace_fixtures.collection.__wrapped__(tmp_path)


@pytest.fixture
def converter(monkeypatch):
    calls = []

    def convert(asset, speed, directory):
        calls.append((asset, speed))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"prepared_{len(calls)}.wav"
        frames = round(2400 / speed)
        sf.write(path, np.full(frames, 0.1, dtype=np.float32), SAMPLE_RATE, subtype="PCM_16")
        return path

    monkeypatch.setattr(score_previews, "prepare_speech", convert)
    return calls


def two_block_revision(service):
    initial = service.detail("example_score")
    return service.save(
        "example_score",
        ScoreRevisionCreate(
            expected_revision_id=initial["revision"]["id"],
            lead_in_ms=2345,
            blocks=[
                {"source_key": "example_1", "pause_after_ms": 601},
                {"source_key": "example_0", "pause_after_ms": 1201},
            ],
        ),
    )


def test_exact_samples_baseline_and_selection_semantics(collection, converter):
    service, _, _, _, _, takes = collection
    revision = two_block_revision(service)
    original = [Path(take.trimmed_file).read_bytes() for take in takes]
    previews = ScorePreviews(service.settings, service)
    complete = previews.preview(
        "example_score",
        ScorePreviewCreate(
            revision_id=revision["revision"]["id"],
        ),
    )
    assert complete["selection_mode"] == "full"
    assert complete["source_keys"] == ["example_1", "example_0"]
    assert complete["lead_samples"] == round(2.345 * SAMPLE_RATE)
    assert [speed for _, speed in converter] == pytest.approx([1.2, 1.1 * 0.98])
    record, audio, manifest_bytes = previews.assets("example_score", complete["id"])
    manifest = json.loads(manifest_bytes)
    assert manifest["timeline"] == record["timeline"]
    assert manifest["revision_id"] == revision["revision"]["id"]
    assert manifest["catalog_sha256"] == revision["catalog_sha256"]
    assert manifest["human_responses_included"] is False
    signal, rate = sf.read(io.BytesIO(audio), dtype="int16")
    assert rate == SAMPLE_RATE
    assert len(signal) == complete["total_samples"]
    assert complete["duration_seconds"] == len(signal) / rate
    assert sf.info(io.BytesIO(audio)).channels == 1
    assert sf.info(io.BytesIO(audio)).subtype == "PCM_16"
    assert np.all(signal[: complete["lead_samples"]] == 0)
    for row in complete["timeline"]:
        assert row["voice_end_sample"] - row["start_sample"] == round(2400 / row["effective_speed"])
        assert row["end_sample"] - row["voice_end_sample"] == row["pause_samples"]
        assert row["pause_samples"] == round(row["pause_after_ms"] * SAMPLE_RATE / 1000)
        assert np.all(signal[row["voice_end_sample"] : row["end_sample"]] == 0)
        assert row["source"]["take_revision_id"] != row["source"]["revision_id"]
    partial = previews.preview(
        "example_score",
        ScorePreviewCreate(
            revision_id=revision["revision"]["id"],
            source_keys=["example_0", "example_1"],
        ),
    )
    assert partial["source_keys"] == complete["source_keys"]  # Snapshot order, not request order.
    assert partial["lead_samples"] == 0
    assert partial["total_samples"] == complete["total_samples"] - complete["lead_samples"]
    single = previews.preview(
        "example_score",
        ScorePreviewCreate(
            revision_id=revision["revision"]["id"],
            source_keys=["example_0"],
        ),
    )
    assert single["lead_in_ms"] == 0
    assert single["timeline"][0]["start_sample"] == 0
    assert single["timeline"][0]["pause_samples"] == round(1.201 * SAMPLE_RATE)
    assert [Path(take.trimmed_file).read_bytes() for take in takes] == original
    assert str(service.settings.data_dir) not in json.dumps(record)


def test_real_cpu_ffmpeg_preview_matches_derived_frames_and_preserves_sources(collection):
    service, _, manifest, path, _, takes = collection
    for index, take in enumerate(takes):
        target = Path(take.trimmed_file)
        signal = 0.1 * np.sin(
            2 * np.pi * (440 + index * 110) * np.arange(3 * SAMPLE_RATE) / SAMPLE_RATE
        )
        sf.write(target, signal, SAMPLE_RATE, subtype="PCM_16")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        service.store.save_take(
            take.model_copy(update={"raw_sha256": digest, "trimmed_sha256": digest})
        )
        manifest["blocks"][index]["source"]["audio_sha256"] = digest
    path.write_text(json.dumps(manifest))
    revision = two_block_revision(service)
    previews = ScorePreviews(service.settings, service)
    result = previews.preview(
        "example_score", ScorePreviewCreate(revision_id=revision["revision"]["id"])
    )
    directory = previews.outputs / result["id"] / "speech"
    for row in result["timeline"]:
        derived = next(
            p
            for p in directory.glob("*.wav")
            if hashlib.sha256(p.read_bytes()).hexdigest() == row["derived_sha256"]
        )
        frames = sf.info(derived).frames
        assert frames == row["voice_end_sample"] - row["start_sample"]
        assert frames / SAMPLE_RATE == pytest.approx(3 / row["effective_speed"], abs=0.08)


def test_unknown_duplicate_empty_and_foreign_composition_sources_rejected(collection, converter):
    service, _, _, _, _, _ = collection
    initial = service.detail("example_score")
    previews = ScorePreviews(service.settings, service)
    with pytest.raises(ValueError, match="saved composition"):
        previews.preview(
            "example_score",
            ScorePreviewCreate(
                revision_id=initial["revision"]["id"],
                source_keys=["example_1"],
            ),
        )
    for keys in ([], ["example_0", "example_0"], ["../outside"], ["example_0"] * 257):
        with pytest.raises(ValidationError):
            ScorePreviewCreate(revision_id=initial["revision"]["id"], source_keys=keys)
    with pytest.raises(ValidationError):
        ScorePreviewCreate(revision_id=initial["revision"]["id"], extra="replacement")
    assert converter == []


@pytest.mark.parametrize("limit", ["sources", "duration", "disk"])
def test_limits_reject_before_conversion(collection, converter, monkeypatch, limit):
    service, _, _, _, _, _ = collection
    revision = two_block_revision(service)
    settings = service.settings.model_copy()
    if limit == "sources":
        settings.score_preview_max_sources = 1
    elif limit == "duration":
        settings.score_preview_max_duration_seconds = 1
    else:
        monkeypatch.setattr(score_previews.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    previews = ScorePreviews(settings, service)
    with pytest.raises(ValueError):
        previews.preview(
            "example_score", ScorePreviewCreate(revision_id=revision["revision"]["id"])
        )
    assert converter == []
    assert not previews.outputs.exists()


def test_disk_is_checked_again_after_conversion_before_wav_write(
    collection, converter, monkeypatch
):
    service, _, _, _, _, _ = collection
    initial = service.detail("example_score")
    checks = iter([1024 * 1024 * 1024, 1])
    monkeypatch.setattr(
        score_previews.shutil, "disk_usage", lambda _: SimpleNamespace(free=next(checks))
    )
    previews = ScorePreviews(service.settings, service)
    with pytest.raises(ValueError, match="disk space"):
        previews.preview("example_score", ScorePreviewCreate(revision_id=initial["revision"]["id"]))
    assert len(converter) == 1
    assert not list(previews.outputs.glob("*/preview.wav"))


def test_actual_converted_duration_is_bounded_before_publication(collection, monkeypatch):
    service, _, _, _, _, _ = collection
    initial = service.detail("example_score")
    revision = service.save(
        "example_score",
        ScoreRevisionCreate(
            expected_revision_id=initial["revision"]["id"],
            lead_in_ms=0,
            blocks=[{"source_key": "example_0", "pause_after_ms": 0}],
        ),
    )

    def oversized(asset, speed, directory):
        directory.mkdir(parents=True)
        path = directory / "oversized.wav"
        sf.write(path, np.zeros(2 * SAMPLE_RATE), SAMPLE_RATE, subtype="PCM_16")
        return path

    monkeypatch.setattr(score_previews, "prepare_speech", oversized)
    settings = service.settings.model_copy(update={"score_preview_max_duration_seconds": 1})
    previews = ScorePreviews(settings, service)
    with pytest.raises(ValueError, match="Converted speech"):
        previews.preview(
            "example_score", ScorePreviewCreate(revision_id=revision["revision"]["id"])
        )
    assert not list(previews.outputs.glob("*/preview.wav"))


def test_failed_publication_never_serves_orphan_assets(collection, converter, monkeypatch):
    service, _, _, _, _, _ = collection
    initial = service.detail("example_score")
    previews = ScorePreviews(service.settings, service)

    def failed(*args):
        raise RuntimeError("simulated publication failure")

    monkeypatch.setattr(previews, "_publish", failed)
    with pytest.raises(RuntimeError):
        previews.preview("example_score", ScorePreviewCreate(revision_id=initial["revision"]["id"]))
    directory = next(previews.outputs.iterdir())
    assert (directory / "preview.wav").exists()
    assert (directory / "manifest.json").exists()
    with pytest.raises(KeyError):
        previews.assets("example_score", directory.name)


def test_historical_and_late_preview_stays_bound_to_requested_snapshot(collection, monkeypatch):
    service, manager, _, _, projects, _ = collection
    initial = service.detail("example_score")
    previews = ScorePreviews(service.settings, service)
    entered = threading.Event()
    finish = threading.Event()
    original_prepare = score_previews.prepare_speech

    def blocked(asset, speed, directory):
        entered.set()
        assert finish.wait(5)
        return original_prepare(asset, speed, directory)

    monkeypatch.setattr(score_previews, "prepare_speech", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            previews.preview,
            "example_score",
            ScorePreviewCreate(
                revision_id=initial["revision"]["id"],
            ),
        )
        try:
            assert entered.wait(5)
            changed = service.save(
                "example_score",
                ScoreRevisionCreate(
                    expected_revision_id=initial["revision"]["id"],
                    lead_in_ms=50,
                    blocks=[{"source_key": "example_0", "pause_after_ms": 0}],
                ),
            )
            with pytest.raises(ScorePreviewBusy):
                ScorePreviews(service.settings, service).preview(
                    "example_score",
                    ScorePreviewCreate(
                        revision_id=changed["revision"]["id"],
                    ),
                )
        finally:
            finish.set()
        result = pending.result(timeout=5)
    assert result["revision_id"] == initial["revision"]["id"]
    assert result["lead_in_ms"] == 10000
    assert result["timeline"][0]["pause_after_ms"] == 1250
    manager.add_revision(
        projects[0].id,
        RevisionCreate(
            blocks=[{"id": "block_001", "text": "Later source."}],
            expected_revision_id=projects[0].revision.id,
        ),
    )
    assert previews.assets("example_score", result["id"])[0] == result
    assert service.detail("example_score")["revision"]["id"] == changed["revision"]["id"]


def test_preview_lock_is_shared_across_processes(collection, converter):
    service, _, _, _, _, _ = collection
    initial = service.detail("example_score")
    previews = ScorePreviews(service.settings, service)
    context = multiprocessing.get_context("fork")
    ready, finish = context.Event(), context.Event()

    def hold():
        with previews._admission():
            ready.set()
            finish.wait(5)

    process = context.Process(target=hold)
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(ScorePreviewBusy):
            previews.preview(
                "example_score", ScorePreviewCreate(revision_id=initial["revision"]["id"])
            )
        assert converter == []
    finally:
        finish.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0


@pytest.mark.parametrize("mutation", ["audio", "manifest", "audio_symlink", "directory_symlink"])
def test_altered_or_substituted_preview_fails_closed(collection, converter, mutation):
    service, _, _, _, _, _ = collection
    initial = service.detail("example_score")
    previews = ScorePreviews(service.settings, service)
    result = previews.preview(
        "example_score", ScorePreviewCreate(revision_id=initial["revision"]["id"])
    )
    directory = previews.outputs / result["id"]
    audio = directory / "preview.wav"
    if mutation == "audio":
        audio.write_bytes(b"altered")
    elif mutation == "manifest":
        (directory / "manifest.json").write_bytes(b"altered")
    elif mutation == "audio_symlink":
        outside = service.root / "outside.wav"
        audio.rename(outside)
        audio.symlink_to(outside)
    else:
        outside = service.root / "outside"
        directory.rename(outside)
        directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        previews.assets("example_score", result["id"])


def test_authenticated_preview_endpoints_and_fixed_beacon(collection, converter, monkeypatch):
    service, manager, manifest, path, projects, _ = collection
    beacon_directory = service.settings.projects_dir / projects[0].id / "beacon"
    beacon_directory.mkdir()
    beacon_file = beacon_directory / "audio.wav"
    beacon_file.write_bytes(workspace_fixtures.silent_wav())
    manifest["beacon"] = {
        "project_id": projects[0].id,
        "asset_sha256": hashlib.sha256(beacon_file.read_bytes()).hexdigest(),
        "enabled": True,
        "offset_seconds": 2.5,
        "volume": 0.3,
    }
    path.write_text(json.dumps(manifest))
    initial = service.detail("example_score")
    app = create_app(service.settings.model_copy(update={"access_token": "x" * 32}))

    def forbidden(*args, **kwargs):
        raise AssertionError("Score previews must never enter voice generation or validators")

    monkeypatch.setattr(app.state.manager.engine, "render_synthesis", forbidden)
    monkeypatch.setattr(app.state.manager.engine, "render_design", forbidden)
    monkeypatch.setattr(app.state.projects.validator, "validate", forbidden)
    client = TestClient(app)
    endpoint = "/api/score-workspaces/example_score/preview"
    body = {"revision_id": initial["revision"]["id"]}
    assert client.post(endpoint, json=body).status_code == 401
    client.post("/api/auth/session", json={"token": "x" * 32})
    response = client.post(endpoint, json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["beacon"]["offset_seconds"] == 2.5
    assert result["beacon"]["volume"] == 0.3
    assert client.get(result["audio_url"]).status_code == 200
    assert client.get(result["manifest_url"]).json()["timeline"] == result["timeline"]
    download = client.get(result["download_url"])
    assert "attachment" in download.headers["content-disposition"]
    manager.add_revision(
        projects[0].id,
        RevisionCreate(
            blocks=[{"id": "block_001", "text": "A later source with no Beacon."}],
            expected_revision_id=projects[0].revision.id,
        ),
    )
    assert client.get(result["beacon"]["audio_url"]).content == beacon_file.read_bytes()
    for asset_url in (result["audio_url"], result["beacon"]["audio_url"]):
        complete = client.get(asset_url)
        data = complete.content
        assert complete.headers["accept-ranges"] == "bytes"
        for range_header, expected, first, last in (
            ("bytes=0-43", data[:44], 0, 43),
            ("bytes=44-", data[44:], 44, len(data) - 1),
            ("bytes=-44", data[-44:], len(data) - 44, len(data) - 1),
        ):
            partial = client.get(asset_url, headers={"Range": range_header})
            assert partial.status_code == 206
            assert partial.content == expected
            assert partial.headers["content-range"] == f"bytes {first}-{last}/{len(data)}"
        for bad_range in ("bytes=9-2", "bytes=-0", f"bytes={len(data)}-",
                          "bytes=0-1,3-4", "bytes=" + "9" * 1000 + "-"):
            rejected = client.get(asset_url, headers={"Range": bad_range})
            assert rejected.status_code == 416
            assert rejected.headers["content-range"] == f"bytes */{len(data)}"
    client.delete("/api/auth/session")
    for url in (
        result["audio_url"],
        result["manifest_url"],
        result["download_url"],
        result["beacon"]["audio_url"],
    ):
        assert client.get(url).status_code == 401
    client.post("/api/auth/session", json={"token": "x" * 32})
    beacon_file.write_bytes(b"altered Beacon")
    assert client.get(
        result["beacon"]["audio_url"], headers={"Range": "bytes=0-43"}
    ).status_code == 409
    assert (
        client.get(result["audio_url"].replace("example_score", "other_score")).status_code == 404
    )
    before = len(converter)
    assert client.post(endpoint, content=b" " * (256 * 1024 + 1)).status_code == 413
    assert len(converter) == before
    assert client.post(endpoint, json={**body, "source_keys": []}).status_code == 422
