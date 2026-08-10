#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

PREFIX = "QVL_EVENT "


def emit(payload: dict) -> None:
    print(PREFIX + json.dumps(payload), flush=True)


def wrapper() -> int:
    marker = os.getenv("QVL_FAKE_GPU_STARTS")
    if marker:
        path = Path(marker)
        count = int(path.read_text() or "0") if path.exists() else 0
        path.write_text(str(count + 1))
    if os.getenv("QVL_FAKE_GPU_MODE") == "deferred":
        print("scheduler is occupied", flush=True)
        return 1
    separator = sys.argv.index("--")
    command = sys.argv[separator + 1 :]
    if os.getenv("QVL_FAKE_SYSTEMD") == "1":
        unit = f"qvl-test-worker-{os.getpid()}.service"
        unit_path = os.getenv("QVL_FAKE_GPU_UNIT_FILE")
        if unit_path:
            Path(unit_path).write_text(unit)
        if command[:1] == ["env"]:
            inherited = [
                f"{name}={value}"
                for name, value in os.environ.items()
                if name.startswith("QVL_FAKE_")
            ]
            command[1:1] = [f"QVL_FAKE_GPU_UNIT={unit}", *inherited]
        return subprocess.run(
            [
                "systemd-run",
                "--quiet",
                "--collect",
                "--wait",
                "--pipe",
                f"--unit={unit}",
                "--service-type=exec",
                "--property=KillMode=control-group",
                "--",
                *command,
            ],
            check=False,
        ).returncode
    os.execvp(command[0], command)
    return 127


def worker() -> int:
    emit({"type": "ready", "unit": os.getenv("QVL_FAKE_GPU_UNIT", "fake-gpu-worker.service")})
    for line in sys.stdin:
        payload = json.loads(line)
        if payload.get("type") == "shutdown":
            return 0
        if os.getenv("QVL_FAKE_GPU_MODE") == "preempt":
            return 75
        request_id = payload["request_id"]
        if os.getenv("QVL_FAKE_GPU_MODE") == "slow":
            emit({"type": "progress", "request_id": request_id, "value": 0.1})
            time.sleep(60)
        output = Path(payload["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(b"\x00\x00" * 16_000)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        emit({"type": "progress", "request_id": request_id, "value": 1})
        emit(
            {
                "type": "result",
                "request_id": request_id,
                "metrics": {
                    "model": "fake-wrapped-qwen",
                    "device": "cuda:0",
                    "load_ms": 10,
                    "generation_ms": 20,
                    "first_audio_ms": 30,
                    "duration_seconds": 1,
                    "rtf": 0.02,
                    "peak_vram_mib": 100,
                    "output_sha256": digest,
                    "output_bytes": output.stat().st_size,
                },
            }
        )
        if os.getenv("QVL_FAKE_GPU_MODE") == "preempt_idle":
            return 75
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["worker"]:
        raise SystemExit(worker())
    if sys.argv[1:2] == ["stop"]:
        unit = sys.argv[2]
        if not unit.startswith("qvl-test-worker-"):
            raise SystemExit(2)
        raise SystemExit(subprocess.run(["systemctl", "stop", unit], check=False).returncode)
    raise SystemExit(wrapper())
