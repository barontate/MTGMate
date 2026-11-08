#!/usr/bin/env bash
# Build the single-file Windows executable: dist/mtgmate-checker.exe
set -euo pipefail
cd "$(dirname "$0")/.."
uv run pyinstaller --noconfirm --clean --onefile --console \
  --name mtgmate-checker \
  --collect-all playwright \
  --collect-all textual \
  --copy-metadata textual \
  --paths src \
  src/mtgmate_checker/__main__.py
