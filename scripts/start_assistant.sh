#!/usr/bin/env bash
# Single-command startup script for Linux/macOS
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

VENV_PYTHON="$PROJECT_ROOT/venv/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
    echo "Virtualenv not found at $VENV_PYTHON, falling back to python3"
    VENV_PYTHON="python3"
fi

exec "$VENV_PYTHON" orchestrator.py "$@"
