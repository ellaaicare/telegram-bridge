import asyncio
import importlib.util
import os
import sys
from pathlib import Path


def load_bridge_module():
    repo_root = Path(__file__).resolve().parents[2]
    bridge_path = repo_root / "services" / "codex-telegram-bridge" / "main.py"
    os.environ.setdefault("CODEX_BRIDGE_LOG_FILE", "/tmp/codex-telegram-bridge-test.log")
    os.environ["TELEGRAM_CANONICAL_UID"] = "omi-user-1"
    os.environ["TELEGRAM_CANONICAL_IDENTITY"] = "plato"
    spec = importlib.util.spec_from_file_location("codex_telegram_bridge_sink", bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_inbound_human_event_uses_telegram_contract():
    bridge = load_bridge_module()
    message = {
        "message_id": 55,
        "date": 1777315200,
        "chat": {"id": -100123, "type": "supergroup"},
        "from": {"id": 9001, "username": "greg", "is_bot": False},
        "text": "hello @ExampleCodexBot",
    }

    event = bridge._telegram_event_payload(
        direction="inbound_human",
        text=message["text"],
        chat_id=-100123,
        message_id=55,
        from_id=9001,
        update_id=777,
        message=message,
        metadata={"from_username": "greg"},
    )

    assert event["uid"] == "omi-user-1"
    assert event["canonical_identity"] == "plato"
    assert event["channel"] == "telegram"
    assert event["provider"] == "telegram-bridge"
    assert event["role"] == "user"
    assert event["scan_policy"] == "immediate"
    assert event["event_id"] == "telegram:inbound_human:777:-100123:55:9001:0"
    assert event["source_ref"]["source_identity"] == "telegram:-100123:55:9001"
    assert event["text"] == "hello @ExampleCodexBot"


def test_outbound_bot_event_is_assistant_and_not_scanned():
    bridge = load_bridge_module()
    bridge.BOT_ID = 42
    bridge.BOT_USERNAME = "ExampleCodexBot"
    sent = {
        "message_id": 56,
        "date": 1777315201,
        "chat": {"id": -100123, "type": "supergroup"},
    }

    event = bridge._telegram_event_payload(
        direction="outbound_bot",
        text="assistant reply",
        chat_id=-100123,
        message_id=56,
        from_id=bridge.BOT_ID,
        message=sent,
    )

    assert event["role"] == "assistant"
    assert event["scan_policy"] == "none"
    assert event["event_id"] == "telegram:outbound_bot:sent:-100123:56:42:0"
    assert event["source_ref"]["source_identity"] == "telegram:-100123:56:42"
    assert event["text"] == "assistant reply"


def test_post_canonical_event_uses_configured_endpoint():
    bridge = load_bridge_module()
    calls = []

    class Response:
        status_code = 200
        text = "ok"

    class Client:
        async def post(self, url, json=None, timeout=None):
            calls.append({"url": url, "json": json, "timeout": timeout})
            return Response()

    async def fake_get_client():
        return Client()

    bridge.get_client = fake_get_client
    event = {"event_id": "event-1", "text": "raw"}

    asyncio.run(bridge.post_canonical_event(event))

    assert calls == [
        {
            "url": "https://api.ella-ai-care.com/v1/ella/events",
            "json": {"events": [event]},
            "timeout": 10.0,
        }
    ]
