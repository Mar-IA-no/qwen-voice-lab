#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
export QVL_DATA_DIR="${QVL_DATA_DIR:-$PROJECT_ROOT/data}"
"$PROJECT_ROOT/scripts/build_frontend.sh"
exec uv run qwen-voice-lab
