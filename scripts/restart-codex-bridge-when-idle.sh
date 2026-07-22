#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-launchctl}"
CURL_BIN="${CURL_BIN:-curl}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

SERVICE_LABEL="com.ella.codex-bridge"
PORT="8110"
TIMEOUT_SECONDS="900"
POLL_SECONDS="5"

usage() {
  cat <<'EOF'
Usage: restart-codex-bridge-when-idle.sh [options]

Wait for a Codex bridge queue to become idle, then restart it exactly once.

Options:
  --service LABEL       launchd service label (default: com.ella.codex-bridge)
  --port PORT           local health endpoint port (default: 8110)
  --timeout SECONDS     stop waiting without restarting (default: 900)
  --poll SECONDS        health polling interval (default: 5)
  -h, --help            show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE_LABEL="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --timeout) TIMEOUT_SECONDS="${2:-}"; shift 2 ;;
    --poll) POLL_SECONDS="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! "${SERVICE_LABEL}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Invalid launchd service label" >&2
  exit 2
fi
if [[ ! "${PORT}" =~ ^[0-9]+$ ]] || [[ ! "${TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "Port and timeout must be non-negative integers" >&2
  exit 2
fi
if [[ ! "${POLL_SECONDS}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "Poll interval must be a non-negative number" >&2
  exit 2
fi

"${ROOT_DIR}/scripts/clear-stale-bridge-restart-jobs.sh" || {
  echo "Unable to clear stale transient restart jobs" >&2
  exit 1
}

health_url="http://127.0.0.1:${PORT}/health"
deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))

is_idle() {
  "${CURL_BIN}" -fsS --max-time 2 "${health_url}" 2>/dev/null | "${PYTHON_BIN}" -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
queue = payload.get("queue") or {}
raise SystemExit(0 if payload.get("status") == "ok" and not queue.get("busy", True) else 1)
' >/dev/null 2>&1
}

while ! is_idle; do
  if (( $(date +%s) >= deadline )); then
    printf 'Timed out waiting for %s on port %s to become idle; no restart performed.\n' \
      "${SERVICE_LABEL}" "${PORT}" >&2
    exit 3
  fi
  sleep "${POLL_SECONDS}"
done

printf 'Queue idle; restarting %s exactly once.\n' "${SERVICE_LABEL}"
"${LAUNCHCTL_BIN}" kickstart -k "gui/$(id -u)/${SERVICE_LABEL}"

health_deadline=$(( $(date +%s) + 60 ))
while ! "${CURL_BIN}" -fsS --max-time 2 "${health_url}" >/dev/null 2>&1; do
  if (( $(date +%s) >= health_deadline )); then
    printf '%s did not become healthy within 60 seconds.\n' "${SERVICE_LABEL}" >&2
    exit 4
  fi
  sleep 1
done

printf '%s is healthy after one restart.\n' "${SERVICE_LABEL}"
