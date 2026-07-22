---
name: project-checkpoint
description: Save and restore durable project context before starting a new agent session, compacting context, changing providers, handing work to another developer, or recovering from an interrupted bridge run. Use for /checkpoint, guarded /new, /resume checkpoint, and any explicit request to preserve learned configuration, decisions, rationale, validation, blockers, or next actions without copying secrets.
---

# Project Checkpoint

Create a structured context capsule that lets another agent continue work without trusting stale runtime state. Keep GitHub issues, pull requests, repository docs, and committed code as the authoritative record; use the capsule as a concise index and handoff.

## Save

1. Run `scripts/project_checkpoint.py prepare --project <cwd> --runtime <runtime> --bridge <bridge> [--session-id <id>]` unless the bridge already supplied a draft path.
2. Open the generated JSON draft and preserve its deterministic metadata.
3. Fill every required knowledge field from the current conversation and verified workspace state.
4. Set `status` to `complete` only after checking the capsule against `references/checkpoint-contract.md`.
5. Run `scripts/project_checkpoint.py validate --draft <path>`.
6. When operating outside a bridge, run `scripts/project_checkpoint.py commit --draft <path>` to promote the draft atomically. A bridge performs this promotion itself after validation.

Record:

- The user objective and current state.
- Work completed, active issues and pull requests, and source-of-truth links.
- Decisions and their reasons, including rejected approaches when they matter.
- Architecture, infrastructure, deployment, provider, model, and configuration knowledge learned during the session.
- Configuration variable names and secret locations, never secret values.
- Validation actually run and its result.
- Blockers, risks, exact next actions, owners, relevant paths, and safe commands.
- Facts that must be revalidated because live state may have changed.

Promote generally reusable facts to repository docs or the relevant GitHub issue before rotating when practical. Do not make the capsule the only record of production configuration or a team decision.

## Restore

1. Run `scripts/project_checkpoint.py latest --project <cwd> --format path` unless the bridge supplied a capsule path.
2. Read the JSON capsule and its sibling Markdown rendering.
3. Reopen all linked issues and pull requests that are still active.
4. Revalidate the current branch, HEAD, dirty files, deployed version, credentials by identifier, and live service health before changing anything.
5. State any conflict between the capsule and current evidence. Current evidence wins.
6. Continue from `next_actions`; do not repeat completed work unless validation shows it is needed.

## Safety

- Never include passwords, bearer tokens, API keys, private keys, cookies, OAuth codes, environment variable values, or credential file contents.
- Name secrets only by variable name or protected path, such as `HERMES_API_KEY` or `~/.config/service/.env`.
- Do not claim a deploy, merge, test, or external mutation that was not observed.
- Do not overwrite a newer checkpoint with an older draft.
- Keep each checkpoint scoped to one project root and authenticated user.
- Treat all live-state facts as observations with timestamps, not permanent truth.

Read `references/checkpoint-contract.md` for the field contract and `references/checkpoint.schema.json` for the machine-readable schema.
