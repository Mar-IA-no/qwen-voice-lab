from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from .engine import sha256_file, write_wav
from .models import ProjectSegment, Take


def read_project_asset(
    configured: Path,
    expected_sha256: str,
    projects_root: Path,
    project_id: str,
    *,
    label: str,
) -> tuple[bytes, str]:
    """Read one project asset through a no-follow descriptor and authenticate its bytes."""
    lexical_root = projects_root.absolute()
    lexical_project = (projects_root / project_id).absolute()
    lexical_path = configured.absolute()
    if lexical_project != lexical_path.parent and lexical_project not in lexical_path.parents:
        raise ValueError(f"{label} is outside its project directory")
    cursor = lexical_root
    for component in lexical_path.relative_to(lexical_root).parts:
        cursor /= component
        if cursor.is_symlink():
            raise ValueError(f"{label} path must not contain symbolic links")
    try:
        path = configured.resolve(strict=True)
        allowed = (projects_root / project_id).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if allowed != path.parent and allowed not in path.parents:
        raise ValueError(f"{label} is outside its project directory")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(configured, flags)
    except OSError as exc:
        raise ValueError(f"{label} could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} is not a regular file")
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    current_sha256 = hashlib.sha256(data).hexdigest()
    if current_sha256 != expected_sha256:
        raise ValueError(f"{label} SHA-256 does not match its immutable record")
    return data, current_sha256


def read_take_asset(take: Take, projects_root: Path, *, raw: bool = False) -> tuple[bytes, str]:
    """Read, authenticate, and return the exact immutable bytes a caller will consume."""
    configured = Path(take.raw_file if raw else take.trimmed_file)
    expected = take.raw_sha256 if raw else take.trimmed_sha256
    return read_project_asset(
        configured,
        expected,
        projects_root,
        take.project_id,
        label="take audio",
    )


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
    lead_pause_ms: int = 0,
    speech_speed: float = 1.0,
) -> dict:
    if lead_pause_ms < 0 or not math.isfinite(speech_speed) or not 0.5 <= speech_speed <= 2:
        raise ValueError("invalid initial pause or speech speed")
    if not segments:
        raise ValueError("cannot assemble an empty project")
    sample_rate = 24_000
    prepared = []
    total_estimate = round(lead_pause_ms * sample_rate / 1000)
    for segment in sorted(segments, key=lambda row: row.position):
        if not segment.selected_take_id or segment.selected_take_id not in takes:
            raise ValueError(f"segment {segment.id} has no selected take")
        take = takes[segment.selected_take_id]
        if (take.project_id != project_id or take.segment_id != segment.id
                or take.text_sha256 != segment.text_sha256):
            raise ValueError(f"selected take {take.id} is incompatible with segment {segment.id}")
        speed = segment.speed if getattr(segment, "speed", None) is not None else speech_speed
        baseline = getattr(take, "baseline_speed", 1.0)
        if not math.isfinite(speed) or not 0.5 <= speed <= 2:
            raise ValueError("speech speed must be between 0.5 and 2")
        if segment.pause_after_ms < 0:
            raise ValueError("pause must be nonnegative")
        asset, verified_sha256 = read_take_asset(take, projects_root)
        derived = prepare_speech(
            asset, baseline * speed, projects_root / project_id / "derived",
        )
        frames = sf.info(derived).frames
        total_estimate += frames + round(segment.pause_after_ms * sample_rate / 1000)
        prepared.append((segment, take, derived, verified_sha256, speed, baseline))
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # PCM16 needs two bytes per sample; leave working headroom on the destination.
    if total_estimate * 2 + 1024 * 1024 > shutil.disk_usage(output.parent).free:
        raise ValueError("not enough disk space for the requested duration")
    timeline = []
    cursor = 0
    fd, temporary = tempfile.mkstemp(dir=output.parent, suffix=".wav")
    os.close(fd)
    try:
        with sf.SoundFile(temporary, "w", samplerate=sample_rate, channels=1,
                          subtype="PCM_16", format="WAV") as target:
            cursor += _write_silence(target, round(lead_pause_ms * sample_rate / 1000))
            for segment, take, derived, verified_sha256, speed, baseline in prepared:
                start = cursor
                with sf.SoundFile(derived) as source:
                    for block in source.blocks(blocksize=65536, dtype="float32"):
                        target.write(block)
                        cursor += len(block)
                pause_samples = round(segment.pause_after_ms * sample_rate / 1000)
                timeline.append({
                    "segment_id": segment.id, "position": segment.position,
                    "text": segment.text, "text_sha256": segment.text_sha256,
                    "take_id": take.id, "take_sha256": verified_sha256,
                    "derived_sha256": sha256_file(derived),
                    "speech_start_sample": start, "speech_end_sample": cursor,
                    "pause_samples": pause_samples, "speed": speed,
                    "baseline_speed": baseline, "effective_speed": speed * baseline,
                    "provenance": getattr(take, "provenance", {}),
                })
                cursor += _write_silence(target, pause_samples)
        os.replace(temporary, output)
        output.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    manifest = {
        "schema_version": "qwen-voice-lab-assembly-v1",
        "project_id": project_id,
        "revision_id": revision_id,
        "sample_rate": sample_rate,
        "channels": 1,
        "format": "PCM_16",
        "lead_pause_ms": lead_pause_ms,
        "speech_speed": speech_speed,
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


def _write_silence(target: sf.SoundFile, frames: int) -> int:
    remaining = frames
    zeros = np.zeros(65536, dtype=np.float32)
    while remaining:
        count = min(remaining, len(zeros))
        target.write(zeros[:count])
        remaining -= count
    return frames


def prepare_speech(asset: bytes, speed: float, cache_dir: Path) -> Path:
    """Derive canonical speech from authenticated bytes; never adjust pauses or originals."""
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("invalid audio tempo")
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cache_dir.is_symlink():
        raise ValueError("derived cache must not be a symbolic link")
    try:
        version = subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True,
                                 timeout=10).stdout.splitlines()[0].decode()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("ffmpeg is required for speech preview") from exc
    recipe = {"audio": hashlib.sha256(asset).hexdigest(), "speed": speed,
              "rate": 24000, "channels": 1, "format": "PCM_16", "ffmpeg": version}
    key = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
    output = cache_dir / f"{key}.wav"
    receipt = cache_dir / f"{key}.json"
    if output.is_symlink() or receipt.is_symlink():
        raise ValueError("derived assets must not be symbolic links")
    if output.exists() and receipt.exists():
        record = json.loads(receipt.read_text())
        if record["sha256"] != sha256_file(output):
            raise ValueError("derived audio hash mismatch")
        return output
    tempo = speed
    filters = []
    while tempo < 0.5:
        filters.append("atempo=0.5")
        tempo /= 0.5
    while tempo > 2:
        filters.append("atempo=2")
        tempo /= 2
    if tempo != 1:
        filters.append(f"atempo={tempo:.12g}")
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", "pipe:0",
               "-vn", "-ac", "1", "-ar", "24000"]
    if filters:
        command.extend(["-af", ",".join(filters)])
    fd, temporary = tempfile.mkstemp(dir=cache_dir, suffix=".wav")
    os.close(fd)
    try:
        subprocess.run([*command, "-c:a", "pcm_s16le", temporary], input=asset,
                       check=True, capture_output=True, timeout=300)
        if not sf.info(temporary).frames:
            raise ValueError("speech preview has no audio")
        os.replace(temporary, output)
        output.chmod(0o600)
        record_fd, record_path = tempfile.mkstemp(dir=cache_dir, suffix=".json")
        try:
            with os.fdopen(record_fd, "w") as stream:
                json.dump({**recipe, "sha256": sha256_file(output)}, stream)
            os.replace(record_path, receipt)
        finally:
            Path(record_path).unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("speech conversion failed") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)
    return output
