import asyncio
import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest


def load_bridge_module(tmp_path):
    root = Path(__file__).resolve().parents[2]
    bridge_path = root / "services" / "claude-telegram-bridge" / "main.py"
    os.environ["BRIDGE_LOG_FILE"] = str(tmp_path / "claude.log")
    os.environ["BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    name = f"claude_mcp_dispatch_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def configure_bridge(bridge, tmp_path):
    bridge.state = {
        "active_folder": str(tmp_path),
        "default_session_id": "atlas-session",
        "folders": {"atlas": str(tmp_path)},
        "folder_sessions": {str(tmp_path): "atlas-session"},
        "sessions": {},
        "checkpoint_rotations": {},
        "pending_checkpoint_bootstrap": {},
        "last_checkpoints": {},
    }
    bridge._prompt_queue = asyncio.Queue()
    bridge.BRIDGE_DISPATCH_CHAT_ID = 436052469
    bridge.BRIDGE_DISPATCH_JOB_DIR = tmp_path / "jobs"
    token_file = tmp_path / "bridge-token"
    token_file.write_text("test-token\n")
    bridge.BRIDGE_DISPATCH_TOKEN_FILE = token_file
    bridge.STATE_FILE = tmp_path / "state.json"
    sent = []

    async def fake_send_message(chat_id, text, reply_to=None):
        sent.append((chat_id, text, reply_to))

    bridge.send_message = fake_send_message
    bridge.send_plain_message = fake_send_message
    return sent


def test_external_dispatch_is_authenticated_and_preserves_claude_model(tmp_path):
    bridge = load_bridge_module(tmp_path)
    sent = configure_bridge(bridge, tmp_path)
    employee_workspace = tmp_path / "employee-workspace"
    employee_workspace.mkdir()
    bridge.state["folder_sessions"][str(employee_workspace)] = "employee-session"

    async def exercise():
        request = bridge.DispatchRequest(
            job_id="tb_atlas_test_123",
            prompt="Inspect Hermes without changing it.",
            label="Atlas smoke test",
            notify_telegram=True,
            model="opus",
            reasoning_effort="max",
            cwd=str(employee_workspace),
        )
        response = await bridge.dispatch(request, x_dispatch_token="test-token")
        item = await bridge._prompt_queue.get()
        return response, item

    response, item = asyncio.run(exercise())

    assert response == {"job_id": "tb_atlas_test_123", "state": "queued"}
    assert item["dispatch_job_id"] == "tb_atlas_test_123"
    assert item["model"] == "opus"
    assert item["reasoning_effort"] == "max"
    assert item["folder"] == str(employee_workspace)
    assert item["session_id"] == "employee-session"
    saved = (tmp_path / "jobs" / "tb_atlas_test_123.json").read_text()
    assert '"state": "queued"' in saved
    assert '"model": "opus"' in saved
    assert f'"cwd": "{employee_workspace}"' in saved
    assert any("MCP dispatch queued: Atlas smoke test" in text for _, text, _ in sent)
    assert any("remain available to the MCP caller" in text for _, text, _ in sent)


def test_external_dispatch_fans_out_progress_and_result(tmp_path):
    bridge = load_bridge_module(tmp_path)
    sent = configure_bridge(bridge, tmp_path)
    bridge.WATCHDOG_ENABLED = False

    async def fake_send_typing(chat_id):
        return None

    async def fake_typing_loop(chat_id):
        await asyncio.Event().wait()

    async def fake_run_harness(prompt, chat_id, **kwargs):
        await bridge.send_message(chat_id, "... inspecting Hermes configuration")
        return "ATLAS_DUAL_OUTPUT_READY", "atlas-session"

    bridge.send_typing = fake_send_typing
    bridge.typing_loop = fake_typing_loop
    bridge.run_harness = fake_run_harness

    async def exercise():
        request = bridge.DispatchRequest(
            job_id="tb_atlas_dual_123",
            prompt="Check dual output without changing files.",
            label="Atlas dual-output smoke",
            notify_telegram=True,
            model="claude-opus-5",
            reasoning_effort="high",
        )
        await bridge.dispatch(request, x_dispatch_token="test-token")
        item = await bridge._prompt_queue.get()
        await bridge._process_prompt(item)

    asyncio.run(exercise())

    texts = [text for _, text, _ in sent]
    assert any("MCP dispatch queued: Atlas dual-output smoke" in text for text in texts)
    assert any("MCP dispatch started: Atlas dual-output smoke" in text for text in texts)
    assert "... inspecting Hermes configuration" in texts
    assert any(
        "MCP dispatch completed: Atlas dual-output smoke" in text
        and "ATLAS_DUAL_OUTPUT_READY" in text
        for text in texts
    )
    saved = (tmp_path / "jobs" / "tb_atlas_dual_123.json").read_text()
    assert '"state": "completed"' in saved
    assert '"result": "ATLAS_DUAL_OUTPUT_READY"' in saved


def test_external_dispatch_rejects_bad_token(tmp_path):
    bridge = load_bridge_module(tmp_path)
    configure_bridge(bridge, tmp_path)

    async def exercise():
        request = bridge.DispatchRequest(
            job_id="tb_atlas_test_456",
            prompt="This must not be queued.",
        )
        await bridge.dispatch(request, x_dispatch_token="wrong")

    with pytest.raises(bridge.HTTPException) as error:
        asyncio.run(exercise())

    assert error.value.status_code == 401
    assert bridge._prompt_queue.empty()


def test_external_dispatch_rejects_missing_workspace(tmp_path):
    bridge = load_bridge_module(tmp_path)
    configure_bridge(bridge, tmp_path)

    async def exercise():
        request = bridge.DispatchRequest(
            job_id="tb_atlas_test_789",
            prompt="This must not be queued.",
            cwd=str(tmp_path / "does-not-exist"),
        )
        await bridge.dispatch(request, x_dispatch_token="test-token")

    with pytest.raises(bridge.HTTPException) as error:
        asyncio.run(exercise())

    assert error.value.status_code == 400
    assert bridge._prompt_queue.empty()
