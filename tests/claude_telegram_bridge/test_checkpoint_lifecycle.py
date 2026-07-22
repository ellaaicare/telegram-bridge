import asyncio
import importlib.util
import os
import sys
import uuid
from pathlib import Path


def load_bridge_module(tmp_path):
    root = Path(__file__).resolve().parents[2]
    bridge_path = root / "services" / "claude-telegram-bridge" / "main.py"
    os.environ["BRIDGE_LOG_FILE"] = str(tmp_path / "claude.log")
    os.environ["BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    name = f"claude_checkpoint_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def configure(bridge, tmp_path):
    bridge.state = {
        "active_folder": str(tmp_path),
        "default_session_id": "session-1",
        "folders": {"repo": str(tmp_path)},
        "folder_sessions": {str(tmp_path): "session-1"},
        "sessions": {},
        "checkpoint_rotations": {},
        "pending_checkpoint_bootstrap": {},
        "last_checkpoints": {},
    }
    bridge._prompt_queue = asyncio.Queue()
    sent = []

    async def fake_send(chat_id, text, reply_to=None):
        sent.append(text)

    bridge.send_message = fake_send
    return sent


def test_guarded_new_queues_checkpoint_without_clearing_session(tmp_path):
    bridge = load_bridge_module(tmp_path)
    sent = configure(bridge, tmp_path)

    asyncio.run(bridge.handle_command(1, 2, "/new next task"))

    assert bridge.state["default_session_id"] == "session-1"
    assert bridge._prompt_queue.qsize() == 1
    assert bridge.state["checkpoint_rotations"][str(tmp_path)]["label"] == "next task"
    assert any("guarded rotation" in message.lower() for message in sent)


def test_force_new_is_explicit_checkpoint_bypass(tmp_path):
    bridge = load_bridge_module(tmp_path)
    sent = configure(bridge, tmp_path)

    asyncio.run(bridge.handle_command(1, 2, "/new force emergency"))

    assert bridge.state["default_session_id"] is None
    assert str(tmp_path) not in bridge.state["folder_sessions"]
    assert bridge._prompt_queue.qsize() == 0
    assert any("bypassed" in message.lower() for message in sent)


def test_force_compact_is_explicit_checkpoint_bypass(tmp_path):
    bridge = load_bridge_module(tmp_path)
    sent = configure(bridge, tmp_path)

    asyncio.run(bridge.handle_command(1, 2, "/compact force"))

    assert bridge.state["default_session_id"] is None
    assert bridge._prompt_queue.qsize() == 0
    assert any("bypassed" in message.lower() for message in sent)


def test_queued_prompt_keeps_its_project_and_does_not_inherit_other_folder_session(tmp_path):
    bridge = load_bridge_module(tmp_path)
    configure(bridge, tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    bridge.state["active_folder"] = str(other)
    bridge.state["default_session_id"] = "other-session"
    bridge.state["folders"]["other"] = str(other)
    bridge.state["folder_sessions"] = {str(other): "other-session"}
    bridge.WATCHDOG_ENABLED = False
    observed = {}

    async def fake_run(prompt, chat_id, **kwargs):
        observed.update(kwargs)
        return "done", "target-session"

    async def fake_send_typing(chat_id):
        return None

    async def fake_typing_loop(chat_id):
        await asyncio.sleep(60)

    bridge.run_harness = fake_run
    bridge.send_typing = fake_send_typing
    bridge.typing_loop = fake_typing_loop
    item = {
        "kind": "prompt",
        "chat_id": 1,
        "msg_id": 2,
        "text": "Continue target work.",
        "a2a_reply_target": None,
        "folder": str(tmp_path),
        "session_id": None,
        "pending_label": None,
        "deferred_checkpoint_rotation": None,
    }

    asyncio.run(bridge._process_prompt(item))

    assert observed["cwd"] == str(tmp_path)
    assert observed["session_id"] is None
    assert observed["use_default_session"] is False
    assert bridge.state["default_session_id"] == "other-session"
    assert bridge.state["folder_sessions"][str(tmp_path)] == "target-session"
