#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Optional local settings: parse .env as literal KEY=VALUE lines (never source/eval it).
# Only the known variable names below are accepted; values are taken verbatim (no quotes,
# no expansion); variables already set in the environment take precedence.
if [[ -f .env ]]; then
  env_names='DEMO_BROKER_TOKEN|DEMO_REVIEWER_TOKEN|DEMO_DISABLE_DEFAULT_TOKENS|ENABLE_API_DOCS|ALLOWED_HOSTS|MAX_BODY_BYTES|RATE_LIMIT_PER_MINUTE|AUTH_FAIL_LIMIT|AUTH_FAIL_WINDOW_SECONDS|AUDIT_HMAC_KEY|DATABASE_PATH'
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    if [[ "$line" =~ ^($env_names)=(.*)$ ]]; then
      name="${BASH_REMATCH[1]}"
      if [[ -z "${!name+x}" ]]; then export "$name=${BASH_REMATCH[2]}"; fi
    else
      echo "dev.sh: ignoring unrecognised line in .env" >&2
    fi
  done < .env
fi
if [[ ! -x .venv/bin/python ]]; then
  python -m venv .venv
  .venv/bin/python -m pip install -r requirements-dev.txt
elif ! .venv/bin/python -c 'import fastapi, uvicorn, sklearn, networkx, httpx, pytest' 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements-dev.txt
fi
if [[ ! -d frontend/node_modules ]]; then npm --prefix frontend ci; fi
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 \
  --no-server-header --no-proxy-headers &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT INT TERM
npm --prefix frontend run dev -- --host 127.0.0.1
