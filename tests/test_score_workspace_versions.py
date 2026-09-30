from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
import test_score_workspaces as workspace_fixtures
from fastapi.testclient import TestClient
from pydantic import ValidationError

from qwen_voice_lab.app import create_app
from qwen_voice_lab.models import RevisionCreate
from qwen_voice_lab.score_workspaces import (
    MAX_SCORE_REQUEST_BYTES,
    ScoreConflict,
    ScoreRevisionCreate,
    ScoreRevisionRestore,
    ScoreUnavailable,
    ScoreWorkspaces,
)


@pytest.fixture
def collection(tmp_path: Path):
    return workspace_fixtures.collection.__wrapped__(tmp_path)


def edit_request(detail: dict, **overrides) -> ScoreRevisionCreate:
    return ScoreRevisionCreate.model_validate({
        "expected_revision_id": detail["revision"]["id"],
        "lead_in_ms": 500,
        "blocks": [{"source_key": block["source_key"], "pause_after_ms": 2500}
                   for block in reversed(detail["catalog_blocks"])],
        **overrides,
    })


def saved_rows(service) -> list[tuple]:
    with sqlite3.connect(service.database_path) as connection:
        return connection.execute("SELECT id,number,payload FROM score_revisions "
                                  "ORDER BY number").fetchall()


def legacy_catalog_snapshot(service) -> dict:
    """Construct exactly the stored Loop 1 shape, with all sources and no date."""
    detail = service.detail("example_score")
    old = deepcopy(detail)
    old.pop("schema_version")
    old["revision"]["blocks"] = old.pop("catalog_blocks")
    for key in ("kind", "created_at", "restored_from_revision_id", "includes_variants"):
        old["revision"].pop(key)
    old["workspace"].pop("source_project_ids")
    with sqlite3.connect(service.database_path) as connection:
        connection.execute("UPDATE score_revisions SET payload=? WHERE id=?",
                           (json.dumps(old), old["revision"]["id"]))
    return old


def test_edit_preserves_pinned_sources_and_persists_order_pauses_and_subset(collection):
    service, manager, _, _, projects, _ = collection
    original = service.detail("example_score")
    original_db = saved_rows(service)
    source_snapshots = [p.revision.model_dump() for p in projects]
    request = edit_request(original)
    changed = service.save("example_score", request)
    assert changed["revision"]["number"] == 2
    assert changed["revision"]["kind"] == "edit"
    assert changed["revision"]["lead_in_ms"] == 500
    assert changed["revision"]["includes_variants"] is True
    assert [b["source_key"] for b in changed["revision"]["blocks"]] == ["example_1", "example_0"]
    assert [b["order"] for b in changed["revision"]["blocks"]] == [0, 1]
    assert [b["pause_after_ms"] for b in changed["revision"]["blocks"]] == [2500, 2500]
    assert changed["catalog_blocks"] == original["catalog_blocks"]
    originals = {b["source_key"]: b for b in original["catalog_blocks"]}
    for block in changed["revision"]["blocks"]:
        source = originals[block["source_key"]]
        for key in ("text", "source", "baseline_speed", "speech_speed", "duration_seconds"):
            assert block[key] == source[key]
    assert saved_rows(service)[0] == original_db[0]
    assert [service.store.get_revision(p.revision.id).model_dump() for p in projects] \
        == source_snapshots
    restarted = ScoreWorkspaces(service.settings, service.store)
    assert restarted.detail("example_score") == changed
    assert restarted.detail("example_score", revision_id=original["revision"]["id"]) == original
    assert restarted.revisions("example_score")[0]["created_at"] is not None
    manager.add_revision(projects[0].id, RevisionCreate(
        blocks=[{"id": "block_001", "text": "Later source revision."}],
        expected_revision_id=projects[0].revision.id,
    ))
    assert restarted.detail("example_score") == changed
    subset = restarted.save("example_score", edit_request(changed, blocks=[{
        "source_key": "example_1", "pause_after_ms": 0,
    }]))
    assert len(subset["revision"]["blocks"]) == 1
    assert len(subset["catalog_blocks"]) == 2
    assert subset["revision"]["blocks"][0]["role"] == "variant"
    assert len(restarted.revisions("example_score")) == 3


def test_restore_appends_version_without_mutating_or_deleting_history(collection):
    service, _, _, _, _, _ = collection
    initial = service.detail("example_score")
    changed = service.save("example_score", edit_request(initial))
    before = saved_rows(service)
    restored = service.restore("example_score", ScoreRevisionRestore(
        expected_revision_id=changed["revision"]["id"], revision_id=initial["revision"]["id"],
    ))
    assert saved_rows(service)[:2] == before
    assert restored["revision"]["number"] == 3
    assert restored["revision"]["id"] not in {b[0] for b in before}
    assert restored["revision"]["kind"] == "restore"
    assert restored["revision"]["restored_from_revision_id"] == initial["revision"]["id"]
    assert restored["revision"]["blocks"] == initial["revision"]["blocks"]
    assert restored["revision"]["lead_in_ms"] == initial["revision"]["lead_in_ms"]
    assert service.detail("example_score", revision_id=changed["revision"]["id"]) == changed
    assert service.revisions("example_score")[0]["block_count"] == 1


@pytest.mark.parametrize("operation", ["save", "restore"])
def test_two_connections_commit_only_one_against_expected_revision(collection, operation):
    service, _, _, _, _, _ = collection
    original = service.detail("example_score")
    barrier = threading.Barrier(2)

    def attempt(_):
        independent = ScoreWorkspaces(service.settings, service.store)
        barrier.wait(timeout=5)
        try:
            if operation == "save":
                return independent.save("example_score", edit_request(original))
            return independent.restore("example_score", ScoreRevisionRestore(
                expected_revision_id=original["revision"]["id"],
                revision_id=original["revision"]["id"],
            ))
        except ScoreConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(isinstance(result, ScoreConflict) for result in results) == 1
    assert len(saved_rows(service)) == 2
    published = next(result for result in results if isinstance(result, dict))
    assert service.detail("example_score")["revision"]["id"] == published["revision"]["id"]


def test_legacy_migration_is_additive_once_and_explicit_restore_keeps_variants(collection):
    service, _, _, _, _, _ = collection
    legacy = legacy_catalog_snapshot(service)
    original_rows = saved_rows(service)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ScoreWorkspaces(service.settings, service.store)
                               .detail("example_score"), range(2)))
    assert results[0] == results[1]
    migrated = results[0]
    assert migrated["revision"]["number"] == 2
    assert migrated["revision"]["kind"] == "initial_composition"
    assert [b["role"] for b in migrated["revision"]["blocks"]] == ["main"]
    assert len(migrated["catalog_blocks"]) == 2
    assert saved_rows(service)[0] == original_rows[0]
    restarted = ScoreWorkspaces(service.settings, service.store)
    assert restarted.detail("example_score") == migrated
    assert len(saved_rows(service)) == 2
    history = restarted.revisions("example_score")
    assert history[1]["kind"] == "catalog_initial"
    assert history[1]["includes_variants"] is True
    assert history[1]["created_at"] is None
    restored = restarted.restore("example_score", ScoreRevisionRestore(
        expected_revision_id=migrated["revision"]["id"], revision_id=legacy["revision"]["id"],
    ))
    assert restored["revision"]["kind"] == "restore"
    assert restored["revision"]["includes_variants"] is True
    assert len(restored["revision"]["blocks"]) == 2
    assert restarted.detail("example_score") == restored  # No implicit variant filtering.
    assert len(saved_rows(service)) == 3


@pytest.mark.parametrize("field,value", [
    ("lead_in_ms", -1), ("lead_in_ms", 60001), ("lead_in_ms", 0.5),
    ("lead_in_ms", True), ("blocks", []),
    ("blocks", [{"source_key": "example_0", "pause_after_ms": float("nan")}]),
    ("blocks", [{"source_key": "example_0", "pause_after_ms": True}]),
    ("blocks", [{"source_key": "example_0", "pause_after_ms": 60001}]),
    ("blocks", [{"source_key": "example_0", "pause_after_ms": 0}] * 257),
    ("blocks", [{"source_key": "example_0", "pause_after_ms": 0}] * 2),
    ("blocks", [{"source_key": "../outside", "pause_after_ms": 0}]),
    ("blocks", [{"source_key": "example_0", "pause_after_ms": 0, "text": "Replacement"}]),
])
def test_strict_edit_fields_reject_invalid_values_before_writing(collection, field, value):
    service, _, _, _, _, _ = collection
    initial = service.detail("example_score")
    with pytest.raises(ValidationError):
        edit_request(initial, **{field: value})
    assert len(saved_rows(service)) == 1


def test_unknown_sources_and_unavailable_catalogs_do_not_publish(collection):
    service, _, manifest, path, _, takes = collection
    initial = service.detail("example_score")
    with pytest.raises(ValueError, match="not in this collection"):
        service.save("example_score", edit_request(initial, blocks=[{
            "source_key": "unauthorized_source", "pause_after_ms": 0,
        }]))
    Path(takes[0].trimmed_file).write_bytes(b"altered speech")
    with pytest.raises(ScoreUnavailable):
        service.save("example_score", edit_request(initial))
    with pytest.raises(ScoreUnavailable):
        service.restore("example_score", ScoreRevisionRestore(
            expected_revision_id=initial["revision"]["id"], revision_id=initial["revision"]["id"],
        ))
    manifest["title"] = "Changed catalog"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ScoreUnavailable):
        service.save("example_score", edit_request(initial))
    assert len(saved_rows(service)) == 1


def test_failed_publication_rolls_back_snapshot_and_pointer_together(collection, monkeypatch):
    service, _, _, _, _, _ = collection
    original = service.detail("example_score")
    before = saved_rows(service)
    publish = service._insert_snapshot

    def fail_after_insert(connection, detail):
        publish(connection, detail)
        raise RuntimeError("simulated publication failure")

    monkeypatch.setattr(service, "_insert_snapshot", fail_after_insert)
    with pytest.raises(RuntimeError, match="simulated publication failure"):
        service.save("example_score", edit_request(original))
    assert saved_rows(service) == before
    assert service.detail("example_score") == original


def test_historical_revision_belongs_to_its_workspace(collection):
    service, _, manifest, path, _, _ = collection
    first = service.detail("example_score")
    second_manifest = deepcopy(manifest)
    second_manifest["id"] = "another_score"
    another_path = path.with_name("another_score.json")
    another_path.write_text(json.dumps(second_manifest))
    second = service.detail("another_score")
    with pytest.raises(KeyError):
        service.detail("example_score", revision_id=second["revision"]["id"])
    with pytest.raises(KeyError):
        service.restore("example_score", ScoreRevisionRestore(
            expected_revision_id=first["revision"]["id"], revision_id=second["revision"]["id"],
        ))
    assert len(service.revisions("example_score")) == 1
    assert len(service.revisions("another_score")) == 1
    assert service.list()["default_workspace_id"] is None


def test_unavailable_manifest_keeps_original_project_membership_and_editorial_mode(collection):
    service, _, manifest, path, projects, _ = collection
    original = service.detail("example_score")
    expected = sorted(p.id for p in projects)
    assert original["workspace"]["source_project_ids"] == expected
    manifest["editorial_mode"] = False
    path.write_text(json.dumps(manifest))
    changed = service.list()
    assert changed["editorial_mode"] is True
    assert changed["workspaces"][0]["source_project_ids"] == expected
    path.unlink()
    missing = service.list()
    assert missing["workspaces"][0]["status"] == "unavailable"
    assert missing["workspaces"][0]["source_project_ids"] == expected
    assert service.detail("example_score")["revision"]["id"] == original["revision"]["id"]


def test_authenticated_versions_api_and_request_limit_before_source_validation(
    collection, monkeypatch,
):
    service, _, _, _, _, _ = collection
    app = create_app(service.settings.model_copy(update={"access_token": "x" * 32}))
    def no_generation(*args, **kwargs):
        raise AssertionError("Version operations cannot render a voice")
    monkeypatch.setattr(app.state.manager.engine, "render_synthesis", no_generation)
    monkeypatch.setattr(app.state.manager.engine, "render_design", no_generation)
    client = TestClient(app)
    initial = service.detail("example_score")
    path = "/api/score-workspaces/example_score"
    payload = edit_request(initial).model_dump(mode="json")
    for suffix, method in (("/revisions", "GET"), ("/revisions", "POST"), ("/restore", "POST")):
        response = client.request(method, path + suffix, json=payload if method == "POST" else None)
        assert response.status_code == 401
    client.post("/api/auth/session", json={"token": "x" * 32})
    response = client.post(path + "/revisions", json=payload)
    assert response.status_code == 200
    saved = response.json()
    assert saved["revision"]["number"] == 2
    assert client.post(path + "/revisions", json=payload).status_code == 409
    assert len(client.get(path + "/revisions").json()) == 2
    assert client.get(path + "/revisions/" + initial["revision"]["id"]).json() == initial
    assert client.get(path + "/revisions/not_a_revision").status_code == 404
    invalid = {**payload, "expected_revision_id": saved["revision"]["id"], "extra": True}
    assert client.post(path + "/revisions", json=invalid).status_code == 422
    invalid["extra"] = False
    restore = {"expected_revision_id": saved["revision"]["id"],
               "revision_id": initial["revision"]["id"]}
    restored = client.post(path + "/restore", json=restore)
    assert restored.status_code == 200
    assert restored.json()["revision"]["restored_from_revision_id"] == initial["revision"]["id"]
    assert client.post(path + "/restore", json=restore).status_code == 409

    def forbidden(*args, **kwargs):
        raise AssertionError("Oversized requests must fail before source validation")

    monkeypatch.setattr(app.state.score_workspaces, "save", forbidden)
    monkeypatch.setattr(app.state.score_workspaces, "restore", forbidden)
    oversized = b" " * (MAX_SCORE_REQUEST_BYTES + 1)
    assert client.post(path + "/revisions", content=oversized).status_code == 413
    assert client.post(path + "/restore", content=iter([oversized[:1000], oversized[1000:]])
                       ).status_code == 413
