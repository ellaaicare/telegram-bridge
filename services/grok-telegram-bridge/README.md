# Grok Build Telegram Bridge

Telegram long-polling bridge for the local `grok` CLI (xAI Grok Build TUI headless mode) using the shared hardened A2A runtime from `services/claude-telegram-bridge`.

> **Status**: Supported by the shared bridge runtime with Grok Build
> streaming JSON, named model/reasoning controls, MCP dispatch ingress, and
> resumable long-lived sessions.

## What This Adds

- the same strict `/handoff@TargetBot {json}` enforcement used by Codex and Claude
- Independent state, port, and service defaults for a Grok Build bot
- Access to Grok's full tool set (read/edit files, shell, web, subagents, MCP, etc.) via Telegram
- Session continuity via Grok's native `--resume <session-id>` behavior
- Configurable Grok sandbox profile (`workspace` by default)
- Dual delivery for MCP-dispatched jobs: progress/final output in Telegram and
  durable results for the calling MCP client

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

`/model` and MCP dispatch model overrides are passed with `grok -m`. Reasoning
effort is passed with `--effort`; supported dispatcher values are `low`,
`medium`, `high`, `xhigh`, and `max`.

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

For a new session, the bridge uses:

```
grok --no-auto-update -p "<prompt>" --output-format streaming-json \
  --always-approve --model grok-4.5 --effort high \
  --sandbox workspace --cwd "<folder>"
```

For an existing session it adds `--resume "<session-id>"`. Do not substitute
`--session-id`/`-s`: current Grok Build uses that option only to assign a UUID
to a *new* conversation and rejects existing session IDs.

The streaming parser handles `text`, `thought`, `end`, and `error` events.
Thoughts are reduced to throttled progress notices rather than being copied
verbatim into the final response.

## Authentication

On a headless host, prefer the browser-independent device flow:

```bash
grok login --device-auth
grok models
```

The human completes the displayed xAI URL/code. Grok stores the credential in
`~/.grok/auth.json`; keep it mode `0600` and never copy it into this repository,
an employee manifest, a skill, or a Telegram message. `XAI_API_KEY` is also
supported for non-interactive service accounts.

## Skills, Plugins, and MCP

Grok Build discovers project and user instructions, skills, plugins, and MCP
servers from the selected working directory. Audit the effective configuration
before deployment:

```bash
grok inspect --json --cwd "${HOME}/ai-company"
```

Marketplace plugins are executable supply-chain inputs. Pin and audit them
before installation; xAI does not verify third-party marketplace plugins.

## A2A & Fleet

The A2A protocol, trusted bot registry, and response hardening are shared with the Codex and Claude bridges via the common runtime.

See the root README and `docs/runbooks/telegram-a2a-handoff.md` for handoff usage.
