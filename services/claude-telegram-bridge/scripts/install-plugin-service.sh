#!/usr/bin/env bash
# Install the Telegram plugin systemd user service for @PlatoDevBot.
# Idempotent — safe to re-run.
#
# Reads CLAUDE_TELEGRAM_BOT_TOKEN from the existing bridge .env so the bot
# token isn't duplicated.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_ENV="${ROOT_DIR}/.env"
PLUGIN_CONFIG_DIR="${HOME}/.claude/channels/telegram"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
UNIT_NAME="claude-telegram-plugin.service"
ALLOWED_USER_ID="${ALLOWED_USER_ID:-436052469}"

if [[ ! -f "${BRIDGE_ENV}" ]]; then
  echo "ERROR: ${BRIDGE_ENV} not found — needed to read CLAUDE_TELEGRAM_BOT_TOKEN" >&2
  exit 1
fi

TOKEN=$(grep '^CLAUDE_TELEGRAM_BOT_TOKEN=' "${BRIDGE_ENV}" | cut -d= -f2-)
if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: CLAUDE_TELEGRAM_BOT_TOKEN not set in ${BRIDGE_ENV}" >&2
  exit 1
fi

echo "==> Plugin installed?"
if ! claude plugin list 2>/dev/null | grep -q "telegram@claude-plugins-official"; then
  echo "    Installing telegram@claude-plugins-official..."
  claude plugin install telegram@claude-plugins-official
else
  echo "    Already installed."
fi

echo "==> Writing ${PLUGIN_CONFIG_DIR}/.env"
mkdir -p "${PLUGIN_CONFIG_DIR}"
umask 077
cat > "${PLUGIN_CONFIG_DIR}/.env" <<EOF
TELEGRAM_BOT_TOKEN=${TOKEN}
EOF

echo "==> Writing ${PLUGIN_CONFIG_DIR}/access.json (allowlist for user ${ALLOWED_USER_ID})"
cat > "${PLUGIN_CONFIG_DIR}/access.json" <<EOF
{
  "dmPolicy": "allowlist",
  "allowFrom": [${ALLOWED_USER_ID}],
  "groups": {},
  "mentionPatterns": []
}
EOF

echo "==> Pre-accepting trust dialog for ${HOME} so service restarts don't stall"
python3 - <<PYEOF
import json, os, pathlib
p = pathlib.Path.home() / ".claude.json"
if not p.exists():
    print(f"WARNING: {p} not found; skipping trust pre-accept", file=__import__('sys').stderr)
else:
    d = json.loads(p.read_text())
    proj = d.setdefault("projects", {}).setdefault(str(pathlib.Path.home()), {})
    if not proj.get("hasTrustDialogAccepted"):
        proj["hasTrustDialogAccepted"] = True
        p.write_text(json.dumps(d, indent=2))
        print(f"    Set {pathlib.Path.home()} trust=True")
    else:
        print(f"    {pathlib.Path.home()} already trusted")
PYEOF

echo "==> Linking systemd unit"
mkdir -p "${SYSTEMD_USER_DIR}"
ln -sf "${ROOT_DIR}/systemd/${UNIT_NAME}" "${SYSTEMD_USER_DIR}/${UNIT_NAME}"
systemctl --user daemon-reload

echo
echo "Setup complete. To cut over from the legacy bridge:"
echo
echo "  systemctl --user stop claude-telegram-bridge.service"
echo "  systemctl --user disable claude-telegram-bridge.service"
echo "  systemctl --user enable --now ${UNIT_NAME}"
echo
echo "Then DM @PlatoDevBot — it will reply with a 6-char pairing code."
echo "Attach to the tmux session and complete pairing:"
echo
echo "  tmux attach -t claude-telegram"
echo "  /telegram:access pair <code>"
echo "  (Ctrl-b d to detach)"
