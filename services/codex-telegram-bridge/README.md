# Codex Telegram Bridge

Telegram long-polling bridge for the local Codex CLI.

It lets one or more approved Telegram users send prompts, images, and files to a host machine and have them executed through `codex exec` with per-folder thread continuity.

## Features

- Direct Telegram polling with no inbound webhook port
- Access control via `ALLOWED_USER_IDS`
- Codex execution via `codex exec --json`
- Thread resume via `codex exec resume <thread_id>`
- Per-folder thread memory in `state.json`
- Attachment support for images, documents, voice messages, video, and stickers
- Sequential work queue so only one Codex run is active at a time
- Queued prompts keep the folder and thread they were sent from, even if you switch folders before they start
- Watchdog for obviously stuck command executions
- `GET /health` endpoint for local health checks

## Layout

```text
codex-bridge/
├── .env.example
├── .gitignore
├── README.md
├── launchd/
│   └── com.ella.codex-bridge.plist
├── main.py
├── deploy-fleet.sh
├── requirements.txt
├── start-bridge.sh
└── systemd/
    └── codex-telegram-bridge.service
```

## Requirements

- Python 3.11+ with `venv`
- Codex CLI installed and already authenticated on the target machine
- A Telegram bot token from BotFather
- The numeric Telegram user ID(s) allowed to use the bridge
- `git` access to this repository for pull-based updates

## Security Model

- Only users listed in `ALLOWED_USER_IDS` can interact with the bridge.
- The bridge runs Codex locally on the host. Treat the allowed Telegram account as equivalent to shell-level operator access for the configured workspace.
- `.env` is intentionally not committed.
- HTTP client request logs for Telegram are suppressed so bot tokens are not written into routine logs.

## Configuration

Copy `.env.example` to `.env` and set the values for your host.

### Required

- `CODEX_TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_IDS`

### Common

- `CODEX_DEFAULT_FOLDER`
- `CODEX_BRIDGE_PORT`
- `CODEX_MODEL`
- `CODEX_REASONING_EFFORT`
- `CODEX_SANDBOX`
- `CODEX_ADD_DIRS`
- `ALLOWED_CHAT_IDS`
- `A2A_TRUST_REGISTRY_BOTS`

If you want Codex shell commands to have outbound internet and unrestricted host access, set `CODEX_SANDBOX=danger-full-access`. Keep `workspace-write` if you want the default sandboxed mode.

### Full Environment Reference

```bash
CODEX_TELEGRAM_BOT_TOKEN=
ALLOWED_USER_IDS=
ALLOWED_CHAT_IDS=-1000000000000
A2A_TRUST_REGISTRY_BOTS=true
A2A_BOT_REGISTRY_PATH=

CODEX_TIMEOUT=900
CODEX_MODEL=
CODEX_REASONING_EFFORT=high
CODEX_SANDBOX=workspace-write
CODEX_SKIP_GIT_REPO_CHECK=true
CODEX_FULL_AUTO=false
CODEX_DANGEROUS_BYPASS=false
CODEX_ADD_DIRS=
CODEX_BRIDGE_PORT=8110

CODEX_DEFAULT_FOLDER=${HOME}
CODEX_BRIDGE_STATE_DIR=${HOME}/.local/state/codex-telegram-bridge
CODEX_BRIDGE_LOG_FILE=${PWD}/logs/codex-telegram-bridge.log

WATCHDOG_ENABLED=true
WATCHDOG_COMMAND_TIMEOUT=900
WATCHDOG_DEFAULT_TIMEOUT=180
WATCHDOG_STAGNATION_KILL=1800
```

## Local Setup

```bash
git clone https://github.com/ellaaicare/telegram-bridge.git ~/telegram-bridge
cd ~/telegram-bridge/services/codex-telegram-bridge
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
mkdir -p logs
```

Then edit `.env` with your bot token and allowed user IDs.

## Fleet Helper Script

The repository includes `deploy-fleet.sh` for pull-based updates on fleet hosts.

Typical use:

```bash
cd ~/telegram-bridge/services/codex-telegram-bridge
./deploy-fleet.sh
```

First-time install on macOS:

```bash
./deploy-fleet.sh --install-service
```

This script:

- runs `git pull --ff-only` by default
- creates `venv/` if missing
- installs or updates Python dependencies
- generates host-specific launchd/systemd service files when run with `--install-service`
- restarts the service for the current OS

## Manual Run

```bash
cd ~/telegram-bridge/services/codex-telegram-bridge
./start-bridge.sh
```

Health check:

```bash
curl http://127.0.0.1:${CODEX_BRIDGE_PORT:-8110}/health
```

## macOS Deployment with launchd

1. Clone the repo to the target machine.
2. Create `.env`.
3. Create the venv and install requirements.
4. Generate/install the launchd plist and start the service:

```bash
./deploy-fleet.sh --install-service
```

The install step writes `~/Library/LaunchAgents/com.ella.codex-bridge.plist`
with absolute paths for the current checkout. Do not copy the bundled
`launchd/com.ella.codex-bridge.plist` without editing it; it is only a sample.

5. If you need a custom label or plist destination:

```bash
SERVICE_NAME=com.example.codex-bridge \
PLIST_DST=~/Library/LaunchAgents/com.example.codex-bridge.plist \
./deploy-fleet.sh --install-service
```

6. Inspect status:

```bash
launchctl print gui/$(id -u)/com.ella.codex-bridge
lsof -nP -iTCP:${CODEX_BRIDGE_PORT:-8110} -sTCP:LISTEN
```

7. Restart after updates:

```bash
./deploy-fleet.sh
```

## Linux Deployment with systemd

The repository includes a sample unit at `systemd/codex-telegram-bridge.service`.
For real installs, prefer the fleet script because it generates a unit with the
current checkout path and service user.

Typical install flow:

```bash
SERVICE_USER=$(id -un) ./deploy-fleet.sh --install-service
sudo systemctl status codex-telegram-bridge
```

For custom service names:

```bash
SYSTEMD_UNIT_NAME=codex-telegram-bridge-dev \
SERVICE_USER=$(id -un) \
./deploy-fleet.sh --install-service
```

The bundled systemd unit uses `/opt/ella/codex-telegram-bridge` as an example
path. Edit it before manual installation, or use `deploy-fleet.sh` to generate
a unit from the current checkout.

## Pull-Based Update Workflow

On every host, use the service directory as the pull target:

```bash
cd /path/to/telegram-bridge/services/codex-telegram-bridge
git pull --ff-only
./deploy-fleet.sh --no-pull
```

Or let the helper pull and restart:

```bash
cd /path/to/telegram-bridge/services/codex-telegram-bridge
./deploy-fleet.sh
```

Use `--install-service` once per host, or whenever you move the checkout path.

## A2A Bot Handoff Protocol

For bot-to-bot work in shared Telegram groups, raw bot mentions are not
executable. Bots must use the repo skill:

- `skills/telegram-a2a-handoff/SKILL.md`
- https://github.com/ellaaicare/telegram-bridge/blob/main/skills/telegram-a2a-handoff/SKILL.md

Bot usernames, aliases, and trusted peer IDs are source-controlled here:

- `services/telegram-a2a/agents.json`

By default `A2A_TRUST_REGISTRY_BOTS=true`, so trusted bot IDs from the registry
are added to `ALLOWED_BOT_IDS`. Set `A2A_TRUST_REGISTRY_BOTS=false` and provide
explicit `ALLOWED_BOT_IDS` if a host should only accept one peer. Use
`A2A_BOT_REGISTRY_PATH` only for non-standard checkout paths.

Required envelope:

```text
/handoff@TargetBot {"from":"SourceBot","to":"TargetBot","task_id":"stable-unique-id","ttl":1,"requires_response":true,"type":"task","body":"Do the work here."}
```

Alias examples accepted by the registry:

```text
/handoff@ExampleCodexBot ...
/handoff@iMacCodex ...
/handoff@ExampleClaudeBot ...
/handoff@ExampleClaude2Bot ...
```

The bridge enforces this protocol in both directions:

- inbound bot messages without a valid handoff are rejected with a detailed
  explanation and the skill link
- duplicate `task_id` values are ignored
- `ack` and `status` handoffs are ignored unless `requires_response=true`
- agent responses to bot-originated handoffs must also be structured handoff
  envelopes back to the source bot
- raw or malformed A2A responses are replaced with a protocol rejection instead
  of being posted as normal bot prose

## Telegram Usage

Message the bot and use:

- `/start` or `/help` for command list
- `/folders` to see known workspaces
- `/folder <name>` to switch workspace
- `/new` to start a fresh thread
- `/resume <id|name>` to continue an older thread
- `/status` to inspect queue, folder, model, and active thread
- `/watchdog` to inspect current command monitoring
- `/interrupt` to stop the current Codex run

Regular non-command messages are sent to Codex as work prompts.

Queue behavior:

- Only one Codex run is active at a time.
- Queued prompts are bound to the folder and thread that were selected when you sent them.
- If you queue multiple prompts into a fresh thread before the first one starts, they stay attached to that same future thread.

## State and Logs

- State file: `CODEX_BRIDGE_STATE_DIR/state.json`
- Uvicorn stdout/stderr: `logs/bridge-stdout.log`, `logs/bridge-stderr.log`
- Application log: `CODEX_BRIDGE_LOG_FILE`

## Fleet Update Workflow

If you deploy this via git on multiple hosts:

```bash
./deploy-fleet.sh
```

Use `./deploy-fleet.sh --install-service` after moving the checkout or changing
service names so generated launchd/systemd files point at the correct path.

## Notes

- The bridge tracks Codex thread IDs in its own `state.json`; it does not depend on the Claude bridge session format.
- `/model` maps to Codex CLI `-m`.
- The default sandbox is `workspace-write`.
- For outbound internet from model-run shell commands, set `CODEX_SANDBOX=danger-full-access` and restart the service.
- On the current Codex CLI, resumed sessions do not expose `--sandbox`, so the bridge maps `danger-full-access` to Codex's bypass flag for `resume` runs.
- If you want even fewer guardrails than that, `CODEX_DANGEROUS_BYPASS=true` adds Codex's bypass flag and should only be used in an externally sandboxed environment.
