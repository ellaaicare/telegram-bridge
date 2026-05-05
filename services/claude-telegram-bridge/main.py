"""
Claude Code <-> Telegram Bridge (v3.4.1)
Direct long-polling — no n8n, no inbound ports needed.
Multi-folder workstation with session continuity.
Supports images, files, voice messages, and other media attachments.
Intelligent watchdog: detects stuck processes, uses Claude-as-judge to evaluate.
Job dispatch: fire-and-forget work to tmux sessions on fleet nodes via GitHub issues.

Commands:
  /folders          — List registered project folders
  /folder <name>    — Switch to folder (auto-continues last session)
  /folder add <name> <path> — Register a new folder
  /folder create <name> [path] — Create new project folder
  /folder rm <name> — Remove a registered folder
  /clone <url> [name] — Clone repo, register, and switch to it
  /init             — Initialize current folder (git init + CLAUDE.md)
  /history [n]      — Show last N messages from current/latest session
  /new [label]      — Start a fresh session in current folder
  /rename <label>   — Rename current session
  /save [label]     — Bookmark current session
  /sessions         — List sessions for current folder
  /resume <id|name> — Resume by ID prefix or name/summary
  /interrupt [msg]  — Stop Claude mid-run; optional message processed next
  /dispatch [opts] <desc> — Create issue + dispatch tmux worker to fleet node
  /jobs             — List active dispatch jobs
  /job <N>          — Check job status (tmux + issue + output)
  /job-kill <N>     — Kill a running worker
  /watchdog         — Show watchdog status
  /status           — Show bridge status
  /dashboard        — Portfolio dashboard link
  /help             — Show commands
  (any text)        — Send to Claude (continues session in current folder)
"""

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def _parse_int_set(raw: str) -> set[int]:
    return {int(value) for value in raw.split(",") if value.strip()}


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in {"1", "true", "yes", "on"}


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
            Path.home()
            / "telegram-bridge"
            / "services"
            / "telegram-a2a"
            / "agents.json",
            Path.home()
            / "dev"
            / "telegram-bridge"
            / "services"
            / "telegram-a2a"
            / "agents.json",
            Path.home()
            / "dev"
            / "ella-ai"
            / "services"
            / "telegram-a2a"
            / "agents.json",
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
            logging.getLogger("claude-bridge").warning(
                "Invalid A2A_BOT_REGISTRY_JSON: %s", e
            )

    for path in _a2a_registry_candidates():
        try:
            if path.exists():
                with path.open() as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logging.getLogger("claude-bridge").warning(
                "Could not load A2A bot registry %s: %s", path, e
            )
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
    prefixes = [
        f"/handoff@{value}" for value in sorted(values, key=len, reverse=True) if value
    ]
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

BRIDGE_VERSION = os.environ.get("BRIDGE_VERSION", "3.5.0")
BRIDGE_BUILD = os.environ.get("BRIDGE_BUILD", "a2a-noise-harden-pr6.9c8bcef")
HARNESS_CLI = os.environ.get("HARNESS_CLI", "claude").strip().lower() or "claude"
HARNESS_LABEL = os.environ.get("HARNESS_LABEL", "").strip() or {
    "claude": "Claude Code",
    "opencode": "OpenCode",
    "kilo": "Kilo Code",
}.get(HARNESS_CLI, HARNESS_CLI.title())
HARNESS_SERVICE_NAME = (
    os.environ.get("HARNESS_SERVICE_NAME", "").strip()
    or f"{HARNESS_CLI}-telegram-bridge"
)
HARNESS_TOKEN_ENV = os.environ.get("HARNESS_TOKEN_ENV", "").strip() or {
    "claude": "CLAUDE_TELEGRAM_BOT_TOKEN",
    "opencode": "OPENCODE_TELEGRAM_BOT_TOKEN",
    "kilo": "KILOCODE_TELEGRAM_BOT_TOKEN",
}.get(HARNESS_CLI, "TELEGRAM_BOT_TOKEN")
HARNESS_AGENT = os.environ.get("HARNESS_AGENT", "").strip()
HARNESS_SESSION_BACKEND = (
    os.environ.get(
        "HARNESS_SESSION_BACKEND",
        "claude" if HARNESS_CLI == "claude" else "bridge",
    )
    .strip()
    .lower()
)
BOT_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    or os.environ.get(HARNESS_TOKEN_ENV, "").strip()
)
A2A_BOT_REGISTRY = _load_a2a_registry()
ALLOWED_USERS = _parse_int_set(os.environ.get("ALLOWED_USER_IDS", ""))
ALLOWED_SENDER_IDS = _parse_int_set(
    os.environ.get("ALLOWED_SENDER_IDS", os.environ.get("ALLOWED_USER_IDS", ""))
)
ALLOWED_BOT_IDS = _parse_int_set(os.environ.get("ALLOWED_BOT_IDS", ""))
if _truthy_env("A2A_TRUST_REGISTRY_BOTS", "true"):
    ALLOWED_BOT_IDS |= _trusted_registry_bot_ids()
ALLOWED_CHAT_IDS = _parse_int_set(os.environ.get("ALLOWED_CHAT_IDS", ""))
BOT_USERNAME = ""
BOT_ID: int | None = None
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
BRIDGE_MODEL = os.environ.get("BRIDGE_MODEL", "").strip()
TELEGRAM_MAX_LENGTH = 4096
A2A_GUIDANCE_COOLDOWN_SECONDS = int(
    os.environ.get("A2A_GUIDANCE_COOLDOWN_SECONDS", "300")
)
A2A_PROGRESS_MODE = os.environ.get("A2A_PROGRESS_MODE", "status").strip().lower()
A2A_IGNORED = "__a2a_ignored__"
POLL_TIMEOUT = 60
STATE_FILE = (
    Path(
        os.environ.get(
            "BRIDGE_STATE_DIR",
            str(Path.home() / ".local" / "state" / HARNESS_SERVICE_NAME),
        )
    )
    / "state.json"
)
HOME = os.environ.get("BRIDGE_DEFAULT_FOLDER", str(Path.home()))
MEDIA_DIR = Path("/tmp/tg-bridge-media")

# --- Watchdog Configuration ---

WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "true").lower() == "true"
WATCHDOG_BASH_TIMEOUT = int(os.environ.get("WATCHDOG_BASH_TIMEOUT", "300"))
WATCHDOG_SSH_TIMEOUT = int(os.environ.get("WATCHDOG_SSH_TIMEOUT", "900"))
WATCHDOG_TASK_TIMEOUT = int(os.environ.get("WATCHDOG_TASK_TIMEOUT", "600"))
WATCHDOG_DEFAULT_TIMEOUT = int(os.environ.get("WATCHDOG_DEFAULT_TIMEOUT", "120"))
WATCHDOG_STAGNATION_KILL = int(os.environ.get("WATCHDOG_STAGNATION_KILL", "1800"))
WATCHDOG_SESSION = os.environ.get("WATCHDOG_SESSION", "watchdog-ops")
WATCHDOG_EVAL_NODE = os.environ.get("WATCHDOG_EVAL_NODE", "local")

# Commands that are ALWAYS infinite — kill immediately, no evaluation needed
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
        logging.FileHandler(
            os.path.expanduser(f"~/.openclaw/workspace/logs/{HARNESS_SERVICE_NAME}.log")
        ),
    ],
)
log = logging.getLogger("claude-bridge")


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
        # Not a handoff targeted at us — silently ignore
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


def _a2a_skill_reference() -> str:
    return (
        "Repo skill: skills/telegram-a2a-handoff/SKILL.md\n"
        "Runbook: docs/runbooks/telegram-a2a-handoff.md\n"
        "Bot registry: services/telegram-a2a/agents.json\n"
        "Git: https://github.com/ellaaicare/telegram-bridge/blob/main/skills/telegram-a2a-handoff/SKILL.md"
    )


def _a2a_envelope_json_compact(payload: dict) -> str:
    """Compact JSON for transmission (minimizes Telegram message length)."""
    return json.dumps(payload, separators=(",", ":"))


def _a2a_envelope_json_pretty(payload: dict) -> str:
    """Pretty-printed JSON for human visibility in Telegram group chats."""
    return json.dumps(payload, indent=2)


def _a2a_guidance_message() -> str:
    """A2A handoff syntax guidance with pretty-printed JSON for readability."""
    target = _canonical_handoff_target(BOT_USERNAME)
    payload = {
        "from": "SourceBot",
        "to": target,
        "task_id": "stable-unique-id",
        "ttl": 1,
        "requires_response": True,
        "type": "task",
        "body": "Do the work here.",
    }
    return (
        "A2A handoff syntax required for bot-to-bot work.\n\n"
        f"Send tasks with this exact shape:\n/handoff@{target} "
        f"{_a2a_envelope_json_pretty(payload)}\n\n"
        f"Known targets: {_known_target_examples()}\n\n"
        "Rules: use ttl=1, use a unique task_id, put the actual request in body, "
        "and do not send raw @bot prose or standing-by chatter.\n\n"
        f"{_a2a_skill_reference()}"
    )


def _a2a_response_rejection(target_username: str, reason: str) -> str:
    """A2A response rejection with pretty-printed JSON for readability."""
    target = _canonical_handoff_target(target_username)
    source = BOT_USERNAME or "BridgeBot"
    payload = {
        "from": source,
        "to": target,
        "task_id": "stable-unique-id",
        "ttl": 1,
        "requires_response": False,
        "type": "result",
        "body": "Result text here.",
    }
    return (
        "A2A handoff syntax required for bot-to-bot work.\n\n"
        "The local agent generated an invalid bot-to-bot response, so the bridge "
        "rejected the raw response instead of posting it.\n\n"
        f"Reason: {reason}\n\n"
        f"Reply with this exact shape:\n/handoff@{target} "
        f"{_a2a_envelope_json_pretty(payload)}\n\n"
        f"Known targets: {_known_target_examples()}\n\n"
        "Rules: structured envelopes are required for A2A responses too; use ttl=1 "
        "or ttl=0 for terminal results, use a unique task_id, and put the response "
        "content in body.\n\n"
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
    # Catch both guidance variants: initial guidance and response-rejection guidance.
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
        key
        for key in ("from", "to", "task_id", "ttl", "requires_response", "type", "body")
        if key not in payload
    ]
    if missing:
        return (
            False,
            f"response JSON envelope missing required fields: {', '.join(missing)}",
        )

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
    """A2A response rejection with pretty-printed JSON for readability."""
    target = _canonical_handoff_target(target_username)
    source = BOT_USERNAME or "BridgeBot"
    payload = {
        "from": source,
        "to": target,
        "task_id": "stable-unique-id",
        "ttl": 1,
        "requires_response": False,
        "type": "result",
        "body": "Result text here.",
    }
    return (
        "A2A handoff syntax required for bot-to-bot work.\n\n"
        "The local agent generated an invalid bot-to-bot response, so the bridge "
        "rejected the raw response instead of posting it.\n\n"
        f"Reason: {reason}\n\n"
        f"Reply with this exact shape:\n/handoff@{target} "
        f"{_a2a_envelope_json_pretty(payload)}\n\n"
        f"Known targets: {_known_target_examples()}\n\n"
        "Rules: structured envelopes are required for A2A responses too; use ttl=1 "
        "or ttl=0 for terminal results, use a unique task_id, and put the response "
        "content in body.\n\n"
        f"{_a2a_skill_reference()}"
    )


def _extract_a2a_task_id(prompt: str) -> str:
    match = re.search(r"task_id=([^,)\s]+)", prompt)
    return match.group(1) if match else f"task-{int(time.time())}"


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
    return f"/handoff@{target} {json.dumps(payload, indent=2)}"


def should_process_group_message(
    message: dict, text: str, caption: str
) -> tuple[bool, str, str, str | None]:
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
        raw[:120] if raw else "[media]",
    )

    if BOT_ID and user_id == BOT_ID:
        return False, text, caption, None

    if chat_type == "private":
        if sender_is_bot or user_id not in ALLOWED_USERS:
            log.warning(
                "Rejected private message from sender %s (%s)", user_id, username
            )
            return False, text, caption, None
        return True, text, caption, None

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        log.warning(
            "Rejected message from non-allowlisted chat %s type=%s", chat_id, chat_type
        )
        return False, text, caption, None

    if chat_type in {"group", "supergroup", "channel"}:
        if sender_is_bot:
            if user_id not in ALLOWED_BOT_IDS:
                log.warning("Rejected group bot sender %s (%s)", user_id, username)
                return False, text, caption, None
            ok, prompt = _parse_handoff(raw)
            if not ok and prompt == A2A_IGNORED:
                return False, text, caption, None
            if not ok and raw and _is_a2a_guidance(raw):
                log.info(
                    "Ignored peer A2A syntax guidance from bot sender %s (%s)",
                    user_id,
                    username,
                )
                return False, text, caption, None
            if not ok and raw:
                # Bot-originated raw messages should never trigger guidance spam in group chats.
                # Log internally only; do not send A2A syntax guidance to bots.
                log.info(
                    "Silently ignoring bot-originated raw message from %s (%s)",
                    user_id,
                    username,
                )
                return False, text, caption, None
            return (ok, prompt if text else text, prompt if caption else caption, None)

        if user_id not in ALLOWED_USERS:
            log.warning("Rejected group user sender %s (%s)", user_id, username)
            return False, text, caption, None

        mention = f"@{BOT_USERNAME}".lower() if BOT_USERNAME else ""
        reply = message.get("reply_to_message") or {}
        reply_to_bot = (
            ((reply.get("from") or {}).get("id") == BOT_ID) if BOT_ID else False
        )
        mentions_bot = bool(mention and mention in raw.lower())
        if not mentions_bot and not reply_to_bot:
            log.info("Ignored group message not addressed to @%s", BOT_USERNAME)
            return False, text, caption, None
        if mentions_bot:
            stripped = (
                raw.replace(f"@{BOT_USERNAME}", "")
                .replace(f"@{BOT_USERNAME.lower()}", "")
                .strip()
            )
            if text:
                text = stripped
            else:
                caption = stripped
        return True, text, caption, None

    return False, text, caption, None


# --- State ---

# Track active Claude subprocess so we can handle graceful shutdown
_active_harness_proc: asyncio.subprocess.Process | None = None
_active_harness_chat_id: int | None = None
_shutting_down = False

# Watchdog state
_watchdog_current_tool: dict | None = (
    None  # {"name", "command", "input_summary", "started"}
)
_watchdog_last_event: float = 0.0
_watchdog_last_progress: float = (
    0.0  # Last time a tool_result/new tool_use arrived (session is progressing)
)
_watchdog_evaluation_in_progress = False
_a2a_guidance_last_sent: dict[str, float] = {}

# Message queue — sequential processing, no concurrent Claude calls
_prompt_queue: asyncio.Queue | None = None
_queue_worker_task: asyncio.Task | None = None
_queue_last_dequeue: float = 0.0  # Last time queue worker picked up an item
_queue_health_task: asyncio.Task | None = None

state = {
    "default_session_id": None,
    "active_folder": HOME,
    "folders": {
        "home": HOME,
    },
    "folder_sessions": {},  # folder_path -> last session_id
    "sessions": {},
    "last_invocation": None,
}

ELLA_AI_REPO = os.environ.get("ELLA_AI_REPO", str(Path.home() / "ella-ai"))
LEGACY_PATH_REMAPS = {
    "/home/plato/ella-dev/ella-ai": ELLA_AI_REPO,
    "/home/plato/dev/ella-ai": ELLA_AI_REPO,
}


def resolve_existing_cwd(path: str | None, fallback: str | None = None) -> str:
    """Return an existing cwd, remapping stale fleet paths when possible."""
    candidates = []
    if path:
        candidates.append(LEGACY_PATH_REMAPS.get(path, path))
    if fallback:
        candidates.append(LEGACY_PATH_REMAPS.get(fallback, fallback))
    candidates.extend([ELLA_AI_REPO, HOME])

    for candidate in candidates:
        expanded = os.path.expanduser(candidate)
        if os.path.isdir(expanded):
            return expanded

    return HOME


# --- Job dispatch state ---
_dispatch_jobs: dict = {}  # job_id -> {node, issue, tmux_session, status, created, output_file}
JOBS_OUTPUT_DIR = "/tmp/claude-jobs"
DISPATCH_DEFAULT_NODE = os.environ.get("DISPATCH_DEFAULT_NODE", "local")
DISPATCH_DEFAULT_REPO = os.environ.get("DISPATCH_DEFAULT_REPO", "")
DEFAULT_DISPATCH_NODES = {
    "local": {"ssh": None, "claude": "claude", "cwd": None},
}


def _load_dispatch_nodes() -> dict:
    raw = os.environ.get("DISPATCH_NODES_JSON", "").strip()
    if not raw:
        return DEFAULT_DISPATCH_NODES
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(
            "Invalid DISPATCH_NODES_JSON, using local-only dispatch nodes: %s", e
        )
        return DEFAULT_DISPATCH_NODES
    if not isinstance(parsed, dict) or not parsed:
        log.warning(
            "DISPATCH_NODES_JSON must be a non-empty object; using local-only dispatch nodes"
        )
        return DEFAULT_DISPATCH_NODES
    return parsed


DISPATCH_NODES = _load_dispatch_nodes()


def load_state():
    global state
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
                state.update(saved)
            state["active_folder"] = resolve_existing_cwd(
                state.get("active_folder"), HOME
            )
            state["folders"] = {
                name: resolve_existing_cwd(path, HOME)
                for name, path in state.get("folders", {"home": HOME}).items()
            }
            state["folders"]["home"] = HOME
            # Ensure defaults
            if "active_folder" not in state:
                state["active_folder"] = HOME
            if "folders" not in state:
                state["folders"] = {"home": HOME}
            if "folder_sessions" not in state:
                state["folder_sessions"] = {}
            log.info(
                f"Loaded state: folder={state['active_folder']}, {len(state['sessions'])} sessions"
            )
        except Exception as e:
            log.warning(f"Failed to load state: {e}")


def save_state():
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.warning(f"Failed to save state: {e}")


def _save_jobs():
    """Persist job state to disk."""
    jobs_file = os.path.join(os.path.dirname(STATE_FILE), "dispatch-jobs.json")
    try:
        with open(jobs_file, "w") as f:
            json.dump(_dispatch_jobs, f, indent=2, default=str)
    except Exception as e:
        log.warning(f"Failed to save dispatch jobs: {e}")


def _load_jobs():
    """Load job state from disk."""
    global _dispatch_jobs
    _dispatch_jobs = {}
    jobs_file = os.path.join(os.path.dirname(STATE_FILE), "dispatch-jobs.json")
    if os.path.exists(jobs_file):
        try:
            with open(jobs_file) as f:
                loaded = json.load(f)
            for job_id, job_data in loaded.items():
                if isinstance(job_data, dict) and "issue" in job_data:
                    _dispatch_jobs[job_id] = job_data
                else:
                    log.warning(f"Skipping invalid job entry: {job_id}")
            log.info(f"Loaded {len(_dispatch_jobs)} dispatch jobs")
        except Exception as e:
            log.warning(f"Failed to load dispatch jobs: {e}")


async def dispatch_job(
    issue_number: int,
    repo: str,
    node: str = "local",
    cwd: str | None = None,
) -> dict:
    """Launch a tmux worker for a GitHub issue on a fleet node."""
    node_cfg = DISPATCH_NODES.get(node)
    if not node_cfg:
        raise ValueError(f"Unknown node: {node}. Known: {', '.join(DISPATCH_NODES)}")

    job_id = str(issue_number)
    tmux_name = f"job-{issue_number}"
    output_file = f"{JOBS_OUTPUT_DIR}/{issue_number}.out"
    if node_cfg.get("ssh"):
        work_cwd = cwd or node_cfg.get("cwd") or state.get("active_folder", "~")
    else:
        work_cwd = resolve_existing_cwd(
            cwd or node_cfg.get("cwd") or state.get("active_folder"), HOME
        )
    claude_bin = node_cfg["claude"]

    worker_prompt = (
        f"You are a worker agent. Your job spec is GitHub issue #{issue_number} "
        f"in repo {repo}.\n\n"
        "Instructions:\n"
        f"1. Read the issue: gh issue view {issue_number} --repo {repo}\n"
        "2. Understand the requirements fully before writing code\n"
        f"3. Create a branch: git checkout -b job-{issue_number}-impl\n"
        "4. Implement the requirements with tests\n"
        "5. Commit frequently with clear messages\n"
        f"6. When done, comment on the issue with a summary: "
        f"gh issue comment {issue_number} --repo {repo} --body 'Done: <summary>'\n"
        f"7. Close the issue: gh issue close {issue_number} --repo {repo}\n"
        "8. If stuck, comment on the issue with what's blocking and leave it open\n"
    )

    # Write prompt + runner script to files — avoids all shell quoting issues
    # with nested tmux/SSH layers
    jobs_dir = "/tmp/claude-jobs"
    prompt_file = f"{jobs_dir}/{issue_number}.prompt"
    script_file = f"{jobs_dir}/{issue_number}.sh"

    runner_script = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f"cd {shlex.quote(work_cwd)}\n"
        f"prompt=$(cat {shlex.quote(prompt_file)})\n"
        f'{shlex.quote(claude_bin)} -p "$prompt" '
        f"--dangerously-skip-permissions --max-turns 50 "
        f"--output-format text "
        f"> {jobs_dir}/{issue_number}.out 2>&1\n"
    )

    if node_cfg["ssh"]:
        # Remote node — transfer files via SSH stdin, then launch tmux
        ssh_target = node_cfg["ssh"]

        # Create jobs dir
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            ssh_target,
            f"mkdir -p {jobs_dir}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15)

        # Write prompt file
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            ssh_target,
            f"cat > {prompt_file}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(
            proc.communicate(input=worker_prompt.encode()), timeout=15
        )

        # Write runner script
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            ssh_target,
            f"cat > {script_file} && chmod +x {script_file}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(
            proc.communicate(input=runner_script.encode()), timeout=15
        )

        # Launch tmux — trivial quoting, just runs a script file
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            ssh_target,
            f"tmux new-session -d -s {tmux_name} 'bash {script_file}'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    else:
        # Local node — write files directly, then launch tmux
        os.makedirs(jobs_dir, exist_ok=True)

        with open(prompt_file, "w") as f:
            f.write(worker_prompt)
        with open(script_file, "w") as f:
            f.write(runner_script)
        os.chmod(script_file, 0o755)

        proc = await asyncio.create_subprocess_shell(
            f"tmux new-session -d -s {tmux_name} 'bash {script_file}'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

    success = proc.returncode == 0
    job_entry = {
        "issue": issue_number,
        "repo": repo,
        "node": node,
        "tmux_session": tmux_name,
        "status": "running" if success else "failed",
        "output_file": f"/tmp/claude-jobs/{issue_number}.out",
        "cwd": work_cwd,
        "created": datetime.now(timezone.utc).isoformat(),
        "error": stderr.decode()[:200] if not success else None,
    }
    _dispatch_jobs[job_id] = job_entry
    _save_jobs()
    return job_entry


async def _check_tmux_alive(tmux_name: str, node: str) -> bool:
    """Check if a tmux session exists on a node."""
    if not tmux_name:
        return False
    try:
        node_cfg = DISPATCH_NODES.get(node, {})
        if node_cfg.get("ssh"):
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                node_cfg["ssh"],
                f"tmux has-session -t {shlex.quote(tmux_name)} 2>/dev/null && echo alive",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                f"tmux has-session -t {shlex.quote(tmux_name)} 2>/dev/null && echo alive",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return b"alive" in stdout
    except Exception:
        return False


def _format_age(iso_str: str) -> str:
    """Format an ISO timestamp as a human-readable age string."""
    try:
        created = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created
        if delta.total_seconds() < 60:
            return f"{int(delta.total_seconds())}s"
        elif delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() / 60)}m"
        elif delta.total_seconds() < 86400:
            return f"{int(delta.total_seconds() / 3600)}h"
        else:
            return f"{int(delta.total_seconds() / 86400)}d"
    except Exception:
        return "?"


async def cmd_dispatch(chat_id: int, msg_id: int, arg: str):
    """Create a GitHub issue and dispatch a tmux worker.

    Usage: /dispatch [--node NODE] [--repo REPO] [--cwd DIR] <task description>
    """
    # Parse flags
    node = DISPATCH_DEFAULT_NODE
    repo = DISPATCH_DEFAULT_REPO
    cwd_arg = None

    parts = arg.split()
    clean_parts = []
    i = 0
    while i < len(parts):
        if parts[i] == "--node" and i + 1 < len(parts):
            node = parts[i + 1]
            i += 2
        elif parts[i] == "--repo" and i + 1 < len(parts):
            repo = parts[i + 1]
            i += 2
        elif parts[i] == "--cwd" and i + 1 < len(parts):
            cwd_arg = parts[i + 1]
            i += 2
        else:
            clean_parts.append(parts[i])
            i += 1
    description = " ".join(clean_parts)

    if not description:
        await send_message(
            chat_id,
            "Usage: `/dispatch [--node NODE] [--repo REPO] <task description>`",
            reply_to=msg_id,
        )
        return

    if not repo:
        await send_message(
            chat_id,
            "No repo configured. Use `--repo owner/name` or set `DISPATCH_DEFAULT_REPO` in .env",
            reply_to=msg_id,
        )
        return

    if node not in DISPATCH_NODES:
        nodes_list = ", ".join(DISPATCH_NODES.keys())
        await send_message(
            chat_id, f"Unknown node: `{node}`. Available: {nodes_list}", reply_to=msg_id
        )
        return

    # Create GitHub issue
    await send_message(
        chat_id, f"Creating issue and dispatching to `{node}`...", reply_to=msg_id
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            description[:80],
            "--body",
            f"## Job Spec\n\n{description}\n\n---\n*Dispatched by claude-telegram-bridge to `{node}`*",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            await send_message(
                chat_id,
                f"Failed to create issue: {stderr.decode()[:200]}",
                reply_to=msg_id,
            )
            return

        # Parse issue URL -> number
        issue_url = stdout.decode().strip()
        issue_number = int(issue_url.rstrip("/").split("/")[-1])
    except Exception as e:
        await send_message(chat_id, f"Error creating issue: {e}", reply_to=msg_id)
        return

    # Dispatch worker
    try:
        job = await dispatch_job(issue_number, repo, node=node, cwd=cwd_arg)
        if job["status"] == "running":
            await send_message(
                chat_id,
                f"Dispatched job #{issue_number} to `{node}`\n"
                f"Issue: {issue_url}\n"
                f"tmux: `{job['tmux_session']}`\n"
                f"Check: `/job {issue_number}`",
                reply_to=msg_id,
            )
        else:
            await send_message(
                chat_id,
                f"Issue created ({issue_url}) but dispatch failed: {job.get('error', 'unknown')}",
                reply_to=msg_id,
            )
    except Exception as e:
        await send_message(
            chat_id,
            f"Issue created ({issue_url}) but dispatch error: {e}",
            reply_to=msg_id,
        )


async def cmd_jobs(chat_id: int, msg_id: int):
    """List all tracked dispatch jobs with live tmux status."""
    if not _dispatch_jobs:
        await send_message(chat_id, "No dispatch jobs tracked.", reply_to=msg_id)
        return

    lines = ["*Dispatch Jobs*\n"]
    for job_id, job in sorted(
        _dispatch_jobs.items(), key=lambda x: x[1].get("created", ""), reverse=True
    ):
        alive = await _check_tmux_alive(
            job.get("tmux_session", ""), job.get("node", "local")
        )

        # Update status if tmux died
        if job.get("status") == "running" and not alive:
            job["status"] = "completed"
            _save_jobs()

        icon = {
            "running": "\u25b6",
            "completed": "\u2705",
            "failed": "\u274c",
            "killed": "\u26d4",
        }.get(job.get("status", "?"), "\u2753")
        node = job.get("node", "local")
        age = _format_age(job.get("created", ""))
        lines.append(
            f"{icon} #{job_id} on `{node}` ({age}) \u2014 {job.get('status', '?')}"
        )

    await send_message(chat_id, "\n".join(lines), reply_to=msg_id)


async def cmd_job_status(chat_id: int, msg_id: int, arg: str):
    """Check status of a specific job -- tmux, issue state, tail output."""
    parts = arg.strip().split()
    if not parts:
        await send_message(chat_id, "Usage: `/job <issue-number>`", reply_to=msg_id)
        return

    job_id = parts[0].lstrip("#")
    job = _dispatch_jobs.get(job_id)
    if not job:
        await send_message(
            chat_id, f"No tracked job #{job_id}. Use `/jobs` to list.", reply_to=msg_id
        )
        return

    node = job.get("node", "local")
    node_cfg = DISPATCH_NODES.get(node, {})
    tmux_name = job.get("tmux_session", "")

    # 1. Check tmux alive
    alive = await _check_tmux_alive(tmux_name, node)

    # 2. Tail output file
    output_tail = ""
    try:
        output_path = shlex.quote(
            job.get("output_file", f"/tmp/claude-jobs/{job_id}.out")
        )
        if node_cfg.get("ssh"):
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                node_cfg["ssh"],
                f"tail -20 {output_path} 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                f"tail -20 {output_path} 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        output_tail = stdout.decode()[-1000:]
    except Exception:
        output_tail = "(could not read output)"

    # 3. Check GitHub issue state
    issue_state = ""
    if job.get("repo"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "issue",
                "view",
                str(job["issue"]),
                "--repo",
                job["repo"],
                "--json",
                "state,title,comments",
                "--jq",
                '.state + " | " + .title + " | " + (.comments | length | tostring) + " comments"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            issue_state = stdout.decode().strip()
        except Exception:
            issue_state = "(could not check issue)"

    # Update status
    if job.get("status") == "running" and not alive:
        job["status"] = "completed"
        _save_jobs()

    status = (
        f"*Job #{job_id}*\n"
        f"Node: `{node}`\n"
        f"tmux: `{tmux_name}` ({'alive' if alive else 'exited'})\n"
        f"Status: {job.get('status', '?')}\n"
    )
    if issue_state:
        status += f"Issue: {issue_state}\n"
    if output_tail:
        tail_preview = output_tail.strip()[-500:]
        status += f"\nLast output:\n```\n{tail_preview}\n```"

    await send_message(chat_id, status, reply_to=msg_id)


async def cmd_job_kill(chat_id: int, msg_id: int, arg: str):
    """Kill a running tmux worker."""
    job_id = arg.strip().lstrip("#")
    if not job_id:
        await send_message(
            chat_id, "Usage: `/job-kill <issue-number>`", reply_to=msg_id
        )
        return

    job = _dispatch_jobs.get(job_id)
    if not job:
        await send_message(chat_id, f"No tracked job #{job_id}.", reply_to=msg_id)
        return

    node = job.get("node", "local")
    node_cfg = DISPATCH_NODES.get(node, {})
    tmux_name = job.get("tmux_session", "")

    try:
        safe_tmux = shlex.quote(tmux_name)
        if node_cfg.get("ssh"):
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                node_cfg["ssh"],
                f"tmux kill-session -t {safe_tmux} 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                f"tmux kill-session -t {safe_tmux} 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        await asyncio.wait_for(proc.communicate(), timeout=15)
        job["status"] = "killed"
        _save_jobs()
        await send_message(
            chat_id, f"Killed job #{job_id} on `{node}`.", reply_to=msg_id
        )
    except Exception as e:
        await send_message(
            chat_id, f"Error killing job #{job_id}: {e}", reply_to=msg_id
        )


def record_session(session_id: str, label: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    folder = state["active_folder"]
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
    # Track last session per folder
    state["folder_sessions"][folder] = session_id
    save_state()


def path_to_claude_key(folder_path: str) -> str:
    """Convert folder path to Claude's project key format."""
    return folder_path.replace("/", "-")


def get_sessions_index_path(folder_path: str) -> Path:
    """Get Claude CLI sessions-index.json for a folder."""
    key = path_to_claude_key(folder_path)
    return Path.home() / ".claude" / "projects" / key / "sessions-index.json"


def get_claude_sessions(folder_path: str | None = None) -> list[dict]:
    """Read sessions from Claude CLI sessions-index.json for active folder."""
    fp = folder_path or state["active_folder"]
    index_path = get_sessions_index_path(fp)
    if not index_path.exists():
        return []
    try:
        with open(index_path) as f:
            data = json.load(f)
        entries = data.get("entries", [])
        entries.sort(key=lambda x: x.get("modified", ""), reverse=True)
        return entries
    except Exception as e:
        log.warning(f"Failed to read sessions index: {e}")
        return []


def get_harness_sessions(folder_path: str | None = None) -> list[dict]:
    if HARNESS_SESSION_BACKEND == "claude":
        return get_claude_sessions(folder_path)
    return []


def get_harness_session_label(entry: dict) -> str:
    return str(entry.get("summary") or entry.get("firstPrompt") or "").strip()


def get_latest_session_id(folder_path: str | None = None) -> str | None:
    """Get the most recent session ID for a folder."""
    sessions = get_harness_sessions(folder_path)
    if sessions:
        return sessions[0]["sessionId"]
    return state.get("default_session_id")


def read_session_messages(
    session_id: str, folder_path: str | None = None, last_n: int = 5
) -> list[dict]:
    """Read the last N human/assistant text messages from a session JSONL file.

    Returns list of {role, text, timestamp} dicts.
    """
    fp = folder_path or state["active_folder"]
    key = path_to_claude_key(fp)
    jsonl_path = Path.home() / ".claude" / "projects" / key / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return []

    messages = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                if msg_type not in ("user", "assistant"):
                    continue

                msg = obj.get("message", {})
                role = msg.get("role", "")
                content = msg.get("content", "")
                timestamp = obj.get("timestamp", "")

                # Extract text from content
                if isinstance(content, list):
                    texts = [
                        b.get("text", "")
                        for b in content
                        if b.get("type") == "text" and b.get("text")
                    ]
                    if not texts:
                        continue  # tool use only, skip
                    text = "\n".join(texts)
                elif isinstance(content, str) and content:
                    text = content
                else:
                    continue

                messages.append({"role": role, "text": text, "timestamp": timestamp})
    except Exception as e:
        log.warning(f"Failed to read session {session_id}: {e}")
        return []

    return messages[-last_n:] if messages else []


def find_session(query: str) -> list[str]:
    """Find sessions by ID prefix, label, or summary in current folder."""
    query_lower = query.lower()
    matches = []
    seen = set()
    folder = state["active_folder"]

    # ID prefix match in bridge state (filter by folder)
    for sid, info in state["sessions"].items():
        if sid.startswith(query) and info.get("folder", HOME) == folder:
            matches.append(sid)
            seen.add(sid)

    # ID prefix match in harness CLI sessions
    for entry in get_harness_sessions():
        sid = entry["sessionId"]
        if sid not in seen and sid.startswith(query):
            matches.append(sid)
            seen.add(sid)

    if matches:
        return matches

    # Label/summary substring match in bridge state
    for sid, info in state["sessions"].items():
        if info.get("folder", HOME) != folder:
            continue
        label = info.get("label", "").lower()
        if query_lower in label and sid not in seen:
            matches.append(sid)
            seen.add(sid)

    # Summary/firstPrompt match in harness CLI sessions
    for entry in get_harness_sessions():
        sid = entry["sessionId"]
        if sid in seen:
            continue
        label = get_harness_session_label(entry).lower()
        if query_lower in label:
            matches.append(sid)
            seen.add(sid)

    return matches


def get_folder_display_name(path: str) -> str:
    """Get short name for a folder path."""
    for name, fp in state["folders"].items():
        if fp == path:
            return name
    return Path(path).name


# --- Telegram API ---

_http_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(POLL_TIMEOUT + 30, connect=10)
        )
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
            log.warning(
                f"Telegram API {method} not ok: {result.get('description', '')}"
            )
        return result
    except Exception as e:
        log.error(f"Telegram API error ({method}): {e}")
        return None


async def send_typing(chat_id: int):
    await tg_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def sanitize_markdown(text: str) -> str:
    """Sanitize text for Telegram Markdown.

    Telegram's Markdown parser is strict about balanced markers.
    This ensures backticks, underscores, and asterisks are balanced,
    and converts triple-backtick code blocks to indented format
    since Telegram Markdown v1 doesn't support them.
    """
    import re

    # Convert ```lang\n...\n``` code blocks to indented (Telegram doesn't support ```)
    def replace_code_block(m):
        code = m.group(1)  # First (and only) capture group
        # Indent each line with 4 spaces for monospace in plain text fallback
        # For Markdown, use single backtick wrapping
        return f"`{code.strip()}`"

    text = re.sub(r"```\w*\n(.*?)```", replace_code_block, text, flags=re.DOTALL)
    # Handle unclosed triple backticks
    text = text.replace("```", "`")

    # Ensure balanced backticks (single)
    backtick_count = text.count("`")
    if backtick_count % 2 != 0:
        text += "`"

    # Ensure balanced underscores (only outside backticks)
    # Simple approach: count underscores outside code spans
    in_code = False
    underscore_count = 0
    for ch in text:
        if ch == "`":
            in_code = not in_code
        elif ch == "_" and not in_code:
            underscore_count += 1
    if underscore_count % 2 != 0:
        text += "_"

    # Ensure balanced asterisks (outside backticks)
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
    """Send message, splitting if too long. Falls back from Markdown to plain text."""
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
        # Try Markdown first
        data["parse_mode"] = "Markdown"
        result = await tg_api("sendMessage", data)
        if result is None or not result.get("ok"):
            # Markdown failed — retry as plain text (strip parse_mode, use original text)
            log.info(
                f"Markdown send failed, retrying as plain text ({len(chunk)} chars)"
            )
            data["text"] = chunk
            data.pop("parse_mode", None)
            await tg_api("sendMessage", data)


async def send_plain_message(chat_id: int, text: str, reply_to: int | None = None):
    """Send one plain-text message without Markdown parsing."""
    data = {"chat_id": chat_id, "text": text}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    return await tg_api("sendMessage", data)


# --- Media Download ---


async def download_telegram_file(
    file_id: str, filename: str, msg_id: int
) -> Path | None:
    """Download a Telegram file by file_id. Returns local path or None."""
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
        log.info(f"Downloaded media: {local_path} ({len(resp.content)} bytes)")
        return local_path
    except Exception as e:
        log.error(f"Failed to download file {file_id}: {e}")
        return None


async def extract_media(message: dict) -> tuple[str | None, str | None]:
    """Extract media from a Telegram message.

    Returns (local_file_path, media_description) or (None, None).
    """
    msg_id = message.get("message_id", 0)

    # Photo — Telegram sends multiple sizes, pick largest
    if "photo" in message:
        sizes = message["photo"]
        best = max(sizes, key=lambda s: s.get("file_size", 0))
        path = await download_telegram_file(best["file_id"], "photo.jpg", msg_id)
        if path:
            return str(path), "image"

    # Document (files, high-res images sent as files)
    if "document" in message:
        doc = message["document"]
        fname = doc.get("file_name", "document")
        path = await download_telegram_file(doc["file_id"], fname, msg_id)
        if path:
            mime = doc.get("mime_type", "")
            kind = "image" if mime.startswith("image/") else "file"
            return str(path), kind

    # Voice message
    if "voice" in message:
        voice = message["voice"]
        path = await download_telegram_file(voice["file_id"], "voice.ogg", msg_id)
        if path:
            return str(path), "voice message"

    # Video note (round video)
    if "video_note" in message:
        vn = message["video_note"]
        path = await download_telegram_file(vn["file_id"], "video_note.mp4", msg_id)
        if path:
            return str(path), "video note"

    # Video
    if "video" in message:
        vid = message["video"]
        fname = vid.get("file_name", "video.mp4")
        path = await download_telegram_file(vid["file_id"], fname, msg_id)
        if path:
            return str(path), "video"

    # Sticker (static only)
    if "sticker" in message:
        sticker = message["sticker"]
        emoji = sticker.get("emoji", "")
        if sticker.get("is_animated") or sticker.get("is_video"):
            return None, None
        path = await download_telegram_file(sticker["file_id"], "sticker.webp", msg_id)
        if path:
            return str(path), f"sticker {emoji}"

    return None, None


async def handle_media_message(
    chat_id: int, msg_id: int, message: dict, user_text: str
):
    """Handle a message containing media (photo, document, etc.)."""
    media_path, media_type = await extract_media(message)

    if not media_path:
        await send_message(
            chat_id,
            "Could not download the attachment. Try sending as a file.",
            reply_to=msg_id,
        )
        return

    if media_type == "image":
        prompt = (
            f"[The user sent an image. View it with the Read tool at: {media_path}]"
        )
    elif media_type == "voice message":
        prompt = f"[The user sent a voice message. The audio file is at: {media_path}]"
    else:
        prompt = f"[The user sent a {media_type}. The file is at: {media_path}]"

    if user_text:
        prompt += f"\n\n{user_text}"
    else:
        prompt += "\n\nPlease examine this and describe what you see."

    await enqueue_prompt(chat_id, msg_id, prompt)


async def cleanup_old_media():
    """Remove downloaded media files older than 1 hour."""
    if not MEDIA_DIR.exists():
        return
    cutoff = time.time() - 3600
    for f in MEDIA_DIR.iterdir():
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# --- Harness CLI ---


def _extract_event_session_id(event: dict) -> str | None:
    candidates = [
        event.get("session_id"),
        event.get("sessionId"),
        event.get("sessionID"),
        (event.get("session") or {}).get("id")
        if isinstance(event.get("session"), dict)
        else None,
        (event.get("message") or {}).get("session_id")
        if isinstance(event.get("message"), dict)
        else None,
        (event.get("message") or {}).get("sessionId")
        if isinstance(event.get("message"), dict)
        else None,
        (event.get("message") or {}).get("sessionID")
        if isinstance(event.get("message"), dict)
        else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _extract_event_text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("result", "text", "content", "message"):
            extracted = _extract_event_text(value.get(key))
            if extracted:
                return extracted
        return ""
    if isinstance(value, list):
        parts = [_extract_event_text(item) for item in value]
        joined = "\n".join(part for part in parts if part)
        return joined.strip()
    return ""


async def run_harness(
    prompt: str,
    chat_id: int,
    session_id: str | None = None,
    new_session: bool = False,
    suppress_progress_messages: bool = False,
    suppress_footer: bool = False,
) -> tuple[str, str | None]:
    """Run the configured harness CLI with streaming. Sends progress to Telegram as events arrive.

    Uses --output-format stream-json to read events line-by-line.
    Watchdog coroutine monitors for stuck tool calls (started by _process_prompt).
    Returns (final_result_text, session_id).
    """
    global _active_harness_proc, _watchdog_current_tool, _watchdog_last_progress

    cwd = state["active_folder"]
    proc_env = os.environ.copy()
    sid = None if new_session else (session_id or state.get("default_session_id"))

    if HARNESS_CLI == "claude":
        cmd = ["claude", "--print", "--output-format", "stream-json", "--verbose"]
        if sid:
            cmd.extend(["--resume", sid])
        cmd.extend(
            [
                "--dangerously-skip-permissions",
                "--append-system-prompt",
                "IMPORTANT: You are running inside the Telegram bridge service. "
                "NEVER run systemctl, service, or process management commands "
                "(systemctl, kill, pkill, service, restart, stop). "
                "These will kill your own host process and crash the bridge. "
                "If asked about service status, explain you cannot check from inside the bridge.",
                "-p",
                prompt,
            ]
        )
        if BRIDGE_MODEL:
            # Only set ANTHROPIC_MODEL if the model is an Anthropic-compatible name.
            # Ollama/openrouter models break the real Claude CLI when injected as env.
            if BRIDGE_MODEL.startswith("claude-") or BRIDGE_MODEL.startswith(
                "anthropic-"
            ):
                proc_env["BRIDGE_MODEL"] = BRIDGE_MODEL
                proc_env["ANTHROPIC_MODEL"] = BRIDGE_MODEL
            else:
                # Pass non-Anthropic models via --model flag (supported by Claude CLI)
                cmd.extend(["--model", BRIDGE_MODEL])
    elif HARNESS_CLI in {"opencode", "kilo"}:
        cmd = [HARNESS_CLI, "run", "--format", "json", "--dir", cwd]
        if sid:
            cmd.extend(["--session", sid])
        if BRIDGE_MODEL:
            cmd.extend(["-m", BRIDGE_MODEL])
        if HARNESS_AGENT:
            cmd.extend(["--agent", HARNESS_AGENT])
        cmd.append(prompt)
    else:
        return f"Error: unsupported harness `{HARNESS_CLI}`", None

    log.info(
        f"{HARNESS_LABEL} in {cwd}: {' '.join(cmd[:8])}... | prompt: {prompt[:80]}"
    )

    result_text = None
    result_session_id = None
    result_duration_ms = 0
    last_activity_update = 0
    tool_uses = []  # Track what the harness is doing

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=proc_env,
            # Increase buffer limit to 4MB to handle large stream-json events
            limit=4 * 1024 * 1024,
        )
        _active_harness_proc = proc

        async for raw_line in proc.stdout:
            _watchdog_last_event = time.time()  # Watchdog heartbeat
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            generic_session_id = _extract_event_session_id(event)
            if generic_session_id:
                result_session_id = generic_session_id

            if (
                HARNESS_CLI == "claude"
                and event_type == "system"
                and event.get("subtype") == "init"
            ):
                result_session_id = event.get("session_id")
                log.info(f"Stream started: session={result_session_id}")

            elif HARNESS_CLI == "claude" and event_type == "assistant":
                msg = event.get("message", {})
                content = msg.get("content", [])
                # Check for tool use — report what the harness is doing
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        # Build a short description of the action
                        desc = _describe_tool_use(tool_name, tool_input)
                        if desc:
                            tool_uses.append(desc)
                            now = time.time()
                            # Send activity update at most every 15s
                            if (
                                not suppress_progress_messages
                                and now - last_activity_update > 15
                            ):
                                await send_message(chat_id, f"_... {desc}_")
                                last_activity_update = now
                        # Watchdog: track current tool call
                        _watchdog_current_tool = {
                            "name": tool_name,
                            "command": tool_input.get("command", "")
                            if tool_name == "Bash"
                            else "",
                            "input_summary": desc or f"{tool_name}",
                            "started": time.time(),
                        }
                        _watchdog_last_progress = time.time()

            elif HARNESS_CLI == "claude" and event_type == "tool_result":
                # Tool completed — clear watchdog tracking and mark progress
                _watchdog_current_tool = None
                _watchdog_last_progress = time.time()

            elif event_type == "result":
                _watchdog_current_tool = None  # Clear on final result too
                result_text = _extract_event_text(
                    event.get("result") or event.get("text") or event
                )
                result_session_id = generic_session_id or event.get(
                    "session_id", result_session_id
                )
                result_duration_ms = event.get("duration_ms", 0)
                is_error = event.get("is_error", False)
                if is_error:
                    result_text = f"Error: {result_text}"
            elif HARNESS_CLI in {"opencode", "kilo"}:
                extracted_text = _extract_event_text(event)
                if extracted_text and event_type in {"assistant", "message", "output"}:
                    result_text = extracted_text

        # Wait for process to fully exit
        await proc.wait()
        _active_harness_proc = None

        # Check stderr if no result
        if result_text is None:
            err = ""
            if proc.stderr:
                err_bytes = await proc.stderr.read()
                err = err_bytes.decode("utf-8", errors="replace").strip()
            if (
                "No conversation found" in err or "no recent" in err.lower()
            ) and not new_session:
                log.info("No existing session found, starting fresh")
                return await run_harness(
                    prompt,
                    chat_id,
                    new_session=True,
                    suppress_progress_messages=suppress_progress_messages,
                    suppress_footer=suppress_footer,
                )
            return f"(empty response)\n\nstderr: {err[:300]}", None

        if suppress_footer:
            return result_text, result_session_id

        folder_name = get_folder_display_name(cwd)
        footer = f"\n\n_({result_duration_ms / 1000:.1f}s \u2022 {folder_name})_"
        return result_text + footer, result_session_id

    except Exception as e:
        _active_harness_proc = None
        log.error(f"{HARNESS_LABEL} stream error: {e}")
        return f"Error: {e}", None


def _describe_tool_use(tool_name: str, tool_input: dict) -> str:
    """Create a short human-readable description of a tool use."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"Running: `{cmd[:60]}`"
    elif tool_name == "Read":
        path = tool_input.get("file_path", "")
        return f"Reading: `{Path(path).name}`"
    elif tool_name in ("Edit", "Write"):
        path = tool_input.get("file_path", "")
        return f"{'Editing' if tool_name == 'Edit' else 'Writing'}: `{Path(path).name}`"
    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return f"Searching: `{pattern}`"
    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"Grep: `{pattern[:40]}`"
    elif tool_name == "WebFetch":
        url = tool_input.get("url", "")
        return f"Fetching: `{url[:50]}`"
    elif tool_name == "WebSearch":
        query = tool_input.get("query", "")
        return f"Searching web: `{query[:40]}`"
    elif tool_name == "Task":
        desc = tool_input.get("description", "")
        return f"Task: {desc[:40]}" if desc else ""
    elif tool_name:
        return f"Using {tool_name}"
    return ""


# --- Watchdog ---


def _is_instant_kill_command(command: str) -> bool:
    """Check if a command matches the known-infinite blocklist."""
    cmd_lower = command.lower().strip()
    for pattern in WATCHDOG_INSTANT_KILL_PATTERNS:
        if pattern.lower() in cmd_lower:
            return True
    # Also catch: ssh <host> '<anything with tail -f or journalctl -f>'
    if "ssh " in cmd_lower and any(
        p in cmd_lower for p in ["tail -f", "tail --follow", "journalctl -f"]
    ):
        return True
    return False


async def _watchdog_monitor(chat_id: int, cwd: str):
    """Monitor active harness process for stuck tool calls.

    Runs as a concurrent task alongside run_harness(). Checks every 10s.
    Three-tier response:
      1. Instant kill for known-infinite commands (blocklist)
      2. Claude-as-judge evaluation for ambiguous hangs
      3. Stagnation kill — only fires when NO progress (tool_result/new tool_use)
         has been seen for WATCHDOG_STAGNATION_KILL seconds. A session that keeps
         completing tools and starting new ones is never killed by Tier 3.
    """
    global _watchdog_evaluation_in_progress, _watchdog_last_progress

    _watchdog_last_progress = time.time()  # Initialize on session start
    last_eval_time = 0.0

    while True:
        await asyncio.sleep(10)

        # Check if process is still running
        if _active_harness_proc is None or _active_harness_proc.returncode is not None:
            return

        now = time.time()
        stagnation_time = now - _watchdog_last_progress

        # --- Tier 3: Stagnation kill ---
        # Only fires when a SINGLE tool has been running with no progress events
        # (no tool_result, no new tool_use) for WATCHDOG_STAGNATION_KILL seconds.
        # Multi-step sessions that keep progressing never trigger this.
        if stagnation_time > WATCHDOG_STAGNATION_KILL:
            tool_desc = ""
            if _watchdog_current_tool:
                tool_desc = f" (stuck on: {_watchdog_current_tool.get('input_summary', 'unknown')[:60]})"
            log.warning(
                f"Watchdog: stagnation timeout ({WATCHDOG_STAGNATION_KILL}s, "
                f"no progress since {stagnation_time:.0f}s ago){tool_desc} — force killing"
            )
            await _watchdog_kill(
                chat_id,
                reason=f"No progress for {stagnation_time / 60:.0f}min{tool_desc}",
                tool_info=_watchdog_current_tool,
            )
            return

        # No active tool call — nothing to check for Tier 1/2
        if _watchdog_current_tool is None:
            continue

        tool = _watchdog_current_tool
        tool_elapsed = now - tool["started"]
        tool_name = tool["name"]
        tool_command = tool.get("command", "")

        # Determine timeout for this tool type
        if tool_name == "Bash":
            # SSH-wrapped commands get a longer timeout — remote builds,
            # device installs, xcrun, etc. can legitimately take 15-20 min
            if tool_command.strip().startswith("ssh "):
                timeout = WATCHDOG_SSH_TIMEOUT
            else:
                timeout = WATCHDOG_BASH_TIMEOUT
        elif tool_name in ("Task", "TaskOutput"):
            timeout = WATCHDOG_TASK_TIMEOUT
        else:
            timeout = WATCHDOG_DEFAULT_TIMEOUT

        # Not timed out yet
        if tool_elapsed < timeout:
            continue

        # --- Tier 1: Instant kill for known-infinite commands ---
        if tool_name == "Bash" and _is_instant_kill_command(tool_command):
            log.warning(f"Watchdog: instant-kill pattern matched: {tool_command[:80]}")
            await _watchdog_kill(
                chat_id,
                reason=f"Known infinite command: `{tool_command[:60]}`",
                tool_info=tool,
            )
            return

        # --- Tier 2: Claude-as-judge evaluation ---
        if not _watchdog_evaluation_in_progress and (now - last_eval_time) > 120:
            _watchdog_evaluation_in_progress = True
            last_eval_time = now
            try:
                verdict = await _watchdog_evaluate(tool, tool_elapsed, cwd)
                if verdict["action"] == "KILL":
                    await _watchdog_kill(
                        chat_id,
                        reason=verdict["reason"],
                        tool_info=tool,
                    )
                    return
                elif verdict["action"] == "EXTEND":
                    extend_mins = verdict.get("minutes", 5)
                    tool["started"] = now - timeout + (extend_mins * 60)
                    log.info(
                        f"Watchdog: extending {extend_mins}min — {verdict['reason']}"
                    )
                    await send_message(
                        chat_id,
                        f"_Watchdog: extending timeout {extend_mins}min — {verdict['reason']}_",
                    )
                else:  # WAIT
                    log.info(f"Watchdog: waiting — {verdict['reason']}")
            except Exception as e:
                log.error(f"Watchdog evaluation failed: {e}")
                # If evaluation fails and we're past 2x timeout, force kill
                if tool_elapsed > timeout * 2:
                    await _watchdog_kill(
                        chat_id,
                        reason=f"Evaluation failed + exceeded 2x timeout ({tool_elapsed:.0f}s)",
                        tool_info=tool,
                    )
                    return
            finally:
                _watchdog_evaluation_in_progress = False


async def _watchdog_kill(chat_id: int, reason: str, tool_info: dict | None):
    """Kill the active harness process and notify user."""
    tool_desc = ""
    if tool_info:
        elapsed = time.time() - tool_info["started"]
        tool_desc = (
            f"\nTool: `{tool_info['name']}`"
            f"\nCommand: `{tool_info.get('command', 'N/A')[:100]}`"
            f"\nRunning: {elapsed / 60:.1f} min"
        )

    log.warning(f"Watchdog KILL: {reason}")

    if _active_harness_proc and _active_harness_proc.returncode is None:
        try:
            _active_harness_proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(_active_harness_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                _active_harness_proc.terminate()
                try:
                    await asyncio.wait_for(_active_harness_proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    _active_harness_proc.kill()
        except ProcessLookupError:
            pass

    await send_message(
        chat_id,
        f"*Watchdog killed stuck process*\n"
        f"Reason: {reason}{tool_desc}\n\n"
        f"_Session preserved — send a new message to continue._",
    )


async def _watchdog_evaluate(tool_info: dict, elapsed: float, cwd: str) -> dict:
    """Ask Claude Code (separate session) whether a stuck process should be killed.

    Returns: {"action": "KILL"|"WAIT"|"EXTEND", "reason": str, "minutes": int}
    """
    tool_name = tool_info["name"]
    tool_command = tool_info.get("command", "N/A")
    tool_input_summary = tool_info.get("input_summary", "N/A")

    eval_prompt = (
        f"You are a process watchdog for a Claude Code Telegram bridge. "
        f"A Claude Code session has a tool call that appears stuck.\n\n"
        f"TOOL: {tool_name}\n"
        f"COMMAND: {tool_command}\n"
        f"WORKING DIR: {cwd}\n"
        f"ELAPSED: {elapsed:.0f} seconds ({elapsed / 60:.1f} minutes)\n"
        f"TOOL INPUT SUMMARY: {tool_input_summary}\n\n"
        f"Based on your knowledge of this system (fleet, projects, network topology, "
        f"typical build times, etc.), should this process be killed?\n\n"
        f"Consider:\n"
        f"- Is this command expected to be long-running or infinite?\n"
        f"- Could it be a dev server, log tailer, or watcher (infinite by design)?\n"
        f"- Is the elapsed time reasonable for the operation type?\n"
        f"- Could network latency or large data explain the delay?\n\n"
        f"Respond with EXACTLY one line in one of these formats:\n"
        f"KILL: <reason>\n"
        f"WAIT: <reason>\n"
        f"EXTEND:<minutes> <reason>\n\n"
        f"Examples:\n"
        f"KILL: npm start is a dev server, runs forever, should not run in bridge\n"
        f"WAIT: large git clone to Mac Mini over Tailscale, 5min is normal\n"
        f"EXTEND:10 pytest on polybot has 200+ tests, may need 15min total"
    )

    log.info(
        f"Watchdog: evaluating {tool_name} ({elapsed:.0f}s) via {WATCHDOG_EVAL_NODE}"
    )

    # Use -p (print mode, one-shot) — no --resume since print mode requires UUID
    # session IDs and the eval is stateless. Each evaluation is independent.
    if WATCHDOG_EVAL_NODE == "local":
        cmd = [
            "claude",
            "-p",
            eval_prompt,
            "--dangerously-skip-permissions",
            "--max-turns",
            "1",
            "--output-format",
            "text",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    else:
        # Remote evaluation via SSH (e.g., WATCHDOG_EVAL_NODE="ssh://letta@100.75.8.74")
        remote = WATCHDOG_EVAL_NODE.replace("ssh://", "")
        remote_cmd = (
            f"claude "
            f"-p {shlex.quote(eval_prompt)} "
            f"--dangerously-skip-permissions --max-turns 1 --output-format text"
        )
        cmd = [
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=no",
            remote,
            remote_cmd,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        log.warning("Watchdog evaluation timed out (60s)")
        if proc.returncode is None:
            proc.kill()
        return {"action": "WAIT", "reason": "Evaluation timed out — defaulting to wait"}

    if not output:
        stderr_text = stderr.decode("utf-8", errors="replace")[:200] if stderr else ""
        log.warning(f"Watchdog evaluation empty output, stderr: {stderr_text}")
        return {
            "action": "WAIT",
            "reason": f"Evaluation failed ({stderr_text[:60]}) — defaulting to wait",
        }

    log.info(f"Watchdog evaluation response: {output[:200]}")
    return _parse_watchdog_verdict(output)


def _parse_watchdog_verdict(output: str) -> dict:
    """Parse KILL/WAIT/EXTEND verdict from the Claude Code evaluation response."""
    for line in reversed(output.strip().split("\n")):
        line = line.strip()
        if line.startswith("KILL:"):
            return {"action": "KILL", "reason": line[5:].strip()}
        elif line.startswith("WAIT:"):
            return {"action": "WAIT", "reason": line[5:].strip()}
        elif line.startswith("EXTEND:"):
            rest = line[7:].strip()
            parts = rest.split(maxsplit=1)
            try:
                minutes = int(parts[0])
            except (ValueError, IndexError):
                minutes = 5
            reason = parts[1] if len(parts) > 1 else "extended"
            return {"action": "EXTEND", "reason": reason, "minutes": minutes}

    # No valid line found — default to WAIT (will retry later)
    log.warning(f"Watchdog: couldn't parse verdict from: {output[:200]}")
    return {
        "action": "WAIT",
        "reason": f"Unparseable response (will retry): {output[:80]}",
    }


# --- Command Handlers ---


async def handle_command(chat_id: int, msg_id: int, text: str):
    """Handle /commands."""
    global BRIDGE_MODEL, HARNESS_AGENT
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/help", "/start"):
        folder_name = get_folder_display_name(state["active_folder"])
        agent_command = ""
        if HARNESS_CLI in {"opencode", "kilo"}:
            agent_command = "`/agent [name|off]` — Show/set harness agent profile\n"
        await send_message(
            chat_id,
            (
                f"*{HARNESS_LABEL} Telegram Bridge v{BRIDGE_VERSION}*\n"
                f"Active folder: `{folder_name}` (`{state['active_folder']}`)\n\n"
                f"Send any message, image, or file to chat with {HARNESS_LABEL}.\n"
                "Photos, documents, and voice messages are supported.\n"
                "Session auto-continues the latest in current folder.\n\n"
                "*Folder Commands:*\n"
                "`/folders` \u2014 List project folders\n"
                "`/folder <name>` \u2014 Switch folder\n"
                "`/folder add <name> <path>` \u2014 Register folder\n"
                "`/folder create <name> [path]` \u2014 Create new project\n"
                "`/folder rm <name>` \u2014 Remove folder\n"
                "`/clone <url> [name]` \u2014 Clone repo + switch\n"
                "`/init` \u2014 Init current folder (git + CLAUDE.md)\n\n"
                "*Session Commands:*\n"
                "`/history [n]` \u2014 Last N messages from session\n"
                "`/new [label]` \u2014 Fresh session in current folder\n"
                "`/rename <label>` \u2014 Rename current session\n"
                "`/save [label]` \u2014 Bookmark session\n"
                "`/sessions` \u2014 List sessions for current folder\n"
                "`/resume <id|name>` \u2014 Resume by ID or name\n"
                "`/model [model-id|off]` \u2014 Show/set harness model override\n"
                f"{agent_command}"
                f"`/interrupt [msg]` \u2014 Stop {HARNESS_LABEL} mid-run; optional msg next\n\n"
                "*Job Dispatch:*\n"
                "`/dispatch [--node N] [--repo R] desc` \u2014 Create issue + launch worker\n"
                "`/jobs` \u2014 List active dispatch jobs\n"
                "`/job N` \u2014 Check job status (tmux + issue + output)\n"
                "`/job-kill N` \u2014 Kill a running worker\n\n"
                "`/watchdog` \u2014 Watchdog status\n"
                "`/status` \u2014 Bridge status"
            ),
            reply_to=msg_id,
        )
        return

    if cmd == "/folders":
        lines = ["*Project Folders:*\n"]
        for name, path in sorted(state["folders"].items()):
            active = " \u2190 active" if path == state["active_folder"] else ""
            has_claude_md = "\u2705" if Path(path, "CLAUDE.md").exists() else ""
            has_git = "\U0001f4e6" if Path(path, ".git").exists() else ""
            last_sid = state["folder_sessions"].get(path)
            session_info = ""
            if last_sid:
                info = state["sessions"].get(last_sid, {})
                label = info.get("label", "")
                if label:
                    session_info = f"\n  Last: _{label}_"
            lines.append(
                f"`{name}` {has_git}{has_claude_md} `{path}`{active}{session_info}"
            )
        lines.append(
            f"\n`/folder <name>` to switch.\n`/folder add <name> <path>` to register."
        )
        await send_message(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd == "/folder":
        if not arg:
            folder_name = get_folder_display_name(state["active_folder"])
            await send_message(
                chat_id,
                f"Current folder: `{folder_name}` (`{state['active_folder']}`)\n\nUse `/folder <name>` to switch.",
                reply_to=msg_id,
            )
            return

        sub_parts = arg.split(maxsplit=2)
        sub_cmd = sub_parts[0].lower()

        # /folder add <name> <path>
        if sub_cmd == "add" and len(sub_parts) >= 3:
            name = sub_parts[1]
            path = os.path.expanduser(sub_parts[2])
            if not os.path.isdir(path):
                await send_message(
                    chat_id, f"Directory not found: `{path}`", reply_to=msg_id
                )
                return
            state["folders"][name] = path
            save_state()
            has_claude_md = (
                " (CLAUDE.md found)" if Path(path, "CLAUDE.md").exists() else ""
            )
            has_git = " (git repo)" if Path(path, ".git").exists() else ""
            await send_message(
                chat_id,
                f"Registered `{name}` \u2192 `{path}`{has_git}{has_claude_md}",
                reply_to=msg_id,
            )
            return

        # /folder create <name> [path]
        if sub_cmd == "create" and len(sub_parts) >= 2:
            name = sub_parts[1]
            if name in state["folders"]:
                await send_message(
                    chat_id,
                    f"Folder `{name}` already exists: `{state['folders'][name]}`",
                    reply_to=msg_id,
                )
                return
            # Default path: ~/projects/<name>
            if len(sub_parts) >= 3:
                base = os.path.expanduser(sub_parts[2])
            else:
                base = os.path.join(HOME, "projects", name)
            try:
                os.makedirs(base, exist_ok=True)
            except OSError as e:
                await send_message(
                    chat_id, f"Failed to create directory: {e}", reply_to=msg_id
                )
                return
            state["folders"][name] = base
            # Switch to the new folder
            if state["default_session_id"]:
                state["folder_sessions"][state["active_folder"]] = state[
                    "default_session_id"
                ]
            state["active_folder"] = base
            state["default_session_id"] = None
            save_state()
            await send_message(
                chat_id,
                (
                    f"Created `{name}` at `{base}`\n"
                    f"Switched to `{name}`.\n\n"
                    f"Use `/init` to set up git + CLAUDE.md, or just start chatting."
                ),
                reply_to=msg_id,
            )
            return

        # /folder rm <name>
        if sub_cmd == "rm" and len(sub_parts) >= 2:
            name = sub_parts[1]
            if name not in state["folders"]:
                await send_message(
                    chat_id, f"No folder named `{name}`", reply_to=msg_id
                )
                return
            if name == "home":
                await send_message(
                    chat_id, "Can't remove `home` folder.", reply_to=msg_id
                )
                return
            removed_path = state["folders"].pop(name)
            if state["active_folder"] == removed_path:
                state["active_folder"] = HOME
                state["default_session_id"] = state["folder_sessions"].get(HOME)
            save_state()
            await send_message(chat_id, f"Removed `{name}`", reply_to=msg_id)
            return

        # /folder <name> — switch to folder
        name = sub_cmd
        if name not in state["folders"]:
            # Try path match
            expanded = os.path.expanduser(arg)
            if os.path.isdir(expanded):
                # Auto-register
                short = Path(expanded).name
                state["folders"][short] = expanded
                name = short
            else:
                available = ", ".join(f"`{n}`" for n in sorted(state["folders"]))
                await send_message(
                    chat_id,
                    f"Unknown folder `{name}`.\nAvailable: {available}",
                    reply_to=msg_id,
                )
                return

        path = state["folders"][name]
        if not os.path.isdir(path):
            await send_message(
                chat_id, f"Directory no longer exists: `{path}`", reply_to=msg_id
            )
            return

        # Save current session for current folder before switching
        if state["default_session_id"]:
            state["folder_sessions"][state["active_folder"]] = state[
                "default_session_id"
            ]

        # Switch folder
        state["active_folder"] = path
        # Restore last session for this folder (--continue behavior)
        state["default_session_id"] = state["folder_sessions"].get(path)
        save_state()

        has_claude_md = "\u2705 CLAUDE.md" if Path(path, "CLAUDE.md").exists() else ""
        has_git = "\U0001f4e6 git" if Path(path, ".git").exists() else ""
        indicators = " ".join(filter(None, [has_git, has_claude_md]))
        if indicators:
            indicators = f" ({indicators})"

        sid = state["default_session_id"]
        if sid:
            info = state["sessions"].get(sid, {})
            label = info.get("label", "")
            if not label:
                cli_info = next(
                    (s for s in get_harness_sessions(path) if s["sessionId"] == sid),
                    None,
                )
                if cli_info:
                    label = get_harness_session_label(cli_info)
            session_msg = f"\nContinuing session `{sid[:8]}`"
            if label:
                session_msg += f" (_{label}_)"
        else:
            session_msg = "\nNo previous session \u2014 next message starts fresh."

        await send_message(
            chat_id,
            f"Switched to `{name}`{indicators}\n`{path}`{session_msg}",
            reply_to=msg_id,
        )
        return

    if cmd == "/clone":
        if not arg:
            await send_message(
                chat_id,
                "Usage: `/clone <repo-url> [name]`\n\nExamples:\n`/clone https://github.com/user/repo`\n`/clone git@github.com:user/repo.git myproject`",
                reply_to=msg_id,
            )
            return

        clone_parts = arg.split(maxsplit=1)
        repo_url = clone_parts[0]
        # Derive name from URL or use provided name
        if len(clone_parts) >= 2:
            name = clone_parts[1].strip()
        else:
            # Extract repo name from URL: github.com/user/repo.git -> repo
            name = repo_url.rstrip("/").split("/")[-1]
            if name.endswith(".git"):
                name = name[:-4]

        if name in state["folders"]:
            await send_message(
                chat_id,
                f"Folder `{name}` already exists: `{state['folders'][name]}`\nUse `/folder {name}` to switch, or provide a different name:\n`/clone {repo_url} other-name`",
                reply_to=msg_id,
            )
            return

        clone_path = os.path.join(HOME, "projects", name)
        if os.path.exists(clone_path):
            await send_message(
                chat_id,
                f"Path already exists: `{clone_path}`\nUse `/folder add {name} {clone_path}` to register it.",
                reply_to=msg_id,
            )
            return

        await send_message(
            chat_id, f"Cloning `{repo_url}` into `{clone_path}`...", reply_to=msg_id
        )
        await send_typing(chat_id)

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                repo_url,
                clone_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                await send_message(
                    chat_id, f"Clone failed:\n`{err[:500]}`", reply_to=msg_id
                )
                return

            # Register and switch
            state["folders"][name] = clone_path
            if state["default_session_id"]:
                state["folder_sessions"][state["active_folder"]] = state[
                    "default_session_id"
                ]
            state["active_folder"] = clone_path
            state["default_session_id"] = None
            save_state()

            has_claude_md = (
                " (CLAUDE.md found)" if Path(clone_path, "CLAUDE.md").exists() else ""
            )
            clone_output = stderr.decode("utf-8", errors="replace").strip()

            await send_message(
                chat_id,
                (
                    f"Cloned and switched to `{name}`{has_claude_md}\n"
                    f"`{clone_path}`\n\n"
                    f"`{clone_output}`\n\n"
                    f"Ready. Send a message to start working."
                ),
                reply_to=msg_id,
            )

        except asyncio.TimeoutError:
            await send_message(chat_id, "Clone timed out after 120s.", reply_to=msg_id)
        except Exception as e:
            await send_message(chat_id, f"Clone error: {e}", reply_to=msg_id)
        return

    if cmd == "/init":
        folder = state["active_folder"]
        folder_name = get_folder_display_name(folder)
        results = []

        # Git init if not already a repo
        if not Path(folder, ".git").exists():
            try:
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
            except Exception as e:
                results.append(f"git init: error ({e})")
        else:
            results.append("git: already initialized")

        # Create CLAUDE.md if it doesn't exist
        claude_md = Path(folder, "CLAUDE.md")
        if not claude_md.exists():
            try:
                claude_md.write_text(
                    f"# CLAUDE.md - {folder_name}\n\n"
                    f"Project initialized via Claude Telegram Bridge.\n\n"
                    f"## Overview\n\n"
                    f"_Describe this project here._\n\n"
                    f"## Key Files\n\n"
                    f"## Common Tasks\n\n"
                    f"## Notes\n"
                )
                results.append("CLAUDE.md: created")
            except Exception as e:
                results.append(f"CLAUDE.md: error ({e})")
        else:
            results.append("CLAUDE.md: already exists")

        # Create .gitignore if it doesn't exist
        gitignore = Path(folder, ".gitignore")
        if not gitignore.exists():
            try:
                gitignore.write_text(
                    "# Dependencies\nnode_modules/\nvenv/\n.venv/\n\n"
                    "# Environment\n.env\n.env.local\n\n"
                    "# IDE\n.vscode/\n.idea/\n\n"
                    "# OS\n.DS_Store\nThumbs.db\n\n"
                    "# Build\ndist/\nbuild/\n__pycache__/\n*.pyc\n"
                )
                results.append(".gitignore: created")
            except Exception as e:
                results.append(f".gitignore: error ({e})")
        else:
            results.append(".gitignore: already exists")

        status = "\n".join(results)
        await send_message(
            chat_id,
            (
                f"Initialized `{folder_name}` (`{folder}`):\n\n"
                f"{status}\n\n"
                f"Ready to go. Send a message to start working."
            ),
            reply_to=msg_id,
        )
        return

    if cmd in ("/history", "/last"):
        count = 5
        if arg:
            try:
                count = int(arg)
                count = min(count, 20)  # cap at 20
            except ValueError:
                pass

        # Use latest session for current folder
        sid = get_latest_session_id()
        if not sid:
            await send_message(
                chat_id, "No sessions in current folder.", reply_to=msg_id
            )
            return

        messages = read_session_messages(sid, last_n=count)
        if not messages:
            await send_message(
                chat_id, f"No messages found in session `{sid[:8]}`.", reply_to=msg_id
            )
            return

        folder_name = get_folder_display_name(state["active_folder"])
        lines = [f"*Last {len(messages)} messages* (`{sid[:8]}` in `{folder_name}`):\n"]

        for msg in messages:
            role = msg["role"]
            text = msg["text"]
            ts = msg.get("timestamp", "")[:16]  # trim to minutes
            prefix = "You" if role == "user" else HARNESS_LABEL

            # Truncate long messages
            if len(text) > 500:
                text = text[:497] + "..."
            # Escape any markdown issues in the preview
            text = text.replace("`", "'")

            lines.append(f"*{prefix}* ({ts}):\n{text}\n")

        await send_message(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd == "/new":
        label = arg if arg else None
        state["default_session_id"] = None
        # Clear folder session too
        state["folder_sessions"].pop(state["active_folder"], None)
        save_state()
        folder_name = get_folder_display_name(state["active_folder"])
        msg = f"Fresh session in `{folder_name}`."
        if label:
            msg += f" Will be labeled: _{label}_"
            state["_pending_label"] = label
            save_state()
        await send_message(chat_id, msg, reply_to=msg_id)
        return

    if cmd == "/rename":
        if not arg:
            await send_message(chat_id, "Usage: `/rename <label>`", reply_to=msg_id)
            return
        sid = state.get("default_session_id")
        if not sid:
            await send_message(chat_id, "No active session to rename.", reply_to=msg_id)
            return
        if sid not in state["sessions"]:
            record_session(sid, label=arg)
        else:
            state["sessions"][sid]["label"] = arg
            save_state()
        await send_message(
            chat_id, f"Session `{sid[:8]}` renamed: _{arg}_", reply_to=msg_id
        )
        return

    if cmd == "/save":
        sid = state.get("default_session_id")
        if not sid:
            await send_message(chat_id, "No active session to save.", reply_to=msg_id)
            return
        if sid not in state["sessions"]:
            record_session(sid, label=arg)
        if arg:
            state["sessions"][sid]["label"] = arg
        state["sessions"][sid]["saved"] = True
        save_state()
        label = state["sessions"][sid].get("label", "")
        name = f" (_{label}_)" if label else ""
        await send_message(
            chat_id,
            f"\U0001f4cc Session `{sid[:8]}`{name} bookmarked.",
            reply_to=msg_id,
        )
        return

    if cmd == "/sessions":
        folder = state["active_folder"]
        folder_name = get_folder_display_name(folder)
        harness_sessions = get_harness_sessions()
        seen = set()
        lines = [f"*Sessions in `{folder_name}`:*\n"]

        # Bridge-tracked sessions for this folder
        bridge_sessions = sorted(
            [
                (sid, info)
                for sid, info in state["sessions"].items()
                if info.get("folder", HOME) == folder
            ],
            key=lambda x: x[1].get("last_used", ""),
            reverse=True,
        )
        for sid, info in bridge_sessions:
            seen.add(sid)
            short = sid[:8]
            msgs = info.get("message_count", 0)
            label = info.get("label", "")
            saved = "\U0001f4cc " if info.get("saved") else ""
            active = " \u2190 active" if sid == state["default_session_id"] else ""

            cli_info = next(
                (s for s in harness_sessions if s["sessionId"] == sid), None
            )
            if cli_info:
                if not label:
                    label = get_harness_session_label(cli_info)
                msgs = max(msgs, cli_info.get("messageCount", 0))

            display = f"`{short}` {saved}{msgs} msgs{active}"
            if label:
                display += f"\n  _{label}_"
            lines.append(display)

        # Harness CLI sessions for this folder
        for entry in harness_sessions:
            sid = entry["sessionId"]
            if sid in seen:
                continue
            seen.add(sid)
            short = sid[:8]
            msgs = entry.get("messageCount", 0)
            label = get_harness_session_label(entry)[:40]
            active = " \u2190 active" if sid == state.get("default_session_id") else ""
            display = f"`{short}` {msgs} msgs{active}"
            if label:
                display += f"\n  _{label}_"
            lines.append(display)
            if len(lines) > 16:
                break

        if len(lines) == 1:
            await send_message(
                chat_id, f"No sessions in `{folder_name}` yet.", reply_to=msg_id
            )
            return

        lines.append(f"\n`/resume <id or name>` to switch.")
        await send_message(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd == "/resume":
        if not arg:
            await send_message(
                chat_id, "Usage: `/resume <session-id or name>`", reply_to=msg_id
            )
            return
        matches = find_session(arg)
        if len(matches) == 1:
            state["default_session_id"] = matches[0]
            state["folder_sessions"][state["active_folder"]] = matches[0]
            save_state()
            info = state["sessions"].get(matches[0], {})
            label = info.get("label", "")
            msgs = info.get("message_count", 0)
            if not label:
                cli_info = next(
                    (s for s in get_harness_sessions() if s["sessionId"] == matches[0]),
                    None,
                )
                if cli_info:
                    label = get_harness_session_label(cli_info)
                    msgs = cli_info.get("messageCount", 0)
            name = f" (_{label}_)" if label else ""
            await send_message(
                chat_id,
                f"Resumed `{matches[0][:8]}`{name} \u2014 {msgs} msgs",
                reply_to=msg_id,
            )
        elif len(matches) > 1:
            items = []
            for s in matches[:5]:
                info = state["sessions"].get(s, {})
                label = info.get("label", "")
                if not label:
                    cli_info = next(
                        (e for e in get_harness_sessions() if e["sessionId"] == s), None
                    )
                    if cli_info:
                        label = get_harness_session_label(cli_info)
                items.append(f"`{s[:8]}` _{label}_" if label else f"`{s[:8]}`")
            await send_message(
                chat_id,
                "Multiple matches:\n" + "\n".join(items) + "\n\nBe more specific.",
                reply_to=msg_id,
            )
        else:
            await send_message(chat_id, f"No session matching `{arg}`", reply_to=msg_id)
        return

    if cmd == "/status":
        inv = state.get("last_invocation")
        sid = state.get("default_session_id")
        folder = state["active_folder"]
        folder_name = get_folder_display_name(folder)
        n_sessions = len(state.get("sessions", {}))
        n_cli = len(get_harness_sessions())
        n_folders = len(state.get("folders", {}))
        saved_count = sum(1 for s in state["sessions"].values() if s.get("saved"))
        queue_size = _prompt_queue.qsize() if _prompt_queue else 0
        busy = (
            _active_harness_proc is not None and _active_harness_proc.returncode is None
        )
        lines = [
            "*Bridge Status*",
            f"Harness: `{HARNESS_LABEL}` (`{HARNESS_CLI}`)",
            f"Version: `{BRIDGE_VERSION}`",
            f"Build: `{BRIDGE_BUILD}`",
            f"Folder: `{folder_name}` (`{folder}`)",
            f"Folders: {n_folders} registered",
            f"Sessions: {n_sessions} bridge + {n_cli} external ({saved_count} saved)",
            f"Queue: {queue_size} pending | {'busy' if busy else 'idle'}",
            f"Timeout: {CLAUDE_TIMEOUT}s",
        ]
        if BRIDGE_MODEL:
            lines.append(f"Model override: `{BRIDGE_MODEL}`")
        if HARNESS_AGENT:
            lines.append(f"Agent profile: `{HARNESS_AGENT}`")
        if sid:
            lines.append(f"Active: `{sid[:8]}`")
            info = state["sessions"].get(sid, {})
            label = info.get("label", "")
            if not label:
                cli_info = next(
                    (s for s in get_harness_sessions() if s["sessionId"] == sid), None
                )
                if cli_info:
                    label = get_harness_session_label(cli_info)
            if label:
                lines.append(f"  _{label}_")
        else:
            lines.append("Active session: none (will auto-continue)")
        if inv:
            lines.append(
                f"Last: {inv.get('elapsed', '?')}s at {inv.get('time', '?')[:16]}"
            )
        await send_message(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd == "/model":
        if not arg:
            current = BRIDGE_MODEL or "default"
            lines = [
                f"*{HARNESS_LABEL} model override:* `{current}`",
                "",
            ]
            if HARNESS_CLI == "claude":
                lines.extend(
                    [
                        "*Common Claude models:*",
                        "`claude-opus-4-6` — Most capable",
                        "`claude-sonnet-4-5-20250929` — Fast + capable",
                        "`claude-haiku-4-5-20251001` — Fastest + cheapest",
                        "",
                        "Applied via `ANTHROPIC_MODEL`/`BRIDGE_MODEL` for the next run.",
                    ]
                )
            else:
                lines.append(f"Applied as `-m <model>` to `{HARNESS_CLI} run`.")
                if HARNESS_AGENT:
                    lines.append(f"Current agent profile: `{HARNESS_AGENT}`")
            lines.extend(
                [
                    "",
                    "Usage: `/model <model-id>`",
                    "Clear override: `/model off`",
                ]
            )
            await send_message(chat_id, "\n".join(lines), reply_to=msg_id)
            return
        model = arg.strip()
        if model.lower() in {"off", "default", "clear", "none"}:
            BRIDGE_MODEL = ""
            await send_message(
                chat_id, f"{HARNESS_LABEL} model override cleared.", reply_to=msg_id
            )
            return
        BRIDGE_MODEL = model
        applied_as = (
            "ANTHROPIC_MODEL/BRIDGE_MODEL"
            if HARNESS_CLI == "claude"
            else f"{HARNESS_CLI} run -m"
        )
        await send_message(
            chat_id,
            f"{HARNESS_LABEL} model set to `{model}` for the next run via `{applied_as}`.",
            reply_to=msg_id,
        )
        return

    if cmd == "/agent":
        if HARNESS_CLI not in {"opencode", "kilo"}:
            await send_message(
                chat_id,
                f"`/agent` is not supported for `{HARNESS_CLI}`.",
                reply_to=msg_id,
            )
            return
        if not arg:
            current = HARNESS_AGENT or "default"
            await send_message(
                chat_id,
                (
                    f"*{HARNESS_LABEL} agent profile:* `{current}`\n\n"
                    "Usage: `/agent <name>`\n"
                    "Clear override: `/agent off`"
                ),
                reply_to=msg_id,
            )
            return
        agent = arg.strip()
        if agent.lower() in {"off", "default", "clear", "none"}:
            HARNESS_AGENT = ""
            await send_message(
                chat_id, f"{HARNESS_LABEL} agent profile cleared.", reply_to=msg_id
            )
            return
        HARNESS_AGENT = agent
        await send_message(
            chat_id,
            f"{HARNESS_LABEL} agent profile set to `{agent}` for the next run.",
            reply_to=msg_id,
        )
        return

    if cmd == "/watchdog":
        tool = _watchdog_current_tool
        if not WATCHDOG_ENABLED:
            status_text = "Watchdog: *disabled*"
        elif tool:
            elapsed = time.time() - tool["started"]
            status_text = (
                f"Watchdog: *active — monitoring tool*\n"
                f"Current tool: `{tool['name']}`\n"
                f"Command: `{tool.get('command', 'N/A')[:80]}`\n"
                f"Running: {elapsed:.0f}s\n"
                f"Eval session: `{WATCHDOG_SESSION}`\n"
                f"Eval node: `{WATCHDOG_EVAL_NODE}`"
            )
        else:
            busy = (
                _active_harness_proc is not None
                and _active_harness_proc.returncode is None
            )
            queue_size = _prompt_queue.qsize() if _prompt_queue else 0
            worker_alive = (
                _queue_worker_task is not None and not _queue_worker_task.done()
            )
            status_text = (
                f"Watchdog: *{'monitoring' if busy else 'idle'}*\n"
                f"Bash timeout: {WATCHDOG_BASH_TIMEOUT}s\n"
                f"SSH timeout: {WATCHDOG_SSH_TIMEOUT}s\n"
                f"Task timeout: {WATCHDOG_TASK_TIMEOUT}s\n"
                f"Stagnation kill: {WATCHDOG_STAGNATION_KILL}s\n"
                f"Blocklist: {len(WATCHDOG_INSTANT_KILL_PATTERNS)} patterns\n"
                f"Queue: {queue_size} pending, worker={'alive' if worker_alive else 'DEAD'}\n"
                f"Eval session: `{WATCHDOG_SESSION}`\n"
                f"Eval node: `{WATCHDOG_EVAL_NODE}`"
            )
        await send_message(chat_id, status_text, reply_to=msg_id)
        return

    if cmd == "/compact":
        await send_message(
            chat_id,
            "Starting fresh session (compact not available in bridge mode).",
            reply_to=msg_id,
        )
        state["default_session_id"] = None
        state["folder_sessions"].pop(state["active_folder"], None)
        save_state()
        return

    if cmd == "/dispatch":
        await cmd_dispatch(chat_id, msg_id, arg)
        return

    if cmd == "/jobs":
        await cmd_jobs(chat_id, msg_id)
        return

    if cmd == "/job":
        await cmd_job_status(chat_id, msg_id, arg)
        return

    if cmd == "/job-kill":
        await cmd_job_kill(chat_id, msg_id, arg)
        return

    if cmd in ("/interrupt", "/stop"):
        await cmd_interrupt(chat_id, msg_id, arg)
        return

    if cmd == "/dashboard":
        await send_message(
            chat_id,
            (
                "Portfolio Dashboard:\n"
                "http://100.121.48.86:3100/stats\n\n"
                "Live P&L, backtest metrics, pair performance.\n"
                "Tailscale VPN required."
            ),
            reply_to=msg_id,
        )
        return

    # Unknown command — treat as prompt
    await enqueue_prompt(chat_id, msg_id, text)


async def cmd_interrupt(chat_id: int, msg_id: int, args: str):
    """Interrupt the currently running harness process."""
    if _active_harness_proc is None:
        await send_message(chat_id, "Nothing running to interrupt.", reply_to=msg_id)
        return

    await send_message(chat_id, "_Interrupting..._", reply_to=msg_id)
    try:
        _active_harness_proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        pass  # Already exited

    # If user provided follow-up text, queue it as the next prompt
    follow_up = args.strip()
    if follow_up:
        await enqueue_prompt(chat_id, msg_id, follow_up)
        log.info(f"Interrupt with follow-up: {follow_up[:60]}")
    else:
        log.info("Interrupt requested (no follow-up)")


async def enqueue_prompt(
    chat_id: int, msg_id: int, text: str, a2a_reply_target: str | None = None
):
    """Add a prompt to the queue. Notifies user of queue position if not first."""
    global _prompt_queue
    if _prompt_queue is None:
        _prompt_queue = asyncio.Queue()

    queue_size = _prompt_queue.qsize()
    await _prompt_queue.put((chat_id, msg_id, text, a2a_reply_target))

    if queue_size > 0:
        await send_message(
            chat_id,
            f"Queued (position {queue_size + 1}). Working on previous task...",
            reply_to=msg_id,
        )
        log.info(f"Queued message (position {queue_size + 1}): {text[:60]}")
    else:
        log.info(f"Processing immediately: {text[:60]}")


async def queue_worker():
    """Process prompts sequentially from the queue."""
    global _prompt_queue, _queue_last_dequeue
    if _prompt_queue is None:
        _prompt_queue = asyncio.Queue()

    log.info("Queue worker started")
    while not _shutting_down:
        try:
            chat_id, msg_id, text, a2a_reply_target = await asyncio.wait_for(
                _prompt_queue.get(), timeout=5
            )
        except asyncio.TimeoutError:
            continue
        except Exception:
            continue

        _queue_last_dequeue = time.time()
        try:
            await _process_prompt(chat_id, msg_id, text, a2a_reply_target)
        except Exception as e:
            log.error(f"Queue worker error: {e}")
            await send_message(
                chat_id, f"Error processing message: {e}", reply_to=msg_id
            )
        finally:
            _prompt_queue.task_done()

    log.info("Queue worker stopped")


async def _queue_health_monitor():
    """Detect and recover from dead queue worker.

    Checks every 30s. If the queue has pending items but no dequeue has happened
    for 120s AND there's no active Claude process, the worker is presumed dead.
    Restarts the worker and notifies the user.
    """
    global _queue_worker_task
    STALL_THRESHOLD = 120  # seconds with no dequeue while queue has items

    while not _shutting_down:
        await asyncio.sleep(30)

        if _prompt_queue is None or _prompt_queue.qsize() == 0:
            continue

        # Queue has items — check if worker is alive and processing
        worker_alive = _queue_worker_task is not None and not _queue_worker_task.done()
        claude_active = (
            _active_harness_proc is not None and _active_harness_proc.returncode is None
        )

        # If Claude is actively running, worker is just waiting for it — not stalled
        if claude_active:
            continue

        # Check stall: queue has items, no active Claude, no recent dequeue
        time_since_dequeue = (
            time.time() - _queue_last_dequeue
            if _queue_last_dequeue > 0
            else float("inf")
        )

        if time_since_dequeue < STALL_THRESHOLD:
            continue

        # Stall detected
        pending = _prompt_queue.qsize()
        log.error(
            f"Queue health: STALL DETECTED — {pending} items pending, "
            f"no dequeue for {time_since_dequeue:.0f}s, worker_alive={worker_alive}"
        )

        # Try to notify the user
        chat_id = _active_harness_chat_id or int(
            os.environ.get("ALLOWED_USER_IDS", "0").split(",")[0]
        )
        if chat_id:
            await send_message(
                chat_id,
                f"Queue stall detected ({pending} messages waiting, "
                f"no activity for {time_since_dequeue / 60:.0f}min). Restarting worker...",
            )

        # Restart the worker
        if _queue_worker_task and not _queue_worker_task.done():
            _queue_worker_task.cancel()
            try:
                await _queue_worker_task
            except asyncio.CancelledError:
                pass

        _queue_worker_task = asyncio.create_task(queue_worker())
        log.info("Queue health: worker restarted")


async def _process_prompt(
    chat_id: int, msg_id: int, text: str, a2a_reply_target: str | None = None
):
    """Send prompt to the harness and respond. Called sequentially by queue worker."""
    global _active_harness_chat_id, _watchdog_current_tool, _watchdog_last_progress
    _active_harness_chat_id = chat_id
    _watchdog_current_tool = None
    _watchdog_last_progress = time.time()

    await send_typing(chat_id)
    typing_task = asyncio.create_task(typing_loop(chat_id))

    # Start watchdog if enabled
    watchdog_task = None
    if WATCHDOG_ENABLED:
        watchdog_task = asyncio.create_task(
            _watchdog_monitor(chat_id, state["active_folder"])
        )

    try:
        start = time.time()
        if a2a_reply_target and A2A_PROGRESS_MODE in {"status", "structured"}:
            task_id = _extract_a2a_task_id(text)
            await send_plain_message(
                chat_id,
                _a2a_status_envelope(
                    a2a_reply_target,
                    task_id,
                    "Accepted. Working silently; final response will be a structured A2A result.",
                ),
                reply_to=msg_id,
            )
        response, session_id = await run_harness(
            text,
            chat_id,
            suppress_progress_messages=bool(a2a_reply_target),
            suppress_footer=bool(a2a_reply_target),
        )
        elapsed = time.time() - start

        if session_id:
            pending = state.pop("_pending_label", None)
            label = pending or (
                text[:50] if session_id != state.get("default_session_id") else ""
            )
            record_session(session_id, label=label)
            state["default_session_id"] = session_id
            save_state()

        state["last_invocation"] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "elapsed": round(elapsed, 1),
            "status": "ok",
            "session_id": session_id,
            "folder": state["active_folder"],
        }
        save_state()

        pending_count = _prompt_queue.qsize() if _prompt_queue else 0
        queue_note = f", {pending_count} queued" if pending_count > 0 else ""
        if a2a_reply_target:
            ok, reason = _validate_handoff_envelope(response, a2a_reply_target)
            if not ok:
                log.warning(
                    f"Rejected invalid A2A response to @{a2a_reply_target}: {reason}"
                )
                # Silently drop invalid A2A responses instead of sending
                # full rejection guidance to group chat (avoids guidance cascades).
                await send_message(
                    chat_id,
                    f"_A2A response invalid: {reason[:60]}..._",
                    reply_to=msg_id,
                )
                return
        log.info(
            f"Response ({elapsed:.1f}s, {len(response)} chars, session={session_id}{queue_note})"
        )
        if a2a_reply_target:
            await send_plain_message(chat_id, response, reply_to=msg_id)
        else:
            await send_message(chat_id, response, reply_to=msg_id)

    finally:
        _active_harness_chat_id = None
        _watchdog_current_tool = None
        typing_task.cancel()
        if watchdog_task:
            watchdog_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        if watchdog_task:
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
        # Clean up old media files
        try:
            await cleanup_old_media()
        except Exception:
            pass


async def typing_loop(chat_id: int):
    """Keep sending typing indicator every 4s."""
    try:
        while True:
            await send_typing(chat_id)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# --- Long-Polling Loop ---


async def poll_loop():
    """Main Telegram long-polling loop."""
    global BOT_USERNAME, BOT_ID
    offset = 0
    log.info("Starting Telegram long-polling...")

    result = await tg_api("deleteWebhook")
    log.info(f"deleteWebhook: {result}")

    me = await tg_api("getMe")
    if me and me.get("ok"):
        bot = me["result"]
        BOT_USERNAME = bot.get("username") or ""
        BOT_ID = bot.get("id")
        log.info(f"Bot: @{bot.get('username')} ({bot.get('first_name')})")

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
                log.error(f"getUpdates error: {data}")
                await asyncio.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue

                from_user = message.get("from") or {}
                user_id = from_user.get("id")
                chat_id = (message.get("chat") or {}).get("id")
                text = message.get("text", "")
                caption = message.get("caption", "")
                msg_id = message.get("message_id")
                raw_text = text or caption or ""

                if not chat_id:
                    continue

                should_process, text, caption, auto_reply = (
                    should_process_group_message(message, text, caption)
                )
                if auto_reply:
                    asyncio.create_task(
                        send_message(chat_id, auto_reply, reply_to=msg_id)
                    )
                if not should_process:
                    continue

                # Check for media (photos, documents, voice, etc.)
                has_media = any(
                    k in message
                    for k in (
                        "photo",
                        "document",
                        "voice",
                        "video_note",
                        "video",
                        "sticker",
                    )
                )

                if not text and not caption and not has_media:
                    continue

                log.info(
                    f"Message from {user_id}: {(text or caption or '[media]')[:80]}"
                )

                if text and text.startswith("/"):
                    asyncio.create_task(handle_command(chat_id, msg_id, text))
                elif has_media:
                    asyncio.create_task(
                        handle_media_message(
                            chat_id, msg_id, message, caption or text or ""
                        )
                    )
                else:
                    a2a_reply_target = None
                    if from_user.get("is_bot") and raw_text.lower().startswith(
                        "/handoff@"
                    ):
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
            log.error(f"Poll loop error: {e}")
            await asyncio.sleep(5)

    log.info("Poll loop stopped (shutting down)")


# --- Orphan Recovery ---


async def recover_orphaned_harness():
    """On startup, check for orphaned harness processes and notify user."""
    if HARNESS_CLI != "claude":
        return
    try:
        # Find any running claude --print processes
        proc = await asyncio.create_subprocess_exec(
            "pgrep",
            "-af",
            "claude.*--print",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().strip().split("\n") if stdout.decode().strip() else []

        orphans = []
        for line in lines:
            parts = line.split(maxsplit=1)
            if len(parts) >= 2:
                pid = parts[0]
                cmd = parts[1]
                # Skip our own pgrep
                if "pgrep" in cmd:
                    continue
                orphans.append((pid, cmd))

        if orphans:
            log.info(f"Found {len(orphans)} orphaned {HARNESS_LABEL} process(es)")
            # Wait for orphans to finish (they're already running)
            for pid, cmd in orphans:
                log.info(f"Waiting for orphan PID {pid}: {cmd[:80]}")
                try:
                    wait_proc = await asyncio.create_subprocess_exec(
                        "tail",
                        "--pid",
                        pid,
                        "-f",
                        "/dev/null",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(
                        wait_proc.communicate(), timeout=CLAUDE_TIMEOUT
                    )
                    log.info(f"Orphan PID {pid} finished")
                except asyncio.TimeoutError:
                    log.warning(f"Orphan PID {pid} still running after timeout")
                except Exception as e:
                    log.warning(f"Error waiting for orphan PID {pid}: {e}")

            # Notify user that bridge restarted with orphan info
            chat_id = next(iter(ALLOWED_USERS))
            await send_message(
                chat_id,
                (
                    "Bridge restarted. "
                    f"Found {len(orphans)} orphaned {HARNESS_LABEL} process(es) from before restart — "
                    "they finished running. Use `/sessions` to see latest state, "
                    "or `/resume` to continue where you left off."
                ),
            )
        else:
            # No orphans, but check if last invocation looks incomplete
            last = state.get("last_invocation")
            if last and last.get("session_id") is None and last.get("status") == "ok":
                chat_id = next(iter(ALLOWED_USERS))
                await send_message(
                    chat_id,
                    (
                        f"Bridge restarted. Last {HARNESS_LABEL} call may not have been delivered. "
                        "Use `--continue` or `/resume` to pick up where you left off."
                    ),
                )

    except Exception as e:
        log.warning(f"Orphan recovery check failed: {e}")


# --- FastAPI (health only) ---

from fastapi import FastAPI

app = FastAPI(title=f"{HARNESS_LABEL}-Telegram Bridge", version=BRIDGE_VERSION)


@app.on_event("startup")
async def startup():
    load_state()
    _load_jobs()
    os.makedirs(JOBS_OUTPUT_DIR, exist_ok=True)
    # Auto-discover folders with CLAUDE.md or .git
    for d in Path.home().iterdir():
        if d.is_dir() and not d.name.startswith("."):
            if (d / "CLAUDE.md").exists() or (d / ".git").exists():
                name = d.name
                if name not in state["folders"]:
                    state["folders"][name] = str(d)
    # Check ~/.openclaw/workspace subdirectories too
    clawd = Path.home() / ".openclaw" / "workspace"
    if clawd.exists():
        for d in clawd.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                if (d / "CLAUDE.md").exists() or (d / ".git").exists():
                    name = d.name
                    if name not in state["folders"]:
                        state["folders"][name] = str(d)
    # Check for orphaned harness processes from a previous crash
    asyncio.create_task(recover_orphaned_harness())

    save_state()

    # Start queue worker, health monitor, and poll loop
    global _prompt_queue, _queue_worker_task, _queue_health_task
    _prompt_queue = asyncio.Queue()
    _queue_worker_task = asyncio.create_task(queue_worker())
    _queue_health_task = asyncio.create_task(_queue_health_monitor())
    asyncio.create_task(poll_loop())


@app.on_event("shutdown")
async def shutdown():
    global _shutting_down
    _shutting_down = True
    log.info("Shutdown signal received")

    # If Claude is running, wait for it to finish instead of killing it
    if _active_harness_proc and _active_harness_proc.returncode is None:
        log.info(f"Waiting for active {HARNESS_LABEL} process to finish...")
        if _active_harness_chat_id:
            await send_message(
                _active_harness_chat_id,
                f"_Bridge restarting — waiting for {HARNESS_LABEL} to finish..._",
            )
        try:
            await asyncio.wait_for(_active_harness_proc.wait(), timeout=CLAUDE_TIMEOUT)
            log.info(f"{HARNESS_LABEL} process finished before shutdown")
        except asyncio.TimeoutError:
            log.warning(
                f"{HARNESS_LABEL} still running at shutdown timeout, terminating"
            )
            _active_harness_proc.terminate()

    save_state()
    log.info("Shutdown complete")


@app.get("/health")
async def health():
    sid = state.get("default_session_id")
    return {
        "status": "ok",
        "service": HARNESS_SERVICE_NAME,
        "harness_cli": HARNESS_CLI,
        "harness_label": HARNESS_LABEL,
        "version": BRIDGE_VERSION,
        "build": BRIDGE_BUILD,
        "mode": "long-polling",
        "active_folder": state.get("active_folder"),
        "folder_count": len(state.get("folders", {})),
        "default_session": sid[:8] if sid else None,
        "session_count": len(state.get("sessions", {})),
        "cli_session_count": len(get_harness_sessions()),
        "model_override": BRIDGE_MODEL or None,
        "agent_profile": HARNESS_AGENT or None,
        "last_invocation": state.get("last_invocation"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
