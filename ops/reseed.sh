#!/usr/bin/env bash
# Full reseed: wipe + regenerate + relink. Services must be running for the
# link phase (start_all.sh first).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COHORT="${1:-200}"

cd "$ROOT/backend"
uv run python -m acme_ofv.infra.db_setup
uv run python -m acme_ofv.seed.run_seed --cohort "$COHORT" --procs 4
uv run python -m acme_ofv.seed.link_all
echo "reseed complete — cohort=$COHORT"
