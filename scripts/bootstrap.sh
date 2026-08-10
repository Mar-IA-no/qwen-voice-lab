#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

uv sync --extra dev
npm --prefix frontend ci

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

echo "Qwen Voice Lab is ready. Run ./scripts/run_dev.sh for CPU/mock mode."
