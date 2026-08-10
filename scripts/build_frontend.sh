#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$PROJECT_ROOT/frontend/dist"
PACKAGE_DIR="$PROJECT_ROOT/src/qwen_voice_lab/static"

npm --prefix "$PROJECT_ROOT/frontend" run build
mkdir -p "$PACKAGE_DIR/assets"
cp "$SOURCE_DIR/index.html" "$PACKAGE_DIR/index.html"
cp "$SOURCE_DIR/assets/app.js" "$PACKAGE_DIR/assets/app.js"
cp "$SOURCE_DIR/assets/index.css" "$PACKAGE_DIR/assets/index.css"

echo "Packaged frontend synchronized in src/qwen_voice_lab/static."
