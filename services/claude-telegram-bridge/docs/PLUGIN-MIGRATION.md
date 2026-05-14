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
| `claude-telegram-plugin.service` | (did not exist) | **new** — runs `claude --channels plugin:telegram@...` in a tmux session |
| `kilo-telegram-bridge.service` | unchanged | unchanged |
| `opencode-telegram-bridge.service` | unchanged | unchanged |
| `claude-telegram-bridge-crypto.service` | unchanged | unchanged |

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

## Rollback

```bash
systemctl --user stop claude-telegram-plugin.service
systemctl --user disable claude-telegram-plugin.service
systemctl --user enable --now claude-telegram-bridge.service
```

The bot token, allowlist, and pairing state in `~/.claude/channels/telegram/`
can stay in place — rollback only flips which service polls Telegram.
