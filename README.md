# Ella Telegram Bridge

Standalone home for Ella's Telegram agent bridges.

This repository contains:

- `services/claude-telegram-bridge`: Telegram bridge for Claude Code CLI (also used as HARNESS for kilo/opencode/grok).
- `services/codex-telegram-bridge`: Telegram bridge for the Codex CLI.
- `services/kilo-telegram-bridge`: Thin wrapper — imports claude bridge with `HARNESS_CLI=kilo`.
- `services/opencode-telegram-bridge`: Thin wrapper — imports claude bridge with `HARNESS_CLI=opencode`.
- `services/grok-telegram-bridge`: Thin wrapper — imports claude bridge with `HARNESS_CLI=grok`.
- `services/telegram-a2a/agents.json`: shared A2A bot registry (example — live registry at `~/.config/telegram-bridge/agents.json`).
- `scripts/deploy-fleet.sh`: Fleet-wide deploy orchestrator.
- `skills/telegram-a2a-handoff/SKILL.md`: A2A handoff protocol guidance.
- `skills/project-checkpoint/`: provider-neutral save/restore contract for durable agent context.
- `docs/runbooks/telegram-a2a-handoff.md`: operator runbook for bridge handoffs.
- `tests/`: bridge regression tests.

## Versions

- Codex bridge: `0.2.0`, build `a2a-quiet-status-pr685.7681cf5`
- Claude bridge: `3.5.0`, build `a2a-quiet-status-pr685.7681cf5`
- Grok bridge: Full support via `HARNESS_CLI=grok` (see `services/grok-telegram-bridge/`).

The main bridges expose version/build metadata from `/health` and `/status`.

## Durable Checkpoints

The Codex and Claude-family bridges share one source-controlled `project-checkpoint`
skill. Fleet deployment links it into both `~/.codex/skills/project-checkpoint`
and `~/.claude/skills/project-checkpoint`; do not maintain divergent copies in
agent home directories.

Bridge commands:

```text
/checkpoint [note]          save the current project/session context
/new [label]                save, validate, then rotate the session
/new force [label]          rotate without saving (explicit recovery bypass)
/compact                    save, validate, then rotate the session
/resume checkpoint          start fresh and load the latest project capsule
```

After guarded `/new`, the first queued prompt automatically loads the committed
capsule. Checkpoints live under
`~/.local/share/agent-checkpoints/projects/<project-key>/` by default. Set
`PROJECT_CHECKPOINT_STORE_ROOT` to move this protected user-local store, or set
`PROJECT_CHECKPOINT_ENABLED=false` to restore legacy immediate rotation.

Capsules never replace GitHub issues, pull requests, runbooks, or committed docs.
They preserve session-specific decisions and learned context, then tell the next
agent which authoritative records and mutable runtime facts to revalidate.

See [`docs/runbooks/project-checkpoint.md`](docs/runbooks/project-checkpoint.md) for
the lifecycle, storage, security, failure handling, and staged rollout procedure.

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
| imac | (local) | Linux | claude, kilo, opencode, **grok** | `systemctl --user` |
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

**Important:** This is a public repository. The file `services/telegram-a2a/agents.json` is only an **example template** with fake IDs. It must never contain real bot IDs, real Telegram group chat IDs, or any production data.

The authoritative registry lives on each machine at:

```
~/.config/telegram-bridge/agents.json
```

Each bridge's `.env` must point to the live file:

```
A2A_BOT_REGISTRY_PATH=/path/to/.config/telegram-bridge/agents.json
```

When adding a new bot (for example, a new Grok Build instance), add an entry like this to the live registry on the relevant nodes:

```json
{
  "canonical": "iMacGrok",
  "username": "ella_grok_bot",
  "id": 8960208722,
  "aliases": ["iMacGrok", "EllaGrokBot", "@ella_grok_bot"],
  "groups": ["ella-dev"],
  "harness": "grok",
  "trusted": true
}
```

After editing the live registry, restart the affected bridge(s) so they pick up the change.

See also `docs/runbooks/telegram-a2a-handoff.md` and `skills/telegram-a2a-handoff/SKILL.md` for operational details.
