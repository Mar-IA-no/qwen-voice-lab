from __future__ import annotations

import json
import select
import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .engine import QwenEngine, RenderCancelled
from .models import DesignRequest, SynthesisRequest, Voice
from .prosody import ProsodyRegistry
from .worker_client import EVENT_PREFIX


def emit(payload: dict[str, Any]) -> None:
    print(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def render(engine: QwenEngine, payload: dict[str, Any]) -> None:
    request_id = str(payload["request_id"])
    cancel_file = Path(payload["cancel_file"])

    def progress(value: float) -> None:
        emit({"type": "progress", "request_id": request_id, "value": value})

    def cancelled() -> bool:
        return cancel_file.exists()

    try:
        output = Path(payload["output"])
        if payload["kind"] == "synthesis":
            metrics = engine.render_synthesis(
                SynthesisRequest.model_validate(payload["request"]),
                Voice.model_validate(payload["voice"]),
                output,
                progress,
                cancelled,
            )
        elif payload["kind"] == "design":
            metrics = engine.render_design(
                DesignRequest.model_validate(payload["request"]),
                output,
                progress,
                cancelled,
            )
        else:
            raise ValueError(f"Unsupported worker job kind: {payload['kind']}")
        if cancelled():
            raise RenderCancelled("render cancelled")
        emit(
            {
                "type": "result",
                "request_id": request_id,
                "metrics": metrics.model_dump(mode="json"),
            }
        )
    except RenderCancelled:
        emit({"type": "cancelled", "request_id": request_id})
    except Exception as exc:
        emit(
            {
                "type": "error",
                "request_id": request_id,
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )


def main() -> int:
    settings = Settings()
    if settings.engine != "qwen":
        emit({"type": "error", "message": "GPU worker requires QVL_ENGINE=qwen."})
        return 2
    prosody = ProsodyRegistry(settings)
    engine = QwenEngine(settings, prosody)
    try:
        QwenEngine.assert_wrapped(settings)
    except Exception as exc:
        emit({"type": "error", "error_type": type(exc).__name__, "message": str(exc)})
        return 2

    unit = QwenEngine.wrapper_unit(settings.gpu_cgroup_pattern)
    emit({"type": "ready", "unit": unit})
    try:
        while True:
            readable, _, _ = select.select([sys.stdin], [], [], settings.model_idle_seconds)
            if not readable:
                emit({"type": "idle_exit"})
                return 0
            line = sys.stdin.readline()
            if not line:
                return 0
            try:
                payload = json.loads(line)
                if payload.get("type") == "shutdown":
                    return 0
                if payload.get("type") != "render":
                    raise ValueError("Unsupported worker message.")
                render(engine, payload)
            except Exception as exc:
                emit({"type": "error", "error_type": type(exc).__name__, "message": str(exc)})
    finally:
        engine.unload()


if __name__ == "__main__":
    raise SystemExit(main())
