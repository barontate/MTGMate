#!/usr/bin/env bash
# One-shot project setup: creates .venv, installs deps and git hooks.
# Safe to re-run any time.
set -euo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || dirname "$0"/..)"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Install it with:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

uv sync
uv run pre-commit install

if ! command -v direnv >/dev/null 2>&1; then
  echo
  echo "Tip: install direnv so .venv activates automatically when you cd here."
  echo "  https://direnv.net  (then run: direnv allow)"
fi

echo
echo "Ready. Activate with: source .venv/bin/activate  (or let direnv do it)"
