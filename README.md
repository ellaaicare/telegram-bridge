# Ella Telegram Bridge

Standalone home for Ella's Telegram agent bridges.

This repository contains:

- `services/codex-telegram-bridge`: Telegram bridge for the Codex CLI.
- `services/claude-telegram-bridge`: Telegram bridge for the Claude Code CLI.
- `services/telegram-a2a/agents.json`: shared A2A bot registry.
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

From a host checkout:

```bash
cd services/codex-telegram-bridge
./deploy-fleet.sh
```

For Claude Code:

```bash
cd services/claude-telegram-bridge
./deploy-fleet.sh
```

Use `--install-service` the first time on a host, or `--no-pull` when service files
were already synced into a live directory.
