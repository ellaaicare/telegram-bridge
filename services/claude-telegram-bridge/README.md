# Claude Code Telegram Bridge

Telegram long-polling bridge for the local Claude Code CLI.

This service is the Claude Code sibling to `services/codex-telegram-bridge`.
It keeps the same A2A bot handoff behavior so teams can run Codex and Claude
bridges side by side without manually porting patches between machines.

## Features

- Direct Telegram polling with no inbound webhook port
- Access control via `ALLOWED_USER_IDS`, `ALLOWED_BOT_IDS`, and `ALLOWED_CHAT_IDS`
- Claude Code execution through the local `claude` CLI
- Per-folder Claude session continuity in `state.json`
- Attachment support for images, documents, voice messages, video, and stickers
- Sequential work queue so only one Claude run is active at a time
- Watchdog for stuck Claude tool calls
- Optional job dispatch to configured local or SSH nodes
- `GET /health` endpoint for local health checks
- Shared A2A bot handoff protocol with the Codex bridge

## Requirements

- Python 3.11+ with `venv`
- Claude Code CLI installed and authenticated on the target machine
- A Telegram bot token from BotFather
- Numeric Telegram user ID(s) allowed to control the bridge
- `git` access to this repository for pull-based updates

## Configuration

Copy `.env.example` to `.env` and fill in at least:

- `CLAUDE_TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_IDS`

For bot-to-bot group coordination, also set:

- `ALLOWED_CHAT_IDS`
- `A2A_TRUST_REGISTRY_BOTS`

Common runtime settings:

```bash
CLAUDE_TIMEOUT=300
BRIDGE_PORT=8100
BRIDGE_DEFAULT_FOLDER=${HOME}
BRIDGE_STATE_DIR=${HOME}/.local/state/claude-telegram-bridge
A2A_TRUST_REGISTRY_BOTS=true
A2A_BOT_REGISTRY_PATH=
```

Optional dispatch nodes can be configured with `DISPATCH_NODES_JSON`:

```bash
DISPATCH_NODES_JSON={"local":{"ssh":null,"claude":"claude","cwd":null},"builder":{"ssh":"dev@example","claude":"/usr/local/bin/claude","cwd":"/srv/ella-ai"}}
```

## Local Setup

```bash
git clone https://github.com/ellaaicare/telegram-bridge.git ~/telegram-bridge
cd ~/telegram-bridge/services/claude-telegram-bridge
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
mkdir -p logs
```

Edit `.env`, then run:

```bash
./start-bridge.sh
```

Health check:

```bash
curl http://127.0.0.1:${BRIDGE_PORT:-8100}/health
```

## Pull-Based Updates

On every host:

```bash
cd /path/to/telegram-bridge/services/claude-telegram-bridge
git pull --ff-only
./deploy-fleet.sh --no-pull
```

Or let the helper pull and restart:

```bash
cd /path/to/telegram-bridge/services/claude-telegram-bridge
./deploy-fleet.sh
```

## macOS launchd

First-time install:

```bash
./deploy-fleet.sh --install-service
```

The install step writes `~/Library/LaunchAgents/com.ella.claude-bridge.plist`
with absolute paths for the current checkout. Do not copy the bundled sample
plist without editing it.

Custom label or destination:

```bash
SERVICE_NAME=com.example.claude-bridge \
PLIST_DST=~/Library/LaunchAgents/com.example.claude-bridge.plist \
./deploy-fleet.sh --install-service
```

Restart after updates:

```bash
./deploy-fleet.sh
```

## Linux systemd

First-time install:

```bash
SERVICE_USER=$(id -un) ./deploy-fleet.sh --install-service
```

Custom service name:

```bash
SYSTEMD_UNIT_NAME=claude-telegram-bridge-dev \
SERVICE_USER=$(id -un) \
./deploy-fleet.sh --install-service
```

The bundled unit uses `/opt/ella/claude-telegram-bridge` as an example path.
Use `deploy-fleet.sh` to generate a real unit from the current checkout.

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

- inbound bot messages without a valid handoff are rejected with the skill link
- duplicate `task_id` values are ignored
- `ack` and `status` handoffs are ignored unless `requires_response=true`
- agent responses to bot-originated handoffs must also be structured handoff
  envelopes back to the source bot
- raw or malformed A2A responses are replaced with a protocol rejection instead
  of being posted as normal bot prose

## Side-By-Side With Codex

Use distinct Telegram bot tokens, ports, and service labels:

```bash
# Codex bridge
cd /path/to/telegram-bridge/services/codex-telegram-bridge
CODEX_BRIDGE_PORT=8110 ./deploy-fleet.sh --install-service

# Claude bridge
cd /path/to/telegram-bridge/services/claude-telegram-bridge
BRIDGE_PORT=8100 ./deploy-fleet.sh --install-service
```

For A2A groups, set the shared group in `ALLOWED_CHAT_IDS`. The repo registry
provides the trusted peer bot IDs unless `A2A_TRUST_REGISTRY_BOTS=false`.
