# Telegram A2A Handoff Protocol

Status: MVP bridge protocol for Ella internal bots.

## Goal

Telegram group chats are useful for human-visible coordination, but raw bot mentions can create loops. Ella bots must not treat every `@BotName` mention from another bot as executable work.

Use a strict A2A-style handoff envelope for bot-to-bot work and keep raw mentions for humans only.

## Security Rules

- Private chats are accepted only from `ALLOWED_USER_IDS`.
- Group human messages are accepted only from `ALLOWED_USER_IDS` and only when they mention the target bot or reply to that bot.
- Group bot messages are accepted only from `ALLOWED_BOT_IDS`.
- Group bot messages are executable only when they start with `/handoff@TargetBot`.
- If `ALLOWED_CHAT_IDS` is set, group messages from any other chat are rejected.
- Bot status, acknowledgement, and “standing by” messages are ignored unless wrapped in a valid handoff with `requires_response=true`.

## Handoff Format

```text
/handoff@TargetBot {"from":"SourceBot","to":"TargetBot","task_id":"unique-id","ttl":1,"requires_response":true,"type":"task","body":"Do the work here."}
```

Required fields:

- `from`: source bot name.
- `to`: target bot name.
- `task_id`: stable unique ID for idempotency.
- `ttl`: hop limit. Use `1` by default.
- `requires_response`: whether the target should produce a reply.
- `type`: `task`, `status`, `ack`, or `result`.
- `body`: concise task text.

## Shared Bot Registry

Bot IDs, canonical names, and aliases live in git:

```text
services/telegram-a2a/agents.json
```

Every distributed bridge should pull the repo and use this file instead of
hard-coding local paths or manually copying bot maps between servers. The Codex
and Claude bridges load the registry from the repo by default. Set
`A2A_BOT_REGISTRY_PATH` only when a host intentionally keeps the registry in a
non-standard checkout location.

Current aliases:

- `MacMiniCodex`, `ExampleCodexBot`, `@ExampleCodexBot`
- `iMacCodex`, `ExampleCodex2Bot`, `@ExampleCodex2Bot`
- `MacMiniClaude`, `ExampleClaudeBot`, `@ExampleClaudeBot`
- `iMacClaude`, `ExampleClaude2Bot`, `@ExampleClaude2Bot`
- `ExampleUtilityBot`, `@ExampleUtilityBot`

The bridges accept any alias for the local bot in `/handoff@TargetBot`, but
rejection messages show the canonical Telegram username to keep examples stable.

## Loop Prevention

- The bridge ignores duplicate `task_id` values.
- The bridge ignores `ttl <= 0`.
- The bridge ignores `ack` and `status` unless `requires_response=true`.
- The bridge does not execute raw bot-authored prose even if it contains a target bot mention.
- Bots should never mention another bot in normal status prose. Use `/handoff` only.
- Non-target bridges silently ignore valid handoffs addressed to other bots (no A2A guidance noise).

- The bridge ignores duplicate `task_id` values.
- The bridge ignores `ttl <= 0`.
- The bridge ignores `ack` and `status` unless `requires_response=true`.
- The bridge does not execute raw bot-authored prose even if it contains a target bot mention.
- Bots should never mention another bot in normal status prose. Use `/handoff` only.

## Examples

MacMiniClaude asking MacMiniCodex to review:

```text
/handoff@ExampleCodexBot {"from":"ExampleClaudeBot","to":"ExampleCodexBot","task_id":"663-pr101-risk-review","ttl":1,"requires_response":true,"type":"task","body":"Review PR #101 hardware validation checklist and reply with risks only."}
```

MacMiniCodex sending a non-actionable status to MacMiniClaude:

```text
/handoff@ExampleClaudeBot {"from":"ExampleCodexBot","to":"ExampleClaudeBot","task_id":"663-pr101-risk-review-status","ttl":1,"requires_response":false,"type":"status","body":"Review complete; no action requested."}
```

The second example should be ignored by the bridge because it is a status with `requires_response=false`.

## Operational Defaults

Example group:

- Group chat ID: `-1000000000000`
- Human admin ID: set per host in `.env`
- `@ExampleCodexBot`: `1000000001`
- `@ExampleClaudeBot`: `1000000003`

Recommended runtime configuration:

```env
ALLOWED_USER_IDS=
ALLOWED_SENDER_IDS=
ALLOWED_CHAT_IDS=-1000000000000
A2A_TRUST_REGISTRY_BOTS=true
```

`A2A_TRUST_REGISTRY_BOTS=true` is the default. It adds trusted bot IDs from
`services/telegram-a2a/agents.json` to `ALLOWED_BOT_IDS`. Use
`A2A_TRUST_REGISTRY_BOTS=false` and explicit `ALLOWED_BOT_IDS` for a locked-down
single-peer deployment.

## Skill Text For Agents

When sending work to another Ella bot in Telegram, never use raw natural-language mentions for bot-to-bot delegation.

Use only `/handoff@TargetBot {json}`.

Never respond to `ack` or `status` handoffs unless `requires_response=true`.

Never include another bot mention in normal prose.

Never send “standing by,” “acknowledged,” or “waiting” to another bot.

Use `ttl=1` unless explicitly coordinating a multi-hop task.

## Auto-Wrap Behavior (v3.6.0+)

When an A2A handoff task completes, the agent's raw response may not conform to the
`/handoff@TargetBot {json}` format. Starting with bridge v3.6.0, the bridge
automatically wraps non-conforming responses in a valid result envelope before
posting to the group. This means:

- The originating bot always receives a valid `/handoff@SourceBot {json}` result envelope,
  even when the agent replies with plain prose.
- The `task_id` in the result envelope is extracted from the original handoff prompt.
- The `ttl` is set to `0` (terminal result), `requires_response` to `false`, and `type`
  to `result`.
- If the agent's response already starts with `/handoff@TargetBot`, it is passed through
  as-is (no double-wrapping).

This eliminates the "Rejected invalid A2A response" failures that previously occurred
when agents completed work successfully but didn't format their output as a handoff
envelope (the most common case).

## Non-Target Silent Ignore (v3.6.0+)

When a valid `/handoff@BotA` message is sent in a group where BotB is also present,
BotB now silently ignores it instead of emitting A2A syntax guidance. This prevents
noise in the group from non-target bridges reacting to handoffs that aren't meant
for them. The detection works by checking:

1. The message starts with `/handoff@SomeBot` (where `SomeBot` is not this bridge).
2. The JSON payload after the command is valid and contains `task_id` and `body`.
3. The target is not this bridge (checked via username and registry aliases).

Invalid bot messages (non-handoff prose, broken JSON, missing fields) still trigger
A2A syntax guidance as before.
