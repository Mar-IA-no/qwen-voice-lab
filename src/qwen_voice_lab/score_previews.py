"""Bounded CPU previews of immutable composition snapshots; no generation jobs."""

from __future__ import annotations

import fcntl
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import soundfile as sf
from pydantic import Field, model_validator

from .audio_pipeline import _write_silence, prepare_speech, read_project_asset, read_take_asset
from .config import Settings
from .engine import sha256_file
from .models import utc_now
from .score_workspaces import Identifier, ScoreUnavailable, ScoreWorkspaces, StrictModel

SAMPLE_RATE = 24_000


class ScorePreviewCreate(StrictModel):
    revision_id: Identifier
    source_keys: list[Identifier] | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_keys(self) -> ScorePreviewCreate:
        if self.source_keys is not None and len(set(self.source_keys)) != len(self.source_keys):
            raise ValueError("source keys must be unique")
        return self


class ScorePreviewBusy(ValueError):
    """Another CPU composition is in progress on this installation."""


class ScorePreviews:
    def __init__(self, settings: Settings, workspaces: ScoreWorkspaces):
        self.settings = settings
        self.workspaces = workspaces
        self.root = workspaces.root
        self.outputs = self.root / "previews"

    @contextmanager
    def _admission(self):
        if self.root.is_symlink():
            raise ScoreUnavailable("Score preview storage is unavailable.")
        descriptor = os.open(
            self.root / "preview.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ScoreUnavailable("Score preview lock is unavailable.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ScorePreviewBusy(
                    "Another preview is running; try again when it finishes."
                ) from exc
            yield
        finally:
            os.close(descriptor)

    def _disk(self, required_bytes: int, directory: Path) -> None:
        if (
            required_bytes + self.settings.score_preview_disk_headroom_bytes
            > shutil.disk_usage(directory).free
        ):
            raise ValueError("Not enough disk space for this score preview.")

    def _select(self, detail: dict, request: ScorePreviewCreate) -> tuple[list[dict], int]:
        blocks = detail["revision"]["blocks"]
        if request.source_keys is None:
            selected = blocks
            lead = detail["revision"]["lead_in_ms"]
        else:
            keys = set(request.source_keys)
            if keys - {b["source_key"] for b in blocks}:
                raise ValueError("Preview sources must belong to this saved composition.")
            selected = [block for block in blocks if block["source_key"] in keys]
            lead = 0
        if not selected or len(selected) > self.settings.score_preview_max_sources:
            raise ValueError("This preview exceeds the source limit or has no selected sources.")
        return selected, lead

    def _preflight(self, blocks: list[dict], lead_ms: int) -> tuple[float, float]:
        speech_seconds = 0.0
        duration = lead_ms / 1000
        for block in blocks:
            speed = block["speech_speed"] * block["baseline_speed"]
            seconds = block["duration_seconds"]
            if (
                not math.isfinite(speed)
                or speed <= 0
                or not math.isfinite(seconds)
                or seconds <= 0
                or not 0 <= block["pause_after_ms"] <= 60_000
            ):
                raise ValueError("Pinned speech timing is invalid.")
            voice_seconds = seconds / speed
            speech_seconds += voice_seconds
            duration += voice_seconds + block["pause_after_ms"] / 1000
        if duration > self.settings.score_preview_max_duration_seconds:
            raise ValueError("This preview exceeds the duration limit.")
        # Reserve for source conversions and the final PCM16 WAV before conversion.
        self._disk(math.ceil((speech_seconds + duration) * SAMPLE_RATE * 2), self.root)
        return speech_seconds, duration

    def _publish(self, workspace_id: str, revision_id: str, response: dict) -> None:
        with sqlite3.connect(self.workspaces.database_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS score_previews "
                "(id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
                "revision_id TEXT NOT NULL, created_at TEXT NOT NULL, "
                "payload TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO score_previews VALUES(?,?,?,?,?)",
                (
                    response["id"],
                    workspace_id,
                    revision_id,
                    utc_now(),
                    json.dumps(response, ensure_ascii=False),
                ),
            )

    def preview(self, workspace_id: str, request: ScorePreviewCreate) -> dict:
        detail = self.workspaces.detail(workspace_id, revision_id=request.revision_id)
        if detail["workspace"]["status"] != "ready":
            raise ScoreUnavailable("Pinned sources are unavailable for this preview.")
        blocks, lead_ms = self._select(detail, request)
        with self._admission():
            self._preflight(blocks, lead_ms)
            if self.outputs.is_symlink():
                raise ScoreUnavailable("Score preview storage is unavailable.")
            self.outputs.mkdir(parents=True, exist_ok=True, mode=0o700)
            preview_id = f"preview_{uuid.uuid4().hex}"
            directory = self.outputs / preview_id
            directory.mkdir(mode=0o700)
            cursor = round(lead_ms * SAMPLE_RATE / 1000)
            prepared = []
            timeline = []
            for block in blocks:
                source = block["source"]
                take = self.workspaces.store.get_take(source["take_id"])
                if (
                    not take
                    or take.project_id != source["project_id"]
                    or take.segment_id != source["segment_id"]
                    or take.text_sha256 != source["text_sha256"]
                    or take.trimmed_sha256 != source["audio_sha256"]
                    or take.baseline_speed != block["baseline_speed"]
                ):
                    raise ScoreUnavailable("Pinned take changed before speech conversion.")
                asset, digest = read_take_asset(take, self.settings.projects_dir)
                effective_speed = block["speech_speed"] * block["baseline_speed"]
                derived = prepare_speech(asset, effective_speed, directory / "speech")
                info = sf.info(derived)
                if (
                    info.samplerate != SAMPLE_RATE
                    or info.channels != 1
                    or info.subtype != "PCM_16"
                    or info.frames <= 0
                ):
                    raise ValueError("Prepared speech must be nonempty 24 kHz mono PCM16.")
                start = cursor
                cursor += info.frames
                voice_end = cursor
                pause_samples = round(block["pause_after_ms"] * SAMPLE_RATE / 1000)
                cursor += pause_samples
                if cursor > self.settings.score_preview_max_duration_seconds * SAMPLE_RATE:
                    raise ValueError("Converted speech exceeds the preview duration limit.")
                prepared.append((derived, pause_samples))
                timeline.append(
                    {
                        "source_key": block["source_key"],
                        "position": len(timeline),
                        "role": block["role"],
                        "stage_id": block["stage_id"],
                        "stage_title": block["stage_title"],
                        "text": block["text"],
                        "source": deepcopy(source),
                        "take_id": take.id,
                        "speech_sha256": digest,
                        "derived_sha256": sha256_file(derived),
                        "speech_speed": block["speech_speed"],
                        "baseline_speed": block["baseline_speed"],
                        "effective_speed": effective_speed,
                        "start_sample": start,
                        "voice_end_sample": voice_end,
                        "end_sample": cursor,
                        "pause_after_ms": block["pause_after_ms"],
                        "pause_samples": pause_samples,
                        "response_marker": block["response_marker"],
                    }
                )
            self._disk(cursor * 2, directory)
            audio_path = directory / "preview.wav"
            with sf.SoundFile(
                audio_path, "w", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16", format="WAV"
            ) as target:
                _write_silence(target, round(lead_ms * SAMPLE_RATE / 1000))
                for derived, pause_samples in prepared:
                    with sf.SoundFile(derived) as source:
                        for samples in source.blocks(blocksize=65_536, dtype="int16"):
                            target.write(samples)
                    _write_silence(target, pause_samples)
            audio_path.chmod(0o600)
            if sf.info(audio_path).frames != cursor:
                raise ValueError("Preview sample count does not match its timeline.")
            base_url = f"/api/score-workspaces/{workspace_id}/previews/{preview_id}"
            beacon = deepcopy(detail["revision"]["beacon"])
            if beacon:
                beacon["audio_url"] = base_url + "/beacon"
            manifest = {
                "schema_version": "score-workspace-preview-v1",
                "id": preview_id,
                "workspace_id": workspace_id,
                "revision_id": request.revision_id,
                "catalog_sha256": detail["catalog_sha256"],
                "created_at": utc_now(),
                "source_keys": [b["source_key"] for b in blocks],
                "selection_mode": "full" if request.source_keys is None else "selection",
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "format": "PCM_16",
                "lead_in_ms": lead_ms,
                "lead_samples": round(lead_ms * SAMPLE_RATE / 1000),
                "total_samples": cursor,
                "duration_seconds": cursor / SAMPLE_RATE,
                "output_sha256": sha256_file(audio_path),
                "timeline": timeline,
                "human_responses_included": False,
                "beacon": beacon,
                "audio_url": base_url + "/audio",
                "download_url": base_url + "/download",
                "manifest_url": base_url + "/manifest",
            }
            manifest_path = directory / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest_path.chmod(0o600)
            manifest_sha = sha256_file(manifest_path)
            # Publication has authority only after both exact assets authenticate.
            read_project_asset(
                audio_path,
                manifest["output_sha256"],
                self.outputs,
                preview_id,
                label="score preview audio",
            )
            read_project_asset(
                manifest_path,
                manifest_sha,
                self.outputs,
                preview_id,
                label="score preview manifest",
            )
            response = {**manifest, "manifest_sha256": manifest_sha}
            self._publish(workspace_id, request.revision_id, response)
            return response

    def _record(self, workspace_id: str, preview_id: str) -> dict:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", workspace_id) or not re.fullmatch(
            r"preview_[a-f0-9]{32}", preview_id
        ):
            raise KeyError(preview_id)
        if not self.workspaces.database_path.exists():
            raise KeyError(preview_id)
        with sqlite3.connect(self.workspaces.database_path) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='score_previews'"
            ).fetchone():
                raise KeyError(preview_id)
            row = connection.execute(
                "SELECT payload FROM score_previews WHERE workspace_id=? AND id=?",
                (workspace_id, preview_id),
            ).fetchone()
        if row is None:
            raise KeyError(preview_id)
        record = json.loads(row[0])
        if record["id"] != preview_id or record["workspace_id"] != workspace_id:
            raise ScoreUnavailable("Score preview identity does not match its record.")
        return record

    def assets(self, workspace_id: str, preview_id: str) -> tuple[dict, bytes, bytes]:
        if self.root.is_symlink() or self.outputs.is_symlink():
            raise ScoreUnavailable("Score preview storage is unavailable.")
        record = self._record(workspace_id, preview_id)
        directory = self.outputs / record["id"]
        audio, _ = read_project_asset(
            directory / "preview.wav",
            record["output_sha256"],
            self.outputs,
            record["id"],
            label="score preview audio",
        )
        manifest, _ = read_project_asset(
            directory / "manifest.json",
            record["manifest_sha256"],
            self.outputs,
            record["id"],
            label="score preview manifest",
        )
        return record, audio, manifest

    def beacon(self, workspace_id: str, preview_id: str) -> tuple[bytes, str]:
        _, _, manifest_bytes = self.assets(workspace_id, preview_id)
        beacon = json.loads(manifest_bytes)["beacon"]
        if beacon is None:
            raise KeyError("beacon")
        directory = self.settings.projects_dir / beacon["project_id"] / "beacon"
        candidates = list(directory.glob("audio.*"))
        if len(candidates) != 1:
            raise ScoreUnavailable("Pinned Beacon audio is unavailable.")
        audio, _ = read_project_asset(
            candidates[0],
            beacon["asset_sha256"],
            self.settings.projects_dir,
            beacon["project_id"],
            label="score Beacon audio",
        )
        return audio, mimetypes.guess_type(candidates[0].name)[0] or "application/octet-stream"
