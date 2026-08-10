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
  echo "Configured GPU wrapper is unavailable: ${GPU_WRAPPER:-<empty>}" >&2
  exit 1
fi

# The web/API process remains CPU-only. In shared mode it launches a separate,
# reusable Qwen worker through the wrapper when the first render is submitted.
exec "$PROJECT_ROOT/.venv/bin/qwen-voice-lab"
