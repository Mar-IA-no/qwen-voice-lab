from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from .config import Settings
from .engine import CancelCallback, ProgressCallback, RenderCancelled
from .gpu_control import stop_command, valid_worker_unit
from .models import DesignRequest, JobMetrics, SynthesisRequest, Voice
from .prosody import ProsodyRegistry

EVENT_PREFIX = "QVL_EVENT "


class GPUWorkerUnavailable(RuntimeError):
    """The shared scheduler did not admit or preempted the GPU worker."""


class WrappedQwenEngine:
    """CPU-side proxy for a lazily admitted, reusable Qwen worker process."""

    device = "cuda:0"

    def __init__(self, settings: Settings, prosody: ProsodyRegistry):
        self.settings = settings
        self.prosody = prosody
        self.device = settings.device
        self._process: subprocess.Popen[str] | None = None
        self._event_queue: queue.Queue[tuple[str, Any]] | None = None
        self._worker_unit: str | None = None
        self._active_cancel_file: Path | None = None
        self._render_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._state = "standby"
        self._reason: str | None = None
        self._cooldown_until = 0.0
        self._logs: deque[str] = deque(maxlen=24)

    def readiness(self) -> tuple[bool, str | None]:
        wrapper = Path(self.settings.gpu_wrapper).expanduser()
        if not self.settings.gpu_wrapper:
            return False, "QVL_GPU_WRAPPER is required for the wrapped worker."
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            return False, f"Configured GPU wrapper is unavailable: {wrapper}"
        if not self.settings.gpu_cgroup_pattern:
            return False, "QVL_GPU_CGROUP_PATTERN is required for wrapper verification."
        if not self.settings.gpu_stop_command.strip():
            return False, "QVL_GPU_STOP_COMMAND is required for guaranteed worker cleanup."
        if not self.settings.gpu_stop_all_command.strip():
            return False, "QVL_GPU_STOP_ALL_COMMAND is required for admission-time cleanup."
        try:
            pattern = re.compile(self.settings.gpu_cgroup_pattern)
            if pattern.groups < 1:
                return False, "QVL_GPU_CGROUP_PATTERN must contain a capturing group."
            if "{unit}" not in self.settings.gpu_stop_command:
                return False, "QVL_GPU_STOP_COMMAND must contain the {unit} placeholder."
            command = self._worker_command()
            cleanup_commands = (
                shlex.split(self.settings.gpu_stop_command),
                shlex.split(self.settings.gpu_stop_all_command),
            )
        except (re.error, ValueError) as exc:
            return False, f"Invalid GPU worker configuration: {exc}"
        executable = command[0]
        if not Path(executable).is_file() and shutil.which(executable) is None:
            return False, f"GPU worker executable is unavailable: {executable}"
        for cleanup in cleanup_commands:
            if not cleanup or (
                not Path(cleanup[0]).is_file() and shutil.which(cleanup[0]) is None
            ):
                return False, f"GPU cleanup executable is unavailable: {cleanup[0]}"
        if not self.settings.gpu_worker_command.strip():
            for module in ("torch", "qwen_tts", "huggingface_hub"):
                if importlib.util.find_spec(module) is None:
                    return False, f"Required Qwen worker module is unavailable: {module}"
            from .engine import QwenEngine

            for source in (
                self.settings.qwen_base_model,
                self.settings.qwen_design_model,
            ):
                try:
                    QwenEngine._resolve_model(source)
                except Exception as exc:
                    return False, f"Qwen model is unavailable locally ({source}): {exc}"
        return True, None

    def runtime_status(self) -> dict[str, Any]:
        configured, configuration_reason = self.readiness()
        with self._state_lock:
            process = self._process
            if process is not None and process.poll() is not None:
                code = process.returncode
                self._process = None
                self._event_queue = None
                if self._state not in {"cooldown", "stopped"}:
                    if code == 0:
                        self._state = "standby"
                        self._reason = None
                    else:
                        self._state = "cooldown"
                        self._reason = self._exit_reason(code)
                        self._cooldown_until = (
                            time.monotonic() + self.settings.gpu_retry_cooldown_seconds
                        )
            if self._state == "cooldown" and time.monotonic() >= self._cooldown_until:
                self._state = "standby"
                self._reason = None
            state = self._state if configured else "misconfigured"
            reason = configuration_reason or self._reason
            verified = state in {"ready", "running"}
        return {
            "configured": configured,
            "state": state,
            "reason": reason,
            "wrapper_verified": verified,
        }

    def render_synthesis(
        self,
        request: SynthesisRequest,
        voice: Voice,
        output: Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> JobMetrics:
        payload = {
            "kind": "synthesis",
            "request": request.model_dump(mode="json"),
            "voice": voice.model_dump(mode="json"),
            "output": str(output.resolve()),
        }
        return self._render(payload, progress, cancelled)

    def render_design(
        self,
        request: DesignRequest,
        output: Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> JobMetrics:
        payload = {
            "kind": "design",
            "request": request.model_dump(mode="json"),
            "output": str(output.resolve()),
        }
        return self._render(payload, progress, cancelled)

    def _render(
        self,
        payload: dict[str, Any],
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> JobMetrics:
        with self._render_lock:
            if cancelled():
                raise RenderCancelled("render cancelled")
            process = self._ensure_worker()
            request_id = uuid.uuid4().hex
            cancel_file = self.settings.temp_dir / f"gpu_cancel_{request_id}"
            message = {
                "type": "render",
                "request_id": request_id,
                "cancel_file": str(cancel_file.resolve()),
                **payload,
            }
            try:
                with self._state_lock:
                    self._active_cancel_file = cancel_file
                self._send(process, message)
                self._set_state("running")
                return self._await_result(process, request_id, cancel_file, progress, cancelled)
            finally:
                cancel_file.unlink(missing_ok=True)
                with self._state_lock:
                    if self._active_cancel_file == cancel_file:
                        self._active_cancel_file = None

    def _ensure_worker(self) -> subprocess.Popen[str]:
        configured, reason = self.readiness()
        if not configured:
            raise GPUWorkerUnavailable(reason or "GPU worker is not configured.")
        with self._state_lock:
            if self._state == "cooldown" and time.monotonic() < self._cooldown_until:
                remaining = max(1, round(self._cooldown_until - time.monotonic()))
                raise GPUWorkerUnavailable(
                    f"GPU reserved for the priority service; retry in about {remaining}s."
                )
            process = self._process
            if process is not None and process.poll() is None:
                return process
            self._process = None
            self._state = "starting"
            self._reason = None

        command = self._wrapper_command()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=Path(__file__).resolve().parents[2],
            env=os.environ.copy(),
        )
        with self._state_lock:
            self._process = process
            self._event_queue = queue.Queue()
            event_queue = self._event_queue
        threading.Thread(
            target=self._pump_output,
            args=(process, event_queue),
            name="qvl-gpu-worker-output",
            daemon=True,
        ).start()
        deadline = time.monotonic() + self.settings.gpu_worker_start_timeout_seconds
        try:
            while True:
                event = self._next_event(process, deadline)
                if event.get("type") == "ready":
                    unit = event.get("unit")
                    if not isinstance(unit, str) or not self._valid_worker_unit(unit):
                        raise GPUWorkerUnavailable(
                            "GPU worker did not report a valid admitted unit."
                        )
                    with self._state_lock:
                        self._worker_unit = unit
                    self._set_state("ready")
                    return process
                if event.get("type") == "error":
                    raise GPUWorkerUnavailable(str(event.get("message", "GPU worker failed")))
        except Exception:
            self._record_worker_exit(process)
            raise

    def _await_result(
        self,
        process: subprocess.Popen[str],
        request_id: str,
        cancel_file: Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> JobMetrics:
        cancellation_written = False
        while True:
            if cancelled() and not cancellation_written:
                cancel_file.touch(mode=0o600, exist_ok=True)
                cancellation_written = True
            try:
                event = self._next_event(
                    process, time.monotonic() + 0.5, allow_timeout=True
                )
            except GPUWorkerUnavailable:
                if cancelled():
                    self._set_state("standby")
                    raise RenderCancelled("render cancelled") from None
                raise
            if event is None:
                continue
            if event.get("request_id") != request_id:
                continue
            event_type = event.get("type")
            if event_type == "progress":
                progress(float(event.get("value", 0)))
            elif event_type == "result":
                self._set_state("ready")
                if cancelled():
                    raise RenderCancelled("render cancelled")
                return JobMetrics.model_validate(event["metrics"])
            elif event_type == "cancelled":
                self._set_state("ready")
                raise RenderCancelled("render cancelled")
            elif event_type == "error":
                self._set_state("ready", str(event.get("message", "GPU worker failed")))
                raise RuntimeError(
                    f"GPU worker {event.get('error_type', 'error')}: "
                    f"{event.get('message', 'render failed')}"
                )

    def _next_event(
        self,
        process: subprocess.Popen[str],
        deadline: float,
        *,
        allow_timeout: bool = False,
    ) -> dict[str, Any] | None:
        event_queue = self._event_queue
        if event_queue is None:
            self._raise_worker_exit(process)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if allow_timeout:
                    return None
                self._terminate(process)
                self._set_cooldown("GPU worker admission timed out.")
                raise GPUWorkerUnavailable("GPU worker admission timed out.")
            try:
                kind, payload = event_queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if process.poll() is not None:
                    self._raise_worker_exit(process)
                continue
            if kind == "eof":
                self._raise_worker_exit(process)
            return payload

    def _pump_output(
        self,
        process: subprocess.Popen[str],
        event_queue: queue.Queue[tuple[str, Any]],
    ) -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if not line.startswith(EVENT_PREFIX):
                if line:
                    self._logs.append(line[-1000:])
                continue
            try:
                event_queue.put(("event", json.loads(line.removeprefix(EVENT_PREFIX))))
            except json.JSONDecodeError:
                self._logs.append(line[-1000:])
        event_queue.put(("eof", process.poll()))

    def _send(self, process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
        if process.stdin is None or process.poll() is not None:
            self._raise_worker_exit(process)
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._record_worker_exit(process)
            raise GPUWorkerUnavailable("GPU worker stopped before accepting the job.") from exc

    def _worker_command(self) -> list[str]:
        if self.settings.gpu_worker_command.strip():
            return shlex.split(self.settings.gpu_worker_command)
        return [sys.executable, "-m", "qwen_voice_lab.gpu_worker"]

    def _wrapper_command(self) -> list[str]:
        environment = [
            "env",
            "QVL_ENGINE=qwen",
            "QVL_GPU_WRAPPED=1",
            f"QVL_DATA_DIR={self.settings.data_dir}",
            f"QVL_DEVICE={self.settings.device}",
            f"QVL_QWEN_BASE_MODEL={self.settings.qwen_base_model}",
            f"QVL_QWEN_DESIGN_MODEL={self.settings.qwen_design_model}",
            f"QVL_QWEN_BASE_MODEL_LABEL={self.settings.qwen_base_model_label}",
            f"QVL_QWEN_DESIGN_MODEL_LABEL={self.settings.qwen_design_model_label}",
            "QVL_REQUIRE_GPU_WRAPPER=true",
            f"QVL_GPU_CGROUP_PATTERN={self.settings.gpu_cgroup_pattern}",
            f"QVL_MODEL_IDLE_SECONDS={self.settings.model_idle_seconds}",
        ]
        return [
            str(Path(self.settings.gpu_wrapper).expanduser()),
            "--name",
            self.settings.gpu_job_name,
            "--",
            *environment,
            *self._worker_command(),
        ]

    def _raise_worker_exit(self, process: subprocess.Popen[str]) -> None:
        try:
            code = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            code = process.poll()
        reason = self._exit_reason(code)
        self._record_worker_exit(process, reason)
        raise GPUWorkerUnavailable(reason)

    def _exit_reason(self, code: int | None) -> str:
        if code == self.settings.gpu_preempt_exit_code:
            return "GPU worker was preempted by the priority service (exit 75)."
        detail = self._logs[-1] if self._logs else "no worker diagnostic"
        if code in self.settings.retryable_exit_codes:
            return f"GPU admission deferred by the priority service (exit {code}): {detail}"
        return f"GPU worker stopped unexpectedly (exit {code}): {detail}"

    def _record_worker_exit(
        self, process: subprocess.Popen[str], reason: str | None = None
    ) -> None:
        code = process.poll()
        if code is None:
            self._terminate(process)
            code = process.poll()
        with self._state_lock:
            if self._process is process:
                self._process = None
                self._event_queue = None
                self._worker_unit = None
        if code == 0 and reason is None:
            self._set_state("standby")
            return
        self._set_cooldown(reason or self._exit_reason(code))

    def _set_state(self, state: str, reason: str | None = None) -> None:
        with self._state_lock:
            self._state = state
            self._reason = reason

    def _set_cooldown(self, reason: str) -> None:
        with self._state_lock:
            self._state = "cooldown"
            self._reason = reason
            self._cooldown_until = time.monotonic() + self.settings.gpu_retry_cooldown_seconds

    def _valid_worker_unit(self, unit: str) -> bool:
        return valid_worker_unit(self.settings, unit)

    def _stop_worker_unit(self, unit: str | None) -> None:
        if not unit or not self._valid_worker_unit(unit):
            return
        command = stop_command(self.settings, unit)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
            raise GPUWorkerUnavailable(
                f"Failed to stop admitted GPU unit {unit}: {detail}"
            )

    def _stop_all_worker_units(self) -> None:
        if not self.settings.gpu_stop_all_command.strip():
            return
        result = subprocess.run(
            shlex.split(self.settings.gpu_stop_all_command),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
            raise GPUWorkerUnavailable(f"Fallback GPU cleanup failed: {detail}")

    @staticmethod
    def _terminate_controller(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        with self._state_lock:
            unit = self._worker_unit if self._process is process else None
        cleanup_error: GPUWorkerUnavailable | None = None
        if unit:
            try:
                self._stop_worker_unit(unit)
            except GPUWorkerUnavailable as exc:
                cleanup_error = exc
        elif self.settings.gpu_stop_all_command.strip():
            try:
                self._stop_all_worker_units()
            except GPUWorkerUnavailable as exc:
                cleanup_error = exc
        if cleanup_error is not None and self.settings.gpu_stop_all_command.strip():
            try:
                self._stop_all_worker_units()
                cleanup_error = None
            except GPUWorkerUnavailable as fallback_exc:
                cleanup_error = GPUWorkerUnavailable(
                    f"{cleanup_error} {fallback_exc}"
                )
        if cleanup_error is None and (unit or self.settings.gpu_stop_all_command.strip()):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self._terminate_controller(process)
        if cleanup_error is not None:
            raise cleanup_error

    def cancel_active(self) -> None:
        with self._state_lock:
            cancel_file = self._active_cancel_file
            unit = self._worker_unit
        if cancel_file is not None:
            cancel_file.touch(mode=0o600, exist_ok=True)
        self._stop_worker_unit(unit)

    def unload(self) -> None:
        with self._state_lock:
            process = self._process
            active = self._active_cancel_file
            unit = self._worker_unit
        if process is None:
            self._set_state("stopped")
            return
        if process.poll() is None:
            try:
                if active is not None:
                    active.touch(mode=0o600, exist_ok=True)
                    self._stop_worker_unit(unit)
                    process.wait(timeout=10)
                else:
                    self._send(process, {"type": "shutdown"})
                    process.wait(timeout=15)
            except (GPUWorkerUnavailable, subprocess.TimeoutExpired):
                self._terminate(process)
        with self._state_lock:
            self._process = None
            self._event_queue = None
            self._worker_unit = None
            self._active_cancel_file = None
            self._state = "stopped"

    def unload_if_idle(self, _: int) -> None:
        # The admitted worker owns its idle deadline; the CPU API must stay resident.
        return None
