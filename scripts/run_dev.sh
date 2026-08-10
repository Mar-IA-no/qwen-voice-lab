#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
export QVL_ENGINE="${QVL_ENGINE:-mock}"
export QVL_DATA_DIR="${QVL_DATA_DIR:-$PROJECT_ROOT/data}"
HOST="${QVL_HOST:-$(uv run python -c 'from qwen_voice_lab.config import get_settings; print(get_settings().host)')}"
PORT="${QVL_PORT:-$(uv run python -c 'from qwen_voice_lab.config import get_settings; print(get_settings().port)')}"

cleanup() {
  local status=$?
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
  [[ -n "${UI_PID:-}" ]] && kill "$UI_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  exit "$status"
}
trap cleanup INT TERM EXIT

uv run uvicorn qwen_voice_lab.app:app --host "$HOST" --port "$PORT" --reload &
API_PID=$!
npm --prefix frontend run dev &
UI_PID=$!
wait -n "$API_PID" "$UI_PID"
