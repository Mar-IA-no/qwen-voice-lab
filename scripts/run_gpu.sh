#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export QVL_ENGINE=qwen
export QVL_DATA_DIR="${QVL_DATA_DIR:-$PROJECT_ROOT/data}"
unset QVL_GPU_WRAPPED

cd "$PROJECT_ROOT"
"$PROJECT_ROOT/scripts/build_frontend.sh"

GPU_WRAPPER="$(uv run python -c 'from qwen_voice_lab.config import get_settings; print(get_settings().gpu_wrapper)')"
REQUIRE_WRAPPER="$(uv run python -c 'from qwen_voice_lab.config import get_settings; print(str(get_settings().require_gpu_wrapper).lower())')"

if [[ "$REQUIRE_WRAPPER" == "true" && ( -z "$GPU_WRAPPER" || ! -x "$GPU_WRAPPER" ) ]]; then
  if ! QVL_GPU_WRAPPER="$GPU_WRAPPER" uv run python -c 'import os, shlex, shutil; from pathlib import Path; command = shlex.split(os.environ.get("QVL_GPU_WRAPPER", "")); raise SystemExit(0 if command and ((Path(command[0]).expanduser().is_file() and os.access(Path(command[0]).expanduser(), os.X_OK)) or shutil.which(command[0])) else 1)'; then
    echo "Configured GPU wrapper is unavailable: ${GPU_WRAPPER:-<empty>}" >&2
    exit 1
  fi
fi

# Dedicated mode runs Qwen in this process. An optional shared-host
# configuration instead launches a reusable admitted worker on first render.
exec "$PROJECT_ROOT/.venv/bin/qwen-voice-lab"
