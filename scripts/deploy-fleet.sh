#!/usr/bin/env bash
# deploy-fleet.sh — Orchestrate telegram-bridge updates across all fleet nodes.
#
# Usage:
#   ./scripts/deploy-fleet.sh                 # Update all bridges on all nodes
#   ./scripts/deploy-fleet.sh --node imac     # Update all bridges on a specific node
#   ./scripts/deploy-fleet.sh --bridge claude  # Update a specific bridge type everywhere
#   ./scripts/deploy-fleet.sh --dry-run       # Show what would happen
#   ./scripts/deploy-fleet.sh --no-restart    # Pull + install deps only, skip restart
#   ./scripts/deploy-fleet.sh --list          # Show fleet inventory
#
# Each node keeps its own .env / config — this script only updates code + deps + restarts.
set -euo pipefail

# ── Fleet inventory ──────────────────────────────────────────────────
# Format: node_name|ssh_target|os|deploy_base|user|service_mgr|bridges(comma-sep)
#
# service_mgr values:
#   systemd-user  = systemctl --user (no sudo)
#   systemd-root  = sudo systemctl
#   launchd       = launchctl (macOS)
#
# bridges: which bridge dirs exist at ${deploy_base}/services/
FLEET=(
  "imac||linux|/home/letta/telegram-bridge|letta|systemd-user|claude-telegram-bridge,kilo-telegram-bridge,opencode-telegram-bridge"
  "macbookair|admin-macbookair1|linux|/home/plato/telegram-bridge|plato|systemd-user|codex-telegram-bridge,claude-telegram-bridge"
  "macmini|ellaai@100.76.138.56|darwin|/Users/ellaai/telegram-bridge|ellaai|launchd|claude-telegram-bridge,codex-telegram-bridge,kilo-telegram-bridge,opencode-telegram-bridge"
)

# ── Helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*" >&2; }
err()  { echo -e "${RED}[error]${NC} $*" >&2; }

DRY_RUN=0
NO_RESTART=0
FILTER_NODE=""
FILTER_BRIDGE=""
LIST_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)     DRY_RUN=1; shift ;;
    --no-restart)  NO_RESTART=1; shift ;;
    --node)        FILTER_NODE="$2"; shift 2 ;;
    --bridge)      FILTER_BRIDGE="$2"; shift 2 ;;
    --list)        LIST_ONLY=1; shift ;;
    -h|--help)
      sed -n '2,/^set /{ /^#/s/^# \?//p }' "$0"
      exit 0
      ;;
    *) err "Unknown option: $1"; exit 1 ;;
  esac
done

# ── List mode ────────────────────────────────────────────────────────
if [[ ${LIST_ONLY} -eq 1 ]]; then
  printf "%-14s %-28s %-7s %-14s %s\n" "NODE" "SSH" "OS" "SVC_MGR" "BRIDGES"
  printf "%-14s %-28s %-7s %-14s %s\n" "----" "---" "--" "-------" "-------"
  for entry in "${FLEET[@]}"; do
    IFS='|' read -r name ssh_target os base user svc_mgr bridges <<< "${entry}"
    [[ -z "${ssh_target}" ]] && ssh_target="(local)"
    printf "%-14s %-28s %-7s %-14s %s\n" "${name}" "${ssh_target}" "${os}" "${svc_mgr}" "${bridges}"
  done
  exit 0
fi

# ── Run command (local or remote) ────────────────────────────────────
run_on() {
  local ssh_target="$1"; shift
  if [[ -z "${ssh_target}" ]]; then
    # Local
    bash -c "$*"
  else
    ssh -o ConnectTimeout=10 -o BatchMode=yes "${ssh_target}" "$*"
  fi
}

# ── Resolve bridge name from partial match ───────────────────────────
# "claude" → "claude-telegram-bridge", "codex" → "codex-telegram-bridge"
resolve_bridge() {
  local input="$1"
  if [[ "${input}" == *-telegram-bridge ]]; then
    echo "${input}"
  else
    echo "${input}-telegram-bridge"
  fi
}

# ── Restart a bridge service ─────────────────────────────────────────
restart_bridge() {
  local ssh_target="$1" svc_mgr="$2" bridge="$3" user="$4"

  case "${svc_mgr}" in
    systemd-user)
      if [[ -z "${ssh_target}" ]]; then
        systemctl --user restart "${bridge}" 2>/dev/null && \
          systemctl --user is-active "${bridge}" --quiet && \
          log "  restarted ${bridge} (systemd --user)" || \
          warn "  ${bridge} not registered as systemd user service"
      else
        run_on "${ssh_target}" "systemctl --user restart ${bridge} 2>/dev/null && echo ok || echo skip" | \
          while read -r line; do
            if [[ "${line}" == "ok" ]]; then
              log "  restarted ${bridge} (systemd --user)"
            else
              warn "  ${bridge} not registered or failed on ${ssh_target}"
            fi
          done
      fi
      ;;
    systemd-root)
      run_on "${ssh_target}" "sudo systemctl restart ${bridge} 2>/dev/null && echo ok || echo skip" | \
        while read -r line; do
          if [[ "${line}" == "ok" ]]; then
            log "  restarted ${bridge} (systemd root)"
          else
            warn "  ${bridge} not registered on ${ssh_target}"
          fi
        done
      ;;
    launchd)
      local plist_label="com.ella.${bridge//-/.}"
      run_on "${ssh_target}" "
        uid=\$(id -u ${user})
        if launchctl print gui/\${uid}/${plist_label} >/dev/null 2>&1; then
          plist_path=\$(find /Users/${user}/Library/LaunchAgents -name '${plist_label}.plist' 2>/dev/null | head -1)
          if [[ -n \"\${plist_path}\" ]]; then
            launchctl bootout gui/\${uid} \"\${plist_path}\" 2>/dev/null || true
            launchctl bootstrap gui/\${uid} \"\${plist_path}\"
            echo ok
          else
            echo no-plist
          fi
        else
          echo not-registered
        fi
      " | while read -r line; do
          case "${line}" in
            ok) log "  restarted ${bridge} (launchd)" ;;
            no-plist) warn "  ${bridge}: plist not found on ${ssh_target}" ;;
            not-registered) warn "  ${bridge} not registered in launchd on ${ssh_target}" ;;
          esac
        done
      ;;
  esac
}

# ── Pull repo once per node ───────────────────────────────────────────
pull_node() {
  local node_name="$1" ssh_target="$2" base="$3"

  log "${CYAN}${node_name}${NC}: git pull"

  if [[ ${DRY_RUN} -eq 1 ]]; then
    log "  [dry-run] would: stash + git pull --ff-only in ${base}"
    return 0
  fi

  run_on "${ssh_target}" "
    cd '${base}'
    if ! git diff --quiet 2>/dev/null; then
      git stash -q 2>&1 && echo 'STASHED local changes'
    fi
    git pull --ff-only 2>&1
  " | while read -r line; do
    log "  git: ${line}"
  done
}

# ── Deploy one bridge (pip + restart) ────────────────────────────────
deploy_bridge() {
  local node_name="$1" ssh_target="$2" base="$3" svc_mgr="$4" bridge="$5" user="$6"
  local svc_dir="${base}/services/${bridge}"

  log "  ${bridge}"

  # Check dir exists
  if ! run_on "${ssh_target}" "test -d '${svc_dir}'" 2>/dev/null; then
    warn "    ${svc_dir} does not exist, skipping"
    return 0
  fi

  if [[ ${DRY_RUN} -eq 1 ]]; then
    log "    [dry-run] would: pip install, restart"
    return 0
  fi

  # Pip install deps
  local venv="${svc_dir}/venv"
  run_on "${ssh_target}" "
    if [[ ! -d '${venv}' ]]; then
      python3 -m venv '${venv}'
    fi
    '${venv}/bin/pip' install -q -r '${svc_dir}/requirements.txt' 2>&1
  " | while read -r line; do
    [[ -n "${line}" ]] && log "    pip: ${line}"
  done

  # Restart
  if [[ ${NO_RESTART} -eq 0 ]]; then
    restart_bridge "${ssh_target}" "${svc_mgr}" "${bridge}" "${user}"
  else
    log "    skipped restart (--no-restart)"
  fi
}

# ── Main loop ────────────────────────────────────────────────────────
RESOLVED_BRIDGE=""
if [[ -n "${FILTER_BRIDGE}" ]]; then
  RESOLVED_BRIDGE="$(resolve_bridge "${FILTER_BRIDGE}")"
fi

SUCCESS=0
FAIL=0
SKIP=0

for entry in "${FLEET[@]}"; do
  IFS='|' read -r name ssh_target os base user svc_mgr bridges <<< "${entry}"

  # Node filter
  if [[ -n "${FILTER_NODE}" ]] && [[ "${name}" != "${FILTER_NODE}" ]]; then
    continue
  fi

  # Check connectivity
  if [[ -n "${ssh_target}" ]]; then
    if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "${ssh_target}" "echo ok" >/dev/null 2>&1; then
      warn "Cannot reach ${name} (${ssh_target}), skipping"
      ((FAIL++)) || true
      continue
    fi
  fi

  # Pull once per node
  pull_node "${name}" "${ssh_target}" "${base}"

  IFS=',' read -ra bridge_list <<< "${bridges}"
  for bridge in "${bridge_list[@]}"; do
    # Bridge filter
    if [[ -n "${RESOLVED_BRIDGE}" ]] && [[ "${bridge}" != "${RESOLVED_BRIDGE}" ]]; then
      continue
    fi

    if deploy_bridge "${name}" "${ssh_target}" "${base}" "${svc_mgr}" "${bridge}" "${user}"; then
      ((SUCCESS++)) || true
    else
      ((FAIL++)) || true
    fi
  done
done

echo ""
log "Done. ${SUCCESS} deployed, ${FAIL} failed/unreachable."
[[ ${FAIL} -eq 0 ]] || exit 1
