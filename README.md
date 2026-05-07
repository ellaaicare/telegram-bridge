# Ella Telegram Bridge

Standalone home for Ella's Telegram agent bridges.

This repository contains:

- `services/claude-telegram-bridge`: Telegram bridge for Claude Code CLI (also used as HARNESS for kilo/opencode).
- `services/codex-telegram-bridge`: Telegram bridge for the Codex CLI.
- `services/kilo-telegram-bridge`: Thin wrapper — imports claude bridge with `HARNESS_CLI=kilo`.
- `services/opencode-telegram-bridge`: Thin wrapper — imports claude bridge with `HARNESS_CLI=opencode`.
- `services/telegram-a2a/agents.json`: shared A2A bot registry (example — live registry at `~/.config/telegram-bridge/agents.json`).
- `scripts/deploy-fleet.sh`: Fleet-wide deploy orchestrator.
- `skills/telegram-a2a-handoff/SKILL.md`: A2A handoff protocol guidance.
- `docs/runbooks/telegram-a2a-handoff.md`: operator runbook for bridge handoffs.
- `tests/`: bridge regression tests.

## Versions

- Codex bridge: `0.2.0`, build `a2a-quiet-status-pr685.7681cf5`
- Claude bridge: `3.5.0`, build `a2a-quiet-status-pr685.7681cf5`

Both bridges expose this metadata from `/health` and `/status`.

## Test

```bash
python3 -m py_compile \
  services/codex-telegram-bridge/main.py \
  services/claude-telegram-bridge/main.py

python3 -m pytest \
  tests/codex_telegram_bridge/test_a2a_guidance.py \
  tests/claude_telegram_bridge/test_a2a_guidance.py
```

## Deploy

### Fleet-wide (recommended)

Update all bridges on all fleet nodes in one command:

```bash
./scripts/deploy-fleet.sh              # Pull + pip install + restart on all nodes
./scripts/deploy-fleet.sh --dry-run    # Preview what would happen
./scripts/deploy-fleet.sh --no-restart # Pull + deps only, skip service restart
./scripts/deploy-fleet.sh --node imac  # Target a single node
./scripts/deploy-fleet.sh --bridge claude  # Target a single bridge type everywhere
./scripts/deploy-fleet.sh --list       # Show fleet inventory
```

The script handles git pull (with stash for dirty trees), venv/pip, and service
restarts across systemd-user (Linux) and launchd (macOS).

### Fleet inventory

| Node | SSH | OS | Bridges | Service Manager |
|------|-----|----|---------|-----------------|
| imac | (local) | Linux | claude, kilo, opencode | `systemctl --user` |
| macbookair | admin-macbookair1 | Linux | codex, claude | `systemctl --user` |
| macmini | ellaai@100.76.138.56 | macOS | claude, codex, kilo, opencode | `launchctl` |

### Single-host deploy

Each service also has a local `deploy-fleet.sh` for deploying on the current machine only:

```bash
cd services/claude-telegram-bridge
./deploy-fleet.sh                  # git pull + pip + restart
./deploy-fleet.sh --install-service  # First time: create systemd/launchd service
./deploy-fleet.sh --no-pull        # Skip git pull (for manually synced dirs)
```

### A2A bot registry

The live bot registry with real bot IDs lives at `~/.config/telegram-bridge/agents.json`
on each node. The file at `services/telegram-a2a/agents.json` is an **example template**
with placeholder IDs. Each bridge's `.env` must set:

```
A2A_BOT_REGISTRY_PATH=/path/to/.config/telegram-bridge/agents.json
```
