from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QVL_", env_file=PROJECT_ROOT / ".env", extra="ignore"
    )

    engine: Literal["mock", "qwen"] = "mock"
    data_dir: Path = PROJECT_ROOT / "data"
    host: str = "127.0.0.1"
    port: int = Field(default=8788, ge=1024, le=65535)

    qwen_base_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    qwen_design_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    qwen_base_model_label: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    qwen_design_model_label: str = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    qwen_asr_model: str = "Qwen/Qwen3-ASR-0.6B"
    qwen_aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    device: str = "cuda:0"
    require_gpu_wrapper: bool = False
    gpu_wrapper: str = ""
    gpu_jobs_dir: Path | None = None
    gpu_job_name: str = "qwen-voice-lab"
    gpu_cgroup_pattern: str = ""
    gpu_unit_prefix: str = ""
    model_idle_seconds: int = Field(default=900, ge=30)
    gpu_worker_command: str = ""
    gpu_worker_start_timeout_seconds: int = Field(default=90, ge=1, le=300)
    gpu_retry_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    gpu_preempt_exit_code: int = Field(default=75, ge=1, le=255)
    gpu_retryable_exit_codes: str = "1,75"
    gpu_stop_command: str = ""
    gpu_stop_all_command: str = ""
    validator_command: str = ""
    validator_enabled: bool = False
    validator_speaker_model: str = ""
    validator_timeout_seconds: int = Field(default=180, ge=10, le=1800)
    project_max_attempts: int = Field(default=3, ge=1, le=10)
    trim_threshold_db: float = Field(default=-48, ge=-80, le=-10)
    trim_padding_ms: int = Field(default=80, ge=0, le=1000)

    access_token: str = ""
    allow_unauthenticated_remote: bool = False
    cookie_secure: bool = False

    max_upload_mib: int = Field(default=50, ge=1, le=500)
    max_text_chars: int = Field(default=12_000, ge=100, le=100_000)
    max_segments: int = Field(default=64, ge=1, le=256)
    max_comparison_voices: int = Field(default=5, ge=2, le=12)

    @model_validator(mode="after")
    def resolve_local_paths(self) -> Settings:
        if not self.data_dir.is_absolute():
            self.data_dir = (PROJECT_ROOT / self.data_dir).resolve()
        if self.gpu_jobs_dir is not None and not self.gpu_jobs_dir.is_absolute():
            self.gpu_jobs_dir = (PROJECT_ROOT / self.gpu_jobs_dir).resolve()
        if self.access_token and len(self.access_token) < 32:
            raise ValueError("QVL_ACCESS_TOKEN must contain at least 32 characters")
        try:
            loopback = ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host == "localhost"
        if not loopback and not self.access_token and not self.allow_unauthenticated_remote:
            raise ValueError(
                "Remote binding requires QVL_ACCESS_TOKEN or an explicit "
                "QVL_ALLOW_UNAUTHENTICATED_REMOTE=true override"
            )
        try:
            retryable = {int(value.strip()) for value in self.gpu_retryable_exit_codes.split(",")}
        except ValueError as exc:
            raise ValueError(
                "QVL_GPU_RETRYABLE_EXIT_CODES must be comma-separated integers"
            ) from exc
        if not retryable or any(code < 1 or code > 255 for code in retryable):
            raise ValueError("QVL_GPU_RETRYABLE_EXIT_CODES must contain codes from 1 to 255")
        if self.validator_enabled and not self.validator_command.strip():
            raise ValueError("QVL_VALIDATOR_COMMAND is required when QVL_VALIDATOR_ENABLED=true")
        if self.validator_enabled and not self.validator_speaker_model.strip():
            raise ValueError("QVL_VALIDATOR_SPEAKER_MODEL is required when validation is enabled")
        return self

    @property
    def retryable_exit_codes(self) -> set[int]:
        return {int(value.strip()) for value in self.gpu_retryable_exit_codes.split(",")}

    @property
    def database_path(self) -> Path:
        return self.data_dir / "qwen_voice_lab.sqlite3"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def prosody_profiles_dir(self) -> Path:
        return self.data_dir / "prosody_profiles"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def prepare(self) -> None:
        for path in (
            self.data_dir,
            self.voices_dir,
            self.renders_dir,
            self.archive_dir,
            self.projects_dir,
            self.prosody_profiles_dir,
            self.temp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare()
    return settings
