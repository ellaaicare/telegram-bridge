#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "${ROOT_DIR}/.env"
  set +a
fi
BRIDGE_SLUG="${BRIDGE_SLUG:-$(basename "${ROOT_DIR}")}"
BRIDGE_PLIST_LABEL="${BRIDGE_SLUG//-/.}"
SERVICE_NAME="${SERVICE_NAME:-com.ella.${BRIDGE_PLIST_LABEL}}"
PLIST_DST="${PLIST_DST:-${HOME}/Library/LaunchAgents/${SERVICE_NAME}.plist}"
SYSTEMD_UNIT_NAME="${SYSTEMD_UNIT_NAME:-${BRIDGE_SLUG}}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"

usage() {
  cat <<'EOF'
Usage:
  ./deploy-fleet.sh [--install-service] [--no-pull]

What it does:
  - optionally git pull in the current repo
  - create the venv if missing
  - install/update Python dependencies
  - restart the bridge for the current OS

Options:
  --install-service  Generate/install the launchd plist on macOS or systemd
                     unit on Linux using this checkout path.
  --no-pull          Skip git pull before updating.
  -h, --help         Show this help.
EOF
}

write_launchd_plist() {
  local dst="$1"
  mkdir -p "$(dirname "${dst}")" "${ROOT_DIR}/logs"
  cat > "${dst}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${SERVICE_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${ROOT_DIR}/start-bridge.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${ROOT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${ROOT_DIR}/logs/bridge-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${ROOT_DIR}/logs/bridge-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
EOF
}

write_systemd_unit() {
  local dst="$1"
  cat > "${dst}" <<EOF
[Unit]
Description=${HARNESS_LABEL:-Telegram} Telegram Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${ROOT_DIR}
EnvironmentFile=${ROOT_DIR}/.env
ExecStart=${ROOT_DIR}/start-bridge.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

INSTALL_SERVICE=0
DO_PULL=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-service)
      INSTALL_SERVICE=1
      shift
      ;;
    --no-pull)
      DO_PULL=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

cd "${ROOT_DIR}"

if [[ ! -f ".env" ]]; then
  echo "Missing ${ROOT_DIR}/.env" >&2
  echo "Copy .env.example to .env and fill in the required values first." >&2
  exit 1
fi

if [[ ${DO_PULL} -eq 1 ]]; then
  git pull --ff-only
fi

if [[ ! -d "${VENV_DIR}" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install -r requirements.txt

OS_NAME="$(uname -s)"

if [[ "${OS_NAME}" == "Darwin" ]]; then
  mkdir -p "${HOME}/Library/LaunchAgents"

  if [[ ${INSTALL_SERVICE} -eq 1 ]]; then
    write_launchd_plist "${PLIST_DST}"
  fi

  if [[ ! -f "${PLIST_DST}" ]]; then
    echo "Missing ${PLIST_DST}; rerun with --install-service or set PLIST_DST." >&2
    exit 1
  fi

  if launchctl print "gui/$(id -u)/${SERVICE_NAME}" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "${PLIST_DST}"
  fi
  launchctl bootstrap "gui/$(id -u)" "${PLIST_DST}"
  launchctl print "gui/$(id -u)/${SERVICE_NAME}" | sed -n '1,40p'
  exit 0
fi

if [[ "${OS_NAME}" == "Linux" ]]; then
  if [[ ${INSTALL_SERVICE} -eq 1 ]]; then
    tmp_unit="$(mktemp)"
    write_systemd_unit "${tmp_unit}"
    sudo install -m 0644 "${tmp_unit}" "/etc/systemd/system/${SYSTEMD_UNIT_NAME}.service"
    rm -f "${tmp_unit}"
    sudo systemctl daemon-reload
    sudo systemctl enable "${SYSTEMD_UNIT_NAME}"
  fi

  sudo systemctl restart "${SYSTEMD_UNIT_NAME}"
  sudo systemctl status "${SYSTEMD_UNIT_NAME}" --no-pager
  exit 0
fi

echo "Unsupported OS: ${OS_NAME}" >&2
exit 1
