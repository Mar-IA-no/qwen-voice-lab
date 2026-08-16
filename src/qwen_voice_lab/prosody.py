from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .config import Settings
from .models import Language, ProsodyFunction, ProsodyProfileView, ScoreSegment, Voice


class ProsodyUnavailableError(ValueError):
    pass


class ProsodyReferenceDefinition(BaseModel):
    reference_file: str
    reference_text: str = Field(min_length=1, max_length=4000)
    reference_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: str = Field(min_length=1, max_length=160)


class ProsodyProfileDefinition(BaseModel):
    schema_version: Literal["qwen-voice-lab-prosody-profile-v1"]
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    voice_ids: list[str] = Field(default_factory=list, max_length=64)
    identity_tags: list[str] = Field(default_factory=list, max_length=12)
    status: Literal["experimental", "canonical"]
    languages: list[Language] = Field(min_length=1)
    references: dict[ProsodyFunction, ProsodyReferenceDefinition]
    notes: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def exactly_four_functions(self) -> ProsodyProfileDefinition:
        if not self.voice_ids and not self.identity_tags:
            raise ValueError("prosody profile must target voice IDs or identity tags")
        expected = {
            ProsodyFunction.T,
            ProsodyFunction.S,
            ProsodyFunction.D,
            ProsodyFunction.R,
        }
        if set(self.references) != expected:
            raise ValueError("prosody profile must define exactly T, S, D and R")
        return self


class ResolvedProsodyReference(BaseModel):
    reference_file: str
    reference_text: str
    reference_sha256: str
    provenance: str


class ResolvedProsodyProfile(BaseModel):
    definition: ProsodyProfileDefinition
    references: dict[ProsodyFunction, ResolvedProsodyReference]


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ProsodyRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profiles = self._load_profiles()

    def _load_profiles(self) -> list[ResolvedProsodyProfile]:
        profiles: list[ResolvedProsodyProfile] = []
        data_root = self.settings.data_dir.resolve()
        for path in sorted(self.settings.prosody_profiles_dir.glob("*.json")):
            definition = ProsodyProfileDefinition.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
            references: dict[ProsodyFunction, ResolvedProsodyReference] = {}
            for function, reference in definition.references.items():
                audio = (data_root / reference.reference_file).resolve()
                if data_root not in audio.parents or not audio.is_file():
                    raise ValueError(
                        f"prosody profile {definition.id} has unavailable {function} audio"
                    )
                if sha256_path(audio) != reference.reference_sha256:
                    raise ValueError(
                        f"prosody profile {definition.id} has invalid {function} SHA-256"
                    )
                references[function] = ResolvedProsodyReference(
                    reference_file=str(audio),
                    reference_text=reference.reference_text,
                    reference_sha256=reference.reference_sha256,
                    provenance=reference.provenance,
                )
            profiles.append(ResolvedProsodyProfile(definition=definition, references=references))
        return profiles

    def profile_for(self, voice: Voice) -> ResolvedProsodyProfile | None:
        tags = set(voice.tags)
        matches = [
            profile
            for profile in self.profiles
            if (
                voice.id in profile.definition.voice_ids
                if profile.definition.voice_ids
                else set(profile.definition.identity_tags).issubset(tags)
            )
        ]
        if len(matches) > 1:
            ids = ", ".join(profile.definition.id for profile in matches)
            raise ValueError(f"voice {voice.id} matches multiple prosody profiles: {ids}")
        return matches[0] if matches else None

    def view_for(self, voice: Voice) -> ProsodyProfileView | None:
        profile = self.profile_for(voice)
        if not profile:
            return None
        return ProsodyProfileView(
            id=profile.definition.id,
            status=profile.definition.status,
            functions=[ProsodyFunction.T, ProsodyFunction.S, ProsodyFunction.D, ProsodyFunction.R],
            notes=profile.definition.notes,
        )

    def validate_score(self, voice: Voice, segments: list[ScoreSegment]) -> None:
        requested = {segment.prosody for segment in segments} - {ProsodyFunction.NEUTRAL}
        if requested and not self.profile_for(voice):
            functions = ", ".join(sorted(function.value for function in requested))
            raise ProsodyUnavailableError(
                f"Voice '{voice.name}' has no active prosody profile for {functions}. "
                "Generate and validate its T/S/D/R variants first."
            )

    def voice_for(self, voice: Voice, function: ProsodyFunction) -> Voice:
        if function == ProsodyFunction.NEUTRAL:
            return voice
        profile = self.profile_for(voice)
        if not profile:
            raise ProsodyUnavailableError(
                f"Voice '{voice.name}' has no active prosody profile for {function.value}."
            )
        reference = profile.references[function]
        return voice.model_copy(
            update={
                "reference_file": reference.reference_file,
                "reference_text": reference.reference_text,
                "reference_sha256": reference.reference_sha256,
            }
        )
