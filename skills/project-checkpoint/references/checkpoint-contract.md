# Checkpoint Contract

The canonical capsule is JSON. A human-readable Markdown file is rendered beside it at commit time.

## Required Meaning

- `objective`: The active user or team outcome, not a generic task label.
- `current_state`: What is true now, including whether work is local, pushed, merged, deployed, or only proposed.
- `next_actions`: Ordered, executable follow-up work. Include an owner when known.
- `decisions`: Important choices paired with reasons. Include rejected alternatives only when forgetting them would cause rework.
- `knowledge`: Project, architecture, and operational facts learned during the session. Add a source and `revalidate: true` for live or uncertain facts.
- `configuration`: Names and locations only. `secret_value_recorded` must always be `false`.
- `validation`: Commands or observations actually performed, with honest results.
- `source_of_truth`: Relevant issue, pull request, commit, runbook, or document references.

## Completion Gate

A complete capsule must:

1. Use schema version 1 and retain its generated checkpoint ID and timestamps.
2. Set `status` to `complete`.
3. Have non-empty `objective`, `current_state`, and `next_actions`.
4. Contain no placeholder text.
5. Contain no likely credential values.
6. Preserve the deterministic Git snapshot from `prepare` without inventing cleaner state.

The validator is intentionally conservative. If it rejects a benign string, rewrite the capsule to refer to the credential by name or protected location instead of including value-shaped data.

## Source Of Truth

Capsules are recovery aids, not canonical project management. Put shared decisions and status in GitHub or committed documentation. A restored agent must re-read those records and re-check mutable infrastructure before acting.
