from __future__ import annotations

import json
import re
import shlex
import subprocess

from .config import PROJECT_ROOT, Settings, get_settings


def valid_worker_unit(settings: Settings, unit: str) -> bool:
    try:
        match = re.search(settings.gpu_cgroup_pattern, unit)
    except re.error:
        return False
    if match is None or match.lastindex is None or match.group(1) != unit:
        return False
    return not settings.gpu_unit_prefix or unit.startswith(settings.gpu_unit_prefix)


def discover_registered_units(settings: Settings) -> list[str]:
    jobs_dir = settings.gpu_jobs_dir
    if jobs_dir is None or not jobs_dir.is_dir():
        return []
    units: list[str] = []
    root = str(PROJECT_ROOT.resolve())
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        unit = record.get("unit")
        command = record.get("command")
        if not isinstance(unit, str) or not isinstance(command, list):
            continue
        joined = " ".join(str(part) for part in command)
        if (
            record.get("name") == settings.gpu_job_name
            and root in joined
            and "qwen_voice_lab.gpu_worker" in joined
            and valid_worker_unit(settings, unit)
        ):
            units.append(unit)
    return units


def stop_command(settings: Settings, unit: str) -> list[str]:
    if not valid_worker_unit(settings, unit):
        raise ValueError(f"Refusing invalid GPU unit: {unit}")
    if "{unit}" not in settings.gpu_stop_command:
        raise ValueError("QVL_GPU_STOP_COMMAND must contain the {unit} placeholder")
    return [part.replace("{unit}", unit) for part in shlex.split(settings.gpu_stop_command)]


def stop_registered_units(settings: Settings) -> list[str]:
    stopped: list[str] = []
    for unit in discover_registered_units(settings):
        result = subprocess.run(
            stop_command(settings, unit), capture_output=True, text=True, timeout=20, check=False
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
            raise RuntimeError(f"Could not stop {unit}: {detail}")
        stopped.append(unit)
    return stopped


def main() -> None:
    settings = get_settings()
    if settings.gpu_stop_all_command.strip():
        result = subprocess.run(
            shlex.split(settings.gpu_stop_all_command),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
            raise SystemExit(f"GPU cleanup failed: {detail}")
        print(result.stdout.strip() or "Qwen Voice Lab has no registered GPU unit.")
        return
    units = stop_registered_units(settings)
    if not units:
        print("Qwen Voice Lab has no registered GPU unit.")
        return
    for unit in units:
        print(f"Stopped {unit}")


if __name__ == "__main__":
    main()
