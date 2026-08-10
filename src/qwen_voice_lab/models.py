from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Language(StrEnum):
    ES = "es"
    EN = "en"


class ProsodyFunction(StrEnum):
    T = "T"
    S = "S"
    D = "D"
    R = "R"
    NEUTRAL = "neutral"


class VoiceKind(StrEnum):
    CLONE = "clone"
    DESIGNED = "designed"


class ProsodyProfileView(BaseModel):
    id: str
    status: Literal["experimental", "canonical"]
    functions: list[ProsodyFunction]
    notes: list[str] = Field(default_factory=list)


class JobKind(StrEnum):
    SYNTHESIS = "synthesis"
    DESIGN = "design"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Voice(BaseModel):
    id: str
    name: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(max_length=500)] = ""
    kind: VoiceKind
    language_hint: Literal["es", "en", "multilingual"] = "multilingual"
    reference_text: Annotated[str, Field(max_length=4000)] = ""
    reference_file: str
    reference_sha256: str
    duration_seconds: float | None = None
    tags: list[str] = Field(default_factory=list, max_length=12)
    created_at: str = Field(default_factory=utc_now)
    design_instruction: str | None = None


class VoiceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    kind: VoiceKind
    language_hint: Literal["es", "en", "multilingual"]
    reference_text: str
    reference_sha256: str
    duration_seconds: float | None = None
    tags: list[str]
    created_at: str
    design_instruction: str | None = None
    prosody_profile: ProsodyProfileView | None = None


class ScoreSegment(BaseModel):
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")]
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    pause_after_ms: int = Field(default=0, ge=0, le=60_000)
    prosody: ProsodyFunction = ProsodyFunction.NEUTRAL


class SynthesisRequest(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=120)] = "Untitled render"
    voice_id: str
    language: Language
    segments: Annotated[list[ScoreSegment], Field(min_length=1, max_length=64)]
    seed: int = Field(default=20260805, ge=0, le=2_147_483_647)
    comparison_id: str | None = None

    @field_validator("segments")
    @classmethod
    def unique_segment_ids(cls, value: list[ScoreSegment]) -> list[ScoreSegment]:
        ids = [row.id for row in value]
        if len(ids) != len(set(ids)):
            raise ValueError("segment IDs must be unique")
        return value


class DesignRequest(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(max_length=500)] = ""
    instruction: Annotated[str, Field(min_length=12, max_length=2000)]
    sample_text: Annotated[str, Field(min_length=12, max_length=4000)]
    language: Language
    seed: int = Field(default=20260805, ge=0, le=2_147_483_647)


class ComparisonRequest(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=120)] = "Voice comparison"
    voice_ids: Annotated[list[str], Field(min_length=2, max_length=5)]
    language: Language
    text: Annotated[str, Field(min_length=1, max_length=12_000)]
    seed: int = Field(default=20260805, ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def unique_voices(self) -> ComparisonRequest:
        if len(self.voice_ids) != len(set(self.voice_ids)):
            raise ValueError("comparison voices must be unique")
        return self


class AuthRequest(BaseModel):
    token: Annotated[str, Field(min_length=1, max_length=512)]


class JobMetrics(BaseModel):
    model: str
    device: str
    load_ms: float = 0
    generation_ms: float = 0
    first_audio_ms: float = 0
    duration_seconds: float = 0
    rtf: float = 0
    peak_vram_mib: float | None = None
    output_sha256: str = ""
    output_bytes: int = 0


class Job(BaseModel):
    id: str
    kind: JobKind
    status: JobStatus = JobStatus.QUEUED
    title: str
    progress: float = 0
    created_at: str = Field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    request: dict
    output_file: str | None = None
    result_voice_id: str | None = None
    metrics: JobMetrics | None = None
    error: str | None = None


class JobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: JobKind
    status: JobStatus
    title: str
    progress: float
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    request: dict
    result_voice_id: str | None = None
    metrics: JobMetrics | None = None
    error: str | None = None


class Comparison(BaseModel):
    id: str
    title: str
    voice_ids: list[str]
    job_ids: list[str]
    language: Language
    text: str
    seed: int
    created_at: str = Field(default_factory=utc_now)


class ComparisonDetail(Comparison):
    jobs: list[JobView | None]


class Capabilities(BaseModel):
    engine: str
    engine_ready: bool
    engine_reason: str | None = None
    base_model: str
    design_model: str
    languages: list[str] = ["es", "en"]
    max_upload_mib: int
    max_text_chars: int
    max_segments: int
    max_comparison_voices: int
    voice_design: bool = True
    voice_cloning: bool = True
    paid_providers: list[str] = []
    gpu_wrapper_required: bool
    gpu_wrapper_verified: bool
    gpu_execution_mode: Literal["in-process", "wrapped-worker"] = "in-process"
    gpu_worker_state: str = "not-applicable"
    gpu_worker_reason: str | None = None


class ArchiveAsset(BaseModel):
    id: str
    name: str
    relative_path: str
    collection: str
    kind: Literal["source", "reference", "segment", "locution", "experiment", "audio"]
    format: str
    size_bytes: int
    canonical: bool = False
