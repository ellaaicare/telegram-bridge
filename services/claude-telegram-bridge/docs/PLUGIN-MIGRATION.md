# Telegram Bridge → Official Plugin Migration

Anthropic's June 15 2026 change splits Claude subscriptions into two pools:
interactive (unchanged) and **programmatic** (capped monthly credit bucket
at full API rates). Spawning `claude --print -p ...` per Telegram message,
as the bridge does today, moves the Claude Code path into the programmatic
bucket.

This migration replaces the `HARNESS_CLI=claude` path with a persistent
interactive `claude` session running the official Telegram plugin
(`telegram@claude-plugins-official`). The bridge's `kilo`, `opencode`, and
crypto-trading instances are unaffected and continue to run.

## Scope

| Service | Before | After |
|---|---|---|
| `claude-telegram-bridge.service` | spawns `claude -p` per message | **stopped/disabled** |
| `claude-telegram-plugin.service` | (did not exist) | **new** — runs `claude --channels plugin:telegram@...` in a tmux session (@PlatoDevBot) |
| `kilo-telegram-bridge.service` | unchanged | unchanged |
| `opencode-telegram-bridge.service` | unchanged | unchanged |
| `claude-telegram-bridge-crypto.service` | spawned `claude -p` per message | **stopped/disabled** (2026-05-29) |
| `claude-telegram-plugin-crypto.service` | (did not exist) | **new** (2026-05-29) — second plugin instance for @CryptoPlatoBot, see "Running a second bot" below |

> **2026-05-29 update:** `claude -p` (`--print`) is deprecated for Claude Code —
> all Telegram-fronted Claude sessions now use the official plugin. The crypto
> trading bot was migrated off its `claude -p` Python bridge to a second plugin
> instance. Both sessions are pinned to the latest Opus via `--model opus`.

## Files

| Path | Purpose |
|---|---|
| `~/.claude/channels/telegram/.env` | Plugin's bot token (mode `0600`) |
| `~/.claude/channels/telegram/access.json` | DM allowlist (replaces bridge's `ALLOWED_USER_IDS`) |
| `~/.config/systemd/user/claude-telegram-plugin.service` | Systemd user unit (symlink to repo copy) |
| `services/claude-telegram-bridge/systemd/claude-telegram-plugin.service` | Repo copy (source of truth) |

## Cutover steps (idempotent)

```bash
# Prereqs: plugin installed once via `claude plugin install telegram@claude-plugins-official`
mkdir -p ~/.claude/channels/telegram
# Token + allowlist written from existing bridge .env (see scripts/install-plugin-service.sh)

# Install systemd unit
ln -sf "$(pwd)/services/claude-telegram-bridge/systemd/claude-telegram-plugin.service" \
  ~/.config/systemd/user/claude-telegram-plugin.service
systemctl --user daemon-reload

# Cutover
systemctl --user stop claude-telegram-bridge.service
systemctl --user disable claude-telegram-bridge.service
systemctl --user enable --now claude-telegram-plugin.service

# Note: the installer pre-accepts the "Trust this folder?" dialog for $HOME
# in ~/.claude.json. Without this, every service restart blocks the session
# until someone attaches to tmux and types "1".

# Pair (one-time)
# IMPORTANT: dmPolicy starts as "pairing" — even users listed in allowFrom
# must complete pairing before the plugin routes messages to Claude.
#
# 1. Send any DM to @PlatoDevBot from Telegram → bot replies with 6-char pairing code
# 2. Attach to tmux: tmux attach -t claude-telegram
# 3. Type: /telegram:access pair <code>
# 4. Detach: Ctrl-b d
# 5. Switch policy to allowlist:
#    jq '.dmPolicy = "allowlist"' ~/.claude/channels/telegram/access.json | sponge ~/.claude/channels/telegram/access.json
#    (no service restart needed — server re-reads access.json on every inbound message)
```

## What is lost (and how to recover later)

| Bridge feature | Status | Recovery path |
|---|---|---|
| `/folders`, `/folder <name>` switching | Lost | Tell Claude `cd ~/some-repo` inline |
| `/sessions`, `/resume <id>` per-folder | Lost | `/clear` to fork; one session per tmux |
| `/new`, `/rename`, `/save`, `/history` | Lost | Rebuild as a custom plugin if needed |
| `/dispatch`, `/jobs`, `/job N`, `/job-kill <N>` | **To port** | Shell scripts the agent invokes; or custom slash commands |
| Watchdog (stuck-tool detection) | Lost | Plugin runs in interactive mode; stuck tools just hang |
| A2A multi-bot routing | Unaffected for Kilo/OpenCode | Plugin path: bot replies directly |
| `--append-system-prompt` guardrail vs systemctl | Replaced | Section in `~/CLAUDE.md` instead |

## Model pinning (Opus)

The launch script appends `--model opus` to the `claude` command (override via
the `PLUGIN_MODEL_FLAG` env in the unit). The `opus` alias always resolves to
the latest Opus, so the session auto-upgrades on restart (e.g. picked up Opus
4.8 automatically). Pin a specific version with `Environment=PLUGIN_MODEL_FLAG=--model claude-opus-4-8`
if you ever need to. Requires a Claude **Max** OAuth credential — Opus is not
available on Pro; the CLI silently falls back to Sonnet otherwise. Confirm with
`tmux [-L <socket>] capture-pane -t <session> -p | grep -E 'Opus|Sonnet|Max|Pro'`.

## Running a second bot (multi-instance) — @CryptoPlatoBot

The plugin reads all its state from `TELEGRAM_STATE_DIR` (default
`~/.claude/channels/telegram`). Point it at a per-bot directory to run a second
bot with its own token + allowlist. Each instance **must** own a distinct tmux
server socket (`TMUX_SOCKET` → `tmux -L`), or systemd `Type=forking` attaches to
the first instance's server, finds no forked daemon, and tears the session down.

Per-bot env (set in the systemd unit):

| Env var | @PlatoDevBot (default) | @CryptoPlatoBot |
|---|---|---|
| `TELEGRAM_STATE_DIR` | `~/.claude/channels/telegram` | `~/.claude/channels/telegram-crypto` |
| `TMUX_SESSION` | `claude-telegram` | `claude-telegram-crypto` |
| `TMUX_SOCKET` | (unset → default server) | `claude-telegram-crypto` |
| `PLUGIN_START_DIR` | `~/.telegram-bridge-workspaces/claude` | `~/dev/polybot` |
| `PLUGIN_CONTINUE_FLAG` | (unset → `-c`) | `` (empty → fresh; no prior convo to resume) |

Channel config (mode `0600` on the `.env`):

```bash
mkdir -p ~/.claude/channels/telegram-crypto
printf 'TELEGRAM_BOT_TOKEN=%s\n' "<crypto bot token>" > ~/.claude/channels/telegram-crypto/.env
chmod 600 ~/.claude/channels/telegram-crypto/.env
cat > ~/.claude/channels/telegram-crypto/access.json <<'JSON'
{ "dmPolicy": "allowlist", "allowFrom": ["436052469"], "groups": {}, "pending": {}, "mentionPatterns": [] }
JSON
```

> **No pairing dance needed.** With `dmPolicy: "allowlist"` and the user's ID in
> `allowFrom`, the plugin delivers directly (server.ts gate: allowlisted sender
> → deliver). Pairing is only required when starting from the `pairing` policy.

Also pre-accept the trust dialog for the new working dir so restarts don't stall:

```bash
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home()/".claude.json"
d = json.loads(p.read_text())
d.setdefault("projects", {}).setdefault("/home/letta/dev/polybot", {})["hasTrustDialogAccepted"] = True
p.write_text(json.dumps(d, indent=2))
PY
```

Cutover:

```bash
ln -sf "$(pwd)/services/claude-telegram-bridge/systemd/claude-telegram-plugin-crypto.service" \
  ~/.config/systemd/user/claude-telegram-plugin-crypto.service
systemctl --user daemon-reload
systemctl --user stop claude-telegram-bridge-crypto.service
systemctl --user disable claude-telegram-bridge-crypto.service
systemctl --user enable --now claude-telegram-plugin-crypto.service
# Verify: tmux -L claude-telegram-crypto capture-pane -t claude-telegram-crypto -p | head
```

## Rollback

PlatoDevBot:
```bash
systemctl --user stop claude-telegram-plugin.service
systemctl --user disable claude-telegram-plugin.service
systemctl --user enable --now claude-telegram-bridge.service
```

CryptoPlatoBot:
```bash
systemctl --user stop claude-telegram-plugin-crypto.service
systemctl --user disable claude-telegram-plugin-crypto.service
systemctl --user enable --now claude-telegram-bridge-crypto.service
```

The bot token, allowlist, and pairing state in `~/.claude/channels/telegram*/`
can stay in place — rollback only flips which service polls Telegram.
