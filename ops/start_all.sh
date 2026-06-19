#!/usr/bin/env bash
# Start mock OFP, DC API, worker and the frontend dev server.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$ROOT/ops/logs"
mkdir -p "$LOGS"

cd "$ROOT/backend"
pkill -f "acme_ofv.mock_ofp" 2>/dev/null || true
pkill -f "acme_ofv.api.app" 2>/dev/null || true
pkill -f "acme_ofv.eraser.worker" 2>/dev/null || true
sleep 1

nohup uv run uvicorn acme_ofv.mock_ofp.app:app --port 8100 --log-level warning > "$LOGS/mock.log" 2>&1 &
nohup uv run uvicorn acme_ofv.api.app:app --port 8010 --reload --log-level warning > "$LOGS/api.log" 2>&1 &
nohup uv run python -m acme_ofv.eraser.worker > "$LOGS/worker.log" 2>&1 &

cd "$ROOT/frontend"
nohup npm run dev > "$LOGS/frontend.log" 2>&1 &

sleep 8
curl -sf localhost:8100/healthz >/dev/null && echo "mock_ofp  : up (8100)"
curl -sf localhost:8010/healthz >/dev/null && echo "api       : up (8010)"
grep -q "worker up" "$LOGS/worker.log" && echo "worker    : up"
echo "frontend  : check $LOGS/frontend.log for the port (usually 5174)"
