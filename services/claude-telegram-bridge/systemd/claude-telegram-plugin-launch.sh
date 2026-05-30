#!/usr/bin/env bash
# Launcher for the claude-telegram-plugin.service systemd unit.
# Starts a detached tmux session running `claude --channels plugin:telegram@...`.
# Exits cleanly after tmux forks (Type=forking).
set -euo pipefail

SESSION_NAME="${TMUX_SESSION:-claude-telegram}"
START_DIR="${PLUGIN_START_DIR:-${HOME}}"
CLAUDE_BIN="${CLAUDE_BIN:-${HOME}/.npm-global/bin/claude}"
TMUX_BIN="${TMUX_BIN:-/usr/bin/tmux}"
# Each plugin service must own a distinct tmux server so systemd Type=forking
# can track it. With the default shared server, a second `new-session` just
# connects to the first service's server and exits without forking a daemon —
# systemd then sees no main process and tears the session down. Set TMUX_SOCKET
# per bot (the second+ instance) to run on its own server socket via `-L`.
TMUX_SOCKET="${TMUX_SOCKET:-}"
TMUX_LFLAG=()
[ -n "${TMUX_SOCKET}" ] && TMUX_LFLAG=(-L "${TMUX_SOCKET}")
# Expand the array with the `${arr[@]+...}` guard everywhere below: macOS ships
# bash 3.2, where "${arr[@]}" on an EMPTY array under `set -u` throws "unbound
# variable" and kills the script before the session is ever created.
# -c continues the most-recent conversation in START_DIR so reboots/restarts
# preserve context. Set PLUGIN_CONTINUE_FLAG="" (empty) to disable — e.g. a
# bot's first launch in a dir with no prior conversation, where -c errors out.
# Use the `-` (not `:-`) operator so an explicit empty value is honored.
CONTINUE_FLAG="${PLUGIN_CONTINUE_FLAG--c}"

# Force OAuth subscription billing — ignore any inherited ANTHROPIC_API_KEY.
unset ANTHROPIC_API_KEY

if "${TMUX_BIN}" ${TMUX_LFLAG[@]+"${TMUX_LFLAG[@]}"} has-session -t "${SESSION_NAME}" 2>/dev/null; then
  echo "tmux session ${SESSION_NAME} already exists" >&2
  exit 0
fi

MODEL_FLAG="${PLUGIN_MODEL_FLAG:---model opus}"

CMD="${CLAUDE_BIN} ${CONTINUE_FLAG} ${MODEL_FLAG} --channels plugin:telegram@claude-plugins-official --dangerously-skip-permissions"

# Run multiple bots on one host by pointing TELEGRAM_STATE_DIR at a per-bot
# config dir (different token + allowlist). Embed it directly in the command
# string so it survives tmux's shared-server environment — a bare
# `new-session` would otherwise inherit the tmux server's env, not ours.
if [ -n "${TELEGRAM_STATE_DIR:-}" ]; then
  CMD="TELEGRAM_STATE_DIR=${TELEGRAM_STATE_DIR} ${CMD}"
fi

exec "${TMUX_BIN}" ${TMUX_LFLAG[@]+"${TMUX_LFLAG[@]}"} new-session -d -s "${SESSION_NAME}" -c "${START_DIR}" "${CMD}"
