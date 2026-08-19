from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from qwen_voice_lab.config import Settings
from qwen_voice_lab.models import Language
from qwen_voice_lab.quality import ContentValidator, ValidationItem

MODEL_HASH = "a" * 64


def test_block_gate_rejects_a_small_missing_or_reordered_block_below_global_wer_limit() -> None:
    blocks = [
        "alfa bravo charlie",
        "delta eco foxtrot",
        "golf hotel india",
        "julieta kilo lima",
        "mike noviembre oscar",
        "papa quebec romeo",
        "sierra tango uno",
        "dos tres cuatro",
        "cinco seis siete",
        "ocho nueve diez",
        "rojo azul verde",
        "blanco negro gris",
        "norte sur este",
        "oeste arriba abajo",
        "dentro fuera antes",
        "despues cerca lejos",
        "rapido lento claro",
        "oscuro suave fuerte",
        "largo corto casa",
        "puente camino puerta",
    ]
    expected = " ".join(blocks)

    missing = " ".join(blocks[:4] + blocks[5:])
    missing_report = ContentValidator._report(
        expected,
        {"transcript": missing, "alignment": [], "validator": "fixture"},
        False,
        expected_blocks=blocks,
    ).content
    assert missing_report.wer is not None and missing_report.wer <= 0.12
    assert missing_report.verdict == "retry"
    assert 4 in missing_report.missing_block_indexes

    reordered_blocks = blocks.copy()
    reordered_blocks[4], reordered_blocks[5] = reordered_blocks[5], reordered_blocks[4]
    reordered_report = ContentValidator._report(
        expected,
        {"transcript": " ".join(reordered_blocks), "alignment": [], "validator": "fixture"},
        False,
        expected_blocks=blocks,
    ).content
    assert reordered_report.wer is not None and reordered_report.wer <= 0.12
    assert reordered_report.verdict == "retry"
    assert reordered_report.missing_block_indexes


def test_reference_only_phrase_leakage_is_retryable() -> None:
    expected = "Respirá con calma y prestá atención al sonido presente."
    transcript = expected + " Esta es la frase portadora secreta."
    report = ContentValidator._report(
        expected,
        {"transcript": transcript, "alignment": [], "validator": "fixture"},
        False,
        reference_text="La frase portadora secreta pertenece únicamente a la referencia.",
        expected_blocks=[expected],
    ).content
    assert report.verdict == "retry"
    assert "frase portadora secreta" in report.leaked_reference_phrases
    assert any("reference-only phrase" in reason for reason in report.reasons)


def test_long_repetitive_block_audit_remains_bounded_and_complete() -> None:
    blocks = [("respira con calma ahora " * 10).strip() for _ in range(150)]
    report = ContentValidator._report(
        " ".join(blocks),
        {"transcript": " ".join(blocks), "alignment": [], "validator": "fixture"},
        False,
        expected_blocks=blocks,
    ).content
    assert report.verdict == "pass"
    assert report.block_coverages == [1.0] * len(blocks)


def validator_settings(
    tmp_path: Path, command: str, *, device: str = "cpu", stop_command: str = ""
) -> Settings:
    return Settings(
        engine="mock",
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        validator_enabled=True,
        validator_command=command,
        validator_stop_command=stop_command,
        validator_speaker_model="fixture-speaker",
        validator_speaker_model_sha256=MODEL_HASH,
        validator_device=device,
        validator_timeout_seconds=60,
    )


def test_validator_device_is_explicitly_sent_to_worker(tmp_path: Path) -> None:
    worker = tmp_path / "echo_worker.py"
    worker.write_text(
        """import json, sys
request = json.loads(sys.stdin.readline())
result = {
  'validator': request['device'],
  'transcript': request['items'][0]['text'],
  'alignment': [],
}
print('QVL_ASR ' + json.dumps({'results': [result]}), flush=True)
""",
        encoding="utf-8",
    )
    validator = ContentValidator(
        validator_settings(tmp_path, f"{sys.executable} {worker}", device="cuda:7")
    )
    result = validator.validate_batch(
        [ValidationItem(tmp_path / "audio.wav", "All words end here", Language.EN)]
    )
    assert result[0].content.validator == "cuda:7"
    assert result[0].content.verdict == "pass"


def test_close_terminates_in_flight_validator_process(tmp_path: Path) -> None:
    worker = tmp_path / "sleep_worker.py"
    worker.write_text(
        "import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    stopped = tmp_path / "stopped.txt"
    stop_worker = tmp_path / "stop_worker.py"
    stop_worker.write_text(
        f"from pathlib import Path\nPath({str(stopped)!r}).write_text('stopped')\n",
        encoding="utf-8",
    )
    validator = ContentValidator(
        validator_settings(
            tmp_path,
            f"{sys.executable} {worker}",
            stop_command=f"{sys.executable} {stop_worker}",
        )
    )
    errors: list[Exception] = []

    def invoke() -> None:
        try:
            validator.validate_batch(
                [ValidationItem(tmp_path / "audio.wav", "Expected speech.", Language.EN)]
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke)
    started = time.monotonic()
    thread.start()
    deadline = time.monotonic() + 3
    while validator._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert validator._process is not None
    validator.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 8
    assert errors
    assert stopped.read_text() == "stopped"
