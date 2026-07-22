import asyncio
import importlib.util
import os
import sys
import uuid
from pathlib import Path


def load_bridge_module(tmp_path):
    root = Path(__file__).resolve().parents[2]
    bridge_path = root / "services" / "codex-telegram-bridge" / "main.py"
    os.environ["CODEX_BRIDGE_LOG_FILE"] = str(tmp_path / "codex.log")
    os.environ["CODEX_BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    name = f"codex_checkpoint_test_{uuid.uuid4().hex}"
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
    bridge._prompt_queue = bridge.PromptQueue()
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


def test_clearqueue_cancels_guarded_rotation_and_allows_another(tmp_path):
    bridge = load_bridge_module(tmp_path)
    configure(bridge, tmp_path)

    async def exercise():
        await bridge.handle_command(1, 2, "/new first")
        await bridge.cmd_clear_queue(1, 3)
        await bridge.handle_command(1, 4, "/new second")

    asyncio.run(exercise())

    rotation = bridge.state["checkpoint_rotations"][str(tmp_path)]
    assert rotation["label"] == "second"
    assert bridge._prompt_queue.qsize() == 1


def test_stop_cancels_guarded_rotation_and_allows_another(tmp_path):
    bridge = load_bridge_module(tmp_path)
    configure(bridge, tmp_path)

    async def exercise():
        await bridge.handle_command(1, 2, "/new first")
        await bridge.cmd_interrupt(1, 3, "", clear_pending=True)
        await bridge.handle_command(1, 4, "/new second")

    asyncio.run(exercise())

    rotation = bridge.state["checkpoint_rotations"][str(tmp_path)]
    assert rotation["label"] == "second"
    assert bridge._prompt_queue.qsize() == 1


def test_cancelled_checkpoint_does_not_clear_newer_rotation(tmp_path):
    bridge = load_bridge_module(tmp_path)
    configure(bridge, tmp_path)
    folder = str(tmp_path)
    bridge.state["checkpoint_rotations"][folder] = {"id": "newer"}

    bridge._cancel_checkpoint_rotations(
        [
            {
                "kind": "checkpoint",
                "folder": folder,
                "checkpoint_rotation_id": "older",
            }
        ]
    )

    assert bridge.state["checkpoint_rotations"][folder]["id"] == "newer"


def test_successful_checkpoint_rotates_and_schedules_bootstrap(tmp_path):
    bridge = load_bridge_module(tmp_path)
    configure(bridge, tmp_path)

    async def fake_prepare(**kwargs):
        draft = tmp_path / "draft.json"
        draft.write_text("{}")
        return {"draft": str(draft)}

    async def fake_run(*args, **kwargs):
        return "CHECKPOINT_READY", "session-1"

    async def fake_commit(draft):
        return {
            "checkpoint_id": "cp-test",
            "latest_json": str(tmp_path / "latest.json"),
        }

    bridge.prepare_checkpoint = fake_prepare
    bridge.run_codex = fake_run
    bridge.commit_checkpoint = fake_commit
    item = {
        "chat_id": 1,
        "msg_id": 2,
        "folder": str(tmp_path),
        "session_id": "session-1",
        "context_id": None,
        "checkpoint_note": "",
        "checkpoint_rotate": True,
        "checkpoint_label": "next",
        "checkpoint_rotation_id": "rotation-1",
    }
    bridge.state["checkpoint_rotations"][str(tmp_path)] = {"id": "rotation-1"}

    asyncio.run(bridge._process_checkpoint(item))

    assert bridge.state["default_session_id"] is None
    assert bridge.state["pending_checkpoint_bootstrap"][str(tmp_path)].endswith("latest.json")
    assert str(tmp_path) not in bridge.state["checkpoint_rotations"]
