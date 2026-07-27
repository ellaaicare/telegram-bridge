import asyncio
import importlib.util
import os
import sys
import uuid
from pathlib import Path


def load_bridge_module(tmp_path):
    root = Path(__file__).resolve().parents[2]
    bridge_path = root / "services" / "codex-telegram-bridge" / "main.py"
    registry_path = root / "services" / "telegram-a2a" / "agents.json"
    os.environ["CODEX_BRIDGE_LOG_FILE"] = str(tmp_path / "codex.log")
    os.environ["CODEX_BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    os.environ["A2A_BOT_REGISTRY_PATH"] = str(registry_path)
    name = f"codex_mcp_dispatch_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def configure_bridge(bridge, tmp_path):
    folder = str(tmp_path)
    bridge.state = {
        "active_folder": folder,
        "default_session_id": "codex-session",
        "folders": {"test": folder},
        "folder_sessions": {folder: "codex-session"},
        "sessions": {},
        "checkpoint_rotations": {},
        "pending_checkpoint_bootstrap": {},
        "last_checkpoints": {},
    }
    bridge._prompt_queue = bridge.PromptQueue()
    bridge.BRIDGE_DISPATCH_CHAT_ID = 436052469
    bridge.BRIDGE_DISPATCH_JOB_DIR = tmp_path / "jobs"
    bridge.STATE_FILE = tmp_path / "state.json"
    token_file = tmp_path / "bridge-token"
    token_file.write_text("test-token\n")
    bridge.BRIDGE_DISPATCH_TOKEN_FILE = token_file
    sent = []

    async def fake_send_message(chat_id, text, reply_to=None):
        sent.append((chat_id, text, reply_to))

    bridge.send_message = fake_send_message
    bridge.send_plain_message = fake_send_message
    return sent


def test_external_dispatch_fans_out_progress_and_result(tmp_path):
    bridge = load_bridge_module(tmp_path)
    sent = configure_bridge(bridge, tmp_path)
    bridge.WATCHDOG_ENABLED = False

    async def fake_send_typing(chat_id):
        return None

    async def fake_typing_loop(chat_id):
        await asyncio.Event().wait()

    async def fake_run_codex(prompt, chat_id, **kwargs):
        await bridge.send_message(chat_id, "... checking backend deployment")
        return "CODEX_DUAL_OUTPUT_READY", "codex-session"

    bridge.send_typing = fake_send_typing
    bridge.typing_loop = fake_typing_loop
    bridge.run_codex = fake_run_codex

    async def exercise():
        request = bridge.DispatchRequest(
            job_id="tb_codex_dual_123",
            prompt="Check dual output without changing files.",
            label="Codex dual-output smoke",
            notify_telegram=True,
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )
        await bridge.dispatch(request, x_dispatch_token="test-token")
        item = await bridge._prompt_queue.get()
        await bridge._process_prompt(item)

    asyncio.run(exercise())

    texts = [text for _, text, _ in sent]
    assert any("MCP dispatch queued: Codex dual-output smoke" in text for text in texts)
    assert any("MCP dispatch started: Codex dual-output smoke" in text for text in texts)
    assert "... checking backend deployment" in texts
    assert any(
        "MCP dispatch completed: Codex dual-output smoke" in text
        and "CODEX_DUAL_OUTPUT_READY" in text
        for text in texts
    )
    saved = (tmp_path / "jobs" / "tb_codex_dual_123.json").read_text()
    assert '"state": "completed"' in saved
    assert '"result": "CODEX_DUAL_OUTPUT_READY"' in saved
