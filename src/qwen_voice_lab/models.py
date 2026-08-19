from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Language(StrEnum):
    ES = "es"
    EN = "en"
    PT = "pt"
    FR = "fr"
    IT = "it"
    DE = "de"


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
    languages: list[Language]
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
    language_hint: Literal["es", "en", "pt", "fr", "it", "de", "multilingual"] = "multilingual"
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
    language_hint: Literal["es", "en", "pt", "fr", "it", "de", "multilingual"]
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
    sampling: SamplingSettings | None = None

    @field_validator("segments")
    @classmethod
    def unique_segment_ids(cls, value: list[ScoreSegment]) -> list[ScoreSegment]:
        ids = [row.id for row in value]
        if len(ids) != len(set(ids)):
            raise ValueError("segment IDs must be unique")
        return value


class SamplingSettings(BaseModel):
    """Resolved Qwen generation controls recorded with every long-form take."""

    do_sample: bool = True
    temperature: float = Field(default=0.9, gt=0, le=2)
    top_p: float = Field(default=1.0, gt=0, le=1)
    top_k: int = Field(default=50, ge=1, le=1000)
    repetition_penalty: float = Field(default=1.05, ge=0.5, le=2)
    subtalker_dosample: bool = True
    subtalker_temperature: float = Field(default=0.9, gt=0, le=2)
    subtalker_top_p: float = Field(default=1.0, gt=0, le=1)
    subtalker_top_k: int = Field(default=50, ge=1, le=1000)
    max_new_tokens: int = Field(default=2048, ge=64, le=8192)

    def generation_kwargs(self) -> dict[str, float | int | bool]:
        return {
            "do_sample": self.do_sample,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repetition_penalty": self.repetition_penalty,
            "subtalker_dosample": self.subtalker_dosample,
            "subtalker_temperature": self.subtalker_temperature,
            "subtalker_top_p": self.subtalker_top_p,
            "subtalker_top_k": self.subtalker_top_k,
            "max_new_tokens": self.max_new_tokens,
        }


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
    languages: list[str] = Field(default_factory=lambda: [language.value for language in Language])
    max_upload_mib: int
    max_text_chars: int
    max_segments: int
    max_comparison_voices: int
    voice_design: bool = True
    voice_cloning: bool = True
    paid_providers: list[str] = Field(default_factory=list)
    gpu_wrapper_required: bool
    gpu_wrapper_verified: bool
    gpu_execution_mode: Literal["in-process", "wrapped-worker"] = "in-process"
    gpu_worker_state: str = "not-applicable"
    gpu_worker_reason: str | None = None
    long_form_projects: bool = True
    local_validator_enabled: bool = False
    validator_models: list[str] = Field(default_factory=list)


class ArchiveAsset(BaseModel):
    id: str
    name: str
    relative_path: str
    collection: str
    kind: Literal["source", "reference", "segment", "locution", "experiment", "audio"]
    format: str
    size_bytes: int
    canonical: bool = False


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    GENERATING = "generating"
    NEEDS_REVIEW = "needs_review"
    READY = "ready"


class TakeStatus(StrEnum):
    GENERATED = "generated"
    PASS = "pass"
    RETRY = "retry"
    NEEDS_REVIEW = "needs_review"
    OVERRIDDEN = "overridden"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ProjectCreate(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=120)]
    voice_id: str
    language: Language
    markdown: Annotated[str, Field(min_length=1, max_length=100_000)]
    project_seed: int = Field(default=20260805, ge=0, le=2_147_483_647)
    sampling: SamplingSettings = Field(default_factory=SamplingSettings)


class RevisionCreate(BaseModel):
    markdown: Annotated[str, Field(min_length=1, max_length=100_000)]


class Project(BaseModel):
    id: str
    title: str
    voice_id: str
    language: Language
    project_seed: int
    sampling: SamplingSettings
    status: ProjectStatus = ProjectStatus.DRAFT
    current_revision_id: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class SourceRevision(BaseModel):
    id: str
    project_id: str
    number: int
    markdown: str
    source_sha256: str
    created_at: str = Field(default_factory=utc_now)


class ProjectSegment(BaseModel):
    id: str
    project_id: str
    revision_id: str
    position: int = Field(ge=0)
    text: str
    normalized_text: str
    text_sha256: str
    pause_after_ms: int = Field(default=0, ge=0, le=60_000)
    selected_take_id: str | None = None


class ProjectDetail(Project):
    revision: SourceRevision | None = None
    segments: list[ProjectSegment] = Field(default_factory=list)


class ProjectRun(BaseModel):
    id: str
    project_id: str
    revision_id: str
    status: RunStatus = RunStatus.QUEUED
    progress: float = Field(default=0, ge=0, le=1)
    max_attempts: int = Field(default=3, ge=1, le=10)
    auto_select: bool = True
    segment_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class Take(BaseModel):
    id: str
    project_id: str
    revision_id: str
    segment_id: str
    attempt: int = Field(ge=1)
    seed: int = Field(ge=0, le=2_147_483_647)
    status: TakeStatus = TakeStatus.GENERATED
    raw_file: str
    trimmed_file: str
    raw_sha256: str
    trimmed_sha256: str
    duration_seconds: float
    trim_start_ms: int = Field(default=0, ge=0)
    trim_end_ms: int = Field(default=0, ge=0)
    voice_id: str
    voice_reference_sha256: str
    model: str
    text_sha256: str
    sampling: SamplingSettings
    selected: bool = False
    override_reason: str | None = None
    created_at: str = Field(default_factory=utc_now)


class QualityReport(BaseModel):
    id: str
    take_id: str
    validator: str
    verdict: Literal["pass", "retry", "review", "unavailable"]
    transcript: str = ""
    normalized_transcript: str = ""
    wer: float | None = None
    cer: float | None = None
    token_coverage: float | None = None
    prefix_coverage: float | None = None
    suffix_coverage: float | None = None
    identity_median: float | None = None
    identity_min: float | None = None
    identity_windows: list[float] = Field(default_factory=list)
    calibration_id: str | None = None
    validator_model_sha256: str | None = None
    alignment: list[dict[str, Any]] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)


class IdentityCalibrationCreate(BaseModel):
    language: Language
    validator: Annotated[str, Field(min_length=1, max_length=200)]
    validator_model_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    min_window_score: float = Field(ge=-1, le=1)
    min_median_score: float = Field(ge=-1, le=1)
    notes: Annotated[str, Field(min_length=1, max_length=1000)]

    @field_validator("validator", "notes")
    @classmethod
    def strip_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class IdentityCalibration(IdentityCalibrationCreate):
    id: str
    voice_id: str
    validator: str = "speechbrain-ecapa-voxceleb-window-v1"
    validator_model_sha256: str = "0" * 64
    created_at: str = Field(default_factory=utc_now)


class TakeView(BaseModel):
    id: str
    project_id: str
    revision_id: str
    segment_id: str
    attempt: int
    seed: int
    status: TakeStatus
    raw_sha256: str
    trimmed_sha256: str
    duration_seconds: float
    trim_start_ms: int
    trim_end_ms: int
    voice_id: str
    voice_reference_sha256: str
    model: str
    text_sha256: str
    sampling: SamplingSettings
    selected: bool
    override_reason: str | None = None
    created_at: str


class TakeDetail(TakeView):
    quality_reports: list[QualityReport] = Field(default_factory=list)


class TakeSelection(BaseModel):
    override: bool = False
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_override_reason(self) -> TakeSelection:
        if self.override and not (self.reason or "").strip():
            raise ValueError("an override reason is required")
        return self


class AssemblyKind(StrEnum):
    PREVIEW = "preview"
    FINAL = "final"


class Assembly(BaseModel):
    id: str
    project_id: str
    revision_id: str
    kind: AssemblyKind
    output_file: str
    output_sha256: str
    manifest_file: str
    manifest_sha256: str
    duration_seconds: float
    sample_rate: int
    audit_status: Literal["pending", "pass", "review", "overridden", "unavailable"]
    audit_report_id: str | None = None
    audit: dict[str, Any] = Field(default_factory=dict)
    override_reason: str | None = None
    created_at: str = Field(default_factory=utc_now)


class AssemblyView(BaseModel):
    id: str
    project_id: str
    revision_id: str
    kind: AssemblyKind
    output_sha256: str
    manifest_sha256: str
    duration_seconds: float
    sample_rate: int
    audit_status: Literal["pending", "pass", "review", "overridden", "unavailable"]
    audit_report_id: str | None = None
    audit: dict[str, Any] = Field(default_factory=dict)
    override_reason: str | None = None
    created_at: str


class AssemblyRequest(BaseModel):
    override_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("override_reason")
    @classmethod
    def normalize_override_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("override_reason must not be blank")
        return value
