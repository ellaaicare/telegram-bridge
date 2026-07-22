#!/usr/bin/env python3
"""Prepare, validate, commit, and locate provider-neutral project checkpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_CAPSULE_BYTES = 512 * 1024
REQUIRED_TOP_LEVEL = {
    "schema_version",
    "checkpoint_id",
    "created_at",
    "snapshot_sha256",
    "status",
    "runtime",
    "project",
    "objective",
    "source_of_truth",
    "completed",
    "current_state",
    "decisions",
    "knowledge",
    "configuration",
    "deployment",
    "validation",
    "blockers",
    "next_actions",
    "risks",
    "relevant_paths",
    "relevant_commands",
    "notes",
}
SNAPSHOT_FIELDS = ("schema_version", "checkpoint_id", "created_at", "runtime", "project")
PLACEHOLDER_RE = re.compile(r"(?i)(?:\bTODO\b|<\s*(?:required|fill|replace)[^>]*>|replace\s+me)")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|gsk_[A-Za-z0-9_-]{12,}|xai-[A-Za-z0-9_-]{12,}|"
        r"AIza[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_-]{20,}|"
        r"github_pat_[A-Za-z0-9_-]{20,})\b"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b[A-Z][A-Z0-9_-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)\s*[:=]\s*"
        r"[\"']?(?!\[?REDACTED|<|\$|ENV\b)[^\s,;\"'&}]{8,}"
    ),
)


class CheckpointError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _project_snapshot(project: str) -> dict[str, Any]:
    cwd = Path(project).expanduser().resolve()
    if not cwd.is_dir():
        raise CheckpointError(f"project directory does not exist: {cwd}")

    git_root_raw = _run_git(cwd, "rev-parse", "--show-toplevel")
    is_repo = bool(git_root_raw)
    root = Path(git_root_raw).resolve() if git_root_raw else cwd
    branch = _run_git(root, "branch", "--show-current") if is_repo else None
    head = _run_git(root, "rev-parse", "HEAD") if is_repo else None
    status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all") if is_repo else ""
    changes = status.splitlines()[:200] if status else []
    key_source = str(root)
    digest = hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:16]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip("-") or "project"

    return {
        "working_directory": str(cwd),
        "root": str(root),
        "key": f"{slug}-{digest}",
        "git": {
            "is_repository": is_repo,
            "branch": branch or None,
            "head": head or None,
            "dirty": bool(changes),
            "changes": changes,
        },
    }


def _snapshot_hash(capsule: dict[str, Any]) -> str:
    payload = {key: capsule.get(key) for key in SNAPSHOT_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _new_capsule(args: argparse.Namespace) -> dict[str, Any]:
    created_at = _utc_now()
    capsule: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "checkpoint_id": f"cp-{created_at[:10]}-{uuid.uuid4().hex[:12]}",
        "created_at": created_at,
        "snapshot_sha256": "",
        "status": "draft",
        "runtime": {
            "provider": args.runtime,
            "bridge": args.bridge,
            "session_id": args.session_id or None,
        },
        "project": _project_snapshot(args.project),
        "objective": "",
        "source_of_truth": [],
        "completed": [],
        "current_state": "",
        "decisions": [],
        "knowledge": [],
        "configuration": [],
        "deployment": [],
        "validation": [],
        "blockers": [],
        "next_actions": [],
        "risks": [],
        "relevant_paths": [],
        "relevant_commands": [],
        "notes": args.note or "",
    }
    capsule["snapshot_sha256"] = _snapshot_hash(capsule)
    return capsule


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _secure_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_text(path: Path, content: str) -> None:
    _secure_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _walk_strings(value: Any, path: str = "$"):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}")


def _expect_type(value: Any, expected: type, field: str) -> None:
    if not isinstance(value, expected):
        raise CheckpointError(f"{field} must be {expected.__name__}")


def _validate_object_list(capsule: dict[str, Any], field: str, required: set[str]) -> None:
    value = capsule.get(field)
    _expect_type(value, list, field)
    for index, item in enumerate(value):
        _expect_type(item, dict, f"{field}[{index}]")
        missing = required - set(item)
        if missing:
            raise CheckpointError(f"{field}[{index}] missing: {', '.join(sorted(missing))}")
        extra = set(item) - required
        if extra:
            raise CheckpointError(f"{field}[{index}] has unknown fields: {', '.join(sorted(extra))}")


def _validate_string_list(capsule: dict[str, Any], field: str) -> None:
    for index, item in enumerate(capsule[field]):
        if not isinstance(item, str):
            raise CheckpointError(f"{field}[{index}] must be str")


def validate_capsule(capsule: dict[str, Any], *, require_complete: bool = True) -> None:
    missing = REQUIRED_TOP_LEVEL - set(capsule)
    extra = set(capsule) - REQUIRED_TOP_LEVEL
    if missing:
        raise CheckpointError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise CheckpointError(f"unknown fields: {', '.join(sorted(extra))}")
    if capsule["schema_version"] != SCHEMA_VERSION:
        raise CheckpointError(f"unsupported schema_version: {capsule['schema_version']}")
    if capsule["snapshot_sha256"] != _snapshot_hash(capsule):
        raise CheckpointError("deterministic runtime/project snapshot was modified")
    if require_complete and capsule["status"] != "complete":
        raise CheckpointError("status must be complete")
    if capsule["status"] not in {"draft", "complete"}:
        raise CheckpointError("status must be draft or complete")

    try:
        _parse_timestamp(capsule["created_at"])
    except (TypeError, ValueError) as exc:
        raise CheckpointError("created_at must be an ISO date-time") from exc

    _expect_type(capsule["runtime"], dict, "runtime")
    _expect_type(capsule["project"], dict, "project")
    if set(capsule["runtime"]) != {"provider", "bridge", "session_id"}:
        raise CheckpointError("runtime fields do not match the schema")
    if not isinstance(capsule["runtime"]["provider"], str) or not isinstance(
        capsule["runtime"]["bridge"], str
    ):
        raise CheckpointError("runtime provider and bridge must be strings")
    if capsule["runtime"]["session_id"] is not None and not isinstance(
        capsule["runtime"]["session_id"], str
    ):
        raise CheckpointError("runtime session_id must be a string or null")
    if set(capsule["project"]) != {"working_directory", "root", "key", "git"}:
        raise CheckpointError("project fields do not match the schema")
    git = capsule["project"].get("git")
    _expect_type(git, dict, "project.git")
    if set(git) != {"is_repository", "branch", "head", "dirty", "changes"}:
        raise CheckpointError("project.git fields do not match the schema")
    if not isinstance(git["is_repository"], bool) or not isinstance(git["dirty"], bool):
        raise CheckpointError("project.git repository and dirty flags must be boolean")
    _expect_type(git["changes"], list, "project.git.changes")
    for index, item in enumerate(git["changes"]):
        if not isinstance(item, str):
            raise CheckpointError(f"project.git.changes[{index}] must be str")
    for field in ("objective", "current_state", "notes"):
        _expect_type(capsule[field], str, field)
    for field in (
        "source_of_truth",
        "completed",
        "decisions",
        "knowledge",
        "configuration",
        "deployment",
        "validation",
        "blockers",
        "next_actions",
        "risks",
        "relevant_paths",
        "relevant_commands",
    ):
        _expect_type(capsule[field], list, field)

    for field in ("completed", "blockers", "risks", "relevant_paths", "relevant_commands"):
        _validate_string_list(capsule, field)

    _validate_object_list(capsule, "source_of_truth", {"type", "ref", "status"})
    _validate_object_list(capsule, "decisions", {"decision", "reason"})
    _validate_object_list(capsule, "knowledge", {"fact", "source", "revalidate"})
    _validate_object_list(
        capsule,
        "configuration",
        {"name", "purpose", "location", "secret_value_recorded"},
    )
    _validate_object_list(capsule, "deployment", {"target", "state", "observed_at", "revalidate"})
    _validate_object_list(capsule, "validation", {"check", "result"})
    _validate_object_list(capsule, "next_actions", {"action", "owner", "priority"})

    for index, item in enumerate(capsule["configuration"]):
        if item.get("secret_value_recorded") is not False:
            raise CheckpointError(f"configuration[{index}].secret_value_recorded must be false")
    for index, item in enumerate(capsule["knowledge"]):
        if not isinstance(item.get("revalidate"), bool):
            raise CheckpointError(f"knowledge[{index}].revalidate must be boolean")
    for index, item in enumerate(capsule["deployment"]):
        if not isinstance(item.get("revalidate"), bool):
            raise CheckpointError(f"deployment[{index}].revalidate must be boolean")

    if require_complete:
        for field in ("objective", "current_state"):
            if not capsule[field].strip():
                raise CheckpointError(f"{field} must not be empty")
        if not capsule["next_actions"]:
            raise CheckpointError("next_actions must not be empty")

    for field_path, value in _walk_strings(capsule):
        if PLACEHOLDER_RE.search(value):
            raise CheckpointError(f"placeholder text remains at {field_path}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(value):
                raise CheckpointError(f"likely secret value detected at {field_path}")


def _load_capsule(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        if source.stat().st_size > MAX_CAPSULE_BYTES:
            raise CheckpointError(
                f"checkpoint exceeds {MAX_CAPSULE_BYTES} byte safety limit: {source}"
            )
        with source.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except CheckpointError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"could not read checkpoint {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise CheckpointError("checkpoint root must be an object")
    return value


def _list_lines(values: list[Any], formatter) -> str:
    if not values:
        return "- None recorded."
    return "\n".join(f"- {formatter(value)}" for value in values)


def render_markdown(capsule: dict[str, Any]) -> str:
    project = capsule["project"]
    git = project["git"]
    runtime = capsule["runtime"]
    lines = [
        "# Project Checkpoint",
        "",
        f"- Checkpoint: `{capsule['checkpoint_id']}`",
        f"- Created: `{capsule['created_at']}`",
        f"- Runtime: `{runtime['provider']}` via `{runtime['bridge']}`",
        f"- Session: `{runtime['session_id'] or 'none'}`",
        f"- Project: `{project['root']}`",
        f"- Branch: `{git['branch'] or 'none'}`",
        f"- HEAD: `{git['head'] or 'none'}`",
        f"- Dirty at capture: `{'yes' if git['dirty'] else 'no'}`",
        "",
        "## Objective",
        "",
        capsule["objective"],
        "",
        "## Current State",
        "",
        capsule["current_state"],
        "",
        "## Completed",
        "",
        _list_lines(capsule["completed"], str),
        "",
        "## Decisions And Reasons",
        "",
        _list_lines(capsule["decisions"], lambda item: f"{item['decision']} Reason: {item['reason']}"),
        "",
        "## Learned Knowledge",
        "",
        _list_lines(
            capsule["knowledge"],
            lambda item: f"{item['fact']} Source: {item['source']}. Revalidate: {item['revalidate']}",
        ),
        "",
        "## Configuration",
        "",
        _list_lines(
            capsule["configuration"],
            lambda item: f"`{item['name']}`: {item['purpose']} Location: `{item['location']}`. Secret value recorded: no",
        ),
        "",
        "## Deployment",
        "",
        _list_lines(
            capsule["deployment"],
            lambda item: f"{item['target']}: {item['state']} Observed: {item['observed_at']}. Revalidate: {item['revalidate']}",
        ),
        "",
        "## Validation",
        "",
        _list_lines(capsule["validation"], lambda item: f"`{item['check']}`: {item['result']}"),
        "",
        "## Blockers",
        "",
        _list_lines(capsule["blockers"], str),
        "",
        "## Next Actions",
        "",
        _list_lines(
            capsule["next_actions"],
            lambda item: f"[{item['priority']}] {item['action']} Owner: {item['owner']}",
        ),
        "",
        "## Risks",
        "",
        _list_lines(capsule["risks"], str),
        "",
        "## Sources Of Truth",
        "",
        _list_lines(
            capsule["source_of_truth"],
            lambda item: f"{item['type']}: {item['ref']} ({item['status']})",
        ),
        "",
        "## Relevant Paths",
        "",
        _list_lines(capsule["relevant_paths"], lambda value: f"`{value}`"),
        "",
        "## Safe Commands",
        "",
        _list_lines(capsule["relevant_commands"], lambda value: f"`{value}`"),
        "",
        "## Capture-Time Git Changes",
        "",
        _list_lines(git["changes"], lambda value: f"`{value}`"),
        "",
        "## Notes",
        "",
        capsule["notes"] or "None.",
        "",
        "Mutable facts above must be revalidated before acting.",
        "",
    ]
    return "\n".join(lines)


def _store_root(value: str | None) -> Path:
    configured = value or os.environ.get("PROJECT_CHECKPOINT_STORE_ROOT")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".local" / "share" / "agent-checkpoints"


def _latest_path(project: str, store_root: str | None) -> Path:
    snapshot = _project_snapshot(project)
    return _store_root(store_root) / "projects" / snapshot["key"] / "latest.json"


def cmd_prepare(args: argparse.Namespace) -> dict[str, Any]:
    capsule = _new_capsule(args)
    draft_dir = Path(args.draft_dir).expanduser().resolve() if args.draft_dir else Path(tempfile.gettempdir()) / "agent-checkpoints"
    _secure_dir(draft_dir)
    draft = draft_dir / f"{capsule['checkpoint_id']}.json"
    _atomic_json(draft, capsule)
    return {"checkpoint_id": capsule["checkpoint_id"], "draft": str(draft), "project_key": capsule["project"]["key"]}


def cmd_validate(args: argparse.Namespace) -> dict[str, Any]:
    capsule = _load_capsule(args.draft)
    validate_capsule(capsule, require_complete=not args.allow_draft)
    return {"valid": True, "checkpoint_id": capsule["checkpoint_id"], "status": capsule["status"]}


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def cmd_commit(args: argparse.Namespace) -> dict[str, Any]:
    capsule = _load_capsule(args.draft)
    validate_capsule(capsule, require_complete=True)
    destination_dir = _store_root(args.store_root) / "projects" / capsule["project"]["key"]
    _secure_dir(destination_dir)
    latest_json = destination_dir / "latest.json"
    if latest_json.exists() and not args.allow_older:
        latest = _load_capsule(latest_json)
        if _parse_timestamp(latest["created_at"]) > _parse_timestamp(capsule["created_at"]):
            raise CheckpointError("refusing to replace a newer checkpoint")

    stem = capsule["checkpoint_id"]
    versioned_json = destination_dir / f"{stem}.json"
    versioned_md = destination_dir / f"{stem}.md"
    markdown = render_markdown(capsule)
    _atomic_json(versioned_json, capsule)
    _atomic_text(versioned_md, markdown)
    _atomic_json(latest_json, capsule)
    _atomic_text(destination_dir / "latest.md", markdown)
    return {
        "checkpoint_id": capsule["checkpoint_id"],
        "json": str(versioned_json),
        "markdown": str(versioned_md),
        "latest_json": str(latest_json),
        "latest_markdown": str(destination_dir / "latest.md"),
    }


def cmd_latest(args: argparse.Namespace) -> Any:
    path = _latest_path(args.project, args.store_root)
    if not path.exists():
        raise CheckpointError(f"no checkpoint exists for project: {args.project}")
    capsule = _load_capsule(path)
    validate_capsule(capsule, require_complete=True)
    if args.format == "path":
        return str(path)
    if args.format == "markdown-path":
        return str(path.with_suffix(".md"))
    return capsule


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="write a deterministic draft")
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--runtime", required=True)
    prepare.add_argument("--bridge", required=True)
    prepare.add_argument("--session-id")
    prepare.add_argument("--note")
    prepare.add_argument("--draft-dir")
    prepare.set_defaults(handler=cmd_prepare)

    validate = subparsers.add_parser("validate", help="validate a draft or complete capsule")
    validate.add_argument("--draft", required=True)
    validate.add_argument("--allow-draft", action="store_true")
    validate.set_defaults(handler=cmd_validate)

    commit = subparsers.add_parser("commit", help="atomically promote a complete draft")
    commit.add_argument("--draft", required=True)
    commit.add_argument("--store-root")
    commit.add_argument("--allow-older", action="store_true")
    commit.set_defaults(handler=cmd_commit)

    latest = subparsers.add_parser("latest", help="locate the latest project checkpoint")
    latest.add_argument("--project", required=True)
    latest.add_argument("--store-root")
    latest.add_argument("--format", choices=("path", "markdown-path", "json"), default="path")
    latest.set_defaults(handler=cmd_latest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except CheckpointError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
