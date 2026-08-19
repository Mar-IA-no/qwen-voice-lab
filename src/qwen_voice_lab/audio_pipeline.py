from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

from .engine import sha256_file, write_wav
from .models import ProjectSegment, Take


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
) -> dict:
    chunks: list[np.ndarray] = []
    timeline = []
    sample_rate: int | None = None
    cursor = 0
    for segment in sorted(segments, key=lambda row: row.position):
        if not segment.selected_take_id or segment.selected_take_id not in takes:
            raise ValueError(f"segment {segment.id} has no selected take")
        take = takes[segment.selected_take_id]
        path = Path(take.trimmed_file)
        audio, rate = sf.read(path, dtype="float32", always_2d=False)
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
                "take_sha256": take.trimmed_sha256,
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
