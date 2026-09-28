from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import soundfile as sf
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Settings
from .editorial import normalize_spoken_text, segments_to_markdown
from .engine import sha256_file
from .models import (
    BeaconSettings,
    Language,
    Project,
    ProjectSegment,
    ProjectStatus,
    RevisionBlock,
    SamplingSettings,
    SourceRevision,
    Take,
    TakeStatus,
    Voice,
    VoiceKind,
)
from .storage import Store

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_BLOCK_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_HOST_PATH = re.compile(r"(?:^|[\s=:])(?:/[^/\s]+|[A-Za-z]:[\\/]|~/)")
_BEACON_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
}


class _BundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _VoiceSpec(_BundleModel):
    name: str = Field(min_length=1, max_length=80)
    reference_file: str
    reference_sha256: str
    reference_text: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def require_transcript(self) -> _VoiceSpec:
        if not self.reference_text.strip():
            raise ValueError("reference transcript cannot be blank")
        return self


class _BeaconSpec(_BundleModel):
    file: str
    sha256: str


class _BlockSpec(_BundleModel):
    id: str
    text: str = Field(min_length=1, max_length=4000)
    pause_after_ms: int = Field(ge=0)
    speed: None = None
    audio_file: str | None = None
    audio_sha256: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def paired_audio(self) -> _BlockSpec:
        if (self.audio_file is None) != (self.audio_sha256 is None):
            raise ValueError("audio_file and audio_sha256 must appear together")
        if not _BLOCK_ID.fullmatch(self.id):
            raise ValueError("invalid block id")
        if not normalize_spoken_text(self.text):
            raise ValueError("block text has no spoken content")
        return self


class _ProjectSpec(_BundleModel):
    source_id: str
    title: str = Field(min_length=1, max_length=120)
    language: Language
    baseline_speed: float
    provenance: dict[str, Any] = Field(default_factory=dict)
    blocks: list[_BlockSpec] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_blocks(self) -> _ProjectSpec:
        if not _SOURCE_ID.fullmatch(self.source_id):
            raise ValueError("invalid source_id")
        if self.baseline_speed != 0.98:
            raise ValueError("bundle project baseline_speed must be 0.98")
        ids = [block.id for block in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate block id")
        return self


class _BundleSpec(_BundleModel):
    schema_version: str
    bundle_id: str
    voice: _VoiceSpec
    beacon: _BeaconSpec | None = None
    projects: list[_ProjectSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity(self) -> _BundleSpec:
        if self.schema_version != "qvl-project-bundle-v1":
            raise ValueError("unsupported bundle schema")
        if not _IDENTIFIER.fullmatch(self.bundle_id):
            raise ValueError("invalid bundle_id")
        ids = [project.source_id for project in self.projects]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source_id")
        return self


def _relative_file(root: Path, name: str) -> Path:
    if not name or "\\" in name or "\x00" in name or ":" in name:
        raise ValueError(f"invalid bundle path: {name!r}")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError(f"bundle path escapes root: {name!r}")
    path = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"bundle symlink forbidden: {name!r}")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"bundle file is not regular: {name!r}")
    return path


def _check_metadata(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _check_metadata(key)
            _check_metadata(item)
    elif isinstance(value, list):
        for item in value:
            _check_metadata(item)
    elif isinstance(value, str) and ("file://" in value or _HOST_PATH.search(value)):
        raise ValueError("host paths are forbidden in provenance")


def _preflight(bundle_path: Path) -> tuple[Path, _BundleSpec, str]:
    if bundle_path.is_symlink():
        raise ValueError("bundle symlink forbidden")
    root = bundle_path if bundle_path.is_dir() else bundle_path.parent
    if root.is_symlink():
        raise ValueError("bundle root symlink forbidden")
    available_manifests = {
        name for name in ("bundle.json", "manifest.json") if os.path.lexists(root / name)
    }
    if available_manifests == {"bundle.json", "manifest.json"}:
        canonical = _relative_file(root, "bundle.json").read_bytes()
        legacy = _relative_file(root, "manifest.json").read_bytes()
        if canonical != legacy:
            raise ValueError("bundle.json and manifest.json differ")
    if bundle_path.is_dir():
        manifest_name = "bundle.json" if "bundle.json" in available_manifests else "manifest.json"
    else:
        manifest_name = bundle_path.name
    manifest_file = _relative_file(root, manifest_name)
    raw = manifest_file.read_bytes()
    manifest_hash = hashlib.sha256(raw).hexdigest()
    spec = _BundleSpec.model_validate_json(raw)
    expected: dict[str, str] = {}

    def register(name: str, digest: str) -> None:
        if name in expected and expected[name] != digest:
            raise ValueError("conflicting hashes for shared bundle file")
        expected[name] = digest

    register(spec.voice.reference_file, spec.voice.reference_sha256)
    if spec.beacon:
        if Path(spec.beacon.file).suffix.lower() not in _BEACON_MIME:
            raise ValueError("unsupported beacon audio format")
        register(spec.beacon.file, spec.beacon.sha256)
    for project in spec.projects:
        _check_metadata(project.provenance)
        for block in project.blocks:
            _check_metadata(block.provenance)
            if block.audio_file:
                register(block.audio_file, block.audio_sha256 or "")
    if set(expected) & (available_manifests | {manifest_name}):
        raise ValueError("manifest cannot be used as audio")
    for name, digest in expected.items():
        if not _SHA256.fullmatch(digest):
            raise ValueError(f"invalid SHA-256 for {name!r}")
        if sha256_file(_relative_file(root, name)) != digest:
            raise ValueError(f"SHA-256 mismatch for {name!r}")
    observed = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError("bundle contains symlink or special file")
            if stat.S_ISREG(mode):
                observed.add(path.relative_to(root).as_posix())
    if observed != set(expected) | available_manifests | {manifest_name}:
        raise ValueError("bundle has missing or unlisted files")
    return root, spec, manifest_hash


def _data_path(path: Path, data_dir: Path) -> None:
    if data_dir.is_symlink() or data_dir.resolve() not in path.resolve().parents:
        raise ValueError("import destination must be inside data directory")
    current = data_dir
    for part in path.relative_to(data_dir).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("data destination symlink forbidden")


def _copy_verified(source: Path, target: Path, digest: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
    target.chmod(0o600)
    if sha256_file(target) != digest:
        raise ValueError("bundle source changed during import")


def _beacon_blob(source: Path, digest: str, settings: Settings) -> Path:
    directory = settings.projects_dir / ".beacon-blobs"
    _data_path(directory, settings.data_dir)
    directory.mkdir(mode=0o700, exist_ok=True)
    blob = directory / digest
    _data_path(blob, settings.data_dir)
    if os.path.lexists(blob):
        if (not stat.S_ISREG(blob.lstat().st_mode) or
                blob.stat().st_mode & 0o222 or sha256_file(blob) != digest):
            raise ValueError("existing beacon blob is not immutable or has the wrong hash")
        return blob

    probe = directory / f".link-probe-{uuid.uuid4().hex}"
    probe_link = directory / f"{probe.name}.link"
    try:
        probe.touch(mode=0o600, exist_ok=False)
        try:
            os.link(probe, probe_link, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError("data filesystem must support hardlinks for beacon import") from exc
    finally:
        probe_link.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)

    staged = directory / f".{digest}.{uuid.uuid4().hex}.tmp"
    try:
        _copy_verified(source, staged, digest)
        staged.chmod(0o400)
        try:
            os.link(staged, blob, follow_symlinks=False)
        except FileExistsError as exc:
            if (not stat.S_ISREG(blob.lstat().st_mode) or
                    blob.stat().st_mode & 0o222 or sha256_file(blob) != digest):
                raise ValueError("existing beacon blob differs from source") from exc
        except OSError as exc:
            raise RuntimeError("could not install beacon hardlink") from exc
    finally:
        staged.unlink(missing_ok=True)
    return blob


def _decode(source: Path, target: Path) -> tuple[float, str]:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:a:0",
         "-vn", "-sn", "-dn", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
         "-f", "wav", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"ffmpeg could not decode imported audio: {result.stderr.strip()}")
    target.chmod(0o600)
    info = sf.info(target)
    if info.samplerate != 24000 or info.channels != 1 or info.subtype != "PCM_16":
        raise ValueError("canonical audio is not mono PCM16 24kHz")
    return info.duration, sha256_file(target)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def import_project_bundle(
    bundle_path: Path, settings: Settings, store: Store | None = None
) -> dict[str, Any]:
    """Import a verified private bundle without modifying existing projects."""
    root, spec, manifest_hash = _preflight(bundle_path)
    data_dir = settings.data_dir
    if any(path.is_symlink() for path in
           (data_dir, settings.voices_dir, settings.projects_dir)):
        raise ValueError("data directory symlink forbidden")
    if root.resolve() == data_dir.resolve() or root.resolve() in data_dir.resolve().parents:
        raise ValueError("data directory cannot be inside the source bundle")
    settings.prepare()
    if store is None:
        store = Store(settings)
    voice_id = _stable_id("voice", spec.voice.reference_sha256, spec.voice.reference_text,
                          spec.voice.name)
    voice = store.get_voice(voice_id)
    if voice:
        existing_reference = Path(voice.reference_file)
        _data_path(existing_reference, data_dir)
        if (settings.voices_dir / voice_id).resolve() not in existing_reference.resolve().parents:
            raise ValueError("existing imported voice reference is outside its voice directory")
        if (voice.reference_sha256 != spec.voice.reference_sha256 or
                voice.reference_text != spec.voice.reference_text or
                voice.name != spec.voice.name or
                not existing_reference.is_file() or
                sha256_file(existing_reference) != spec.voice.reference_sha256):
            raise ValueError("existing imported voice differs from bundle")
    else:
        suffix = Path(spec.voice.reference_file).suffix.lower() or ".audio"
        voice_dir = settings.voices_dir / voice_id
        _data_path(voice_dir, data_dir)
        voice_dir.mkdir(mode=0o700)
        reference = voice_dir / f"reference{suffix}"
        _copy_verified(_relative_file(root, spec.voice.reference_file), reference,
                       spec.voice.reference_sha256)
        try:
            reference_duration = sf.info(reference).duration
        except RuntimeError:
            reference_duration = None
        voice = Voice(id=voice_id, name=spec.voice.name, kind=VoiceKind.CLONE,
                      reference_text=spec.voice.reference_text,
                      reference_file=str(reference.resolve()),
                      reference_sha256=spec.voice.reference_sha256,
                      duration_seconds=reference_duration)
        store.save_voice(voice)
    results = []
    beacon_blob: Path | None = None
    for source in spec.projects:
        project_id = _stable_id("project", spec.bundle_id, source.source_id, manifest_hash)
        existing = store.get_project(project_id)
        if existing:
            identity = existing.provenance.get("import", {})
            if identity != {"bundle_id": spec.bundle_id, "source_id": source.source_id,
                            "manifest_sha256": manifest_hash}:
                raise ValueError("project ID collision or changed import provenance")
            if not store.get_revision(_stable_id("revision", project_id, "1")):
                raise ValueError("existing import is incomplete: source revision missing")
            if any(block.audio_file and not store.get_take(
                _stable_id("take", project_id, block.id)
            ) for block in source.blocks):
                raise ValueError("existing import is incomplete: imported take missing")
            results.append({"source_id": source.source_id, "project_id": project_id,
                            "status": "existing"})
            continue
        final_dir = settings.projects_dir / project_id
        _data_path(final_dir, data_dir)
        if final_dir.exists():
            raise FileExistsError("project assets already exist without a project record")
        stage = settings.projects_dir / f".import-{uuid.uuid4().hex}"
        stage.mkdir(mode=0o700)
        revision_id = _stable_id("revision", project_id, "1")
        segments = []
        takes = []
        revision_blocks = []
        installed = False
        try:
            for position, block in enumerate(source.blocks):
                normalized = normalize_spoken_text(block.text)
                text_hash = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
                take_id = _stable_id("take", project_id, block.id) if block.audio_file else None
                segment = ProjectSegment(id=block.id, project_id=project_id,
                                         revision_id=revision_id, position=position,
                                         text=block.text, normalized_text=normalized,
                                         text_sha256=text_hash,
                                         pause_after_ms=block.pause_after_ms,
                                         speed=None, provenance=block.provenance,
                                         selected_take_id=take_id)
                segments.append(segment)
                revision_blocks.append({"id": block.id, "text": block.text,
                                        "pause_after_ms": block.pause_after_ms,
                                        "speed": None, "provenance": block.provenance,
                                        "selected_take_id": take_id})
                if block.audio_file:
                    suffix = Path(block.audio_file).suffix.lower() or ".audio"
                    relative = Path("takes") / block.id / take_id
                    original = stage / relative / f"original{suffix}"
                    canonical = stage / relative / "speech.wav"
                    _copy_verified(_relative_file(root, block.audio_file), original,
                                   block.audio_sha256 or "")
                    duration, canonical_hash = _decode(original, canonical)
                    takes.append(Take(id=take_id, project_id=project_id,
                                      revision_id=revision_id, segment_id=block.id,
                                      attempt=1, seed=0, status=TakeStatus.NEEDS_REVIEW,
                                      raw_file=str(final_dir / relative / original.name),
                                      trimmed_file=str(final_dir / relative / "speech.wav"),
                                      raw_sha256=block.audio_sha256,
                                      trimmed_sha256=canonical_hash,
                                      duration_seconds=duration, trim_start_ms=0,
                                      trim_end_ms=0, voice_id=voice_id,
                                      voice_reference_sha256=voice.reference_sha256,
                                      model="imported-audio", text_sha256=text_hash,
                                      sampling=SamplingSettings(), selected=True,
                                      baseline_speed=1, provenance=block.provenance))
            if spec.beacon:
                suffix = Path(spec.beacon.file).suffix.lower() or ".audio"
                beacon_file = stage / "beacon" / f"audio{suffix}"
                if beacon_blob is None:
                    beacon_blob = _beacon_blob(
                        _relative_file(root, spec.beacon.file), spec.beacon.sha256, settings
                    )
                beacon_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.link(beacon_blob, beacon_file, follow_symlinks=False)
                except OSError as exc:
                    raise RuntimeError("could not link beacon into project") from exc
                beacon_manifest = {"sha256": spec.beacon.sha256,
                                   "mime_type": _BEACON_MIME[suffix]}
                (stage / "beacon" / "manifest.json").write_text(
                    json.dumps(beacon_manifest, indent=2) + "\n", encoding="utf-8")
            markdown = segments_to_markdown(segments)
            snapshot = [RevisionBlock(**block) for block in revision_blocks]
            beacon_settings = BeaconSettings(
                enabled=False, asset_id=spec.beacon.sha256 if spec.beacon else None,
                offset_seconds=0, volume=0.25,
            )
            source_payload = json.dumps(
                {"blocks": [block.model_dump() for block in snapshot],
                 "lead_pause_ms": 0, "speech_speed": 1,
                 "beacon": beacon_settings.model_dump()},
                ensure_ascii=False, sort_keys=True,
            )
            revision = SourceRevision(id=revision_id, project_id=project_id, number=1,
                                      markdown=markdown,
                                      source_sha256=hashlib.sha256(
                                          source_payload.encode()
                                      ).hexdigest(),
                                      blocks=snapshot,
                                      lead_pause_ms=0, speech_speed=1,
                                      beacon=beacon_settings)
            project = Project(id=project_id, title=source.title, voice_id=voice_id,
                              language=source.language, project_seed=20260805,
                              sampling=SamplingSettings(), status=ProjectStatus.NEEDS_REVIEW,
                              current_revision_id=revision_id, baseline_speed=0.98,
                              provenance={**source.provenance,
                                          "import": {"bundle_id": spec.bundle_id,
                                                     "source_id": source.source_id,
                                                     "manifest_sha256": manifest_hash}})
            stage.rename(final_dir)
            installed = True
            store.save_project_revision(project, revision, segments)
            for take in takes:
                store.save_take(take)
            results.append({"source_id": source.source_id, "project_id": project_id,
                            "status": "created"})
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if installed and not store.get_project(project_id) and final_dir.exists():
                shutil.rmtree(final_dir)
    return {"schema_version": spec.schema_version, "bundle_id": spec.bundle_id,
            "manifest_sha256": manifest_hash, "voice_id": voice_id, "projects": results}
