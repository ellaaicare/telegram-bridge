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


def test_external_dispatch_is_authenticated_and_preserves_claude_model(tmp_path):
    bridge = load_bridge_module(tmp_path)
    configure_bridge(bridge, tmp_path)

    async def exercise():
        request = bridge.DispatchRequest(
            job_id="tb_atlas_test_123",
            prompt="Inspect Hermes without changing it.",
            label="Atlas smoke test",
            notify_telegram=True,
            model="opus",
            reasoning_effort="max",
        )
        response = await bridge.dispatch(request, x_dispatch_token="test-token")
        item = await bridge._prompt_queue.get()
        return response, item

    response, item = asyncio.run(exercise())

    assert response == {"job_id": "tb_atlas_test_123", "state": "queued"}
    assert item["dispatch_job_id"] == "tb_atlas_test_123"
    assert item["model"] == "opus"
    assert item["reasoning_effort"] == "max"
    assert item["session_id"] == "atlas-session"
    saved = (tmp_path / "jobs" / "tb_atlas_test_123.json").read_text()
    assert '"state": "queued"' in saved
    assert '"model": "opus"' in saved


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
