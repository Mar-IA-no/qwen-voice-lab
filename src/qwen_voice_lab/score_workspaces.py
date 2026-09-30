"""Pinned editorial collections and additive composition versions, without synthesis."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import uuid
from copy import deepcopy
from typing import Annotated, Any, Literal

import soundfile as sf
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .audio_pipeline import read_project_asset, read_take_asset
from .config import Settings
from .models import SourceRevision, utc_now
from .storage import Store

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Pause = Annotated[int, Field(strict=True, ge=0, le=60_000)]
WORKSPACE_SCHEMA = "score-workspace-v2"
MAX_SCORE_REQUEST_BYTES = 256 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ScoreSource(StrictModel):
    project_id: Identifier
    revision_id: Identifier
    revision_sha256: Digest
    revision_snapshot_sha256: Digest
    segment_id: Identifier
    take_id: Identifier
    text_sha256: Digest
    audio_sha256: Digest


class ScoreCatalogBlock(StrictModel):
    source_key: Identifier
    role: Literal["main", "variant"]
    stage_id: Identifier
    stage_title: str = Field(min_length=1, max_length=200)
    order: int = Field(strict=True, ge=0)
    text: str = Field(min_length=1, max_length=4000)
    pause_after_ms: Pause
    speech_speed: float = Field(ge=0.5, le=2)
    source: ScoreSource
    response_marker: str | None = Field(default=None, max_length=1000)


class ScoreBeacon(StrictModel):
    project_id: Identifier
    asset_sha256: Digest
    enabled: bool = False
    offset_seconds: float = Field(default=0, ge=0)
    volume: float = Field(default=0.25, ge=0, le=1)


class ScoreCollection(StrictModel):
    schema_version: Literal["score-workspace-collection-v1"]
    id: Identifier
    title: str = Field(min_length=1, max_length=200)
    is_default: bool = False
    editorial_mode: bool = True
    lead_in_ms: Pause = 0
    blocks: list[ScoreCatalogBlock] = Field(min_length=1, max_length=256)
    beacon: ScoreBeacon | None = None

    @model_validator(mode="after")
    def unique_keys(self) -> ScoreCollection:
        if len({block.source_key for block in self.blocks}) != len(self.blocks):
            raise ValueError("source keys must be unique")
        if len({block.order for block in self.blocks}) != len(self.blocks):
            raise ValueError("block order must be unique")
        identities = {(b.source.project_id, b.source.revision_id, b.source.segment_id)
                      for b in self.blocks}
        if len(identities) != len(self.blocks):
            raise ValueError("source block references must be unique")
        return self


class ScoreEditBlock(StrictModel):
    source_key: Identifier
    pause_after_ms: Pause


class ScoreRevisionCreate(StrictModel):
    expected_revision_id: Identifier
    lead_in_ms: Pause
    blocks: list[ScoreEditBlock] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_keys(self) -> ScoreRevisionCreate:
        if len({block.source_key for block in self.blocks}) != len(self.blocks):
            raise ValueError("source keys must be unique")
        return self


class ScoreRevisionRestore(StrictModel):
    expected_revision_id: Identifier
    revision_id: Identifier


class ScoreConflict(ValueError):
    """The composition pointer changed since the client loaded it."""


class ScoreUnavailable(ValueError):
    """Pinned collection or source cannot currently be authenticated."""


def revision_snapshot_sha256(revision: SourceRevision) -> str:
    """Hash the complete canonical JSON revision, distinct from editorial source SHA."""
    payload = json.dumps(revision.model_dump(mode="json"), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ScoreWorkspaces:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.root = settings.data_dir / "score_workspaces"
        self.collections = self.root / "collections"
        self.database_path = self.root / "score_workspaces.sqlite3"

    def _saved(self, workspace_id: str, revision_id: str | None = None) -> dict | None:
        if not self.database_path.exists():
            return None
        with sqlite3.connect(self.database_path) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                      "AND name='score_workspaces'").fetchone():
                return None
            if revision_id is None:
                row = connection.execute(
                    "SELECT revision.payload FROM score_workspaces AS workspace "
                    "JOIN score_revisions AS revision ON revision.id=workspace.current_revision_id "
                    "WHERE workspace.id=?", (workspace_id,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT payload FROM score_revisions WHERE workspace_id=? AND id=?",
                    (workspace_id, revision_id)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _normalize_snapshot(detail: dict) -> dict:
        """Expose a legacy catalog snapshot honestly without mutating its stored bytes."""
        result = deepcopy(detail)
        result.setdefault("editorial_mode", True)
        if result.get("schema_version") != WORKSPACE_SCHEMA:
            result["catalog_blocks"] = deepcopy(result["revision"]["blocks"])
            result["revision"].update(kind="catalog_initial", created_at=None,
                                     restored_from_revision_id=None)
        result["revision"]["includes_variants"] = any(
            b["role"] == "variant" for b in result["revision"]["blocks"])
        result["workspace"]["source_project_ids"] = sorted({
            b["source"]["project_id"] for b in result["catalog_blocks"]})
        return result

    @staticmethod
    def _insert_snapshot(connection: sqlite3.Connection, detail: dict) -> None:
        workspace_id = detail["workspace"]["id"]
        revision = detail["revision"]
        connection.execute("INSERT INTO score_revisions VALUES(?,?,?,?)",
                           (revision["id"], workspace_id, revision["number"],
                            json.dumps(detail, ensure_ascii=False)))
        connection.execute("INSERT INTO score_workspaces VALUES(?,?) "
                           "ON CONFLICT(id) DO UPDATE SET current_revision_id=excluded."
                           "current_revision_id", (workspace_id, revision["id"]))

    def _migrate_initial(self, detail: dict) -> dict:
        """Publish one main-only bootstrap after the immutable Loop 1 catalog snapshot."""
        if detail.get("schema_version") == WORKSPACE_SCHEMA:
            return detail
        workspace_id = detail["workspace"]["id"]
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision.payload FROM score_workspaces AS workspace "
                "JOIN score_revisions AS revision ON revision.id=workspace.current_revision_id "
                "WHERE workspace.id=?", (workspace_id,)).fetchone()
            current = json.loads(row[0])
            if current.get("schema_version") == WORKSPACE_SCHEMA:
                return current
            updated = self._normalize_snapshot(current)
            updated["schema_version"] = WORKSPACE_SCHEMA
            updated["revision"] = {
                **updated["revision"], "id": f"score_{uuid.uuid4().hex}",
                "number": current["revision"]["number"] + 1, "created_at": utc_now(),
                "kind": "initial_composition", "includes_variants": False,
                "blocks": [deepcopy(b) for b in updated["catalog_blocks"] if b["role"] == "main"],
            }
            for order, block in enumerate(updated["revision"]["blocks"]):
                block["order"] = order
            self._insert_snapshot(connection, updated)
        return updated

    def _publish_initial(self, detail: dict) -> dict:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database_path.touch(mode=0o600, exist_ok=True)
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS score_revisions "
                               "(id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
                               "number INTEGER NOT NULL, payload TEXT NOT NULL, "
                               "UNIQUE(workspace_id,number))")
            connection.execute("CREATE TABLE IF NOT EXISTS score_workspaces "
                               "(id TEXT PRIMARY KEY, current_revision_id TEXT NOT NULL)")
            existing = connection.execute("SELECT payload FROM score_revisions "
                                          "WHERE workspace_id=? AND number=1",
                                          (detail["workspace"]["id"],)).fetchone()
            if existing:
                return json.loads(existing[0])
            self._insert_snapshot(connection, detail)
        self.database_path.chmod(0o600)
        return detail

    def _catalog(self, workspace_id: str) -> tuple[ScoreCollection, str]:
        if self.collections.is_symlink() or self.root.is_symlink():
            raise ValueError("Collection manifest is unavailable.")
        if not self.collections.exists():
            raise KeyError(workspace_id)
        path = self.collections / f"{workspace_id}.json"
        if path.is_symlink():
            raise ValueError("Collection manifest is unavailable.")
        if not path.exists():
            raise KeyError(workspace_id)
        payload = path.read_bytes()
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError("Collection manifest exceeds its size limit.")
        catalog = ScoreCollection.model_validate_json(payload)
        if catalog.id != workspace_id:
            raise ValueError("Collection identity does not match its manifest.")
        return catalog, hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _summary(catalog: ScoreCollection, status: str) -> dict:
        return {"id": catalog.id, "title": catalog.title, "is_default": catalog.is_default,
                "status": status,
                "main_block_count": sum(b.role == "main" for b in catalog.blocks),
                "variant_block_count": sum(b.role == "variant" for b in catalog.blocks),
                "source_project_ids": sorted({b.source.project_id for b in catalog.blocks})}

    def _block(self, block: ScoreCatalogBlock) -> dict[str, Any]:
        result = block.model_dump(mode="json")
        result.update(status="unavailable", unavailable_reason=None, baseline_speed=None,
                      duration_seconds=None)
        result["source"].update(take_revision_id=None, validation_status=None,
                                selection_override_reason=None, provenance={}, take_provenance={})
        source = block.source
        try:
            if not self.store.get_project(source.project_id):
                raise ValueError("Pinned source project is unavailable.")
            revision = self.store.get_revision(source.revision_id)
            if not revision or revision.project_id != source.project_id:
                raise ValueError("Pinned source revision is unavailable.")
            if (revision.source_sha256 != source.revision_sha256
                    or revision_snapshot_sha256(revision) != source.revision_snapshot_sha256):
                raise ValueError("Pinned source revision has changed.")
            segment = next((b for b in revision.blocks if b.id == source.segment_id), None)
            if not segment or segment.selected_take_id != source.take_id:
                raise ValueError("Pinned source selection does not match.")
            if (segment.text != block.text
                    or hashlib.sha256(segment.text.encode()).hexdigest() != source.text_sha256):
                raise ValueError("Pinned source text does not match.")
            if (segment.pause_after_ms != block.pause_after_ms
                    or (segment.speed or revision.speech_speed) != block.speech_speed):
                raise ValueError("Pinned source timing does not match.")
            result["source"].update(selection_override_reason=segment.selection_override_reason,
                                    provenance=segment.provenance)
            take = self.store.get_take(source.take_id)
            if (not take or take.project_id != source.project_id
                    or take.segment_id != source.segment_id
                    or take.text_sha256 != source.text_sha256
                    or take.trimmed_sha256 != source.audio_sha256):
                raise ValueError("Pinned take does not match the source block.")
            result["source"].update(take_revision_id=take.revision_id,
                                    validation_status=take.status.value,
                                    take_provenance=take.provenance)
            result["baseline_speed"] = take.baseline_speed
            audio, _ = read_take_asset(take, self.settings.projects_dir)
            info = sf.info(io.BytesIO(audio))
            result["duration_seconds"] = info.frames / info.samplerate
            result["status"] = "ready"
        except (ValueError, OSError, sf.SoundFileError):
            # Never serialize file paths or decoder diagnostics to the browser.
            result["unavailable_reason"] = "Pinned source is missing, altered or incompatible."
        return result

    def _beacon(self, beacon: ScoreBeacon | None) -> dict | None:
        if beacon is None:
            return None
        result = beacon.model_dump(mode="json")
        result.update(status="unavailable", unavailable_reason=None)
        try:
            directory = self.settings.projects_dir / beacon.project_id / "beacon"
            candidates = list(directory.glob("audio.*"))
            if len(candidates) != 1:
                raise ValueError("Beacon asset is unavailable")
            read_project_asset(candidates[0], beacon.asset_sha256, self.settings.projects_dir,
                               beacon.project_id, label="beacon audio")
            result["status"] = "ready"
        except (OSError, ValueError):
            result["unavailable_reason"] = "Pinned Beacon audio is missing or altered."
        return result

    @staticmethod
    def _unavailable(detail: dict, reason: str) -> dict:
        detail["workspace"]["status"] = "unavailable"
        detail["unavailable_reason"] = reason
        for block in detail["revision"]["blocks"] + detail["catalog_blocks"]:
            block.update(status="unavailable", unavailable_reason=reason)
        if detail["revision"]["beacon"]:
            detail["revision"]["beacon"].update(status="unavailable", unavailable_reason=reason)
        return detail

    def _validate_snapshot(self, detail: dict, catalog: ScoreCollection, digest: str) -> dict:
        detail = self._normalize_snapshot(detail)
        if detail["catalog_sha256"] != digest:
            return self._unavailable(detail, "Collection changed; saved sources remain pinned.")
        frozen = {b["source_key"]: b for b in detail["catalog_blocks"]}
        validated = {}
        # Validate original source timings; editorial pauses are independent of them.
        for entry in catalog.blocks:
            checked = self._block(entry)
            prior = frozen[entry.source_key]
            if checked["status"] == "ready" and (
                checked["source"] != prior["source"]
                or checked["baseline_speed"] != prior["baseline_speed"]
                or checked["duration_seconds"] != prior["duration_seconds"]
            ):
                checked.update(status="unavailable",
                               unavailable_reason="Pinned take metadata has changed.")
            validated[entry.source_key] = {**prior, "status": checked["status"],
                                           "unavailable_reason": checked["unavailable_reason"]}
        detail["catalog_blocks"] = [validated[b["source_key"]] for b in detail["catalog_blocks"]]
        for block in detail["revision"]["blocks"]:
            checked = validated[block["source_key"]]
            block.update(status=checked["status"], unavailable_reason=checked["unavailable_reason"])
        beacon = self._beacon(catalog.beacon)
        if detail["revision"]["beacon"]:
            detail["revision"]["beacon"].update(
                status=beacon["status"], unavailable_reason=beacon["unavailable_reason"])
        ready = all(b["status"] == "ready" for b in detail["catalog_blocks"])
        ready = ready and (not beacon or beacon["status"] == "ready")
        detail["workspace"]["status"] = "ready" if ready else "unavailable"
        detail["unavailable_reason"] = None if ready else "One or more pinned sources unavailable."
        return detail

    def detail(self, workspace_id: str, *, revision_id: str | None = None) -> dict:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", workspace_id):
            raise KeyError(workspace_id)
        saved = self._saved(workspace_id, revision_id)
        if revision_id is not None and saved is None:
            raise KeyError(revision_id)
        try:
            catalog, digest = self._catalog(workspace_id)
        except (KeyError, ValueError, OSError, ValidationError) as exc:
            if saved:
                return self._unavailable(self._normalize_snapshot(saved),
                                         "Collection manifest is invalid or unavailable.")
            if isinstance(exc, KeyError):
                raise
            return {"workspace": {"id": workspace_id, "title": workspace_id,
                                  "is_default": False, "status": "unavailable",
                                  "main_block_count": 0, "variant_block_count": 0,
                                  "source_project_ids": []},
                    "catalog_sha256": None, "catalog_blocks": [], "revision": None,
                    "editorial_mode": True,
                    "unavailable_reason": "Collection manifest is invalid or unavailable."}
        if saved:
            checked = self._validate_snapshot(saved, catalog, digest)
            if (revision_id is None and saved.get("schema_version") != WORKSPACE_SCHEMA
                    and checked["workspace"]["status"] == "ready"):
                saved = self._migrate_initial(saved)
                return self._validate_snapshot(saved, catalog, digest)
            return checked
        blocks = [self._block(block) for block in sorted(catalog.blocks, key=lambda b: b.order)]
        beacon = self._beacon(catalog.beacon)
        ready = all(b["status"] == "ready" for b in blocks)
        ready = ready and (not beacon or beacon["status"] == "ready")
        main_blocks = [deepcopy(b) for b in blocks if b["role"] == "main"]
        for order, block in enumerate(main_blocks):
            block["order"] = order
        detail = {"schema_version": WORKSPACE_SCHEMA,
                  "editorial_mode": catalog.editorial_mode,
                  "workspace": self._summary(catalog, "ready" if ready else "unavailable"),
                  "catalog_sha256": digest, "catalog_blocks": blocks,
                  "unavailable_reason": None if ready else "One or more sources unavailable.",
                  "revision": {"id": f"score_{digest[:32]}", "number": 1,
                               "created_at": utc_now(), "kind": "initial_composition",
                               "restored_from_revision_id": None, "includes_variants": False,
                               "lead_in_ms": catalog.lead_in_ms, "blocks": main_blocks,
                               "beacon": beacon}}
        if ready:
            saved = self._publish_initial(detail)
            return self._validate_snapshot(saved, catalog, digest)
        return detail

    def revisions(self, workspace_id: str) -> list[dict]:
        self.detail(workspace_id)
        if not self.database_path.exists():
            return []
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute("SELECT payload FROM score_revisions WHERE workspace_id=? "
                                      "ORDER BY number DESC", (workspace_id,)).fetchall()
        result = []
        for row in rows:
            snapshot = self._normalize_snapshot(json.loads(row[0]))
            revision = snapshot["revision"]
            result.append({key: revision[key] for key in
                           ("id", "number", "created_at", "kind", "restored_from_revision_id",
                            "includes_variants", "lead_in_ms")})
            result[-1]["block_count"] = len(revision["blocks"])
        return result

    def _commit(self, workspace_id: str, expected_revision_id: str, detail: dict) -> dict:
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT current_revision_id FROM score_workspaces "
                                         "WHERE id=?", (workspace_id,)).fetchone()
            if not current or current[0] != expected_revision_id:
                raise ScoreConflict("Score revision changed; preserve your draft and reload.")
            try:
                _, digest = self._catalog(workspace_id)
            except (KeyError, ValueError, OSError) as exc:
                raise ScoreUnavailable("Pinned collection manifest is unavailable.") from exc
            if digest != detail["catalog_sha256"]:
                raise ScoreUnavailable("Collection changed; saved sources remain pinned.")
            number = connection.execute("SELECT MAX(number) FROM score_revisions WHERE "
                                        "workspace_id=?", (workspace_id,)).fetchone()[0] + 1
            detail = deepcopy(detail)
            detail["schema_version"] = WORKSPACE_SCHEMA
            detail["revision"].update(id=f"score_{uuid.uuid4().hex}", number=number,
                                     created_at=utc_now())
            self._insert_snapshot(connection, detail)
        return detail

    def save(self, workspace_id: str, request: ScoreRevisionCreate) -> dict:
        detail = self.detail(workspace_id)
        if not detail["revision"] or detail["workspace"]["status"] != "ready":
            raise ScoreUnavailable("Pinned sources are unavailable; preserve your draft.")
        catalog_blocks = {b["source_key"]: b for b in detail["catalog_blocks"]}
        selected = []
        for order, edit in enumerate(request.blocks):
            if edit.source_key not in catalog_blocks:
                raise ValueError("The selected source is not in this collection.")
            block = deepcopy(catalog_blocks[edit.source_key])
            block.update(order=order, pause_after_ms=edit.pause_after_ms)
            selected.append(block)
        detail["revision"].update(blocks=selected, lead_in_ms=request.lead_in_ms, kind="edit",
                                 restored_from_revision_id=None,
                                 includes_variants=any(b["role"] == "variant" for b in selected))
        return self._commit(workspace_id, request.expected_revision_id, detail)

    def restore(self, workspace_id: str, request: ScoreRevisionRestore) -> dict:
        current = self.detail(workspace_id)
        target = self.detail(workspace_id, revision_id=request.revision_id)
        if (current["workspace"]["status"] != "ready"
                or target["workspace"]["status"] != "ready"):
            raise ScoreUnavailable("Pinned sources are unavailable; preserve your draft.")
        if target["catalog_sha256"] != current["catalog_sha256"]:
            raise ScoreUnavailable("The historical collection does not match the pinned sources.")
        target["revision"].update(kind="restore", restored_from_revision_id=request.revision_id)
        return self._commit(workspace_id, request.expected_revision_id, target)

    def list(self) -> dict:
        workspaces = []
        editorial_mode = False
        known = set()
        if self.collections.exists() and not self.collections.is_symlink():
            known.update(path.stem for path in self.collections.glob("*.json"))
        if self.database_path.exists():
            with sqlite3.connect(self.database_path) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                      "AND name='score_workspaces'").fetchone():
                    known.update(row[0] for row in connection.execute(
                        "SELECT id FROM score_workspaces").fetchall())
        for workspace_id in sorted(known):
            try:
                detail = self.detail(workspace_id)
                workspaces.append(detail["workspace"])
                editorial_mode = editorial_mode or detail["editorial_mode"]
            except KeyError:
                continue
        defaults = [w["id"] for w in workspaces if w["is_default"]]
        return {"workspaces": workspaces, "editorial_mode": editorial_mode,
                "default_workspace_id": defaults[0] if len(defaults) == 1 else None}
