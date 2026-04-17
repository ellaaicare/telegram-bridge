---
name: telegram-a2a-handoff
description: Send safe bot-to-bot work in Ella Telegram coordination groups using strict A2A-style /handoff envelopes. Use when an agent must ask another Ella bot to do work through Telegram.
---

# Telegram A2A Handoff

Never delegate to another Ella bot with raw prose like `@OtherBot please do X`.

Use the source-controlled bot registry for target names and aliases:

```text
services/telegram-a2a/agents.json
```

Known aliases include:

- `MacMiniCodex`, `ExampleCodexBot`, `@ExampleCodexBot`
- `iMacCodex`, `ExampleCodex2Bot`, `@ExampleCodex2Bot`
- `MacMiniClaude`, `ExampleClaudeBot`, `@ExampleClaudeBot`
- `iMacClaude`, `ExampleClaude2Bot`, `@ExampleClaude2Bot`

Use only:

```text
/handoff@TargetBot {"from":"SourceBot","to":"TargetBot","task_id":"stable-unique-id","ttl":1,"requires_response":true,"type":"task","body":"Do the work here."}
```

Rules:

- Use `ttl=1` unless explicitly asked to coordinate a multi-hop task.
- Use a stable `task_id`; do not resend the same task ID for different work.
- Use `type=task` for actionable work.
- Use `type=status`, `type=ack`, or `type=result` only for non-actionable updates.
- Set `requires_response=false` for status/ack updates.
- Do not reply to `ack` or `status` unless `requires_response=true`.
- Do not mention another bot in normal prose.
- Do not send “standing by,” “acknowledged,” “waiting,” or similar loop-prone status chatter to another bot.

Security:

- Only allowlisted human IDs may control bots.
- Only allowlisted bot IDs may send `/handoff` messages.
- Group bot messages without a valid `/handoff@TargetBot` JSON envelope must be ignored.
- Duplicate `task_id` values must be ignored.
- Expired handoffs with `ttl <= 0` must be ignored.

Example:

```text
/handoff@ExampleCodexBot {"from":"ExampleClaudeBot","to":"ExampleCodexBot","task_id":"663-pr101-risk-review","ttl":1,"requires_response":true,"type":"task","body":"Review PR #101 hardware validation checklist and reply with risks only."}
```

Response example:

```text
/handoff@ExampleClaudeBot {"from":"ExampleCodexBot","to":"ExampleClaudeBot","task_id":"663-pr101-risk-review-result","ttl":0,"requires_response":false,"type":"result","body":"Review complete. No blocking risks found."}
```

See `docs/runbooks/telegram-a2a-handoff.md` for bridge configuration and operational details.
