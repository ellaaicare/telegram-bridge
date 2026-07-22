# Project Checkpoint Runbook

## Purpose

Agent-native compaction preserves a conversation but does not create a portable,
reviewable handoff. Immediate `/new` rotation loses session-only decisions,
configuration discoveries, and rationale. Project checkpoints add a provider-neutral
recovery layer without making the Telegram bridge the owner of agent instructions.

## Ownership

- `skills/project-checkpoint/SKILL.md` owns agent behavior and safety policy.
- `skills/project-checkpoint/references/checkpoint.schema.json` owns the capsule schema.
- `skills/project-checkpoint/scripts/project_checkpoint.py` owns deterministic capture,
  validation, secret rejection, atomic promotion, rendering, and lookup.
- `services/checkpoint_runtime.py` adapts the skill CLI to bridge async code.
- Each bridge owns only queue ordering, session rotation, and bootstrap injection.

The skill is linked into both `~/.codex/skills` and `~/.claude/skills`. Edit the
repository copy and deploy it; do not edit those links as separate implementations.

## Guarded Rotation

1. `/new` or `/compact` queues a lifecycle item behind current human work and ahead
   of pending automation.
2. The deterministic helper writes a draft in the system temporary directory. This
   location remains writable to sandboxed agents and does not dirty the project.
3. The current agent session fills the structured draft from conversation context and
   verified workspace evidence.
4. The bridge validates the schema, immutable Git snapshot, required fields, and
   likely-secret patterns.
5. Only a valid complete draft is atomically copied to the durable user-local store.
6. The bridge clears the selected native session and records the latest capsule for
   the project.
7. The first subsequent prompt is prefixed with a restore instruction. The new agent
   reads the capsule, reopens source-of-truth links, and revalidates mutable state.

Failure before step 5 preserves the existing session. `/new force` is the explicit
operator bypass. It never silently treats a partial capsule as complete.

## Storage

Default:

```text
~/.local/share/agent-checkpoints/projects/<project-name>-<path-hash>/
  latest.json
  latest.md
  cp-YYYY-MM-DD-<id>.json
  cp-YYYY-MM-DD-<id>.md
```

Directories use mode `0700`; files use `0600`. Set
`PROJECT_CHECKPOINT_STORE_ROOT` to use another protected local path. Capsules are
user-scoped and must not be placed in a shared web root.

## Security

The collector never reads environment values, credential files, Git remotes, or diff
contents. It captures only the project path, branch, HEAD, and status file names.
The validator rejects common provider keys, bearer tokens, JWTs, Telegram bot tokens,
private-key blocks, and key-like assignments. Agents may record variable names and
protected locations only.

Treat a rejected capsule as potentially sensitive. The bridge deletes temporary drafts
after success or failure and does not include capsule contents in Telegram responses.

## Installation

```bash
scripts/install-agent-skills.sh --repo-root "$PWD"
```

Fleet deployment runs this once per node after `git pull` and before bridge restarts.
An existing non-symlink skill is not overwritten; resolve that conflict manually so a
locally modified skill is not destroyed.

## Validation

```bash
python3 /path/to/skill-creator/scripts/quick_validate.py skills/project-checkpoint
python3 -m pytest -q
bash -n scripts/install-agent-skills.sh scripts/deploy-fleet.sh
```

For rollout, deploy one non-primary bridge first. Run `/checkpoint`, inspect the
generated JSON/Markdown for secrets and useful continuation detail, then run guarded
`/new` followed by a normal prompt. Confirm the new native session names the loaded
checkpoint and revalidates Git state before deploying to the remaining fleet.

## Source Of Truth

Checkpoint files are not a replacement for GitHub or repository documentation. Before
rotation, agents should post team-visible status and decisions to the relevant issue or
pull request when practical. On restore, current GitHub, Git, CI, and runtime evidence
overrides stale capsule observations.
