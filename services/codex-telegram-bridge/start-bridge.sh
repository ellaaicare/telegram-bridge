#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/venv}"

cd "${ROOT_DIR}"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if [[ "$(uname -s)" == "Darwin" ]] && [[ "${CODEX_BRIDGE_CLEAR_STALE_RESTART_JOBS:-true}" == "true" ]]; then
  "${ROOT_DIR}/../../scripts/clear-stale-bridge-restart-jobs.sh" || \
    echo "[bridge-startup] warning: stale transient launchd job cleanup failed" >&2
fi

PORT="${CODEX_BRIDGE_PORT:-8110}"
HOST="${CODEX_BRIDGE_HOST:-127.0.0.1}"

exec "${VENV_DIR}/bin/uvicorn" main:app --host "$HOST" --port "$PORT"
