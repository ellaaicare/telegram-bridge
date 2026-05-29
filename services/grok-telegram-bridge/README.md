# Grok Build Telegram Bridge

Telegram long-polling bridge for the local `grok` CLI (xAI Grok Build TUI headless mode) using the shared hardened A2A runtime from `services/claude-telegram-bridge`.

> **Status**: Thin wrapper created. Full `HARNESS_CLI=grok` support (invocation + streaming NDJSON parser) is still required in the core `claude-telegram-bridge/main.py`. See the tracking issue for progress.

## What This Adds

- the same strict `/handoff@TargetBot {json}` enforcement used by Codex and Claude
- Independent state, port, and service defaults for a Grok Build bot
- Access to Grok's full tool set (read/edit files, shell, web, subagents, MCP, etc.) via Telegram
- Session continuity via Grok's native `-s` / `--session-id` and `-c` / `--continue` flags

## Requirements

- Python 3.11+ with `venv`
- `grok` CLI installed and authenticated on the target machine (`grok login` or `XAI_API_KEY`)
- a Telegram bot token
- numeric Telegram user ID(s) in `ALLOWED_USER_IDS`

## Configuration

Copy `.env.example` to `.env` and fill in at least:

- `TELEGRAM_BOT_TOKEN` or `GROK_TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_IDS`

Recommended settings (Linux user service example):

```bash
HARNESS_CLI=grok
HARNESS_LABEL="Grok Build"
HARNESS_SERVICE_NAME=grok-telegram-bridge
HARNESS_SESSION_BACKEND=bridge
BRIDGE_PORT=8140
BRIDGE_DEFAULT_FOLDER=${HOME}
BRIDGE_STATE_DIR=${HOME}/.local/state/grok-telegram-bridge
A2A_TRUST_REGISTRY_BOTS=true
A2A_PROGRESS_MODE=status
WATCHDOG_ENABLED=false
```

`/model` will be passed via `grok -m ...` once the core harness supports it.

## Local Setup

```bash
git clone https://github.com/ellaaicare/telegram-bridge.git ~/telegram-bridge
cd ~/telegram-bridge/services/grok-telegram-bridge
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
mkdir -p logs
./start-bridge.sh
```

Health check:

```bash
curl http://127.0.0.1:${BRIDGE_PORT:-8140}/health
```

## Headless Grok Invocation Notes

The bridge will use something like:

```
grok -p "<prompt>" --output-format streaming-json --yolo -s "<session>" --cwd "<folder>"
```

Grok's streaming format (`type: text | thought | end`) requires dedicated parser support in the core harness (tracked in the main issue).

## A2A & Fleet

The A2A protocol, trusted bot registry, and response hardening are shared with the Codex and Claude bridges via the common runtime.

See the root README and `docs/runbooks/telegram-a2a-handoff.md` for handoff usage.
