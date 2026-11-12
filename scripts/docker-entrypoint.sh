#!/usr/bin/env bash
# Container entrypoint: API on loopback + UI on all container interfaces (published to the
# host's 127.0.0.1 only by docker-compose.yml). If either process exits, the container exits.
set -euo pipefail
cd /app

python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 \
  --no-server-header --no-proxy-headers &
api_pid=$!

# --configLoader runner: load vite.config.ts in memory instead of bundling it to a
# temp file under node_modules (the image runs as non-root with a read-only root).
# Run from frontend/ so Vite resolves dist/ there.
(cd frontend && exec node node_modules/vite/bin/vite.js preview \
  --configLoader runner --host 0.0.0.0 --port 5173 --strictPort) &
web_pid=$!

trap 'kill "$api_pid" "$web_pid" 2>/dev/null || true' EXIT INT TERM
wait -n "$api_pid" "$web_pid"
