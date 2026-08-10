from __future__ import annotations

import hashlib
from pathlib import Path

from .models import ArchiveAsset

AUDIO_SUFFIXES = {".flac", ".m4a", ".mp3", ".ogg", ".wav", ".webm"}


def _kind(relative: Path) -> str:
    parts = set(relative.parts)
    if relative.parent == Path("."):
        return "source"
    if "candidates" in parts or "references" in parts:
        return "reference"
    if "segments" in parts:
        return "segment"
    if "locutions" in parts:
        return "locution"
    if "runs" in parts:
        return "experiment"
    return "audio"


def _collection(relative: Path) -> str:
    parts = relative.parts
    for marker in ("locutions", "runs"):
        if marker in parts:
            index = parts.index(marker)
            if len(parts) > index + 1:
                return parts[index + 1]
    return relative.parent.as_posix()


def list_archive_assets(root: Path) -> list[ArchiveAsset]:
    root = root.resolve()
    if not root.is_dir():
        return []
    assets: list[ArchiveAsset] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        relative = path.relative_to(root)
        relative_text = relative.as_posix()
        asset_id = hashlib.sha256(relative_text.encode()).hexdigest()[:20]
        lowered = relative_text.lower()
        assets.append(
            ArchiveAsset(
                id=asset_id,
                name=path.stem,
                relative_path=relative_text,
                collection=_collection(relative),
                kind=_kind(relative),
                format=path.suffix.lower().lstrip("."),
                size_bytes=path.stat().st_size,
                canonical=("canonical" in lowered or "winner" in lowered),
            )
        )
    return assets


def resolve_archive_asset(root: Path, asset_id: str) -> Path | None:
    root = root.resolve()
    for asset in list_archive_assets(root):
        if asset.id != asset_id:
            continue
        path = (root / asset.relative_path).resolve()
        if root in path.parents and path.is_file():
            return path
    return None
