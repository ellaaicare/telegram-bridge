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
import json
import logging
import os
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI


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
    return {value for value in values if value}


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
    prefixes = [f"/handoff@{value}" for value in sorted(values) if value]
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

BRIDGE_VERSION = os.environ.get("CODEX_BRIDGE_VERSION", "0.2.0")
BRIDGE_BUILD = os.environ.get("CODEX_BRIDGE_BUILD", "a2a-quiet-status-pr685.7681cf5")
BOT_TOKEN = os.environ.get("CODEX_TELEGRAM_BOT_TOKEN", "")
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
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "900"))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "")
CODEX_SANDBOX = os.environ.get("CODEX_SANDBOX", "workspace-write")
CODEX_SKIP_GIT_REPO_CHECK = os.environ.get("CODEX_SKIP_GIT_REPO_CHECK", "true").lower() == "true"
CODEX_FULL_AUTO = os.environ.get("CODEX_FULL_AUTO", "false").lower() == "true"
CODEX_DANGEROUS_BYPASS = os.environ.get("CODEX_DANGEROUS_BYPASS", "false").lower() == "true"
CODEX_EXTRA_DIRS = [d for d in os.environ.get("CODEX_ADD_DIRS", "").split(":") if d]
CODEX_BRIDGE_PORT = int(os.environ.get("CODEX_BRIDGE_PORT", "8110"))
TELEGRAM_MAX_LENGTH = 4096
A2A_GUIDANCE_COOLDOWN_SECONDS = int(os.environ.get("A2A_GUIDANCE_COOLDOWN_SECONDS", "300"))
A2A_PROGRESS_MODE = os.environ.get("A2A_PROGRESS_MODE", "status").strip().lower()
A2A_IGNORED = "__a2a_ignored__"
POLL_TIMEOUT = 60
STATE_FILE = Path(
    os.environ.get(
        "CODEX_BRIDGE_STATE_DIR",
        str(Path.home() / "codexd" / "services" / "codex-telegram-bridge"),
    )
) / "state.json"
HOME = os.environ.get("CODEX_DEFAULT_FOLDER", str(Path.home()))
MEDIA_DIR = Path("/tmp/tg-codex-bridge-media")


# --- Watchdog Configuration ---

WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "true").lower() == "true"
WATCHDOG_COMMAND_TIMEOUT = int(os.environ.get("WATCHDOG_COMMAND_TIMEOUT", "900"))
WATCHDOG_DEFAULT_TIMEOUT = int(os.environ.get("WATCHDOG_DEFAULT_TIMEOUT", "180"))
WATCHDOG_STAGNATION_KILL = int(os.environ.get("WATCHDOG_STAGNATION_KILL", "1800"))
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
        return False, ""

    payload_text = raw_stripped[len(matched_prefix):].strip()
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


def _handoff_command_target(raw: str) -> str:
    stripped = raw.strip()
    if not stripped.lower().startswith("/handoff@"):
        return ""
    command = stripped.split(maxsplit=1)[0]
    return command[len("/handoff@"):].strip()


def _handoff_target_matches(target: str, target_username: str) -> bool:
    if not target or not target_username:
        return False
    target_key = _norm_bot_key(target)
    return target_key in {
        _norm_bot_key(prefix[len("/handoff@"):])
        for prefix in _handoff_prefixes_for_target(target_username)
    }


def _is_handoff_for_other_bot(raw: str) -> bool:
    target = _handoff_command_target(raw)
    return bool(target and not _handoff_target_matches(target, BOT_USERNAME))


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

    json_text = stripped[len(matched_prefix):].strip()
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
    import re

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
    return f"/handoff@{target} {json.dumps(payload, separators=(',', ':'))}"


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
        raw[:120] if raw else "[media]",
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
            if user_id not in ALLOWED_BOT_IDS:
                log.warning("Rejected group bot sender %s (%s)", user_id, username)
                return False, text, caption, None
            ok, prompt = _parse_handoff(raw)
            if not ok and prompt == A2A_IGNORED:
                return False, text, caption, None
            if not ok and raw and _is_handoff_for_other_bot(raw):
                log.info("Ignored A2A handoff for another bot from sender %s (%s)", user_id, username)
                return False, text, caption, None
            if not ok and raw and _is_a2a_guidance(raw):
                log.info("Ignored peer A2A syntax guidance from bot sender %s (%s)", user_id, username)
                return False, text, caption, None
            if not ok and raw:
                if _should_send_a2a_guidance(chat_id, user_id):
                    log.info("Sending A2A syntax guidance to bot sender %s (%s)", user_id, username)
                    return False, text, caption, _a2a_guidance_message()
                log.info("Suppressed repeated A2A syntax guidance to bot sender %s (%s)", user_id, username)
                return False, text, caption, None
            return (ok, prompt if text else text, prompt if caption else caption, None)

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

_active_codex_proc: asyncio.subprocess.Process | None = None
_active_codex_chat_id: int | None = None
_shutting_down = False

_watchdog_current_item: dict | None = None
_watchdog_last_progress: float = 0.0
_a2a_guidance_last_sent: dict[str, float] = {}

_prompt_queue: asyncio.Queue | None = None
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
        prompt_parts.append(
            f"The user sent a {media_type}. A local copy is available in the workspace if needed."
        )

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
) -> list[str]:
    images = images or []
    if session_id:
        cmd = ["codex", "exec", "resume", "--json"]
        if CODEX_SKIP_GIT_REPO_CHECK:
            cmd.append("--skip-git-repo-check")
        if CODEX_MODEL:
            cmd.extend(["-m", CODEX_MODEL])
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
    if CODEX_MODEL:
        cmd.extend(["-m", CODEX_MODEL])
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
    return f"Running: `{command[:80]}`"


async def run_codex(
    prompt: str,
    chat_id: int,
    cwd: str,
    session_id: str | None = None,
    images: list[str] | None = None,
    suppress_progress_messages: bool = False,
    suppress_footer: bool = False,
) -> tuple[str, str | None]:
    global _active_codex_proc, _watchdog_current_item, _watchdog_last_progress

    cmd = build_codex_command(prompt, cwd=cwd, session_id=session_id, images=images)
    log.info("Codex in %s: %s | prompt: %s", cwd, " ".join(cmd[:8]), prompt[:80])

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

    try:
        async for raw_line in proc.stdout:
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

        await proc.wait()
        _active_codex_proc = None

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
        _active_codex_proc = None
        log.error("Codex stream error: %s", e)
        return f"Error: {e}", result_session_id


# --- Watchdog ---


def _is_instant_kill_command(command: str) -> bool:
    cmd_lower = command.lower().strip()
    for pattern in WATCHDOG_INSTANT_KILL_PATTERNS:
        if pattern.lower() in cmd_lower:
            return True
    if "ssh " in cmd_lower and any(
        p in cmd_lower for p in ["tail -f", "tail --follow", "journalctl -f"]
    ):
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
                f"Known infinite command: `{command[:60]}`",
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

    detail = f"\nCommand: `{command[:100]}`" if command else ""
    await send_message(
        chat_id,
        f"*Watchdog killed stuck process*\nReason: {reason}{detail}\n\n"
        "_Session preserved — send a new message to continue._",
    )


# --- Command Handlers ---


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
                "`/new [label]` — Start a fresh Codex thread\n"
                "`/save [label]` — Bookmark current thread\n"
                "`/rename <label>` — Rename current thread\n"
                "`/sessions` — List known threads for this folder\n"
                "`/resume <id|name>` — Resume a saved thread\n"
                "`/interrupt [msg]` — Stop the running Codex task\n\n"
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
            path = (
                os.path.expanduser(sub_parts[2])
                if len(sub_parts) >= 3
                else os.path.join(HOME, "projects", name)
            )
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
        name = clone_parts[1].strip() if len(clone_parts) >= 2 else repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
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

    if cmd == "/new":
        label = arg if arg else None
        previous_context = _pending_thread_contexts.get(state["active_folder"])
        if previous_context:
            _resolved_thread_contexts.pop(previous_context["id"], None)
        state["default_session_id"] = None
        state["folder_sessions"].pop(state["active_folder"], None)
        _pending_thread_contexts[state["active_folder"]] = {
            "id": uuid.uuid4().hex,
            "label": label,
        }
        state.pop("_pending_label", None)
        save_state()
        await send_message(chat_id, "Fresh Codex thread for this folder.", reply_to=msg_id)
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
        items = [
            (sid, info)
            for sid, info in state["sessions"].items()
            if info.get("folder", HOME) == folder
        ]
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
            await send_message(chat_id, "Usage: `/resume <id|name>`", reply_to=msg_id)
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
        busy = _active_codex_proc is not None and _active_codex_proc.returncode is None
        lines = [
            "*Bridge Status*",
            f"Version: `{BRIDGE_VERSION}`",
            f"Build: `{BRIDGE_BUILD}`",
            f"Folder: `{get_folder_display_name(state['active_folder'])}` (`{state['active_folder']}`)",
            f"Folders: {len(state.get('folders', {}))} registered",
            f"Threads: {len(state.get('sessions', {}))} tracked",
            f"Queue: {queue_size} pending | {'busy' if busy else 'idle'}",
            f"Model: `{CODEX_MODEL or 'default'}`",
            f"Sandbox: `{CODEX_SANDBOX}`",
        ]
        if sid:
            lines.append(f"Active: `{sid[:8]}`")
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
                f"Watchdog: *active*\n"
                f"Command: `{item.get('command', '')[:120]}`\n"
                f"Running: {elapsed:.0f}s"
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

    if cmd in ("/interrupt", "/stop"):
        await cmd_interrupt(chat_id, msg_id, arg)
        return

    await enqueue_prompt(chat_id, msg_id, text)


async def cmd_interrupt(chat_id: int, msg_id: int, args: str):
    if _active_codex_proc is None:
        await send_message(chat_id, "Nothing running to interrupt.", reply_to=msg_id)
        return
    await send_message(chat_id, "_Interrupting..._", reply_to=msg_id)
    try:
        _active_codex_proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        pass
    follow_up = args.strip()
    if follow_up:
        await enqueue_prompt(chat_id, msg_id, follow_up)


async def enqueue_prompt(
    chat_id: int,
    msg_id: int,
    text: str,
    images: list[str] | None = None,
    a2a_reply_target: str | None = None,
):
    global _prompt_queue
    if _prompt_queue is None:
        _prompt_queue = asyncio.Queue()
    folder = state["active_folder"]
    session_id = state.get("default_session_id")
    context_id = None
    pending_label = None
    if session_id is None:
        context = get_or_create_pending_thread_context(folder)
        context_id = context["id"]
        pending_label = context.get("label")
        if pending_label:
            context["label"] = None
    queue_size = _prompt_queue.qsize()
    await _prompt_queue.put(
        {
            "chat_id": chat_id,
            "msg_id": msg_id,
            "text": text,
            "images": images or [],
            "folder": folder,
            "session_id": session_id,
            "context_id": context_id,
            "pending_label": pending_label,
            "a2a_reply_target": a2a_reply_target,
        }
    )
    if queue_size > 0:
        await send_message(
            chat_id,
            f"Queued (position {queue_size + 1}). Working on previous task...",
            reply_to=msg_id,
        )
    else:
        log.info("Processing immediately: %s", text[:60])


async def queue_worker():
    global _prompt_queue, _queue_last_dequeue
    if _prompt_queue is None:
        _prompt_queue = asyncio.Queue()
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
        except Exception as e:
            log.error("Queue worker error: %s", e)
            chat_id = item["chat_id"]
            msg_id = item["msg_id"]
            await send_message(chat_id, f"Error processing message: {e}", reply_to=msg_id)
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
        time_since_dequeue = (
            time.time() - _queue_last_dequeue if _queue_last_dequeue > 0 else float("inf")
        )
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

    _active_codex_chat_id = chat_id
    _watchdog_current_item = None
    _watchdog_last_progress = time.time()

    await send_typing(chat_id)
    typing_task = asyncio.create_task(typing_loop(chat_id))
    watchdog_task = None
    if WATCHDOG_ENABLED:
        watchdog_task = asyncio.create_task(_watchdog_monitor(chat_id))

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
        effective_session_id = session_id
        if effective_session_id is None and context_id:
            effective_session_id = _resolved_thread_contexts.get(context_id)

        response, result_session_id = await run_codex(
            text,
            chat_id,
            cwd=folder,
            session_id=effective_session_id,
            images=images,
            suppress_progress_messages=bool(a2a_reply_target),
            suppress_footer=bool(a2a_reply_target),
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
        if a2a_reply_target:
            ok, reason = _validate_handoff_envelope(response, a2a_reply_target)
            if not ok:
                log.warning("Rejected invalid A2A response to @%s: %s", a2a_reply_target, reason)
                response = _a2a_response_rejection(a2a_reply_target, reason)
        if a2a_reply_target:
            await send_plain_message(chat_id, response, reply_to=msg_id)
        else:
            await send_message(chat_id, response, reply_to=msg_id)
    finally:
        _active_codex_chat_id = None
        _watchdog_current_item = None
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

                has_media = any(
                    k in message for k in ("photo", "document", "voice", "video_note", "video", "sticker")
                )
                if not text and not caption and not has_media:
                    continue

                log.info("Message from %s: %s", user_id, (text or caption or "[media]")[:80])

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
    _prompt_queue = asyncio.Queue()
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
        "last_invocation": state.get("last_invocation"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
