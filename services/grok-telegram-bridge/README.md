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
- Configurable Grok sandbox profile (`off` for the general-purpose Mini employee)
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
BRIDGE_DEFAULT_FOLDER=${HOME}/dev
BRIDGE_STATE_DIR=${HOME}/.local/state/grok-telegram-bridge
GROK_BRIDGE_SANDBOX=off
ALLOWED_CHAT_IDS=-1
A2A_TRUST_REGISTRY_BOTS=false
A2A_PROGRESS_MODE=status
WATCHDOG_ENABLED=false
```

`/model` and MCP dispatch model overrides are passed with `grok -m`. Reasoning
effort is passed with `--effort`; supported dispatcher values are `low`,
`medium`, `high`, `xhigh`, and `max`.

The checked-in Grok configuration is private-only: `ALLOWED_USER_IDS` and
`ALLOWED_SENDER_IDS` must contain the owner's numeric Telegram ID,
`ALLOWED_CHAT_IDS=-1` rejects all groups, `ALLOWED_BOT_IDS` remains empty, and
`A2A_TRUST_REGISTRY_BOTS=false` prevents registry bots from becoming accepted
senders. The token-authenticated loopback MCP dispatcher remains available and
can still deliver progress and final results to the owner's private chat.

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
  --sandbox off --cwd "<folder>"
```

For an existing session it adds `--resume "<session-id>"`. Do not substitute
`--session-id`/`-s`: current Grok Build uses that option only to assign a UUID
to a *new* conversation and rejects existing session IDs.

The streaming parser handles `text`, `thought`, `end`, and `error` events.
Thoughts are reduced to throttled progress notices rather than being copied
verbatim into the final response.

## Repository and Worktree Layout

On the Mac Mini, launch the general-purpose employee from `${HOME}/dev`. That
is the stable repository namespace; individual repositories may be symlinks
into an external volume.

For any write task, create or select an isolated worktree at:

```text
${HOME}/worktrees/<repo>-minigrok-<task>
```

`${HOME}/worktrees` should itself resolve to the external worktree volume.
Do not create repositories or worktrees inside `${HOME}/ai-company`, write
directly to a shared primary checkout without explicit authorization, or reuse
another employee's active worktree.

The general-purpose employee uses `GROK_BRIDGE_SANDBOX=off` because repository
and worktree symlinks can resolve outside `${HOME}/dev`; Grok's workspace
sandbox would block those external targets. This setting is appropriate only
for the private owner-only bridge on a trusted host. A bridge pinned to one
real, non-symlinked worktree can instead use `workspace`.

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
(cd "${HOME}/dev" && grok inspect --json)
```

Marketplace plugins are executable supply-chain inputs. Pin and audit them
before installation; xAI does not verify third-party marketplace plugins.

If the Grok employee should be reachable from MCP before a dedicated Telegram
bot token has been provisioned, set `TELEGRAM_POLLING_ENABLED=false`. The
loopback `/dispatch` ingress and durable MCP job ledger remain active, while
Telegram polling and delivery stay off. After installing a valid bot token,
set it back to `true`, restart the bridge, and enable Telegram notification in
the employee manifest.

## A2A & Fleet

The A2A protocol, trusted bot registry, and response hardening are shared with the Codex and Claude bridges via the common runtime.

See the root README and `docs/runbooks/telegram-a2a-handoff.md` for handoff usage.
