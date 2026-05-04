# Kilo Code Telegram Bridge

Telegram long-polling bridge for a local `kilo` CLI using the shared hardened
A2A runtime from `services/claude-telegram-bridge`.

## What This Adds

- the same strict `/handoff@TargetBot {json}` enforcement used by Codex and Claude
- `/model` mapped to `kilo run -m <model>`
- `/agent` mapped to `kilo run --agent <name>`
- independent state, port, and service defaults for a Kilo Code bot

## Requirements

- Python 3.11+ with `venv`
- `kilo` installed and authenticated on the target machine
- a Telegram bot token
- numeric Telegram user ID(s) in `ALLOWED_USER_IDS`

## Configuration

Copy `.env.example` to `.env` and fill in at least:

- `TELEGRAM_BOT_TOKEN` or `KILOCODE_TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_IDS`

Recommended A2A settings:

```bash
HARNESS_CLI=kilo
HARNESS_LABEL=Kilo Code
HARNESS_SERVICE_NAME=kilo-telegram-bridge
HARNESS_SESSION_BACKEND=bridge
BRIDGE_PORT=8130
BRIDGE_STATE_DIR=${HOME}/.local/state/kilo-telegram-bridge
A2A_TRUST_REGISTRY_BOTS=true
A2A_PROGRESS_MODE=status
WATCHDOG_ENABLED=false
```

`/model` is translated into `kilo run -m <model>`.
`/agent` is translated into `kilo run --agent <name>`.

## Local Setup

```bash
git clone https://github.com/ellaaicare/telegram-bridge.git ~/telegram-bridge
cd ~/telegram-bridge/services/kilo-telegram-bridge
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
mkdir -p logs
./start-bridge.sh
```

Health check:

```bash
curl http://127.0.0.1:${BRIDGE_PORT:-8130}/health
```

The A2A protocol, trusted bot registry, and response hardening are shared with
the Codex and Claude bridges via the common runtime.
