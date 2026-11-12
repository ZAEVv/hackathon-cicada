#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! .venv/bin/python -c 'import pytest, httpx' 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements-dev.txt
fi
.venv/bin/python -m pytest -q
npm --prefix frontend run build
