from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from pathlib import Path

import numpy as np
import soundfile as sf

from .engine import sha256_file, write_wav
from .models import ProjectSegment, Take


def read_take_asset(take: Take, projects_root: Path, *, raw: bool = False) -> tuple[bytes, str]:
    """Read, authenticate, and return the exact immutable bytes a caller will consume."""
    configured = Path(take.raw_file if raw else take.trimmed_file)
    lexical_root = projects_root.absolute()
    lexical_path = configured.absolute()
    if lexical_root != lexical_path.parent and lexical_root not in lexical_path.parents:
        raise ValueError("take audio is outside the projects directory")
    cursor = lexical_root
    for component in lexical_path.relative_to(lexical_root).parts:
        cursor /= component
        if cursor.is_symlink():
            raise ValueError("take audio path must not contain symbolic links")
    try:
        path = configured.resolve(strict=True)
        allowed = (projects_root / take.project_id).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("take audio is unavailable") from exc
    if allowed != path.parent and allowed not in path.parents:
        raise ValueError("take audio is outside its project directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(configured, flags)
    except OSError as exc:
        raise ValueError("take audio could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("take audio is not a regular file")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    expected = take.raw_sha256 if raw else take.trimmed_sha256
    current_sha256 = hashlib.sha256(data).hexdigest()
    if current_sha256 != expected:
        raise ValueError("take audio SHA-256 does not match its immutable record")
    return data, current_sha256


def trim_speech_edges(
    raw_file: Path,
    trimmed_file: Path,
    *,
    threshold_db: float,
    padding_ms: int,
) -> tuple[int, int, float]:
    audio, sample_rate = sf.read(raw_file, dtype="float32", always_2d=False)
    array = np.asarray(audio, dtype=np.float32)
    mono = np.max(np.abs(array), axis=1) if array.ndim > 1 else np.abs(array)
    threshold = 10 ** (threshold_db / 20)
    active = np.flatnonzero(mono >= threshold)
    if len(active):
        padding = round(sample_rate * padding_ms / 1000)
        start = max(0, int(active[0]) - padding)
        end = min(len(array), int(active[-1]) + padding + 1)
    else:
        start, end = 0, len(array)
    trimmed = array[start:end]
    write_wav(trimmed_file, trimmed, sample_rate)
    return (
        round(start * 1000 / sample_rate),
        round((len(array) - end) * 1000 / sample_rate),
        len(trimmed) / sample_rate,
    )


def build_timeline(
    output: Path,
    manifest_file: Path,
    segments: list[ProjectSegment],
    takes: dict[str, Take],
    *,
    project_id: str,
    revision_id: str,
    projects_root: Path,
) -> dict:
    chunks: list[np.ndarray] = []
    timeline = []
    sample_rate: int | None = None
    cursor = 0
    for segment in sorted(segments, key=lambda row: row.position):
        if not segment.selected_take_id or segment.selected_take_id not in takes:
            raise ValueError(f"segment {segment.id} has no selected take")
        take = takes[segment.selected_take_id]
        if (
            take.project_id != project_id
            or take.segment_id != segment.id
            or take.text_sha256 != segment.text_sha256
        ):
            raise ValueError(f"selected take {take.id} is incompatible with segment {segment.id}")
        asset, verified_sha256 = read_take_asset(take, projects_root)
        audio, rate = sf.read(io.BytesIO(asset), dtype="float32", always_2d=False)
        if sample_rate is None:
            sample_rate = rate
        if rate != sample_rate:
            raise ValueError("selected takes use different sample rates")
        start = cursor
        chunks.append(np.asarray(audio, dtype=np.float32))
        cursor += len(audio)
        pause_samples = round(segment.pause_after_ms * sample_rate / 1000)
        if pause_samples:
            chunks.append(np.zeros(pause_samples, dtype=np.float32))
        timeline.append(
            {
                "segment_id": segment.id,
                "position": segment.position,
                "text_sha256": segment.text_sha256,
                "take_id": take.id,
                "take_sha256": verified_sha256,
                "speech_start_sample": start,
                "speech_end_sample": cursor,
                "pause_samples": pause_samples,
            }
        )
        cursor += pause_samples
    if sample_rate is None:
        raise ValueError("cannot assemble an empty project")
    write_wav(output, np.concatenate(chunks), sample_rate)
    manifest = {
        "schema_version": "qwen-voice-lab-assembly-v1",
        "project_id": project_id,
        "revision_id": revision_id,
        "sample_rate": sample_rate,
        "total_samples": cursor,
        "output_sha256": sha256_file(output),
        "timeline": timeline,
    }
    manifest_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_file.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_file.chmod(0o600)
    return manifest
