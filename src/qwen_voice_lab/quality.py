from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import Settings
from .editorial import normalize_spoken_text
from .models import Language, QualityReport

LANGUAGE_NAMES = {
    Language.ES: "Spanish",
    Language.EN: "English",
    Language.PT: "Portuguese",
    Language.FR: "French",
    Language.IT: "Italian",
    Language.DE: "German",
}
MOCK_IDENTITY_VALIDATOR = "mock-acoustic-window-v1"
MOCK_IDENTITY_MODEL_SHA256 = hashlib.sha256(MOCK_IDENTITY_VALIDATOR.encode()).hexdigest()


@dataclass(frozen=True)
class ValidationResult:
    content: QualityReport
    identity: QualityReport | None = None


def _distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for index, expected in enumerate(reference, start=1):
        current = [index]
        for offset, actual in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[offset] + 1,
                    previous[offset - 1] + (expected != actual),
                )
            )
        previous = current
    return previous[-1]


def content_metrics(expected: str, transcript: str) -> dict[str, float]:
    expected_normalized = normalize_spoken_text(expected)
    actual_normalized = normalize_spoken_text(transcript)
    expected_words = expected_normalized.split()
    actual_words = actual_normalized.split()
    wer = _distance(expected_words, actual_words) / max(1, len(expected_words))
    cer = _distance(list(expected_normalized), list(actual_normalized)) / max(
        1, len(expected_normalized)
    )
    actual_set = set(actual_words)
    coverage = sum(word in actual_set for word in expected_words) / max(1, len(expected_words))
    edge = min(3, len(expected_words))
    expected_prefix = expected_words[:edge]
    expected_suffix = expected_words[-edge:]
    actual_prefix = actual_words[:edge]
    actual_suffix = actual_words[-edge:]
    prefix = 1 - _distance(expected_prefix, actual_prefix) / max(1, edge)
    suffix = 1 - _distance(expected_suffix, actual_suffix) / max(1, edge)
    return {
        "wer": wer,
        "cer": cer,
        "token_coverage": coverage,
        "prefix_coverage": max(0.0, prefix),
        "suffix_coverage": max(0.0, suffix),
    }


def _spectral_embedding(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    if not len(mono):
        return np.zeros(32, dtype=np.float32)
    target = 16_000
    if sample_rate != target:
        positions = np.linspace(0, len(mono) - 1, round(len(mono) * target / sample_rate))
        mono = np.interp(positions, np.arange(len(mono)), mono)
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono)), n=4096))[1:2049]
    bands = np.array_split(np.log1p(spectrum), 32)
    embedding = np.asarray([float(np.mean(row)) for row in bands], dtype=np.float32)
    norm = float(np.linalg.norm(embedding))
    return embedding / norm if norm else embedding


def identity_scores(reference_file: Path, take_file: Path) -> list[float]:
    reference, reference_rate = sf.read(reference_file, dtype="float32", always_2d=False)
    audio, sample_rate = sf.read(take_file, dtype="float32", always_2d=False)
    reference_embedding = _spectral_embedding(np.asarray(reference), reference_rate)
    mono = audio.mean(axis=1) if np.asarray(audio).ndim > 1 else np.asarray(audio)
    window = max(1, round(sample_rate * 2.0))
    hop = max(1, window // 2)
    chunks = [mono[start : start + window] for start in range(0, max(1, len(mono)), hop)]
    chunks = [chunk for chunk in chunks if len(chunk) >= min(window, sample_rate // 2)] or [mono]
    return [
        float(np.dot(reference_embedding, _spectral_embedding(chunk, sample_rate)))
        for chunk in chunks
    ]


class ContentValidator:
    """Serial JSON-lines client for the separately admitted local Qwen ASR worker."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._invoke_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._closed = False

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            process = self._process
        if not process or process.poll() is not None:
            return
        self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    def validate(
        self, audio: Path, expected: str, language: Language, *, mock: bool = False
    ) -> QualityReport:
        return self.validate_batch([(audio, expected, language, None)], mock=mock)[0].content

    def validate_batch(
        self,
        items: list[tuple[Path, str, Language, Path | None]],
        *,
        mock: bool = False,
    ) -> list[ValidationResult]:
        if mock:
            payloads = [
                {
                    "validator": "mock-content-oracle-v1",
                    "transcript": expected,
                    "alignment": [],
                    "identity_validator": MOCK_IDENTITY_VALIDATOR,
                    "identity_model_sha256": MOCK_IDENTITY_MODEL_SHA256,
                    "identity_scores": (
                        identity_scores(reference, audio) if reference is not None else []
                    ),
                }
                for audio, expected, _, reference in items
            ]
        elif not self.settings.validator_enabled:
            return [
                ValidationResult(
                    content=QualityReport(
                        id=f"qc_{uuid.uuid4().hex[:16]}",
                        take_id="pending",
                        validator="qwen3-asr-0.6b+forced-aligner-0.6b",
                        verdict="unavailable",
                        reasons=["local validator is disabled"],
                    ),
                    identity=(
                        QualityReport(
                            id=f"qc_{uuid.uuid4().hex[:16]}",
                            take_id="pending",
                            validator="ecapa-speaker-window-v1",
                            verdict="unavailable",
                            reasons=["local validator is disabled"],
                        )
                        if reference is not None
                        else None
                    ),
                )
                for _, _, _, reference in items
            ]
        else:
            payloads = self._invoke_batch(items)
        return [
            self._report(expected, payload, reference is not None)
            for (_, expected, _, reference), payload in zip(items, payloads, strict=True)
        ]

    @staticmethod
    def _report(expected: str, payload: dict, identity_expected: bool) -> ValidationResult:
        transcript = str(payload.get("transcript", ""))
        metrics = content_metrics(expected, transcript)
        reasons = []
        if metrics["wer"] > 0.12:
            reasons.append(f"WER {metrics['wer']:.3f} exceeds 0.120")
        if metrics["token_coverage"] < 0.9:
            reasons.append("token coverage is below 0.900")
        if metrics["prefix_coverage"] < 0.8:
            reasons.append("opening words are missing")
        if metrics["suffix_coverage"] < 0.8:
            reasons.append("ending words are missing")
        alignment = list(payload.get("alignment", []))
        if alignment:
            aligned_text = " ".join(str(row.get("text", "")) for row in alignment)
            aligned_suffix = content_metrics(expected, aligned_text)["suffix_coverage"]
            if aligned_suffix < 0.8:
                reasons.append("forced-alignment endpoint does not contain the expected ending")
        content = QualityReport(
            id=f"qc_{uuid.uuid4().hex[:16]}",
            take_id="pending",
            validator=str(payload.get("validator", "qwen3-asr-0.6b+forced-aligner-0.6b")),
            verdict="retry" if reasons else "pass",
            transcript=transcript,
            normalized_transcript=normalize_spoken_text(transcript),
            alignment=alignment,
            reasons=reasons,
            **metrics,
        )

        scores = [float(value) for value in payload.get("identity_scores", [])]
        identity = None
        if identity_expected:
            identity = QualityReport(
                id=f"qc_{uuid.uuid4().hex[:16]}",
                take_id="pending",
                validator=str(payload.get("identity_validator", "ecapa-speaker-window-v1")),
                validator_model_sha256=str(payload.get("identity_model_sha256", "")) or None,
                verdict="pass" if scores else "unavailable",
                identity_median=float(np.median(scores)) if scores else None,
                identity_min=min(scores) if scores else None,
                identity_windows=scores,
                reasons=["no matching calibration; speaker score is advisory"],
            )
        return ValidationResult(content=content, identity=identity)

    def _invoke_batch(self, items: list[tuple[Path, str, Language, Path | None]]) -> list[dict]:
        command = shlex.split(self.settings.validator_command)
        if not command:
            raise RuntimeError("validator command is not configured")
        request = json.dumps(
            {
                "asr_model": self.settings.qwen_asr_model,
                "aligner_model": self.settings.qwen_aligner_model,
                "speaker_model": self.settings.validator_speaker_model,
                "speaker_model_sha256": self.settings.validator_speaker_model_sha256,
                "device": self.settings.validator_device,
                "items": [
                    {
                        "audio": str(audio.resolve()),
                        "text": expected,
                        "language": LANGUAGE_NAMES[language],
                        "reference": str(reference.resolve()) if reference else None,
                    }
                    for audio, expected, language, reference in items
                ],
            }
        )
        with self._invoke_lock:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("validator is closed")
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            with self._state_lock:
                if self._closed:
                    self._terminate(process)
                    raise RuntimeError("validator is closed")
                self._process = process
            try:
                try:
                    stdout, stderr = process.communicate(
                        request + "\n", timeout=self.settings.validator_timeout_seconds
                    )
                except subprocess.TimeoutExpired as exc:
                    self._terminate(process)
                    raise RuntimeError("validator worker timed out and was terminated") from exc
            finally:
                with self._state_lock:
                    if self._process is process:
                        self._process = None
        if process.returncode:
            detail = stderr.strip() or stdout.strip()
            raise RuntimeError(f"validator worker failed ({process.returncode}): {detail}")
        lines = [line for line in stdout.splitlines() if line.startswith("QVL_ASR ")]
        if not lines:
            raise RuntimeError("validator worker returned no QVL_ASR result")
        payload = json.loads(lines[-1].removeprefix("QVL_ASR "))
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(items):
            raise RuntimeError("validator worker returned an invalid result batch")
        return results
