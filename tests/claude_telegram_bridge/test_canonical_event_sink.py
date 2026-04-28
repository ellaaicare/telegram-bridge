import importlib.util
import os
import sys
from pathlib import Path


def load_bridge_module():
    repo_root = Path(__file__).resolve().parents[2]
    bridge_path = repo_root / "services" / "claude-telegram-bridge" / "main.py"
    os.environ.setdefault("BRIDGE_STATE_DIR", "/tmp/claude-telegram-bridge-test-state")
    os.environ["TELEGRAM_CANONICAL_UID"] = "omi-user-1"
    os.environ["TELEGRAM_CANONICAL_IDENTITY"] = "plato"
    spec = importlib.util.spec_from_file_location("claude_telegram_bridge_sink", bridge_path)
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
        "text": "hello @ExampleClaudeBot",
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
    assert event["source_ref"]["bridge"] == "claude-telegram-bridge"
    assert event["text"] == "hello @ExampleClaudeBot"


def test_outbound_bot_event_is_assistant_and_not_scanned():
    bridge = load_bridge_module()
    bridge.BOT_ID = 43
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
    assert event["event_id"] == "telegram:outbound_bot:sent:-100123:56:43:0"
    assert event["source_ref"]["source_identity"] == "telegram:-100123:56:43"
    assert event["text"] == "assistant reply"
