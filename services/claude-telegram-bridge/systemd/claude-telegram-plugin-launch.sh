#!/usr/bin/env bash
# Launcher for the claude-telegram-plugin.service systemd unit.
# Starts a detached tmux session running `claude --channels plugin:telegram@...`.
# Exits cleanly after tmux forks (Type=forking).
set -euo pipefail

SESSION_NAME="${TMUX_SESSION:-claude-telegram}"
START_DIR="${PLUGIN_START_DIR:-${HOME}}"
CLAUDE_BIN="${CLAUDE_BIN:-${HOME}/.npm-global/bin/claude}"
TMUX_BIN="${TMUX_BIN:-/usr/bin/tmux}"

# Force OAuth subscription billing — ignore any inherited ANTHROPIC_API_KEY.
unset ANTHROPIC_API_KEY

if "${TMUX_BIN}" has-session -t "${SESSION_NAME}" 2>/dev/null; then
  echo "tmux session ${SESSION_NAME} already exists" >&2
  exit 0
fi

exec "${TMUX_BIN}" new-session -d -s "${SESSION_NAME}" -c "${START_DIR}" \
  "${CLAUDE_BIN} --channels plugin:telegram@claude-plugins-official --dangerously-skip-permissions"
