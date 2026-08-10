#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from qwen_voice_lab.archive import list_archive_assets
from qwen_voice_lab.config import Settings
from qwen_voice_lab.engine import audio_info, sha256_file
from qwen_voice_lab.models import Voice, VoiceKind
from qwen_voice_lab.storage import Store


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import an authorized private corpus into a local Qwen Voice Lab."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--related-assets", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--archive-name", default="private_voice_corpus")
    parser.add_argument("--confirm-authorized", action="store_true")
    return parser.parse_args()


def private_copy(source: Path, destination: Path) -> None:
    if source == destination or source in destination.parents:
        raise RuntimeError("Archive destination must not be inside its source")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise RuntimeError(f"Private import refuses symbolic links: {source}")
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    destination.chmod(0o700)
    for path in destination.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)


def resolve_source(source_root: Path, relative: str) -> Path:
    path = (source_root / relative).resolve()
    if source_root not in path.parents or not path.is_file():
        raise RuntimeError(f"Reference source escapes or is missing: {relative}")
    return path


def build_reference(source_root: Path, row: dict, destination: Path) -> Path:
    if "source_file" in row:
        return resolve_source(source_root, row["source_file"])
    chunks: list[np.ndarray] = []
    sample_rate: int | None = None
    for relative in row.get("concatenate_files", []):
        audio, rate = sf.read(
            resolve_source(source_root, relative), dtype="float32", always_2d=False
        )
        if sample_rate is not None and rate != sample_rate:
            raise RuntimeError("Reference segments have different sample rates")
        sample_rate = rate
        chunks.append(np.asarray(audio, dtype=np.float32))
    if not chunks:
        raise RuntimeError(f"Voice {row['id']} has no source_file or concatenate_files")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sf.write(destination, np.concatenate(chunks), sample_rate or 24_000, subtype="PCM_16")
    destination.chmod(0o600)
    return destination


def save_clone(
    store: Store, settings: Settings, source_root: Path, row: dict
) -> Voice:
    voice_id = row["id"]
    staged = build_reference(source_root, row, settings.temp_dir / f"{voice_id}.wav")
    suffix = staged.suffix.lower()
    voice_dir = settings.voices_dir / voice_id
    voice_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    reference = voice_dir / f"reference{suffix}"
    shutil.copy2(staged, reference)
    reference.chmod(0o600)
    if staged.parent == settings.temp_dir:
        staged.unlink(missing_ok=True)
    duration, _ = audio_info(reference)
    return store.save_voice(
        Voice(
            id=voice_id,
            name=row["name"],
            description=row.get("description", ""),
            kind=VoiceKind.CLONE,
            language_hint=row.get("language_hint", "multilingual"),
            reference_text=row.get("reference_text", ""),
            reference_file=str(reference.resolve()),
            reference_sha256=sha256_file(reference),
            duration_seconds=duration,
            tags=row.get("tags", [])[:12],
        )
    )


def main() -> int:
    args = arguments()
    if not args.confirm_authorized:
        raise SystemExit("Refusing private voice import without --confirm-authorized")
    if Path(args.archive_name).name != args.archive_name:
        raise SystemExit("Archive name must be one path component")
    source = args.source.resolve()
    if not source.is_dir():
        raise SystemExit(f"Private corpus directory does not exist: {source}")

    settings = Settings(data_dir=args.data_dir.resolve())
    settings.prepare()
    store = Store(settings)
    archive_root = settings.archive_dir / args.archive_name
    private_copy(source, archive_root / "corpus")
    if args.related_assets:
        private_copy(args.related_assets.resolve(), archive_root / "related_assets")

    profile: dict = {"voices": [], "existing_voice_updates": []}
    if args.profile:
        profile = json.loads(args.profile.resolve().read_text(encoding="utf-8"))
    imported = [
        save_clone(store, settings, source, row).id for row in profile.get("voices", [])
    ]
    for update in profile.get("existing_voice_updates", []):
        voice = store.get_voice(update["id"])
        if not voice:
            continue
        for field in ("name", "description", "tags", "language_hint"):
            if field in update:
                setattr(voice, field, update[field])
        store.save_voice(voice)

    assets = list_archive_assets(settings.archive_dir)
    report = {
        "schema_version": "qwen-voice-lab-private-import-v1",
        "imported_at": datetime.now(UTC).isoformat(),
        "archive_name": args.archive_name,
        "authorization_basis": profile.get("authorization_basis", "operator_confirmation"),
        "source_consent_provenance": profile.get("source_consent_provenance", "not_supplied"),
        "voice_ids": imported,
        "archive_audio_assets": len(assets),
        "archive_bytes": sum(asset.size_bytes for asset in assets),
    }
    report_path = archive_root / "IMPORT_REPORT.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    report_path.chmod(0o600)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
