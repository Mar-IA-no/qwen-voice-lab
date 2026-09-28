import hashlib
import io
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from qwen_voice_lab.audio_pipeline import build_timeline, prepare_speech


def tone(rate=24000, seconds=3, stereo=False):
    signal = (0.1 * np.sin(2 * np.pi * 440 * np.arange(rate * seconds) / rate)).astype("float32")
    if stereo:
        signal = np.column_stack((signal, signal))
    stream = io.BytesIO()
    sf.write(stream, signal, rate, format="WAV", subtype="PCM_16")
    return stream.getvalue()


def fixture_audio(tmp_path, *, baseline=1, speed=None, pause=1200):
    audio = tmp_path / "project" / "take.wav"
    audio.parent.mkdir()
    asset = tone()
    audio.write_bytes(asset)
    take = SimpleNamespace(id="take", project_id="project", segment_id="block",
        text_sha256="text", trimmed_file=str(audio), raw_file=str(audio),
        trimmed_sha256=hashlib.sha256(asset).hexdigest(),
        raw_sha256=hashlib.sha256(asset).hexdigest(), baseline_speed=baseline, provenance={})
    segment = SimpleNamespace(id="block", position=0, text="example", text_sha256="text",
        selected_take_id="take", speed=speed, pause_after_ms=pause)
    return segment, take, asset


def test_tempo_preserves_pitch_and_uses_cache(tmp_path):
    asset = tone()
    path = prepare_speech(asset, 0.8, tmp_path / "cache")
    signal, rate = sf.read(path)
    assert rate == 24000
    assert abs(len(signal) / rate - 3 / 0.8) < 0.08
    peak = np.fft.rfftfreq(len(signal), 1 / rate)[np.argmax(abs(np.fft.rfft(signal)))]
    assert abs(peak - 440) < 2
    stamp = path.stat().st_mtime_ns
    assert prepare_speech(asset, 0.8, tmp_path / "cache") == path
    assert path.stat().st_mtime_ns == stamp


def test_override_preserves_exact_pauses_and_original(tmp_path):
    segment, take, original = fixture_audio(tmp_path, speed=1, pause=61001)
    output = tmp_path / "project" / "assembly.wav"
    manifest = build_timeline(output, tmp_path / "project" / "manifest.json", [segment],
        {"take": take}, project_id="project", revision_id="v1", projects_root=tmp_path,
        lead_pause_ms=2345, speech_speed=0.5)
    signal, rate = sf.read(output)
    row = manifest["timeline"][0]
    assert row["speech_start_sample"] == round(2.345 * rate)
    assert row["speech_end_sample"] - row["speech_start_sample"] == 3 * rate
    assert row["pause_samples"] == round(61.001 * rate)
    assert np.all(signal[:row["speech_start_sample"]] == 0)
    assert np.all(signal[row["speech_end_sample"]:] == 0)
    assert output.stat().st_size > 0
    assert original == open(take.raw_file, "rb").read()


def test_baseline_and_editorial_factor_applied_once(tmp_path):
    segment, take, _ = fixture_audio(tmp_path, baseline=0.98)
    manifest = build_timeline(tmp_path / "project" / "out.wav", tmp_path / "project" / "m.json",
        [segment], {"take": take}, project_id="project", revision_id="v1",
        projects_root=tmp_path, speech_speed=0.8)
    assert manifest["timeline"][0]["effective_speed"] == pytest.approx(0.784)
    assert manifest["timeline"][0]["speech_end_sample"] / 24000 == pytest.approx(3 / .784, abs=.08)


def test_canonical_conversion_and_corrupt_cache(tmp_path):
    asset = tone(rate=16000, stereo=True)
    path = prepare_speech(asset, 1, tmp_path)
    assert sf.info(path).samplerate == 24000
    assert sf.info(path).channels == 1
    path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="hash mismatch"):
        prepare_speech(asset, 1, tmp_path)


def test_tampered_source_is_rejected_before_transform(tmp_path):
    segment, take, _ = fixture_audio(tmp_path)
    with open(take.trimmed_file, "wb") as stream:
        stream.write(tone(seconds=1))
    with pytest.raises(ValueError, match="SHA-256"):
        build_timeline(tmp_path / "out.wav", tmp_path / "m.json", [segment], {"take": take},
            project_id="project", revision_id="v1", projects_root=tmp_path)


def test_insufficient_disk_does_not_publish_partial_assembly(tmp_path, monkeypatch):
    segment, take, _ = fixture_audio(tmp_path)
    output = tmp_path / "out.wav"
    monkeypatch.setattr("qwen_voice_lab.audio_pipeline.shutil.disk_usage",
                        lambda _: SimpleNamespace(free=1))
    with pytest.raises(ValueError, match="disk space"):
        build_timeline(output, tmp_path / "m.json", [segment], {"take": take},
            project_id="project", revision_id="v1", projects_root=tmp_path)
    assert not output.exists()
    assert not (tmp_path / "m.json").exists()
