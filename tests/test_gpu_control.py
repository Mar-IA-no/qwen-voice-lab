from __future__ import annotations

import json
import sys
from pathlib import Path

from qwen_voice_lab.config import PROJECT_ROOT, Settings
from qwen_voice_lab.gpu_control import discover_registered_units, stop_registered_units


def settings_for(tmp_path: Path, stop_log: Path) -> Settings:
    return Settings(
        engine="mock",
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        access_token="",
        gpu_jobs_dir=tmp_path / "jobs",
        gpu_job_name="qwen-voice-lab",
        gpu_cgroup_pattern=r"(voice-lab-[0-9]+[.]service)",
        gpu_unit_prefix="voice-lab-",
        gpu_stop_command=(
            f"{sys.executable} -c \"from pathlib import Path; "
            f"Path(r'{stop_log}').write_text('{{unit}}')\""
        ),
        gpu_stop_all_command="",
    )


def write_record(path: Path, *, unit: str, name: str, command: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "unit": unit, "name": name, "command": command}),
        encoding="utf-8",
    )


def test_stop_all_uses_only_owned_worker_metadata(tmp_path: Path) -> None:
    stop_log = tmp_path / "stopped.txt"
    settings = settings_for(tmp_path, stop_log)
    owned_unit = "voice-lab-100.service"
    owned_command = [
        str(PROJECT_ROOT / ".venv/bin/python"),
        "-m",
        "qwen_voice_lab.gpu_worker",
    ]
    write_record(
        settings.gpu_jobs_dir / "owned.json",
        unit=owned_unit,
        name="qwen-voice-lab",
        command=owned_command,
    )
    write_record(
        settings.gpu_jobs_dir / "foreign-name.json",
        unit="voice-lab-200.service",
        name="production",
        command=owned_command,
    )
    write_record(
        settings.gpu_jobs_dir / "foreign-command.json",
        unit="voice-lab-300.service",
        name="qwen-voice-lab",
        command=["/elsewhere/python", "-m", "qwen_voice_lab.gpu_worker"],
    )
    write_record(
        settings.gpu_jobs_dir / "wrong-module.json",
        unit="voice-lab-400.service",
        name="qwen-voice-lab",
        command=[str(PROJECT_ROOT / "tool"), "other.module"],
    )

    assert discover_registered_units(settings) == [owned_unit]
    assert stop_registered_units(settings) == [owned_unit]
    assert stop_log.read_text() == owned_unit
