from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import Settings
from .engine import audio_info, sha256_file
from .models import Voice, VoiceKind
from .storage import Store

STARTER_ROOT = Path(__file__).resolve().parent / "starter_voices"


def seed_starter_voices(settings: Settings, store: Store) -> list[str]:
    seeded: list[str] = []
    if not STARTER_ROOT.is_dir():
        return seeded
    for manifest_path in sorted(STARTER_ROOT.glob("*/voice.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = manifest_path.parent / payload["reference_file"]
        expected_sha = payload["reference_sha256"]
        if not source.is_file() or sha256_file(source) != expected_sha:
            raise ValueError(
                f"bundled starter voice has invalid audio: {manifest_path.parent.name}"
            )

        existing = next(
            (voice for voice in store.list_voices() if voice.reference_sha256 == expected_sha),
            None,
        )
        if existing:
            existing.name = payload["name"]
            existing.description = payload["description"]
            existing.kind = VoiceKind.DESIGNED
            existing.reference_text = payload["reference_text"]
            existing.language_hint = payload["language_hint"]
            existing.tags = payload["tags"]
            existing.design_instruction = payload.get("design_instruction")
            store.save_voice(existing)
            seeded.append(existing.id)
            continue

        voice_id = payload["id"]
        voice_dir = settings.voices_dir / voice_id
        voice_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        reference = voice_dir / source.name
        shutil.copy2(source, reference)
        reference.chmod(0o600)
        duration, _ = audio_info(reference)
        store.save_voice(
            Voice(
                id=voice_id,
                name=payload["name"],
                description=payload["description"],
                kind=VoiceKind.DESIGNED,
                language_hint=payload["language_hint"],
                reference_text=payload["reference_text"],
                reference_file=str(reference.resolve()),
                reference_sha256=expected_sha,
                duration_seconds=duration,
                tags=payload["tags"],
                design_instruction=payload.get("design_instruction"),
            )
        )
        seeded.append(voice_id)
    return seeded
