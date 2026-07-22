"""Thin bridge adapter for the canonical project-checkpoint skill."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "project-checkpoint"
SKILL_FILE = SKILL_DIR / "SKILL.md"
CHECKPOINT_SCRIPT = SKILL_DIR / "scripts" / "project_checkpoint.py"


class CheckpointRuntimeError(RuntimeError):
    pass


async def _run_helper(*args: str) -> Any:
    if not CHECKPOINT_SCRIPT.is_file():
        raise CheckpointRuntimeError(f"checkpoint helper not found: {CHECKPOINT_SCRIPT}")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(CHECKPOINT_SCRIPT),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    raw_stdout = stdout.decode("utf-8", errors="replace").strip()
    raw_stderr = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        try:
            detail = json.loads(raw_stderr).get("error")
        except (json.JSONDecodeError, AttributeError):
            detail = raw_stderr or raw_stdout or f"exit {process.returncode}"
        raise CheckpointRuntimeError(str(detail))
    try:
        parsed = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return raw_stdout
    if isinstance(parsed, dict) and parsed.get("ok") is True:
        return parsed.get("result")
    return parsed


def _store_args() -> list[str]:
    store_root = os.environ.get("PROJECT_CHECKPOINT_STORE_ROOT", "").strip()
    return ["--store-root", store_root] if store_root else []


async def prepare_checkpoint(
    *,
    project: str,
    runtime: str,
    bridge: str,
    session_id: str | None,
    note: str = "",
) -> dict[str, str]:
    args = [
        "prepare",
        "--project",
        project,
        "--runtime",
        runtime,
        "--bridge",
        bridge,
    ]
    if session_id:
        args.extend(["--session-id", session_id])
    if note:
        args.extend(["--note", note])
    result = await _run_helper(*args)
    if not isinstance(result, dict) or not result.get("draft"):
        raise CheckpointRuntimeError("checkpoint helper returned no draft path")
    return result


async def commit_checkpoint(draft: str) -> dict[str, str]:
    result = await _run_helper("commit", "--draft", draft, *_store_args())
    if not isinstance(result, dict) or not result.get("latest_json"):
        raise CheckpointRuntimeError("checkpoint helper returned no committed path")
    return result


async def latest_checkpoint(project: str) -> str:
    result = await _run_helper(
        "latest",
        "--project",
        project,
        "--format",
        "path",
        *_store_args(),
    )
    if not isinstance(result, str) or not result:
        raise CheckpointRuntimeError("checkpoint helper returned no latest path")
    return result


def checkpoint_save_prompt(draft: str) -> str:
    return (
        "This is a session-lifecycle checkpoint, not a request to continue feature work. "
        f"Read and follow the canonical skill at {SKILL_FILE}. "
        f"Complete the existing JSON draft at {draft} using knowledge from this conversation "
        "and verified workspace state. Preserve all deterministic metadata and snapshot_sha256. "
        "Record decisions with reasons, project/configuration/infra knowledge, completed work, "
        "truthful validation, blockers, and exact next actions. Name credential variables and "
        "protected files only; never copy secret values. Set status to complete, then run "
        f"{CHECKPOINT_SCRIPT} validate --draft {draft}. Do not modify project files or deploy anything. "
        "Reply only with CHECKPOINT_READY after validation succeeds; otherwise explain the blocker."
    )


def checkpoint_resume_prompt(checkpoint_path: str, user_prompt: str = "") -> str:
    continuation = user_prompt.strip() or "Review the restored state and report the next action before proceeding."
    return (
        "Restore project context before handling the request. "
        f"Read and follow the canonical skill at {SKILL_FILE}. "
        f"Load the complete capsule at {checkpoint_path} and its sibling latest.md. "
        "Re-read active GitHub/repository sources of truth and revalidate mutable Git, runtime, "
        "deployment, and credential-by-name facts. Current evidence overrides the capsule. "
        "Briefly identify the checkpoint loaded and any stale/conflicting facts, then continue.\n\n"
        f"User request: {continuation}"
    )


def remove_draft(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
