from __future__ import annotations

from pathlib import Path

import pytest

from qwen_voice_lab.config import Settings
from qwen_voice_lab.engine import QwenEngine


@pytest.mark.parametrize(
    ("code", "qwen_name"),
    [
        ("es", "Spanish"),
        ("en", "English"),
        ("pt", "Portuguese"),
        ("fr", "French"),
        ("it", "Italian"),
        ("de", "German"),
    ],
)
def test_language_codes_map_to_qwen_native_names(code: str, qwen_name: str) -> None:
    assert QwenEngine._language(code) == qwen_name


def test_dedicated_gpu_mode_does_not_require_a_wrapper(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", host="127.0.0.1")

    assert settings.require_gpu_wrapper is False
    assert settings.gpu_wrapper == ""


def test_qwen_mode_requires_gpu_wrapper_marker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("QVL_GPU_WRAPPED", raising=False)
    settings = Settings(
        engine="qwen",
        data_dir=tmp_path,
        host="127.0.0.1",
        access_token="",
        require_gpu_wrapper=True,
        gpu_cgroup_pattern=r"(gpu-priority-job-[0-9]+-voice-lab[.]service)",
    )
    try:
        QwenEngine.assert_wrapped(settings)
    except RuntimeError as exc:
        assert "configured priority cgroup" in str(exc)
    else:
        raise AssertionError("unwrapped GPU mode was accepted")


def test_wrapper_marker_without_priority_cgroup_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QVL_GPU_WRAPPED", "1")
    monkeypatch.setattr(QwenEngine, "wrapper_unit", staticmethod(lambda _: None))
    settings = Settings(
        engine="qwen",
        data_dir=tmp_path,
        host="127.0.0.1",
        access_token="",
        require_gpu_wrapper=True,
        gpu_cgroup_pattern=r"(gpu-priority-job-[0-9]+-voice-lab[.]service)",
    )
    with pytest.raises(RuntimeError, match="environment marker alone"):
        QwenEngine.assert_wrapped(settings)


def test_priority_cgroup_and_marker_are_accepted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("QVL_GPU_WRAPPED", "1")
    monkeypatch.setattr(
        QwenEngine,
        "wrapper_unit",
        staticmethod(lambda _: "gpu-priority-job-123-voice-lab.service"),
    )
    settings = Settings(
        engine="qwen",
        data_dir=tmp_path,
        host="127.0.0.1",
        access_token="",
        require_gpu_wrapper=True,
        gpu_cgroup_pattern=r"(gpu-priority-job-[0-9]+-voice-lab[.]service)",
    )
    QwenEngine.assert_wrapped(settings)


def test_repository_has_no_paid_voice_provider() -> None:
    root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore").lower()
        for path in (root / "src").rglob("*.py")
    )
    forbidden = ("elevenlabs", "play.ht", "cartesia", "openai.audio", "azure speech")
    assert not any(provider in source for provider in forbidden)
