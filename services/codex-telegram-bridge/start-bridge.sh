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

PORT="${CODEX_BRIDGE_PORT:-8110}"

exec "${VENV_DIR}/bin/uvicorn" main:app --host 0.0.0.0 --port "$PORT"
