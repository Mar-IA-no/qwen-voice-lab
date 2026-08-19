from __future__ import annotations

import gc
import hashlib
import math
import os
import re
import time
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import Settings
from .models import DesignRequest, JobMetrics, SynthesisRequest, Voice
from .prosody import ProsodyRegistry

ProgressCallback = Callable[[float], None]
CancelCallback = Callable[[], bool]


class RenderCancelled(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sf.write(path, np.asarray(audio, dtype=np.float32), sample_rate, subtype="PCM_16")
    path.chmod(0o600)


def audio_info(path: Path) -> tuple[float, int]:
    info = sf.info(path)
    return float(info.duration), int(info.samplerate)


class MockEngine:
    model_name = "mock-local-signal"
    device = "cpu"

    def __init__(self, settings: Settings, prosody: ProsodyRegistry):
        self.settings = settings
        self.prosody = prosody

    @staticmethod
    def _signal(text: str, identity: str, seed: int) -> tuple[np.ndarray, int]:
        sample_rate = 24_000
        words = max(1, len(text.split()))
        duration = min(24.0, max(0.8, words * 0.19))
        count = round(duration * sample_rate)
        digest = hashlib.sha256(f"{identity}:{seed}".encode()).digest()
        base = 135 + digest[0] * 0.55
        t = np.arange(count, dtype=np.float32) / sample_rate
        envelope = np.minimum(1.0, t * 8) * np.minimum(1.0, (duration - t) * 8)
        pulse = 0.72 + 0.28 * np.sin(2 * math.pi * (2.3 + digest[1] / 255) * t)
        audio = (
            0.15 * np.sin(2 * math.pi * base * t)
            + 0.045 * np.sin(2 * math.pi * base * 2.01 * t)
            + 0.018 * np.sin(2 * math.pi * base * 3.98 * t)
        )
        return (audio * envelope * pulse).astype(np.float32), sample_rate

    def render_synthesis(
        self,
        request: SynthesisRequest,
        voice: Voice,
        output: Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> JobMetrics:
        started = time.monotonic()
        chunks: list[np.ndarray] = []
        sample_rate = 24_000
        speech_samples = 0
        for index, segment in enumerate(request.segments):
            if cancelled():
                raise RenderCancelled("render cancelled")
            render_voice = self.prosody.voice_for(voice, segment.prosody)
            audio, sample_rate = self._signal(
                segment.text, render_voice.reference_sha256, request.seed + index
            )
            chunks.append(audio)
            speech_samples += len(audio)
            pause_samples = round(segment.pause_after_ms * sample_rate / 1000)
            if pause_samples:
                chunks.append(np.zeros(pause_samples, dtype=np.float32))
            progress((index + 1) / len(request.segments))
        combined = np.concatenate(chunks)
        write_wav(output, combined, sample_rate)
        elapsed = time.monotonic() - started
        duration = len(combined) / sample_rate
        return JobMetrics(
            model=self.model_name,
            device=self.device,
            generation_ms=elapsed * 1000,
            first_audio_ms=elapsed * 1000 / max(1, len(request.segments)),
            duration_seconds=duration,
            rtf=elapsed / max(duration, 0.001),
            output_sha256=sha256_file(output),
            output_bytes=output.stat().st_size,
        )

    def render_design(
        self,
        request: DesignRequest,
        output: Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> JobMetrics:
        if cancelled():
            raise RenderCancelled("design cancelled")
        started = time.monotonic()
        audio, sample_rate = self._signal(
            request.sample_text, request.instruction, request.seed
        )
        progress(0.7)
        write_wav(output, audio, sample_rate)
        progress(1.0)
        elapsed = time.monotonic() - started
        duration = len(audio) / sample_rate
        return JobMetrics(
            model="mock-voice-design",
            device="cpu",
            generation_ms=elapsed * 1000,
            first_audio_ms=elapsed * 1000,
            duration_seconds=duration,
            rtf=elapsed / max(duration, 0.001),
            output_sha256=sha256_file(output),
            output_bytes=output.stat().st_size,
        )

    def unload(self) -> None:
        return None

    def unload_if_idle(self, _: int) -> None:
        return None


class QwenEngine:
    device = "cuda:0"

    def __init__(self, settings: Settings, prosody: ProsodyRegistry):
        self.settings = settings
        self.prosody = prosody
        self.device = settings.device
        self._model = None
        self._model_kind: str | None = None
        self._model_source = ""
        self._prompt_cache: dict[str, object] = {}
        self._loaded_at = 0.0

    @staticmethod
    def wrapper_unit(pattern: str) -> str | None:
        if not pattern:
            return None
        try:
            cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        except OSError:
            return None
        match = re.search(pattern, cgroup)
        return match.group(1) if match else None

    @classmethod
    def wrapper_verified(cls, settings: Settings) -> bool:
        return (
            os.getenv("QVL_GPU_WRAPPED") == "1"
            and cls.wrapper_unit(settings.gpu_cgroup_pattern) is not None
        )

    @classmethod
    def assert_wrapped(cls, settings: Settings) -> None:
        if settings.require_gpu_wrapper and not settings.gpu_cgroup_pattern:
            raise RuntimeError(
                "QVL_REQUIRE_GPU_WRAPPER is enabled but QVL_GPU_CGROUP_PATTERN is empty"
            )
        if settings.require_gpu_wrapper and not cls.wrapper_verified(settings):
            raise RuntimeError(
                "Qwen GPU mode must run inside the configured priority cgroup; "
                "an environment marker alone is not sufficient"
            )

    @staticmethod
    def _resolve_model(source: str) -> str:
        path = Path(source).expanduser()
        if path.is_dir():
            return str(path.resolve())
        from huggingface_hub import snapshot_download

        return snapshot_download(source, local_files_only=True)

    def _ensure_model(self, kind: str) -> float:
        self.assert_wrapped(self.settings)
        source = (
            self.settings.qwen_base_model
            if kind == "clone"
            else self.settings.qwen_design_model
        )
        if self._model is not None and self._model_kind == kind and self._model_source == source:
            self._loaded_at = time.monotonic()
            return 0.0
        self.unload()
        import torch
        from qwen_tts import Qwen3TTSModel

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable for Qwen mode")
        torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()
        self._model = Qwen3TTSModel.from_pretrained(
            self._resolve_model(source),
            device_map=self.settings.device,
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
        )
        self._model_kind = kind
        self._model_source = source
        self._loaded_at = time.monotonic()
        return (time.monotonic() - started) * 1000

    @staticmethod
    def _language(value: str) -> str:
        return {
            "es": "Spanish",
            "en": "English",
            "pt": "Portuguese",
            "fr": "French",
            "it": "Italian",
            "de": "German",
        }[value]

    def _seed(self, value: int) -> None:
        import torch

        torch.manual_seed(value)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(value)

    def _prompt(self, voice: Voice):
        text_hash = hashlib.sha256(voice.reference_text.encode()).hexdigest()
        key = f"{voice.reference_sha256}:{text_hash}"
        if key not in self._prompt_cache:
            assert self._model is not None
            self._prompt_cache[key] = self._model.create_voice_clone_prompt(
                ref_audio=voice.reference_file,
                ref_text=voice.reference_text or None,
                x_vector_only_mode=not bool(voice.reference_text.strip()),
            )
        return self._prompt_cache[key]

    @staticmethod
    def _peak_vram() -> float | None:
        try:
            import torch

            return torch.cuda.max_memory_allocated() / 1024**2
        except Exception:
            return None

    def render_synthesis(
        self,
        request: SynthesisRequest,
        voice: Voice,
        output: Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> JobMetrics:
        load_ms = self._ensure_model("clone")
        self._seed(request.seed)
        started = time.monotonic()
        first_audio_ms = 0.0
        chunks: list[np.ndarray] = []
        sample_rate = 24_000
        assert self._model is not None
        for index, segment in enumerate(request.segments):
            if cancelled():
                raise RenderCancelled("render cancelled")
            segment_started = time.monotonic()
            render_voice = self.prosody.voice_for(voice, segment.prosody)
            prompt = self._prompt(render_voice)
            generation = (
                request.sampling.generation_kwargs()
                if request.sampling is not None
                else {"max_new_tokens": 2048}
            )
            wavs, sample_rate = self._model.generate_voice_clone(
                text=segment.text,
                language=self._language(request.language),
                voice_clone_prompt=prompt,
                **generation,
            )
            if index == 0:
                first_audio_ms = (time.monotonic() - segment_started + load_ms / 1000) * 1000
            audio = np.asarray(wavs[0], dtype=np.float32)
            chunks.append(audio)
            pause = round(segment.pause_after_ms * sample_rate / 1000)
            if pause:
                chunks.append(np.zeros(pause, dtype=np.float32))
            progress((index + 1) / len(request.segments))
        combined = np.concatenate(chunks)
        write_wav(output, combined, sample_rate)
        self._loaded_at = time.monotonic()
        elapsed = time.monotonic() - started
        duration = len(combined) / sample_rate
        return JobMetrics(
            model=self.settings.qwen_base_model_label,
            device=self.device,
            load_ms=load_ms,
            generation_ms=elapsed * 1000,
            first_audio_ms=first_audio_ms,
            duration_seconds=duration,
            rtf=elapsed / max(duration, 0.001),
            peak_vram_mib=self._peak_vram(),
            output_sha256=sha256_file(output),
            output_bytes=output.stat().st_size,
        )

    def render_design(
        self,
        request: DesignRequest,
        output: Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> JobMetrics:
        load_ms = self._ensure_model("design")
        self._seed(request.seed)
        if cancelled():
            raise RenderCancelled("design cancelled")
        started = time.monotonic()
        assert self._model is not None
        wavs, sample_rate = self._model.generate_voice_design(
            text=request.sample_text,
            language=self._language(request.language),
            instruct=request.instruction,
            max_new_tokens=2048,
        )
        progress(0.8)
        audio = np.asarray(wavs[0], dtype=np.float32)
        write_wav(output, audio, sample_rate)
        self._loaded_at = time.monotonic()
        progress(1.0)
        elapsed = time.monotonic() - started
        duration = len(audio) / sample_rate
        return JobMetrics(
            model=self.settings.qwen_design_model_label,
            device=self.device,
            load_ms=load_ms,
            generation_ms=elapsed * 1000,
            first_audio_ms=(load_ms / 1000 + elapsed) * 1000,
            duration_seconds=duration,
            rtf=elapsed / max(duration, 0.001),
            peak_vram_mib=self._peak_vram(),
            output_sha256=sha256_file(output),
            output_bytes=output.stat().st_size,
        )

    def unload(self) -> None:
        self._prompt_cache.clear()
        self._model = None
        self._model_kind = None
        self._model_source = ""
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def unload_if_idle(self, idle_seconds: int) -> None:
        if self._model is not None and time.monotonic() - self._loaded_at >= idle_seconds:
            self.unload()


def build_engine(
    settings: Settings, prosody: ProsodyRegistry | None = None
) -> MockEngine | QwenEngine | object:
    prosody = prosody or ProsodyRegistry(settings)
    if settings.engine == "qwen":
        if settings.require_gpu_wrapper:
            from .worker_client import WrappedQwenEngine

            return WrappedQwenEngine(settings, prosody)
        return QwenEngine(settings, prosody)
    return MockEngine(settings, prosody)


def write_preview_wav(path: Path) -> None:
    """Create a tiny valid WAV used by packaging smoke tests."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00\x00" * 160)
