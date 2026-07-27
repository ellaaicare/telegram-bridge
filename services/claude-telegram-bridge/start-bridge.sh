#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${ROOT_DIR}/venv}"

cd "${ROOT_DIR}"

ENV_FILE="${BRIDGE_ENV_FILE:-.env}"

if [ -f "${ENV_FILE}" ]; then
  set -a
  . "${ENV_FILE}"
  set +a
fi

PORT="${BRIDGE_PORT:-8100}"
HOST="${BRIDGE_HOST:-127.0.0.1}"

exec "${VENV_DIR}/bin/uvicorn" main:app --host "$HOST" --port "$PORT"
