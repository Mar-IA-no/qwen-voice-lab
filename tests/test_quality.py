from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from qwen_voice_lab.config import Settings
from qwen_voice_lab.models import Language
from qwen_voice_lab.quality import ContentValidator

MODEL_HASH = "a" * 64


def validator_settings(tmp_path: Path, command: str, *, device: str = "cpu") -> Settings:
    return Settings(
        engine="mock",
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        validator_enabled=True,
        validator_command=command,
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
        [(tmp_path / "audio.wav", "All words end here", Language.EN, None)]
    )
    assert result[0].content.validator == "cuda:7"
    assert result[0].content.verdict == "pass"


def test_close_terminates_in_flight_validator_process(tmp_path: Path) -> None:
    worker = tmp_path / "sleep_worker.py"
    worker.write_text(
        "import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    validator = ContentValidator(validator_settings(tmp_path, f"{sys.executable} {worker}"))
    errors: list[Exception] = []

    def invoke() -> None:
        try:
            validator.validate_batch(
                [(tmp_path / "audio.wav", "Expected speech.", Language.EN, None)]
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
