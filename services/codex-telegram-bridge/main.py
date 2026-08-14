"""
Codex <-> Telegram Bridge
Direct long-polling bridge for the local Codex CLI.

Features:
  - Telegram long-polling (no inbound webhook needed)
  - Per-folder session continuity using `codex exec resume`
  - Sequential prompt queue
  - Basic media attachment support
  - Watchdog for long-running command executions
  - FastAPI health endpoint for launchd checks
"""

import asyncio
import hmac
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

SERVICES_DIR = Path(__file__).resolve().parents[1]
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from checkpoint_runtime import (  # noqa: E402
    CheckpointRuntimeError,
    checkpoint_resume_prompt,
    checkpoint_save_prompt,
    commit_checkpoint,
    latest_checkpoint,
    prepare_checkpoint,
    remove_draft,
)


def _parse_int_set(raw: str) -> set[int]:
    return {int(value) for value in raw.split(",") if value.strip()}


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes", "on"}


def _redact_log_text(raw: str) -> str:
    text = str(raw or "")
    text = re.sub(
        r"(?i)\b([A-Z][A-Z0-9_-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD))"
        r"(\s*[:=]\s*)([\"']?)[^\s,;\"'&}]+([\"']?)",
        r"\1\2[REDACTED_SECRET]",
        text,
    )
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]{12,}",
        r"\1 [REDACTED_BEARER_TOKEN]",
        text,
    )
    text = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{12,}|gsk_[A-Za-z0-9_-]{12,}|xai-[A-Za-z0-9_-]{12,}|"
        r"AIza[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_-]{20,}|"
        r"github_pat_[A-Za-z0-9_-]{20,})\b",
        "[REDACTED_API_KEY]",
        text,
    )
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "[REDACTED_JWT]",
        text,
    )
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED_BOT_TOKEN]", text)
    return text


def _norm_bot_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").lstrip("@").lower() if ch.isalnum())


def _a2a_registry_candidates() -> list[Path]:
    candidates = []
    env_path = os.environ.get("A2A_BOT_REGISTRY_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())
    here = Path(__file__).resolve()
    candidates.extend(
        [
            here.parents[1] / "telegram-a2a" / "agents.json",
            here.parents[2] / "services" / "telegram-a2a" / "agents.json",
            Path.home() / "telegram-bridge" / "services" / "telegram-a2a" / "agents.json",
            Path.home() / "dev" / "telegram-bridge" / "services" / "telegram-a2a" / "agents.json",
            Path.home() / "dev" / "ella-ai" / "services" / "telegram-a2a" / "agents.json",
            Path.home() / "ella-ai" / "services" / "telegram-a2a" / "agents.json",
        ]
    )
    return candidates


def _load_a2a_registry() -> dict:
    inline = os.environ.get("A2A_BOT_REGISTRY_JSON", "").strip()
    if inline:
        try:
            data = json.loads(inline)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as e:
            logging.getLogger("codex-bridge").warning("Invalid A2A_BOT_REGISTRY_JSON: %s", e)

    for path in _a2a_registry_candidates():
        try:
            if path.exists():
                with path.open() as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logging.getLogger("codex-bridge").warning("Could not load A2A bot registry %s: %s", path, e)
    return {}


def _registry_bots() -> list[dict]:
    bots = A2A_BOT_REGISTRY.get("bots", [])
    return bots if isinstance(bots, list) else []


def _bot_alias_values(bot: dict) -> set[str]:
    values = {str(bot.get("canonical") or ""), str(bot.get("username") or "")}
    values.update(str(alias) for alias in bot.get("aliases", []) if alias)
    return {value.lstrip("@") for value in values if value}


def _resolve_bot_alias(value: str) -> dict | None:
    key = _norm_bot_key(value)
    if not key:
        return None
    for bot in _registry_bots():
        if key in {_norm_bot_key(alias) for alias in _bot_alias_values(bot)}:
            return bot
    return None


def _handoff_prefixes_for_target(target_username: str) -> list[str]:
    bot = _resolve_bot_alias(target_username)
    values = _bot_alias_values(bot) if bot else {target_username}
    prefixes = [f"/handoff@{value}" for value in sorted(values, key=len, reverse=True) if value]
    return prefixes or [f"/handoff@{target_username}"]


def _canonical_handoff_target(target_username: str) -> str:
    bot = _resolve_bot_alias(target_username)
    return str((bot or {}).get("username") or target_username or "TargetBot")


def _known_target_examples(limit: int = 5) -> str:
    examples = []
    for bot in _registry_bots():
        username = str(bot.get("username") or "").strip()
        canonical = str(bot.get("canonical") or username).strip()
        if username:
            examples.append(f"/handoff@{username} ({canonical})")
    return ", ".join(examples[:limit]) if examples else "/handoff@TargetBot"


def _trusted_registry_bot_ids() -> set[int]:
    ids = set()
    for bot in _registry_bots():
        if bot.get("trusted", False) is True and bot.get("id") is not None:
            try:
                ids.add(int(bot["id"]))
            except (TypeError, ValueError):
                continue
    return ids


# --- Configuration ---

BRIDGE_VERSION = os.environ.get("CODEX_BRIDGE_VERSION", "0.4.0")
BRIDGE_BUILD = os.environ.get("CODEX_BRIDGE_BUILD", "mcp-dispatch-ingress-v1")
BOT_TOKEN = os.environ.get("CODEX_TELEGRAM_BOT_TOKEN", "")
A2A_BOT_REGISTRY = _load_a2a_registry()
A2A_RETIRED_TARGETS = {
    _norm_bot_key(value)
    for value in os.environ.get(
        "A2A_RETIRED_TARGETS",
        "linda,claude,atlas,ellaminibot,macminiclaude",
    ).split(",")
    if _norm_bot_key(value)
}
ALLOWED_USERS = _parse_int_set(os.environ.get("ALLOWED_USER_IDS", ""))
ALLOWED_SENDER_IDS = _parse_int_set(os.environ.get("ALLOWED_SENDER_IDS", os.environ.get("ALLOWED_USER_IDS", "")))
ALLOWED_BOT_IDS = _parse_int_set(os.environ.get("ALLOWED_BOT_IDS", ""))
if _truthy_env("A2A_TRUST_REGISTRY_BOTS", "true"):
    ALLOWED_BOT_IDS |= _trusted_registry_bot_ids()
ALLOWED_CHAT_IDS = _parse_int_set(os.environ.get("ALLOWED_CHAT_IDS", ""))
BOT_USERNAME = ""
BOT_ID: int | None = None
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "900"))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "high").strip()
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "workspace-write")
CODEX_SKIP_GIT_REPO_CHECK = os.environ.get("CODEX_SKIP_GIT_REPO_CHECK", "true").lower() == "true"
CODEX_FULL_AUTO = os.environ.get("CODEX_FULL_AUTO", "false").lower() == "true"
CODEX_DANGEROUS_BYPASS = os.environ.get("CODEX_DANGEROUS_BYPASS", "false").lower() == "true"
CODEX_EXTRA_DIRS = [d for d in os.environ.get("CODEX_ADD_DIRS", "").split(":") if d]
CODEX_BRIDGE_PORT = int(os.environ.get("CODEX_BRIDGE_PORT", "8110"))
TELEGRAM_MAX_LENGTH = 4096
A2A_GUIDANCE_COOLDOWN_SECONDS = int(os.environ.get("A2A_GUIDANCE_COOLDOWN_SECONDS", "300"))
A2A_PROGRESS_MODE = os.environ.get("A2A_PROGRESS_MODE", "status").strip().lower()
A2A_QUEUE_COALESCE_ENABLED = _truthy_env("A2A_QUEUE_COALESCE_ENABLED", "true")
A2A_QUEUE_COALESCE_SENDERS = {
    value.strip().lower()
    for value in os.environ.get("A2A_QUEUE_COALESCE_SENDERS", "n8n-github-router").split(",")
    if value.strip()
}
A2A_QUEUE_COALESCE_MAX_EVENTS = int(os.environ.get("A2A_QUEUE_COALESCE_MAX_EVENTS", "20"))
A2A_QUEUE_COALESCE_MAX_CHARS = int(os.environ.get("A2A_QUEUE_COALESCE_MAX_CHARS", "50000"))
A2A_IGNORED = "__a2a_ignored__"
POLL_TIMEOUT = 60
STATE_FILE = (
    Path(
        os.environ.get(
            "CODEX_BRIDGE_STATE_DIR",
            str(Path.home() / "codexd" / "services" / "codex-telegram-bridge"),
        )
    )
    / "state.json"
)
HOME = os.environ.get("CODEX_DEFAULT_FOLDER", str(Path.home()))
MEDIA_DIR = Path("/tmp/tg-codex-bridge-media")
PROJECT_CHECKPOINT_ENABLED = _truthy_env("PROJECT_CHECKPOINT_ENABLED", "true")
BRIDGE_DISPATCH_TOKEN_FILE = Path(
    os.environ.get(
        "CODEX_BRIDGE_DISPATCH_TOKEN_FILE",
        str(Path.home() / ".gpt5mcp" / "bridge-token"),
    )
).expanduser()
BRIDGE_DISPATCH_JOB_DIR = Path(
    os.environ.get(
        "CODEX_BRIDGE_DISPATCH_JOB_DIR",
        str(Path.home() / ".gpt5mcp" / "bridge-jobs"),
    )
).expanduser()
BRIDGE_DISPATCH_CHAT_ID = int(os.environ.get("CODEX_BRIDGE_DISPATCH_CHAT_ID", "0") or "0")


# --- Watchdog Configuration ---

WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "true").lower() == "true"
WATCHDOG_COMMAND_TIMEOUT = int(os.environ.get("WATCHDOG_COMMAND_TIMEOUT", "900"))
WATCHDOG_DEFAULT_TIMEOUT = int(os.environ.get("WATCHDOG_DEFAULT_TIMEOUT", "180"))
WATCHDOG_STAGNATION_KILL = int(os.environ.get("WATCHDOG_STAGNATION_KILL", "1800"))
# A long-running turn reports progress at this interval without interrupting the child.
CODEX_PROGRESS_UPDATE_INTERVAL = max(
    0, int(os.environ.get("CODEX_PROGRESS_UPDATE_INTERVAL", "1800"))
)
# ella-ai#1174: absolute ceiling on a single child. The stagnation watchdog above only
# fires when progress STOPS; a child that keeps emitting events can otherwise run
# unbounded and head-of-line-block the queue, because `busy` is derived purely from the
# child's returncode. Keep this materially longer than the progress-update interval so
# a healthy turn is not mistaken for a status checkpoint.
CODEX_MAX_RUNTIME = max(60, int(os.environ.get("CODEX_MAX_RUNTIME", "7200")))
# Grace between SIGTERM and SIGKILL when the ceiling is hit.
CODEX_KILL_GRACE = max(
    1, min(60, int(os.environ.get("CODEX_KILL_GRACE", "10")))
)
LOG_FILE = os.environ.get(
    "CODEX_BRIDGE_LOG_FILE",
    str(Path.home() / "codex-bridge" / "logs" / "codex-telegram-bridge.log"),
)
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

WATCHDOG_INSTANT_KILL_PATTERNS = [
    "tail -f",
    "tail --follow",
    "tail -F",
    "journalctl -f",
    "journalctl --follow",
    "watch ",
    "cat /dev/zero",
    "cat /dev/urandom",
    "yes |",
    "npm start",
    "npm run dev",
    "npm run serve",
    "yarn start",
    "yarn dev",
    "python -m http.server",
    "python3 -m http.server",
    "flask run",
    "uvicorn ",
    "gunicorn ",
    "nodemon ",
    "ng serve",
    "next dev",
    "vite",
    "webpack serve",
    "live-server",
    "http-server",
    "sleep infinity",
]


# --- Logging ---

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger("codex-bridge")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _parse_handoff(raw: str) -> tuple[bool, str]:
    """Parse `/handoff@TargetBot {json}`. Raw bot mentions are not executable."""
    if not BOT_USERNAME:
        return False, ""

    raw_stripped = raw.strip()
    matched_prefix = next(
        (
            prefix
            for prefix in _handoff_prefixes_for_target(BOT_USERNAME)
            if raw_stripped.lower().startswith(prefix.lower())
        ),
        "",
    )
    if not matched_prefix:
        # Not a handoff targeted at us: silently ignore.
        return False, A2A_IGNORED

    payload_text = raw_stripped[len(matched_prefix) :].strip()
    if not payload_text:
        log.warning("Rejected empty A2A handoff for @%s", BOT_USERNAME)
        return False, ""

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as e:
        log.warning("Rejected invalid A2A handoff JSON: %s", e)
        return False, ""

    ttl = int(payload.get("ttl", 0) or 0)
    task_id = str(payload.get("task_id") or "").strip()
    body = str(payload.get("body") or "").strip()
    msg_type = str(payload.get("type") or "task").strip()
    requires_response = bool(payload.get("requires_response", False))
    sender = str(payload.get("from") or "").strip()

    if ttl <= 0:
        log.info("Ignored expired A2A handoff task_id=%s", task_id or "(missing)")
        return False, A2A_IGNORED
    if not task_id or not body:
        log.warning("Rejected A2A handoff missing task_id/body")
        return False, ""
    if msg_type in {"ack", "status"} and not requires_response:
        log.info("Ignored non-actionable A2A %s task_id=%s", msg_type, task_id)
        return False, A2A_IGNORED

    processed = state.setdefault("processed_handoffs", {})
    if task_id in processed:
        log.info("Ignored duplicate A2A handoff task_id=%s", task_id)
        return False, A2A_IGNORED
    processed[task_id] = datetime.now(timezone.utc).isoformat()
    if len(processed) > 500:
        for old_key in list(processed.keys())[:100]:
            processed.pop(old_key, None)
    save_state()

    prompt = (
        f"A2A handoff from {sender or 'unknown'} "
        f"(task_id={task_id}, type={msg_type}, ttl={ttl}, requires_response={requires_response}).\n\n"
        f"{body}"
    )
    return True, prompt


def _retired_outbound_handoff_target(text: str) -> str:
    match = re.match(r"\s*/handoff@([A-Za-z0-9_]+)\b", str(text or ""), re.IGNORECASE)
    if not match:
        return ""
    target = match.group(1)
    return target if _norm_bot_key(target) in A2A_RETIRED_TARGETS else ""


def _a2a_skill_reference() -> str:
    return (
        "Repo skill: skills/telegram-a2a-handoff/SKILL.md\n"
        "Runbook: docs/runbooks/telegram-a2a-handoff.md\n"
        "Bot registry: services/telegram-a2a/agents.json\n"
        "Git: https://github.com/ellaaicare/telegram-bridge/blob/main/skills/telegram-a2a-handoff/SKILL.md"
    )


def _a2a_guidance_message() -> str:
    target = _canonical_handoff_target(BOT_USERNAME)
    return (
        "A2A handoff syntax required for bot-to-bot work.\n\n"
        f"Send tasks with this exact shape:\n/handoff@{target} "
        f'{{"from":"SourceBot","to":"{target}","task_id":"stable-unique-id",'
        '"ttl":1,"requires_response":true,"type":"task","body":"Do the work here."}\n\n'
        f"Known targets: {_known_target_examples()}\n\n"
        "Rules: use ttl=1, use a unique task_id, put the actual request in body, "
        "and do not send raw @bot prose or standing-by chatter.\n\n"
        f"{_a2a_skill_reference()}"
    )


def _should_send_a2a_guidance(chat_id: int | None, user_id: int | None) -> bool:
    key = f"{chat_id}:{user_id}"
    now = time.time()
    last = _a2a_guidance_last_sent.get(key, 0)
    if now - last < A2A_GUIDANCE_COOLDOWN_SECONDS:
        return False
    _a2a_guidance_last_sent[key] = now
    return True


def _is_a2a_guidance(raw: str) -> bool:
    return raw.lstrip().startswith("A2A handoff syntax required for bot-to-bot work.")


def _validate_handoff_envelope(raw: str, target_username: str) -> tuple[bool, str]:
    stripped = raw.strip()
    canonical_target = _canonical_handoff_target(target_username)
    matched_prefix = next(
        (
            prefix
            for prefix in _handoff_prefixes_for_target(target_username)
            if stripped.lower().startswith(prefix.lower())
        ),
        "",
    )
    if not matched_prefix:
        return False, f"response must start with /handoff@{canonical_target}"

    json_text = stripped[len(matched_prefix) :].strip()
    if not json_text:
        return False, "response is missing the JSON envelope after the command"

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as e:
        return False, f"response has invalid JSON envelope: {e}"

    if not isinstance(payload, dict):
        return False, "response JSON envelope must be an object"

    missing = [
        key for key in ("from", "to", "task_id", "ttl", "requires_response", "type", "body") if key not in payload
    ]
    if missing:
        return False, f"response JSON envelope missing required fields: {', '.join(missing)}"

    if not str(payload.get("task_id") or "").strip():
        return False, "response JSON envelope must include a non-empty task_id"
    if not str(payload.get("body") or "").strip():
        return False, "response JSON envelope must include a non-empty body"

    try:
        ttl = int(payload.get("ttl"))
    except (TypeError, ValueError):
        return False, "response JSON envelope ttl must be an integer"
    if ttl < 0:
        return False, "response JSON envelope ttl must be >= 0"

    return True, ""


def _a2a_response_rejection(target_username: str, reason: str) -> str:
    target = _canonical_handoff_target(target_username)
    return (
        "A2A handoff syntax required for bot-to-bot work.\n\n"
        "The local agent generated an invalid bot-to-bot response, so the bridge "
        "rejected the raw response instead of posting it.\n\n"
        f"Reason: {reason}\n\n"
        f"Reply with this exact shape:\n/handoff@{target} "
        f'{{"from":"SourceBot","to":"{target}","task_id":"stable-unique-id",'
        '"ttl":1,"requires_response":false,"type":"result","body":"Result text here."}\n\n'
        f"Known targets: {_known_target_examples()}\n\n"
        "Rules: structured envelopes are required for A2A responses too; use ttl=1 "
        "or ttl=0 for terminal results, use a unique task_id, and put the response "
        "content in body.\n\n"
        f"{_a2a_skill_reference()}"
    )


def _extract_a2a_task_id(prompt: str) -> str:
    match = re.search(r"task_id=([^,)\s]+)", prompt)
    return match.group(1) if match else f"task-{int(time.time())}"


def _extract_a2a_sender(prompt: str) -> str:
    match = re.match(r"A2A handoff from ([^\s(]+)", prompt.strip())
    return match.group(1) if match else ""


def _coalescible_a2a_event(prompt: str, reply_target: str | None) -> dict | None:
    if not A2A_QUEUE_COALESCE_ENABLED or not reply_target:
        return None
    sender = _extract_a2a_sender(prompt)
    if sender.lower() not in A2A_QUEUE_COALESCE_SENDERS:
        return None
    return {
        "task_id": _extract_a2a_task_id(prompt),
        "sender": sender,
        "prompt": prompt,
    }


def _format_coalesced_a2a_prompt(events: list[dict]) -> str:
    if len(events) == 1:
        return events[0]["prompt"]

    lines = [
        f"Coalesced A2A backlog containing {len(events)} automated GitHub events.",
        "",
        "Process these events in chronological order in one run. GitHub is the source of truth: "
        "read the latest state, deduplicate superseded notifications, perform actionable work once per "
        "issue or pull request, and post progress/final status there. Do not skip an event merely because "
        "a later event references the same entity.",
    ]
    for index, event in enumerate(events, start=1):
        lines.extend(
            [
                "",
                f"--- Event {index}/{len(events)} | task_id={event['task_id']} ---",
                event["prompt"],
            ]
        )
    return "\n".join(lines)


def _a2a_response_body(response: str, target_username: str) -> str:
    stripped = response.strip()
    matched_prefix = next(
        (
            prefix
            for prefix in _handoff_prefixes_for_target(target_username)
            if stripped.lower().startswith(prefix.lower())
        ),
        "",
    )
    if not matched_prefix:
        return response
    try:
        payload = json.loads(stripped[len(matched_prefix) :].strip())
    except json.JSONDecodeError:
        return response
    body = str(payload.get("body") or "").strip() if isinstance(payload, dict) else ""
    return body or response


def _a2a_status_envelope(target_username: str, task_id: str, body: str) -> str:
    target = _canonical_handoff_target(target_username)
    source = BOT_USERNAME or "BridgeBot"
    payload = {
        "from": source,
        "to": target,
        "task_id": f"{task_id}:status",
        "ttl": 1,
        "requires_response": False,
        "type": "status",
        "body": body,
    }
    return f"/handoff@{target} {json.dumps(payload, separators=(',', ':'))}"


def _a2a_autowrap_result(target_username: str, task_id: str, body: str) -> str:
    """Wrap a raw agent response in a valid A2A result envelope.

    When the agent produces plain text instead of a structured /handoff
    envelope, the bridge auto-wraps it so the requester still receives
    a valid A2A result.  The ttl is 0 (terminal) and requires_response
    is False — the requester can decide whether to follow up.
    """
    target = _canonical_handoff_target(target_username)
    source = BOT_USERNAME or "BridgeBot"
    payload = {
        "from": source,
        "to": target,
        "task_id": task_id,
        "ttl": 0,
        "requires_response": False,
        "type": "result",
        "body": body,
    }
    return f"/handoff@{target} {json.dumps(payload, indent=2)}"


def should_process_group_message(message: dict, text: str, caption: str) -> tuple[bool, str, str, str | None]:
    """Return whether a message should run, sanitized content, and optional bridge reply."""
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")
    from_user = message.get("from") or {}
    user_id = from_user.get("id")
    sender_is_bot = bool(from_user.get("is_bot"))
    username = from_user.get("username") or ""
    raw = text or caption or ""

    log.info(
        "Incoming Telegram update: chat_id=%s chat_type=%s from_id=%s from_username=%s text=%s",
        chat_id,
        chat_type,
        user_id,
        username,
        _redact_log_text(raw)[:120] if raw else "[media]",
    )

    if BOT_ID and user_id == BOT_ID:
        return False, text, caption, None

    if chat_type == "private":
        if sender_is_bot or user_id not in ALLOWED_USERS:
            log.warning("Rejected private message from sender %s (%s)", user_id, username)
            return False, text, caption, None
        return True, text, caption, None

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        log.warning("Rejected message from non-allowlisted chat %s type=%s", chat_id, chat_type)
        return False, text, caption, None

    if chat_type in {"group", "supergroup", "channel"}:
        if sender_is_bot:
            # Try parsing as a targeted handoff FIRST — accept valid handoffs
            # addressed to this bot regardless of whether sender is pre-registered.
            ok, prompt = _parse_handoff(raw)
            if ok:
                if user_id not in ALLOWED_BOT_IDS:
                    log.info("Accepted targeted handoff from unregistered bot %s (%s)", user_id, username)
                return (ok, prompt if text else text, prompt if caption else caption, None)
            if prompt == A2A_IGNORED:
                return False, text, caption, None
            # For non-handoff messages, require bot to be in allowlist
            if user_id not in ALLOWED_BOT_IDS:
                log.warning("Rejected group bot sender %s (%s)", user_id, username)
                return False, text, caption, None
            if raw and _is_a2a_guidance(raw):
                log.info("Ignored peer A2A syntax guidance from bot sender %s (%s)", user_id, username)
                return False, text, caption, None
            if raw:
                log.info("Silently ignoring bot-originated raw message from %s (%s)", user_id, username)
                return False, text, caption, None
            return False, text, caption, None

        if user_id not in ALLOWED_USERS:
            log.warning("Rejected group user sender %s (%s)", user_id, username)
            return False, text, caption, None

        mention = f"@{BOT_USERNAME}".lower() if BOT_USERNAME else ""
        reply = message.get("reply_to_message") or {}
        reply_to_bot = ((reply.get("from") or {}).get("id") == BOT_ID) if BOT_ID else False
        mentions_bot = bool(mention and mention in raw.lower())
        if not mentions_bot and not reply_to_bot:
            log.info("Ignored group message not addressed to @%s", BOT_USERNAME)
            return False, text, caption, None
        if mentions_bot:
            stripped = raw.replace(f"@{BOT_USERNAME}", "").replace(f"@{BOT_USERNAME.lower()}", "").strip()
            if text:
                text = stripped
            else:
                caption = stripped
        return True, text, caption, None

    return False, text, caption, None


# --- State ---


class PromptQueue:
    """FIFO queue with human priority, bounded A2A batching, and explicit clearing."""

    def __init__(self):
        self._items = deque()
        self._condition = asyncio.Condition()
        self._unfinished_tasks = 0

    def qsize(self) -> int:
        return len(self._items)

    def event_count(self) -> int:
        return sum(len(item.get("a2a_events") or [None]) for item in self._items)

    def automated_count(self) -> int:
        return sum(1 for item in self._items if item.get("automated"))

    def unfinished_count(self) -> int:
        """Return queued plus dequeued work that has not reached task_done()."""
        return self._unfinished_tasks

    def _can_coalesce(self, existing: dict, incoming: dict) -> bool:
        existing_events = existing.get("a2a_events") or []
        incoming_events = incoming.get("a2a_events") or []
        if not existing_events or not incoming_events:
            return False
        if existing.get("coalesce_key") != incoming.get("coalesce_key"):
            return False
        if len(existing_events) + len(incoming_events) > A2A_QUEUE_COALESCE_MAX_EVENTS:
            return False
        total_chars = sum(len(event["prompt"]) for event in existing_events + incoming_events)
        return total_chars <= A2A_QUEUE_COALESCE_MAX_CHARS

    async def put(self, item: dict, *, front: bool = False) -> dict:
        async with self._condition:
            if not front and self._items and self._can_coalesce(self._items[-1], item):
                existing = self._items[-1]
                existing["a2a_events"].extend(item["a2a_events"])
                existing["a2a_task_ids"].extend(item["a2a_task_ids"])
                existing["text"] = _format_coalesced_a2a_prompt(existing["a2a_events"])
                log.info(
                    "Coalesced A2A task_id=%s into queue position %s (%s events)",
                    item["a2a_task_ids"][0],
                    len(self._items),
                    len(existing["a2a_events"]),
                )
                return {
                    "position": len(self._items),
                    "coalesced": True,
                    "batch_size": len(existing["a2a_events"]),
                }

            if front:
                self._items.appendleft(item)
                position = 1
            elif not item.get("automated"):
                position = len(self._items) + 1
                for index, pending in enumerate(self._items):
                    if pending.get("automated"):
                        self._items.insert(index, item)
                        position = index + 1
                        break
                else:
                    self._items.append(item)
            else:
                self._items.append(item)
                position = len(self._items)

            self._unfinished_tasks += 1
            self._condition.notify(1)
            return {"position": position, "coalesced": False, "batch_size": 1}

    async def get(self) -> dict:
        async with self._condition:
            while not self._items:
                await self._condition.wait()
            return self._items.popleft()

    def task_done(self):
        if self._unfinished_tasks <= 0:
            raise ValueError("task_done() called too many times")
        self._unfinished_tasks -= 1

    async def clear(self) -> list[dict]:
        async with self._condition:
            removed = list(self._items)
            self._items.clear()
            self._unfinished_tasks = max(0, self._unfinished_tasks - len(removed))
            return removed


_active_codex_proc: asyncio.subprocess.Process | None = None
_active_codex_chat_id: int | None = None
_active_codex_started: float | None = None  # UTC wall clock for operator health output
_active_codex_started_monotonic: float | None = None  # duration/deadline authority
_shutting_down = False

_watchdog_current_item: dict | None = None
_watchdog_last_progress: float = 0.0
_a2a_guidance_last_sent: dict[str, float] = {}

_prompt_queue: PromptQueue | None = None
_queue_worker_task: asyncio.Task | None = None
_queue_last_dequeue: float = 0.0
_queue_health_task: asyncio.Task | None = None
_pending_thread_contexts: dict[str, dict[str, str | None]] = {}
_resolved_thread_contexts: dict[str, str] = {}

state = {
    "default_session_id": None,
    "active_folder": HOME,
    "folders": {
        "home": HOME,
    },
    "folder_sessions": {},
    "sessions": {},
    "last_invocation": None,
    "checkpoint_rotations": {},
    "pending_checkpoint_bootstrap": {},
    "last_checkpoints": {},
}


def load_state():
    global state
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            state.update(saved)
            state.setdefault("active_folder", HOME)
            state.setdefault("folders", {"home": HOME})
            state.setdefault("folder_sessions", {})
            state.setdefault("sessions", {})
            state.setdefault("checkpoint_rotations", {})
            state.setdefault("pending_checkpoint_bootstrap", {})
            state.setdefault("last_checkpoints", {})
            if state["checkpoint_rotations"]:
                log.warning(
                    "Discarding %s stale checkpoint rotation(s); sessions remain selected",
                    len(state["checkpoint_rotations"]),
                )
                state["checkpoint_rotations"] = {}
            log.info(
                "Loaded state: folder=%s, %s sessions",
                state["active_folder"],
                len(state["sessions"]),
            )
        except Exception as e:
            log.warning("Failed to load state: %s", e)


def save_state():
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.warning("Failed to save state: %s", e)


def record_session(session_id: str, folder: str, label: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    if session_id not in state["sessions"]:
        state["sessions"][session_id] = {
            "created": now,
            "last_used": now,
            "message_count": 1,
            "label": label,
            "saved": False,
            "folder": folder,
        }
    else:
        state["sessions"][session_id]["last_used"] = now
        state["sessions"][session_id]["message_count"] += 1
        if label and not state["sessions"][session_id].get("label"):
            state["sessions"][session_id]["label"] = label
    save_state()


def get_folder_display_name(path: str) -> str:
    for name, fp in state["folders"].items():
        if fp == path:
            return name
    return Path(path).name


def find_session(query: str) -> list[str]:
    query_lower = query.lower()
    folder = state["active_folder"]
    matches = []

    for sid, info in state["sessions"].items():
        if info.get("folder", HOME) != folder:
            continue
        if sid.startswith(query):
            matches.append(sid)
            continue
        label = info.get("label", "").lower()
        if query_lower in label:
            matches.append(sid)
    return matches


def _parse_new_argument(arg: str) -> tuple[bool, str | None]:
    parts = arg.split(maxsplit=1)
    if parts and parts[0].lower() in {"force", "--force"}:
        return True, parts[1].strip() if len(parts) > 1 else None
    return False, arg or None


def _rotate_codex_session(folder: str, label: str | None = None) -> None:
    previous_context = _pending_thread_contexts.get(folder)
    if previous_context:
        _resolved_thread_contexts.pop(previous_context["id"], None)
    state["folder_sessions"].pop(folder, None)
    if state.get("active_folder") == folder:
        state["default_session_id"] = None
    _pending_thread_contexts[folder] = {
        "id": uuid.uuid4().hex,
        "label": label,
    }
    state.pop("_pending_label", None)
    save_state()


def get_or_create_pending_thread_context(folder: str) -> dict[str, str | None]:
    context = _pending_thread_contexts.get(folder)
    if context is None:
        context = {
            "id": uuid.uuid4().hex,
            "label": state.pop("_pending_label", None),
        }
        _pending_thread_contexts[folder] = context
        save_state()
    return context


def context_is_still_selected(folder: str, session_id: str | None, context_id: str | None) -> bool:
    if state["active_folder"] != folder:
        return False
    if state.get("default_session_id") != session_id:
        return False
    if session_id is not None or context_id is None:
        return True
    pending = _pending_thread_contexts.get(folder)
    return pending is not None and pending.get("id") == context_id


def folder_context_is_current(folder: str, context_id: str | None) -> bool:
    if context_id is None:
        return True
    pending = _pending_thread_contexts.get(folder)
    return pending is not None and pending.get("id") == context_id


# --- Telegram API ---

_http_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(POLL_TIMEOUT + 30, connect=10))
    return _http_client


async def tg_api(method: str, data: dict | None = None) -> dict | None:
    client = await get_client()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        if data:
            resp = await client.post(url, json=data)
        else:
            resp = await client.get(url)
        result = resp.json()
        if not result.get("ok"):
            log.warning("Telegram API %s not ok: %s", method, result.get("description", ""))
        return result
    except Exception as e:
        log.error("Telegram API error (%s): %s", method, e)
        return None


async def send_typing(chat_id: int):
    await tg_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def sanitize_markdown(text: str) -> str:
    import re

    def replace_code_block(m):
        code = m.group(1)
        return f"`{code.strip()}`"

    text = re.sub(r"```\w*\n(.*?)```", replace_code_block, text, flags=re.DOTALL)
    text = text.replace("```", "`")

    backtick_count = text.count("`")
    if backtick_count % 2 != 0:
        text += "`"

    in_code = False
    underscore_count = 0
    for ch in text:
        if ch == "`":
            in_code = not in_code
        elif ch == "_" and not in_code:
            underscore_count += 1
    if underscore_count % 2 != 0:
        text += "_"

    in_code = False
    asterisk_count = 0
    for ch in text:
        if ch == "`":
            in_code = not in_code
        elif ch == "*" and not in_code:
            asterisk_count += 1
    if asterisk_count % 2 != 0:
        text += "*"

    return text


async def send_message(chat_id: int, text: str, reply_to: int | None = None):
    retired_target = _retired_outbound_handoff_target(text)
    if retired_target:
        log.warning("Suppressed outbound A2A handoff to retired target @%s", retired_target)
        return None
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_MAX_LENGTH:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, TELEGRAM_MAX_LENGTH)
        if split_at == -1:
            split_at = TELEGRAM_MAX_LENGTH
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    for i, chunk in enumerate(chunks):
        data = {"chat_id": chat_id, "text": sanitize_markdown(chunk)}
        if i == 0 and reply_to:
            data["reply_to_message_id"] = reply_to
        data["parse_mode"] = "Markdown"
        result = await tg_api("sendMessage", data)
        if result is None or not result.get("ok"):
            data["text"] = chunk
            data.pop("parse_mode", None)
            await tg_api("sendMessage", data)


async def send_plain_message(chat_id: int, text: str, reply_to: int | None = None):
    retired_target = _retired_outbound_handoff_target(text)
    if retired_target:
        log.warning("Suppressed outbound A2A handoff to retired target @%s", retired_target)
        return None
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_MAX_LENGTH:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, TELEGRAM_MAX_LENGTH)
        if split_at == -1:
            split_at = TELEGRAM_MAX_LENGTH
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    for i, chunk in enumerate(chunks):
        data = {"chat_id": chat_id, "text": chunk}
        if i == 0 and reply_to:
            data["reply_to_message_id"] = reply_to
        await tg_api("sendMessage", data)


# --- Media Download ---


async def download_telegram_file(file_id: str, filename: str, msg_id: int) -> Path | None:
    try:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        result = await tg_api("getFile", {"file_id": file_id})
        if not result or not result.get("ok"):
            return None
        file_path = result["result"]["file_path"]
        client = await get_client()
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        local_path = MEDIA_DIR / f"{msg_id}_{filename}"
        local_path.write_bytes(resp.content)
        return local_path
    except Exception as e:
        log.error("Failed to download file %s: %s", file_id, e)
        return None


async def extract_media(message: dict) -> tuple[list[str], str | None]:
    msg_id = message.get("message_id", 0)

    if "photo" in message:
        sizes = message["photo"]
        best = max(sizes, key=lambda s: s.get("file_size", 0))
        path = await download_telegram_file(best["file_id"], "photo.jpg", msg_id)
        if path:
            return [str(path)], "image"

    if "document" in message:
        doc = message["document"]
        fname = doc.get("file_name", "document")
        path = await download_telegram_file(doc["file_id"], fname, msg_id)
        if path:
            mime = doc.get("mime_type", "")
            kind = "image" if mime.startswith("image/") else "file"
            return [str(path)], kind

    if "voice" in message:
        voice = message["voice"]
        path = await download_telegram_file(voice["file_id"], "voice.ogg", msg_id)
        if path:
            return [str(path)], "voice message"

    if "video_note" in message:
        vn = message["video_note"]
        path = await download_telegram_file(vn["file_id"], "video_note.mp4", msg_id)
        if path:
            return [str(path)], "video note"

    if "video" in message:
        vid = message["video"]
        fname = vid.get("file_name", "video.mp4")
        path = await download_telegram_file(vid["file_id"], fname, msg_id)
        if path:
            return [str(path)], "video"

    if "sticker" in message:
        sticker = message["sticker"]
        if sticker.get("is_animated") or sticker.get("is_video"):
            return [], None
        path = await download_telegram_file(sticker["file_id"], "sticker.webp", msg_id)
        if path:
            return [str(path)], f"sticker {sticker.get('emoji', '')}".strip()

    return [], None


async def handle_media_message(chat_id: int, msg_id: int, message: dict, user_text: str):
    image_paths, media_type = await extract_media(message)
    if not image_paths and not media_type:
        await send_message(
            chat_id,
            "Could not download the attachment. Try sending it as a file.",
            reply_to=msg_id,
        )
        return

    prompt_parts = []
    if media_type == "image":
        prompt_parts.append("The user sent an image. Inspect it and respond.")
    elif media_type:
        prompt_parts.append(f"The user sent a {media_type}. A local copy is available in the workspace if needed.")

    if user_text:
        prompt_parts.append(user_text)
    elif media_type != "image":
        prompt_parts.append("Please examine it and explain the relevant details.")

    await enqueue_prompt(
        chat_id,
        msg_id,
        "\n\n".join(prompt_parts),
        images=image_paths,
    )


async def cleanup_old_media():
    if not MEDIA_DIR.exists():
        return
    cutoff = time.time() - 3600
    for f in MEDIA_DIR.iterdir():
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# --- Codex Execution ---


def build_codex_command(
    prompt: str,
    cwd: str,
    session_id: str | None = None,
    images: list[str] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> list[str]:
    images = images or []
    effective_model = model or CODEX_MODEL
    effective_reasoning_effort = reasoning_effort or CODEX_REASONING_EFFORT
    if session_id:
        cmd = ["codex", "exec", "resume", "--json"]
        if CODEX_SKIP_GIT_REPO_CHECK:
            cmd.append("--skip-git-repo-check")
        if effective_model:
            cmd.extend(["-m", effective_model])
        if effective_reasoning_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{effective_reasoning_effort}"'])
        if CODEX_FULL_AUTO:
            cmd.append("--full-auto")
        # `codex exec resume` on the current CLI does not expose `-s/--sandbox`.
        # When the bridge is configured for full-access mode, use the bypass flag
        # so resumed sessions inherit the intended unrestricted execution model.
        if CODEX_DANGEROUS_BYPASS or CODEX_SANDBOX == "danger-full-access":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        for image in images:
            cmd.extend(["-i", image])
        cmd.extend([session_id, prompt])
        return cmd

    cmd = ["codex", "exec", "--json", "-C", cwd, "-s", CODEX_SANDBOX]
    if CODEX_SKIP_GIT_REPO_CHECK:
        cmd.append("--skip-git-repo-check")
    if effective_model:
        cmd.extend(["-m", effective_model])
    if effective_reasoning_effort:
        cmd.extend(["-c", f'model_reasoning_effort="{effective_reasoning_effort}"'])
    if CODEX_FULL_AUTO:
        cmd.append("--full-auto")
    if CODEX_DANGEROUS_BYPASS:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    for extra_dir in CODEX_EXTRA_DIRS:
        cmd.extend(["--add-dir", extra_dir])
    for image in images:
        cmd.extend(["-i", image])
    cmd.append(prompt)
    return cmd


def _describe_command_execution(item: dict) -> str:
    command = item.get("command", "")
    if not command:
        return "Running command"
    return f"Running: `{_redact_log_text(command)[:80]}`"


async def _notify_turn_progress(
    chat_id: int,
    started_monotonic: float,
    stop_event: asyncio.Event,
) -> None:
    """Send content-free status checkpoints without stopping the active turn."""
    if CODEX_PROGRESS_UPDATE_INTERVAL <= 0:
        return

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=CODEX_PROGRESS_UPDATE_INTERVAL
            )
            return
        except asyncio.TimeoutError:
            elapsed_minutes = max(
                1, int((time.monotonic() - started_monotonic) // 60)
            )
            try:
                await send_message(
                    chat_id,
                    f"_Still working: {elapsed_minutes} minutes elapsed. "
                    "This is a status update; the turn is continuing._",
                )
            except Exception as exc:
                log.warning(
                    "Unable to send turn progress checkpoint: %s",
                    type(exc).__name__,
                )


async def run_codex(
    prompt: str,
    chat_id: int,
    cwd: str,
    session_id: str | None = None,
    images: list[str] | None = None,
    suppress_progress_messages: bool = False,
    suppress_footer: bool = False,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, str | None]:
    global _active_codex_proc, _active_codex_started, _active_codex_started_monotonic
    global _watchdog_current_item, _watchdog_last_progress

    cmd = build_codex_command(
        prompt,
        cwd=cwd,
        session_id=session_id,
        images=images,
        model=model,
        reasoning_effort=reasoning_effort,
    )
    log.info(
        "Codex in %s: %s | prompt: %s",
        cwd,
        _redact_log_text(" ".join(cmd[:8])),
        _redact_log_text(prompt)[:80],
    )

    result_session_id = session_id
    latest_agent_message = None
    usage = None
    last_activity_update = 0.0

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=4 * 1024 * 1024,
    )
    _active_codex_proc = proc
    _active_codex_started = time.time()
    _active_codex_started_monotonic = time.monotonic()
    progress_stop = asyncio.Event()
    progress_task: asyncio.Task | None = None
    if not suppress_progress_messages:
        progress_task = asyncio.create_task(
            _notify_turn_progress(
                chat_id, _active_codex_started_monotonic, progress_stop
            )
        )

    hit_ceiling = False
    try:
        while True:
            # Bound the read itself. An `async for` deadline check cannot stop a live
            # child that holds stdout open without emitting another line.
            remaining = CODEX_MAX_RUNTIME - (
                time.monotonic() - _active_codex_started_monotonic
            )
            if remaining <= 0:
                hit_ceiling = True
                log.error(
                    "Codex child pid=%s hit CODEX_MAX_RUNTIME (%.0fs > %ss) mid-stream "
                    "-- ending the turn",
                    proc.pid,
                    time.monotonic() - _active_codex_started_monotonic,
                    CODEX_MAX_RUNTIME,
                )
                break

            try:
                raw_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=remaining
                )
            except asyncio.TimeoutError:
                hit_ceiling = True
                log.error(
                    "Codex child pid=%s hit CODEX_MAX_RUNTIME while stdout was idle",
                    proc.pid,
                )
                break
            if not raw_line:
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            if event_type == "thread.started":
                result_session_id = event.get("thread_id", result_session_id)
            elif event_type == "item.started":
                item = event.get("item", {})
                if item.get("type") == "command_execution":
                    _watchdog_current_item = {
                        "type": item.get("type"),
                        "command": item.get("command", ""),
                        "started": time.time(),
                    }
                    _watchdog_last_progress = time.time()
                    now = time.time()
                    if (not suppress_progress_messages) and now - last_activity_update > 15:
                        await send_message(chat_id, f"_... {_describe_command_execution(item)}_")
                        last_activity_update = now
            elif event_type == "item.completed":
                item = event.get("item", {})
                item_type = item.get("type")
                if item_type == "agent_message":
                    text = item.get("text", "").strip()
                    if text:
                        latest_agent_message = text
                        _watchdog_last_progress = time.time()
                elif item_type == "command_execution":
                    _watchdog_current_item = None
                    _watchdog_last_progress = time.time()
            elif event_type == "turn.completed":
                usage = event.get("usage")
            elif event_type == "error":
                latest_agent_message = event.get("message", "Codex execution failed")

        await _wait_child_bounded(proc, _active_codex_started_monotonic)

        if hit_ceiling:
            # Say so rather than returning a silently-truncated answer as if complete.
            mins = CODEX_MAX_RUNTIME // 60
            note = (
                f"\n\n_Turn stopped at the {mins}-minute ceiling. "
                f"This is a partial result -- ask me to continue if it was still useful._"
            )
            latest_agent_message = (
                latest_agent_message or "_No output produced before the ceiling._"
            ) + note

        if latest_agent_message is None:
            err = ""
            if proc.stderr:
                err = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
            return f"(empty response)\n\nstderr: {err[:300]}", result_session_id

        if suppress_footer:
            return latest_agent_message, result_session_id

        footer_parts = [get_folder_display_name(cwd)]
        if usage and usage.get("output_tokens") is not None:
            footer_parts.insert(0, f"{usage['output_tokens']} out tok")
        footer = f"\n\n_({' • '.join(footer_parts)})_"
        return latest_agent_message + footer, result_session_id
    except Exception as e:
        log.error("Codex stream error: %s", e)
        return f"Error: {e}", result_session_id
    finally:
        progress_stop.set()
        if progress_task is not None:
            await asyncio.gather(progress_task, return_exceptions=True)
        if proc.returncode is None:
            await _terminate_child(proc, "run_codex exit before child completion")
        # ella-ai#1174: `busy` is computed from _active_codex_proc.returncode, so ANY path
        # that leaves this set with a live child pins the bridge busy forever and the queue
        # never drains. Clearing it here makes that structurally impossible.
        _active_codex_proc = None
        _active_codex_started = None
        _active_codex_started_monotonic = None


async def _wait_child_bounded(
    proc: asyncio.subprocess.Process, started_monotonic: float
) -> None:
    """Wait for the codex child, with an absolute ceiling and SIGTERM->SIGKILL escalation.

    ella-ai#1174. This replaced a bare `await proc.wait()`, which had NO timeout at all --
    CODEX_TIMEOUT was only ever applied on the shutdown path, so a child could run
    unbounded and wedge the bridge (twice on 2026-08-02).

    Deliberately progress-aware: a child that is still emitting events is doing real work
    and is NOT killed at CODEX_TIMEOUT. Only the absolute CODEX_MAX_RUNTIME ceiling stops
    it, so we never destroy a healthy long-running turn to satisfy a timer. Stagnation
    (progress stopped) remains the existing watchdog's job.
    """
    while True:
        remaining = CODEX_MAX_RUNTIME - (time.monotonic() - started_monotonic)
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(proc.wait(), timeout=min(remaining, 30))
            return  # exited on its own -- the normal path
        except asyncio.TimeoutError:
            continue  # still running; re-check the ceiling

    age = time.monotonic() - started_monotonic
    log.error(
        "Codex child pid=%s exceeded CODEX_MAX_RUNTIME (%.0fs > %ss) -- escalating",
        proc.pid, age, CODEX_MAX_RUNTIME,
    )
    await _terminate_child(proc, "absolute runtime ceiling")


async def _terminate_child(
    proc: asyncio.subprocess.Process, reason: str
) -> None:
    """Terminate and reap a child before releasing bridge ownership."""
    if proc.returncode is not None:
        await proc.wait()
        return

    log.error("Terminating Codex child pid=%s: %s", proc.pid, reason)
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=CODEX_KILL_GRACE)
        log.error("Codex child pid=%s terminated on SIGTERM", proc.pid)
    except asyncio.TimeoutError:
        try:
            proc.kill()  # SIGKILL: a child that ignores SIGTERM must not pin busy
            await asyncio.wait_for(proc.wait(), timeout=CODEX_KILL_GRACE)
            log.error("Codex child pid=%s SIGKILLed after grace", proc.pid)
        except Exception as exc:
            log.error("Codex child pid=%s survived SIGKILL: %s", proc.pid, exc)
    except ProcessLookupError:
        await proc.wait()


# --- Watchdog ---


def _is_instant_kill_command(command: str) -> bool:
    cmd_lower = command.lower().strip()
    for pattern in WATCHDOG_INSTANT_KILL_PATTERNS:
        if pattern.lower() in cmd_lower:
            return True
    if "ssh " in cmd_lower and any(p in cmd_lower for p in ["tail -f", "tail --follow", "journalctl -f"]):
        return True
    return False


async def _watchdog_monitor(chat_id: int):
    global _watchdog_last_progress

    _watchdog_last_progress = time.time()
    while True:
        await asyncio.sleep(10)
        if _active_codex_proc is None or _active_codex_proc.returncode is not None:
            return

        now = time.time()
        stagnation_time = now - _watchdog_last_progress
        if stagnation_time > WATCHDOG_STAGNATION_KILL:
            await _watchdog_kill(
                chat_id,
                f"No progress for {stagnation_time / 60:.0f}min",
                _watchdog_current_item,
            )
            return

        if _watchdog_current_item is None:
            continue

        command = _watchdog_current_item.get("command", "")
        item_elapsed = now - _watchdog_current_item["started"]
        timeout = WATCHDOG_COMMAND_TIMEOUT
        if not command:
            timeout = WATCHDOG_DEFAULT_TIMEOUT
        if item_elapsed < timeout:
            continue

        if command and _is_instant_kill_command(command):
            await _watchdog_kill(
                chat_id,
                f"Known infinite command: `{_redact_log_text(command)[:60]}`",
                _watchdog_current_item,
            )
            return


async def _watchdog_kill(chat_id: int, reason: str, item_info: dict | None):
    command = ""
    if item_info:
        command = item_info.get("command", "")

    log.warning("Watchdog KILL: %s", reason)

    if _active_codex_proc and _active_codex_proc.returncode is None:
        try:
            _active_codex_proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(_active_codex_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                _active_codex_proc.terminate()
                try:
                    await asyncio.wait_for(_active_codex_proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    _active_codex_proc.kill()
        except ProcessLookupError:
            pass

    detail = f"\nCommand: `{_redact_log_text(command)[:100]}`" if command else ""
    await send_message(
        chat_id,
        f"*Watchdog killed stuck process*\nReason: {reason}{detail}\n\n"
        "_Session preserved — send a new message to continue._",
    )


# --- Command Handlers ---


async def enqueue_checkpoint(
    chat_id: int,
    msg_id: int,
    *,
    note: str = "",
    rotate: bool = False,
    label: str | None = None,
) -> None:
    global _prompt_queue
    if _prompt_queue is None:
        _prompt_queue = PromptQueue()

    folder = state["active_folder"]
    if rotate and folder in state["checkpoint_rotations"]:
        await send_message(
            chat_id,
            "A guarded session rotation is already pending. Wait for its result or use `/new force`.",
            reply_to=msg_id,
        )
        return

    session_id = state.get("default_session_id")
    context_id = None
    if session_id is None:
        context_id = get_or_create_pending_thread_context(folder)["id"]

    rotation_id = uuid.uuid4().hex if rotate else None
    if rotation_id:
        state["checkpoint_rotations"][folder] = {
            "id": rotation_id,
            "label": label,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        save_state()

    item = {
        "kind": "checkpoint",
        "chat_id": chat_id,
        "msg_id": msg_id,
        "text": "",
        "images": [],
        "folder": folder,
        "session_id": session_id,
        "context_id": context_id,
        "pending_label": None,
        "a2a_reply_target": None,
        "automated": False,
        "a2a_events": [],
        "a2a_task_ids": [],
        "coalesce_key": None,
        "checkpoint_note": note,
        "checkpoint_rotate": rotate,
        "checkpoint_label": label,
        "checkpoint_rotation_id": rotation_id,
    }
    result = await _prompt_queue.put(item)
    action = "Checkpoint and guarded rotation" if rotate else "Checkpoint"
    suffix = f" (queue position {result['position']})" if result["position"] > 1 else ""
    await send_message(chat_id, f"{action} queued{suffix}.", reply_to=msg_id)


async def _process_checkpoint(item: dict) -> None:
    global _active_codex_chat_id
    chat_id = item["chat_id"]
    msg_id = item["msg_id"]
    folder = item["folder"]
    session_id = item.get("session_id")
    context_id = item.get("context_id")
    effective_session_id = session_id
    if effective_session_id is None and context_id:
        effective_session_id = _resolved_thread_contexts.get(context_id)

    _active_codex_chat_id = chat_id
    draft_path = None
    try:
        prepared = await prepare_checkpoint(
            project=folder,
            runtime="codex",
            bridge="codex-telegram-bridge",
            session_id=effective_session_id,
            note=item.get("checkpoint_note", ""),
        )
        draft_path = prepared["draft"]
        response, result_session_id = await run_codex(
            checkpoint_save_prompt(draft_path),
            chat_id,
            cwd=folder,
            session_id=effective_session_id,
            suppress_progress_messages=True,
            suppress_footer=True,
        )
        if result_session_id:
            record_session(result_session_id, folder=folder)
        committed = await commit_checkpoint(draft_path)
        latest_path = committed["latest_json"]
        state["last_checkpoints"][folder] = {
            "path": latest_path,
            "checkpoint_id": committed["checkpoint_id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        rotate = item.get("checkpoint_rotate", False)
        rotation_id = item.get("checkpoint_rotation_id")
        current_rotation = state["checkpoint_rotations"].get(folder)
        rotation_is_current = bool(
            rotate and current_rotation and current_rotation.get("id") == rotation_id
        )
        if rotation_is_current:
            state["checkpoint_rotations"].pop(folder, None)
            state["pending_checkpoint_bootstrap"][folder] = latest_path
            _rotate_codex_session(folder, item.get("checkpoint_label"))
        else:
            save_state()

        detail = " Session rotated; the next prompt will restore it." if rotation_is_current else ""
        await send_message(
            chat_id,
            f"Checkpoint saved: `{committed['checkpoint_id']}`.{detail}",
            reply_to=msg_id,
        )
        log.info(
            "Checkpoint committed id=%s folder=%s response=%s",
            committed["checkpoint_id"],
            folder,
            _redact_log_text(response)[:80],
        )
    except Exception as exc:
        rotation_id = item.get("checkpoint_rotation_id")
        current_rotation = state["checkpoint_rotations"].get(folder)
        if current_rotation and current_rotation.get("id") == rotation_id:
            state["checkpoint_rotations"].pop(folder, None)
            save_state()
        await send_message(
            chat_id,
            "Checkpoint failed; the current session was preserved. "
            f"Reason: `{_redact_log_text(str(exc))[:500]}` Use `/new force` only to bypass preservation.",
            reply_to=msg_id,
        )
    finally:
        remove_draft(draft_path)
        _active_codex_chat_id = None


async def handle_command(chat_id: int, msg_id: int, text: str):
    global CODEX_MODEL

    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/help", "/start"):
        folder_name = get_folder_display_name(state["active_folder"])
        await send_message(
            chat_id,
            (
                "*Codex Telegram Bridge*\n"
                f"Active folder: `{folder_name}` (`{state['active_folder']}`)\n\n"
                "Send any message, image, or file to work with Codex.\n\n"
                "*Folder Commands:*\n"
                "`/folders` — List folders\n"
                "`/folder <name>` — Switch folder\n"
                "`/folder add <name> <path>` — Register folder\n"
                "`/folder create <name> [path]` — Create folder\n"
                "`/folder rm <name>` — Remove folder\n"
                "`/clone <url> [name]` — Clone repo + switch\n"
                "`/init` — Init current folder\n\n"
                "*Session Commands:*\n"
                "`/checkpoint [note]` — Save durable project context\n"
                "`/new [label]` — Checkpoint, then start a fresh thread\n"
                "`/new force [label]` — Start fresh without a checkpoint\n"
                "`/save [label]` — Bookmark current thread\n"
                "`/rename <label>` — Rename current thread\n"
                "`/sessions` — List known threads for this folder\n"
                "`/resume <id|name|checkpoint>` — Resume a thread or latest checkpoint\n"
                "`/interrupt [msg]` — Stop the active task; keep queued work\n"
                "`/stop [msg]` — Stop the active task and discard queued work\n"
                "`/clearqueue` — Discard queued work without stopping the active task\n\n"
                "*Status:*\n"
                "`/status` — Bridge status\n"
                "`/watchdog` — Watchdog status\n"
                "`/model [id]` — Show or set Codex model"
            ),
            reply_to=msg_id,
        )
        return

    if cmd == "/folders":
        lines = ["*Project Folders:*\n"]
        for name, path in sorted(state["folders"].items()):
            active = " ← active" if path == state["active_folder"] else ""
            has_codex_md = "✅" if Path(path, "AGENTS.md").exists() or Path(path, "README.md").exists() else ""
            has_git = "📦" if Path(path, ".git").exists() else ""
            last_sid = state["folder_sessions"].get(path)
            session_info = ""
            if last_sid:
                info = state["sessions"].get(last_sid, {})
                label = info.get("label", "")
                if label:
                    session_info = f"\n  Last: _{label}_"
            lines.append(f"`{name}` {has_git}{has_codex_md} `{path}`{active}{session_info}")
        await send_message(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd == "/folder":
        if not arg:
            folder_name = get_folder_display_name(state["active_folder"])
            await send_message(
                chat_id,
                f"Current folder: `{folder_name}` (`{state['active_folder']}`)",
                reply_to=msg_id,
            )
            return

        sub_parts = arg.split(maxsplit=2)
        sub_cmd = sub_parts[0].lower()

        if sub_cmd == "add" and len(sub_parts) >= 3:
            name = sub_parts[1]
            path = os.path.expanduser(sub_parts[2])
            if not os.path.isdir(path):
                await send_message(chat_id, f"Directory not found: `{path}`", reply_to=msg_id)
                return
            state["folders"][name] = path
            save_state()
            await send_message(chat_id, f"Registered `{name}` → `{path}`", reply_to=msg_id)
            return

        if sub_cmd == "create" and len(sub_parts) >= 2:
            name = sub_parts[1]
            if name in state["folders"]:
                await send_message(chat_id, f"Folder `{name}` already exists.", reply_to=msg_id)
                return
            path = os.path.expanduser(sub_parts[2]) if len(sub_parts) >= 3 else os.path.join(HOME, "projects", name)
            os.makedirs(path, exist_ok=True)
            state["folders"][name] = path
            state["active_folder"] = path
            state["default_session_id"] = None
            save_state()
            await send_message(
                chat_id,
                f"Created and switched to `{name}` at `{path}`.",
                reply_to=msg_id,
            )
            return

        if sub_cmd == "rm" and len(sub_parts) >= 2:
            name = sub_parts[1]
            if name == "home":
                await send_message(chat_id, "Can't remove `home`.", reply_to=msg_id)
                return
            removed = state["folders"].pop(name, None)
            if not removed:
                await send_message(chat_id, f"No folder named `{name}`.", reply_to=msg_id)
                return
            if state["active_folder"] == removed:
                state["active_folder"] = HOME
                state["default_session_id"] = state["folder_sessions"].get(HOME)
            save_state()
            await send_message(chat_id, f"Removed `{name}`.", reply_to=msg_id)
            return

        name = sub_cmd
        if name not in state["folders"]:
            expanded = os.path.expanduser(arg)
            if os.path.isdir(expanded):
                name = Path(expanded).name
                state["folders"][name] = expanded
            else:
                await send_message(chat_id, f"Unknown folder `{name}`.", reply_to=msg_id)
                return

        path = state["folders"][name]
        state["active_folder"] = path
        state["default_session_id"] = state["folder_sessions"].get(path)
        save_state()
        sid = state["default_session_id"]
        session_msg = f"\nContinuing `{sid[:8]}`" if sid else "\nNext message starts a fresh thread."
        await send_message(
            chat_id,
            f"Switched to `{name}`\n`{path}`{session_msg}",
            reply_to=msg_id,
        )
        return

    if cmd == "/clone":
        if not arg:
            await send_message(chat_id, "Usage: `/clone <repo-url> [name]`", reply_to=msg_id)
            return
        clone_parts = arg.split(maxsplit=1)
        repo_url = clone_parts[0]
        name = (
            clone_parts[1].strip()
            if len(clone_parts) >= 2
            else repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
        )
        clone_path = os.path.join(HOME, "projects", name)
        if name in state["folders"] or os.path.exists(clone_path):
            await send_message(chat_id, f"Target already exists: `{clone_path}`", reply_to=msg_id)
            return
        await send_message(chat_id, f"Cloning `{repo_url}` into `{clone_path}`...", reply_to=msg_id)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            repo_url,
            clone_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            await send_message(chat_id, f"Clone failed:\n`{err[:500]}`", reply_to=msg_id)
            return
        state["folders"][name] = clone_path
        state["active_folder"] = clone_path
        state["default_session_id"] = None
        save_state()
        output = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
        await send_message(
            chat_id,
            f"Cloned and switched to `{name}`\n`{clone_path}`\n\n`{output[:500]}`",
            reply_to=msg_id,
        )
        return

    if cmd == "/init":
        folder = state["active_folder"]
        folder_name = get_folder_display_name(folder)
        results = []
        if not Path(folder, ".git").exists():
            proc = subprocess.run(
                ["git", "init"],
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                results.append("git init: done")
            else:
                results.append(f"git init: failed ({proc.stderr.strip()})")
        else:
            results.append("git: already initialized")

        readme = Path(folder, "README.md")
        if not readme.exists():
            readme.write_text(f"# {folder_name}\n\nInitialized via Codex Telegram Bridge.\n")
            results.append("README.md: created")
        else:
            results.append("README.md: already exists")

        gitignore = Path(folder, ".gitignore")
        if not gitignore.exists():
            gitignore.write_text(
                "# Dependencies\nnode_modules/\nvenv/\n.venv/\n\n"
                "# Environment\n.env\n.env.local\n\n"
                "# IDE\n.vscode/\n.idea/\n\n"
                "# OS\n.DS_Store\nThumbs.db\n\n"
                "# Build\ndist/\nbuild/\n__pycache__/\n*.pyc\n"
            )
            results.append(".gitignore: created")
        else:
            results.append(".gitignore: already exists")

        await send_message(
            chat_id,
            f"Initialized `{folder_name}` (`{folder}`):\n\n" + "\n".join(results),
            reply_to=msg_id,
        )
        return

    if cmd == "/checkpoint":
        await enqueue_checkpoint(chat_id, msg_id, note=arg)
        return

    if cmd in {"/new", "/compact"}:
        force, label = _parse_new_argument(arg)
        folder = state["active_folder"]
        if force or not PROJECT_CHECKPOINT_ENABLED:
            state["checkpoint_rotations"].pop(folder, None)
            state["pending_checkpoint_bootstrap"].pop(folder, None)
            _rotate_codex_session(folder, label)
            reason = "Checkpoint bypassed by force." if force else "Checkpointing is disabled."
            await send_message(
                chat_id,
                f"Fresh Codex thread for this folder. {reason}",
                reply_to=msg_id,
            )
            return
        await enqueue_checkpoint(
            chat_id,
            msg_id,
            note="Guarded session rotation requested from Telegram.",
            rotate=True,
            label=label,
        )
        return

    if cmd == "/save":
        sid = state.get("default_session_id")
        if not sid:
            await send_message(chat_id, "No active thread to save.", reply_to=msg_id)
            return
        info = state["sessions"].setdefault(
            sid,
            {
                "created": datetime.now(timezone.utc).isoformat(),
                "last_used": datetime.now(timezone.utc).isoformat(),
                "message_count": 0,
                "label": "",
                "saved": False,
                "folder": state["active_folder"],
            },
        )
        if arg:
            info["label"] = arg
        info["saved"] = True
        save_state()
        await send_message(chat_id, f"📌 Saved `{sid[:8]}`.", reply_to=msg_id)
        return

    if cmd == "/rename":
        sid = state.get("default_session_id")
        if not sid:
            await send_message(chat_id, "No active thread to rename.", reply_to=msg_id)
            return
        state["sessions"].setdefault(
            sid,
            {
                "created": datetime.now(timezone.utc).isoformat(),
                "last_used": datetime.now(timezone.utc).isoformat(),
                "message_count": 0,
                "label": "",
                "saved": False,
                "folder": state["active_folder"],
            },
        )["label"] = arg
        save_state()
        await send_message(chat_id, f"Renamed `{sid[:8]}` to _{arg}_.", reply_to=msg_id)
        return

    if cmd == "/sessions":
        folder = state["active_folder"]
        folder_name = get_folder_display_name(folder)
        items = [(sid, info) for sid, info in state["sessions"].items() if info.get("folder", HOME) == folder]
        items.sort(key=lambda x: x[1].get("last_used", ""), reverse=True)
        if not items:
            await send_message(chat_id, f"No known threads in `{folder_name}`.", reply_to=msg_id)
            return
        lines = [f"*Threads in `{folder_name}`:*\n"]
        for sid, info in items[:20]:
            saved = "📌 " if info.get("saved") else ""
            active = " ← active" if sid == state.get("default_session_id") else ""
            label = info.get("label", "")
            msg_count = info.get("message_count", 0)
            line = f"`{sid[:8]}` {saved}{msg_count} msgs{active}"
            if label:
                line += f"\n  _{label}_"
            lines.append(line)
        await send_message(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd == "/resume":
        if not arg:
            await send_message(chat_id, "Usage: `/resume <id|name|checkpoint>`", reply_to=msg_id)
            return
        if arg.lower() in {"checkpoint", "latest"}:
            folder = state["active_folder"]
            try:
                checkpoint_path = await latest_checkpoint(folder)
            except CheckpointRuntimeError as exc:
                await send_message(
                    chat_id,
                    f"No valid checkpoint could be loaded: `{_redact_log_text(str(exc))[:500]}`",
                    reply_to=msg_id,
                )
                return
            state["checkpoint_rotations"].pop(folder, None)
            state["pending_checkpoint_bootstrap"].pop(folder, None)
            _rotate_codex_session(folder)
            await enqueue_prompt(
                chat_id,
                msg_id,
                checkpoint_resume_prompt(checkpoint_path),
            )
            return
        matches = find_session(arg)
        if len(matches) == 1:
            sid = matches[0]
            state["default_session_id"] = sid
            state["folder_sessions"][state["active_folder"]] = sid
            save_state()
            label = state["sessions"].get(sid, {}).get("label", "")
            extra = f" (_{label}_)" if label else ""
            await send_message(chat_id, f"Resumed `{sid[:8]}`{extra}", reply_to=msg_id)
        elif len(matches) > 1:
            await send_message(
                chat_id,
                "Multiple matches:\n" + "\n".join(f"`{sid[:8]}`" for sid in matches[:5]),
                reply_to=msg_id,
            )
        else:
            await send_message(chat_id, f"No thread matching `{arg}`.", reply_to=msg_id)
        return

    if cmd == "/status":
        inv = state.get("last_invocation")
        sid = state.get("default_session_id")
        queue_size = _prompt_queue.qsize() if _prompt_queue else 0
        queue_events = _prompt_queue.event_count() if _prompt_queue else 0
        automated_runs = _prompt_queue.automated_count() if _prompt_queue else 0
        busy = _active_codex_proc is not None and _active_codex_proc.returncode is None
        lines = [
            "*Bridge Status*",
            f"Version: `{BRIDGE_VERSION}`",
            f"Build: `{BRIDGE_BUILD}`",
            f"Folder: `{get_folder_display_name(state['active_folder'])}` (`{state['active_folder']}`)",
            f"Folders: {len(state.get('folders', {}))} registered",
            f"Threads: {len(state.get('sessions', {}))} tracked",
            (
                f"Queue: {queue_size} runs / {queue_events} events pending "
                f"({automated_runs} automated) | {'busy' if busy else 'idle'}"
            ),
            f"Model: `{CODEX_MODEL or 'default'}`",
            f"Sandbox: `{CODEX_SANDBOX}`",
        ]
        if sid:
            lines.append(f"Active: `{sid[:8]}`")
        checkpoint = state.get("last_checkpoints", {}).get(state["active_folder"])
        if checkpoint:
            lines.append(f"Checkpoint: `{checkpoint.get('checkpoint_id', 'unknown')}`")
        if state.get("checkpoint_rotations", {}).get(state["active_folder"]):
            lines.append("Checkpoint rotation: pending")
        if inv:
            lines.append(f"Last: {inv.get('elapsed', '?')}s at {inv.get('time', '?')[:16]}")
        await send_message(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd == "/model":
        if not arg:
            await send_message(
                chat_id,
                f"*Codex model:* `{CODEX_MODEL or 'default'}`\n\nUsage: `/model <model-id>`",
                reply_to=msg_id,
            )
            return
        CODEX_MODEL = arg.strip()
        await send_message(chat_id, f"Codex model set to `{CODEX_MODEL}`.", reply_to=msg_id)
        return

    if cmd == "/watchdog":
        item = _watchdog_current_item
        if not WATCHDOG_ENABLED:
            text_out = "Watchdog: *disabled*"
        elif item:
            elapsed = time.time() - item["started"]
            text_out = (
                f"Watchdog: *active*\n" f"Command: `{item.get('command', '')[:120]}`\n" f"Running: {elapsed:.0f}s"
            )
        else:
            worker_alive = _queue_worker_task is not None and not _queue_worker_task.done()
            text_out = (
                f"Watchdog: *idle*\n"
                f"Command timeout: {WATCHDOG_COMMAND_TIMEOUT}s\n"
                f"Stagnation kill: {WATCHDOG_STAGNATION_KILL}s\n"
                f"Queue worker: {'alive' if worker_alive else 'dead'}"
            )
        await send_message(chat_id, text_out, reply_to=msg_id)
        return

    if cmd == "/interrupt":
        await cmd_interrupt(chat_id, msg_id, arg, clear_pending=False)
        return

    if cmd == "/stop":
        await cmd_interrupt(chat_id, msg_id, arg, clear_pending=True)
        return

    if cmd == "/clearqueue":
        await cmd_clear_queue(chat_id, msg_id)
        return

    await enqueue_prompt(chat_id, msg_id, text)


async def cmd_clear_queue(chat_id: int, msg_id: int) -> tuple[int, int]:
    removed = await _prompt_queue.clear() if _prompt_queue else []
    _cancel_checkpoint_rotations(removed)
    await _notify_terminal_a2a_items(removed, "Cancelled by operator before execution via /clearqueue.")
    event_count = sum(len(item.get("a2a_events") or [None]) for item in removed)
    await send_message(
        chat_id,
        f"Cleared {len(removed)} queued runs ({event_count} events).",
        reply_to=msg_id,
    )
    return len(removed), event_count


def _cancel_checkpoint_rotations(items: list[dict]) -> None:
    changed = False
    rotations = state.setdefault("checkpoint_rotations", {})
    for item in items:
        if item.get("kind") != "checkpoint":
            continue
        folder = item.get("folder")
        rotation_id = item.get("checkpoint_rotation_id")
        current = rotations.get(folder) if folder and rotation_id else None
        if current and current.get("id") == rotation_id:
            rotations.pop(folder, None)
            changed = True
    if changed:
        save_state()


async def _notify_terminal_a2a_items(items: list[dict], reason: str):
    for item in items:
        target = item.get("a2a_reply_target")
        if not target:
            continue
        terminal_task_ids = item.setdefault("terminal_a2a_task_ids", set())
        for task_id in item.get("a2a_task_ids") or []:
            if task_id in terminal_task_ids:
                continue
            try:
                await send_plain_message(
                    item["chat_id"],
                    _a2a_autowrap_result(target, task_id, reason),
                    reply_to=item.get("msg_id"),
                )
            except Exception as exc:
                log.error(
                    "Failed to send terminal A2A result task_id=%s: %s",
                    task_id,
                    _redact_log_text(str(exc)),
                )
                continue
            terminal_task_ids.add(task_id)


async def cmd_interrupt(
    chat_id: int,
    msg_id: int,
    args: str,
    *,
    clear_pending: bool,
):
    removed = []
    if clear_pending and _prompt_queue:
        removed = await _prompt_queue.clear()
        _cancel_checkpoint_rotations(removed)
        await _notify_terminal_a2a_items(removed, "Cancelled by operator before execution via /stop.")

    active = _active_codex_proc is not None and _active_codex_proc.returncode is None
    if active:
        suffix = f" and cleared {len(removed)} queued runs" if clear_pending else ""
        await send_message(chat_id, f"_Interrupting active task{suffix}..._", reply_to=msg_id)
        try:
            _active_codex_proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
    elif clear_pending:
        event_count = sum(len(item.get("a2a_events") or [None]) for item in removed)
        await send_message(
            chat_id,
            f"No active task. Cleared {len(removed)} queued runs ({event_count} events).",
            reply_to=msg_id,
        )
    else:
        await send_message(chat_id, "Nothing running to interrupt.", reply_to=msg_id)

    follow_up = args.strip()
    if follow_up:
        await enqueue_prompt(chat_id, msg_id, follow_up, front=True)


async def enqueue_prompt(
    chat_id: int,
    msg_id: int,
    text: str,
    images: list[str] | None = None,
    a2a_reply_target: str | None = None,
    front: bool = False,
    dispatch_job_id: str | None = None,
    dispatch_label: str = "",
    notify_telegram: bool = True,
    model: str | None = None,
    reasoning_effort: str | None = None,
):
    global _prompt_queue
    if _prompt_queue is None:
        _prompt_queue = PromptQueue()
    folder = state["active_folder"]
    rotation = state.setdefault("checkpoint_rotations", {}).get(folder)
    deferred_rotation_id = rotation.get("id") if rotation else None
    bootstrap_path = None
    if not deferred_rotation_id:
        bootstrap_path = state.setdefault("pending_checkpoint_bootstrap", {}).pop(folder, None)
        if bootstrap_path:
            text = checkpoint_resume_prompt(bootstrap_path, text)
            save_state()

    session_id = None if deferred_rotation_id else state.get("default_session_id")
    context_id = None
    pending_label = None
    if session_id is None and not deferred_rotation_id:
        context = get_or_create_pending_thread_context(folder)
        context_id = context["id"]
        pending_label = context.get("label")
        if pending_label:
            context["label"] = None
    a2a_event = _coalescible_a2a_event(text, a2a_reply_target)
    item = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "text": text,
        "images": images or [],
        "folder": folder,
        "session_id": session_id,
        "context_id": context_id,
        "pending_label": pending_label,
        "a2a_reply_target": a2a_reply_target,
        "automated": a2a_reply_target is not None,
        "a2a_events": [a2a_event] if a2a_event else [],
        "a2a_task_ids": [_extract_a2a_task_id(text)] if a2a_reply_target else [],
        "coalesce_key": (
            (
                chat_id,
                folder,
                session_id,
                context_id,
                a2a_reply_target,
                a2a_event["sender"].lower(),
            )
            if a2a_event
            else None
        ),
        "deferred_checkpoint_rotation": deferred_rotation_id,
        "dispatch_job_id": dispatch_job_id,
        "dispatch_label": dispatch_label,
        "notify_telegram": notify_telegram,
        "model": model,
        "reasoning_effort": reasoning_effort,
    }
    result = await _prompt_queue.put(item, front=front)
    position = result["position"]
    if a2a_reply_target:
        log.info(
            "Queued A2A task_id=%s position=%s coalesced=%s batch_size=%s",
            item["a2a_task_ids"][0],
            position,
            result["coalesced"],
            result["batch_size"],
        )
    elif position > 1 and notify_telegram:
        await send_message(
            chat_id,
            f"Queued (position {position}). Working on previous task...",
            reply_to=msg_id,
        )
    else:
        log.info("Processing immediately: %s", text[:60])


async def queue_worker():
    global _prompt_queue, _queue_last_dequeue
    if _prompt_queue is None:
        _prompt_queue = PromptQueue()
    log.info("Queue worker started")
    while not _shutting_down:
        try:
            item = await asyncio.wait_for(
                _prompt_queue.get(),
                timeout=5,
            )
        except asyncio.TimeoutError:
            continue
        except Exception:
            continue

        _queue_last_dequeue = time.time()
        try:
            await _process_prompt(item)
        except asyncio.CancelledError:
            await _notify_terminal_a2a_items(
                [item],
                "Cancelled before completion by bridge worker shutdown. Retry from GitHub source of truth.",
            )
            raise
        except Exception as e:
            safe_error = _redact_log_text(str(e))
            log.error("Queue worker error: %s", safe_error)
            chat_id = item["chat_id"]
            msg_id = item["msg_id"]
            dispatch_job_id = item.get("dispatch_job_id")
            if dispatch_job_id:
                _write_dispatch_job(
                    dispatch_job_id,
                    state="failed",
                    error=safe_error,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            try:
                if item.get("notify_telegram", True):
                    await send_message(chat_id, f"Error processing message: {safe_error}", reply_to=msg_id)
            except Exception as notify_error:
                log.error("Failed to send queue error message: %s", _redact_log_text(str(notify_error)))
            await _notify_terminal_a2a_items(
                [item],
                f"Bridge processing failed before completion ({type(e).__name__}). "
                "Retry from GitHub source of truth.",
            )
        finally:
            _prompt_queue.task_done()

    log.info("Queue worker stopped")


async def _queue_health_monitor():
    global _queue_worker_task
    stall_threshold = 120
    while not _shutting_down:
        await asyncio.sleep(30)
        if _prompt_queue is None or _prompt_queue.qsize() == 0:
            continue
        worker_alive = _queue_worker_task is not None and not _queue_worker_task.done()
        codex_active = _active_codex_proc is not None and _active_codex_proc.returncode is None
        if codex_active:
            continue
        time_since_dequeue = time.time() - _queue_last_dequeue if _queue_last_dequeue > 0 else float("inf")
        if time_since_dequeue < stall_threshold:
            continue
        pending = _prompt_queue.qsize()
        log.error(
            "Queue health: STALL DETECTED — %s items pending, no dequeue for %.0fs, worker_alive=%s",
            pending,
            time_since_dequeue,
            worker_alive,
        )
        chat_id = _active_codex_chat_id or next(iter(ALLOWED_USERS), 0)
        if chat_id:
            await send_message(
                chat_id,
                (
                    f"Queue stall detected ({pending} messages waiting, no activity for "
                    f"{time_since_dequeue / 60:.0f}min). Restarting worker..."
                ),
            )
        if _queue_worker_task and not _queue_worker_task.done():
            _queue_worker_task.cancel()
            try:
                await _queue_worker_task
            except asyncio.CancelledError:
                pass
        _queue_worker_task = asyncio.create_task(queue_worker())
        log.info("Queue health: worker restarted")


async def _process_prompt(item: dict):
    if item.get("kind") == "checkpoint":
        await _process_checkpoint(item)
        return

    global _active_codex_chat_id, _watchdog_current_item, _watchdog_last_progress
    chat_id = item["chat_id"]
    msg_id = item["msg_id"]
    text = item["text"]
    images = item["images"]
    folder = item["folder"]
    session_id = item["session_id"]
    context_id = item["context_id"]
    pending_label = item["pending_label"]
    a2a_reply_target = item.get("a2a_reply_target")
    a2a_task_ids = item.get("a2a_task_ids") or []
    dispatch_job_id = item.get("dispatch_job_id")
    dispatch_label = item.get("dispatch_label", "")
    notify_telegram = item.get("notify_telegram", True)
    model = item.get("model")
    reasoning_effort = item.get("reasoning_effort")

    if item.get("deferred_checkpoint_rotation"):
        session_id = state["folder_sessions"].get(folder)
        context_id = None
        if session_id is None:
            context_id = get_or_create_pending_thread_context(folder)["id"]
        bootstrap_path = state.setdefault("pending_checkpoint_bootstrap", {}).pop(folder, None)
        if bootstrap_path:
            text = checkpoint_resume_prompt(bootstrap_path, text)
            save_state()

    _active_codex_chat_id = chat_id
    _watchdog_current_item = None
    _watchdog_last_progress = time.time()

    if dispatch_job_id:
        _write_dispatch_job(
            dispatch_job_id,
            state="running",
            folder=folder,
            session_id=session_id,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        if notify_telegram:
            await _send_mcp_dispatch_notice(
                chat_id,
                "started",
                dispatch_job_id,
                dispatch_label,
                (
                    f"Folder: {folder}\n"
                    f"Model: {model or CODEX_MODEL or 'default'}\n"
                    f"Effort: {reasoning_effort or CODEX_REASONING_EFFORT or 'default'}"
                ),
            )

    typing_task = None
    if notify_telegram:
        await send_typing(chat_id)
        typing_task = asyncio.create_task(typing_loop(chat_id))
    watchdog_task = None
    if WATCHDOG_ENABLED and notify_telegram:
        watchdog_task = asyncio.create_task(_watchdog_monitor(chat_id))

    try:
        start = time.time()
        if a2a_reply_target and A2A_PROGRESS_MODE in {"status", "structured"}:
            task_id = a2a_task_ids[0] if a2a_task_ids else _extract_a2a_task_id(text)
            batch_suffix = f" Batch contains {len(a2a_task_ids)} events." if len(a2a_task_ids) > 1 else ""
            await send_plain_message(
                chat_id,
                _a2a_status_envelope(
                    a2a_reply_target,
                    task_id,
                    "Accepted. Working silently; final response will be a structured A2A result." + batch_suffix,
                ),
                reply_to=msg_id,
            )
        effective_session_id = session_id
        if effective_session_id is None and context_id:
            effective_session_id = _resolved_thread_contexts.get(context_id)

        response, result_session_id = await run_codex(
            text,
            chat_id,
            cwd=folder,
            session_id=effective_session_id,
            images=images,
            suppress_progress_messages=bool(a2a_reply_target) or not notify_telegram,
            suppress_footer=bool(a2a_reply_target) or not notify_telegram,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        elapsed = time.time() - start

        if result_session_id:
            if context_id:
                _resolved_thread_contexts[context_id] = result_session_id
            label = pending_label or (text[:50] if result_session_id != effective_session_id else "")
            record_session(result_session_id, folder=folder, label=label)

            if context_is_still_selected(folder, session_id, context_id):
                state["default_session_id"] = result_session_id

            current_folder_session = state["folder_sessions"].get(folder)
            if current_folder_session == session_id or (
                current_folder_session is None and session_id is None and folder_context_is_current(folder, context_id)
            ):
                state["folder_sessions"][folder] = result_session_id

            pending = _pending_thread_contexts.get(folder)
            if pending and pending.get("id") == context_id:
                _pending_thread_contexts.pop(folder, None)

        state["last_invocation"] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "elapsed": round(elapsed, 1),
            "status": "ok",
            "session_id": result_session_id,
            "folder": folder,
        }
        save_state()
        if dispatch_job_id:
            _write_dispatch_job(
                dispatch_job_id,
                state="completed",
                folder=folder,
                session_id=result_session_id,
                elapsed=round(elapsed, 1),
                result=response,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        if a2a_reply_target:
            ok, reason = _validate_handoff_envelope(response, a2a_reply_target)
            if not ok:
                # Agent produced raw text instead of a /handoff envelope.
                # Auto-wrap it into a valid A2A result so the requester
                # still receives the work product (see issue #6).
                task_id = _extract_a2a_task_id(text)
                response = _a2a_autowrap_result(a2a_reply_target, task_id, response)
                log.info(
                    "Auto-wrapped raw A2A response to @%s (reason: %s)",
                    a2a_reply_target,
                    reason,
                )
        if a2a_reply_target and len(a2a_task_ids) > 1:
            response_body = _a2a_response_body(response, a2a_reply_target)
            for task_id in a2a_task_ids:
                await send_plain_message(
                    chat_id,
                    _a2a_autowrap_result(a2a_reply_target, task_id, response_body),
                    reply_to=msg_id,
                )
                item.setdefault("terminal_a2a_task_ids", set()).add(task_id)
        elif a2a_reply_target:
            await send_plain_message(chat_id, response, reply_to=msg_id)
            if a2a_task_ids:
                item.setdefault("terminal_a2a_task_ids", set()).add(a2a_task_ids[0])
        elif notify_telegram:
            telegram_response = response
            if dispatch_job_id:
                display = dispatch_label.strip()[:120] or dispatch_job_id
                telegram_response = (
                    f"MCP dispatch completed: {display}\n"
                    f"Job: {dispatch_job_id}\n\n{response}"
                )
            await send_message(chat_id, telegram_response, reply_to=msg_id)
    finally:
        _active_codex_chat_id = None
        _watchdog_current_item = None
        if typing_task:
            typing_task.cancel()
        if watchdog_task:
            watchdog_task.cancel()
        if typing_task:
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
        if watchdog_task:
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
        try:
            await cleanup_old_media()
        except Exception:
            pass


async def typing_loop(chat_id: int):
    try:
        while True:
            await send_typing(chat_id)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# --- Long-Polling Loop ---


async def poll_loop():
    global BOT_USERNAME, BOT_ID
    offset = 0
    log.info("Starting Telegram long-polling...")

    await tg_api("deleteWebhook")
    me = await tg_api("getMe")
    if me and me.get("ok"):
        bot = me["result"]
        BOT_USERNAME = bot.get("username") or ""
        BOT_ID = bot.get("id")
        log.info("Bot: @%s (%s)", bot.get("username"), bot.get("first_name"))

    while not _shutting_down:
        try:
            client = await get_client()
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            resp = await client.post(
                url,
                json={
                    "offset": offset,
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": ["message"],
                },
            )
            data = resp.json()
            if not data.get("ok"):
                log.error("getUpdates error: %s", data)
                await asyncio.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue

                user_id = (message.get("from") or {}).get("id")
                from_user = message.get("from") or {}
                chat_id = (message.get("chat") or {}).get("id")
                text = message.get("text", "")
                caption = message.get("caption", "")
                msg_id = message.get("message_id")
                raw_text = text or caption or ""

                if not chat_id:
                    continue
                should_process, text, caption, auto_reply = should_process_group_message(message, text, caption)
                if auto_reply:
                    asyncio.create_task(send_message(chat_id, auto_reply, reply_to=msg_id))
                if not should_process:
                    continue

                has_media = any(k in message for k in ("photo", "document", "voice", "video_note", "video", "sticker"))
                if not text and not caption and not has_media:
                    continue

                log.info("Message from %s: %s", user_id, _redact_log_text(text or caption or "[media]")[:80])

                if text and text.startswith("/"):
                    asyncio.create_task(handle_command(chat_id, msg_id, text))
                elif has_media:
                    asyncio.create_task(handle_media_message(chat_id, msg_id, message, caption or text or ""))
                else:
                    a2a_reply_target = None
                    if from_user.get("is_bot") and raw_text.lower().startswith("/handoff@"):
                        a2a_reply_target = from_user.get("username") or None
                    asyncio.create_task(
                        enqueue_prompt(
                            chat_id,
                            msg_id,
                            text,
                            a2a_reply_target=a2a_reply_target,
                        )
                    )

        except httpx.ReadTimeout:
            continue
        except Exception as e:
            log.error("Poll loop error: %s", e)
            await asyncio.sleep(5)

    log.info("Poll loop stopped (shutting down)")


# --- FastAPI ---

app = FastAPI(title="Codex Telegram Bridge", version=BRIDGE_VERSION)


class DispatchRequest(BaseModel):
    job_id: str
    prompt: str
    label: str = ""
    notify_telegram: bool = True
    model: str | None = None
    reasoning_effort: str | None = None


def _dispatch_token() -> str:
    try:
        return BRIDGE_DISPATCH_TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def _write_dispatch_job(job_id: str, **patch) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", job_id):
        raise ValueError("invalid dispatch job id")
    BRIDGE_DISPATCH_JOB_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    BRIDGE_DISPATCH_JOB_DIR.chmod(0o700)
    path = BRIDGE_DISPATCH_JOB_DIR / f"{job_id}.json"
    current = {}
    try:
        current = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    current.update(
        {
            "schemaVersion": 1,
            "job_id": job_id,
            "bridge": BOT_USERNAME or "codex-telegram-bridge",
            **patch,
        }
    )
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(current, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(path)
    return current


async def _send_mcp_dispatch_notice(
    chat_id: int,
    phase: str,
    job_id: str,
    label: str = "",
    detail: str = "",
) -> None:
    """Best-effort Telegram notice that never changes MCP job acceptance."""
    display = label.strip()[:120] or job_id
    message = f"MCP dispatch {phase}: {display}\nJob: {job_id}"
    if detail:
        message += f"\n{detail}"
    try:
        await send_plain_message(chat_id, message)
    except Exception as exc:
        log.warning(
            "Could not send MCP dispatch %s notice: %s",
            phase,
            _redact_log_text(str(exc)),
        )


@app.post("/dispatch")
async def dispatch(
    request: DispatchRequest,
    x_dispatch_token: str | None = Header(default=None),
):
    expected = _dispatch_token()
    if not expected:
        raise HTTPException(status_code=503, detail="bridge dispatch token is not configured")
    if not x_dispatch_token or not hmac.compare_digest(x_dispatch_token, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request.job_id):
        raise HTTPException(status_code=400, detail="invalid job_id")
    if not request.prompt.strip() or len(request.prompt) > 100_000:
        raise HTTPException(status_code=400, detail="prompt must contain 1-100000 characters")
    if request.model and not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", request.model):
        raise HTTPException(status_code=400, detail="invalid model")
    allowed_efforts = {"low", "medium", "high", "xhigh", "max", "ultra"}
    if request.reasoning_effort and request.reasoning_effort not in allowed_efforts:
        raise HTTPException(status_code=400, detail="invalid reasoning_effort")
    chat_id = BRIDGE_DISPATCH_CHAT_ID or next(iter(ALLOWED_USERS), 0)
    if not chat_id:
        raise HTTPException(status_code=503, detail="no dispatch chat id or allowed user configured")
    job_path = BRIDGE_DISPATCH_JOB_DIR / f"{request.job_id}.json"
    if job_path.exists():
        raise HTTPException(status_code=409, detail="job_id already exists")
    _write_dispatch_job(
        request.job_id,
        state="queued",
        label=request.label,
        notify_telegram=request.notify_telegram,
        model=request.model or CODEX_MODEL or "default",
        reasoning_effort=request.reasoning_effort or CODEX_REASONING_EFFORT or "default",
        queued_at=datetime.now(timezone.utc).isoformat(),
    )
    await enqueue_prompt(
        chat_id,
        0,
        request.prompt,
        dispatch_job_id=request.job_id,
        dispatch_label=request.label,
        notify_telegram=request.notify_telegram,
        model=request.model,
        reasoning_effort=request.reasoning_effort,
    )
    if request.notify_telegram:
        await _send_mcp_dispatch_notice(
            chat_id,
            "queued",
            request.job_id,
            request.label,
            "Activity and the final result will appear here and remain available to the MCP caller.",
        )
    return {"job_id": request.job_id, "state": "queued"}


@app.on_event("startup")
async def startup():
    load_state()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for d in Path.home().iterdir():
        if d.is_dir() and not d.name.startswith("."):
            if (d / ".git").exists() or (d / "README.md").exists():
                state["folders"].setdefault(d.name, str(d))
    projects_dir = Path(HOME) / "projects"
    if projects_dir.exists():
        for d in projects_dir.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                if (d / ".git").exists() or (d / "README.md").exists():
                    state["folders"].setdefault(d.name, str(d))
    save_state()

    global _prompt_queue, _queue_worker_task, _queue_health_task
    _prompt_queue = PromptQueue()
    _queue_worker_task = asyncio.create_task(queue_worker())
    _queue_health_task = asyncio.create_task(_queue_health_monitor())
    asyncio.create_task(poll_loop())


@app.on_event("shutdown")
async def shutdown():
    global _shutting_down
    _shutting_down = True
    log.info("Shutdown signal received")
    if _active_codex_proc and _active_codex_proc.returncode is None:
        if _active_codex_chat_id:
            await send_message(
                _active_codex_chat_id,
                "_Bridge restarting — waiting for Codex to finish..._",
            )
        try:
            await asyncio.wait_for(_active_codex_proc.wait(), timeout=CODEX_TIMEOUT)
        except asyncio.TimeoutError:
            _active_codex_proc.terminate()
    save_state()
    log.info("Shutdown complete")


@app.get("/health")
async def health():
    sid = state.get("default_session_id")
    queue_runs = _prompt_queue.qsize() if _prompt_queue else 0
    return {
        "status": "ok",
        "service": "codex-telegram-bridge",
        "version": BRIDGE_VERSION,
        "build": BRIDGE_BUILD,
        "mode": "long-polling",
        "active_folder": state.get("active_folder"),
        "folder_count": len(state.get("folders", {})),
        "default_session": sid[:8] if sid else None,
        "session_count": len(state.get("sessions", {})),
        "codex_model": CODEX_MODEL or "default",
        "codex_reasoning_effort": CODEX_REASONING_EFFORT or "default",
        "queue": {
            "runs": queue_runs,
            "events": _prompt_queue.event_count() if _prompt_queue else 0,
            "automated_runs": _prompt_queue.automated_count() if _prompt_queue else 0,
            "unfinished_runs": _prompt_queue.unfinished_count() if _prompt_queue else 0,
            "busy": _active_codex_proc is not None and _active_codex_proc.returncode is None,
            # ella-ai#1174: /health returned "ok" while the bridge was wedged for 90min,
            # because busy alone cannot distinguish "working" from "stuck". These let a
            # monitor alarm on busy-with-no-progress without guessing.
            "busy_since": (
                datetime.fromtimestamp(_active_codex_started, timezone.utc).isoformat()
                if _active_codex_started else None
            ),
            "active_child_age_seconds": (
                round(time.time() - _active_codex_started, 1) if _active_codex_started else None
            ),
            "active_child_pid": (
                _active_codex_proc.pid
                if _active_codex_proc is not None and _active_codex_proc.returncode is None
                else None
            ),
            "seconds_since_progress": (
                round(time.time() - _watchdog_last_progress, 1) if _watchdog_last_progress else None
            ),
            "max_runtime_seconds": CODEX_MAX_RUNTIME,
        },
        "last_invocation": state.get("last_invocation"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
