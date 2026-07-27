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

PORT="${BRIDGE_PORT:-8140}"
HOST="${BRIDGE_HOST:-127.0.0.1}"

exec "${VENV_DIR}/bin/uvicorn" main:app --host "$HOST" --port "$PORT"
