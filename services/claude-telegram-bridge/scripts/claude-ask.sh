#!/usr/bin/env bash
# claude-ask.sh — Lightweight one-shot question to Claude Code on a fleet node
#
# Usage:
#   claude-ask.sh <node> <question> [cwd]
#   claude-ask.sh <node> --file <prompt-file> [cwd]
#
# Examples:
#   claude-ask.sh admin-macbookair1 "What were the last RN1 backtest results?" /home/plato/dev/polybot
#   claude-ask.sh local "Summarize the equity curves in reports/"
#   claude-ask.sh vultr "Check omi backend health"
#
# Nodes: local, admin-macbookair1, mac-mini, vultr
#
# Returns: Claude's response on stdout. Non-zero exit on failure.

set -euo pipefail

# Fleet node config
declare -A NODE_SSH=(
    [local]=""
    [admin-macbookair1]="plato@100.67.113.120"
    [mac-mini]="ellaai@100.76.138.56"
    [vultr]="plato@100.101.168.91"
)
declare -A NODE_CWD=(
    [local]="$HOME"
    [admin-macbookair1]="/home/plato/ella-dev/ella-ai"
    [mac-mini]="/Users/ellaai"
    [vultr]="/home/plato"
)

node="${1:?Usage: claude-ask.sh <node> <question|--file path> [cwd]}"
shift

if [ "${1:-}" = "--file" ]; then
    prompt_file="${2:?Missing prompt file path}"
    question=$(cat "$prompt_file")
    shift 2
else
    question="${1:?Missing question}"
    shift
fi

cwd="${1:-${NODE_CWD[$node]:-$HOME}}"

if [ -z "${NODE_SSH[$node]+x}" ]; then
    echo "ERROR: Unknown node: $node" >&2
    echo "Available: ${!NODE_SSH[*]}" >&2
    exit 1
fi

ssh_target="${NODE_SSH[$node]}"

# Use file-based prompt transfer to avoid quoting issues
tmp_id="ask-$$-$(date +%s)"
prompt_path="/tmp/claude-ask/${tmp_id}.prompt"
script_path="/tmp/claude-ask/${tmp_id}.sh"

# Write runner script
cat > "/tmp/${tmp_id}.sh" << SCRIPT_EOF
#!/usr/bin/env bash
set -e
# Source environment
[ -f "\$HOME/.bashrc" ] && source "\$HOME/.bashrc" 2>/dev/null || true
[ -f "\$HOME/.openclaw/.env" ] && set -a && source "\$HOME/.openclaw/.env" && set +a 2>/dev/null || true
# Find latest nvm node version
_nvm_latest=""
for d in "\$HOME/.nvm/versions/node"/*/bin; do [ -d "\$d" ] && _nvm_latest="\$d"; done
[ -n "\${_nvm_latest:-}" ] && export PATH="\$_nvm_latest:\$PATH"
# macOS paths
[ -d "/usr/local/bin" ] && export PATH="/usr/local/bin:\$PATH"
# Verify claude exists
if ! command -v claude &>/dev/null; then
    echo "ERROR: claude not found in PATH" >&2
    exit 1
fi
cd $(printf '%q' "$cwd")
prompt=\$(cat "$prompt_path")
claude -p "\$prompt" --dangerously-skip-permissions --output-format text --max-turns 20 2>/dev/null
# Cleanup
rm -f "$prompt_path" "$script_path"
rmdir /tmp/claude-ask 2>/dev/null || true
SCRIPT_EOF
chmod +x "/tmp/${tmp_id}.sh"

# Write prompt to file
mkdir -p /tmp/claude-ask
echo "$question" > "/tmp/${tmp_id}.prompt"

if [ -n "$ssh_target" ]; then
    # Remote: transfer files and execute
    ssh "$ssh_target" "mkdir -p /tmp/claude-ask"
    scp -q "/tmp/${tmp_id}.prompt" "${ssh_target}:${prompt_path}"
    scp -q "/tmp/${tmp_id}.sh" "${ssh_target}:${script_path}"
    ssh "$ssh_target" "bash ${script_path}"
    # Local cleanup
    rm -f "/tmp/${tmp_id}.prompt" "/tmp/${tmp_id}.sh"
else
    # Local: just run it
    mv "/tmp/${tmp_id}.prompt" "$prompt_path"
    mv "/tmp/${tmp_id}.sh" "$script_path"
    bash "$script_path"
fi
