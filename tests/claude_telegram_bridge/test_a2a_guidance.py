import importlib.util
import os
import sys
import uuid
from pathlib import Path


def load_bridge_module(env_updates=None):
    repo_root = Path(__file__).resolve().parents[2]
    bridge_path = repo_root / "services" / "claude-telegram-bridge" / "main.py"
    registry_path = repo_root / "services" / "telegram-a2a" / "agents.json"
    os.environ.setdefault("BRIDGE_STATE_DIR", "/tmp/claude-telegram-bridge-test-state")
    # Force test registry so live env doesn't leak into tests
    os.environ["A2A_BOT_REGISTRY_PATH"] = str(registry_path)
    if env_updates:
        for key, value in env_updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    module_name = f"claude_telegram_bridge_main_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_allowed_bot_raw_chatter_is_silently_ignored():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleClaudeBot"
    bridge.BOT_ID = 1000000003
    bridge.ALLOWED_BOT_IDS = {1000000001}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    should_process, text, caption, auto_reply = bridge.should_process_group_message(
        {
            "chat": {"id": -1000000000000, "type": "supergroup"},
            "from": {"id": 1000000001, "username": "ExampleCodexBot", "is_bot": True},
        },
        "MacMiniCodex standing by",
        "",
    )

    assert should_process is False
    assert text == "MacMiniCodex standing by"
    assert caption == ""
    assert auto_reply is None


def test_bridge_version_metadata_is_exposed():
    bridge = load_bridge_module()

    assert bridge.BRIDGE_VERSION == "3.5.0"
    assert bridge.BRIDGE_BUILD == "a2a-noise-harden-pr6.9c8bcef"
    assert bridge.app.version == bridge.BRIDGE_VERSION


def test_repeated_bad_bot_syntax_is_silently_ignored():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleClaudeBot"
    bridge.BOT_ID = 1000000003
    bridge.ALLOWED_BOT_IDS = {1000000001}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}
    bridge._a2a_guidance_last_sent = {}

    message = {
        "chat": {"id": -1000000000000, "type": "supergroup"},
        "from": {"id": 1000000001, "username": "ExampleCodexBot", "is_bot": True},
    }

    first = bridge.should_process_group_message(message, "Using TodoWrite", "")
    second = bridge.should_process_group_message(message, "Reading page.dart", "")

    assert first == (False, "Using TodoWrite", "", None)
    assert second == (False, "Reading page.dart", "", None)


def test_a2a_status_envelope_is_non_actionable_and_ignored():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleClaudeBot"
    bridge.state = {"processed_handoffs": {}}

    ok, prompt = bridge._parse_handoff(
        '/handoff@ExampleClaudeBot {"from":"ExampleCodexBot","to":"ExampleClaudeBot",'
        '"task_id":"task-1:status","ttl":1,"requires_response":false,'
        '"type":"status","body":"Accepted and working silently."}'
    )

    assert ok is False
    assert prompt == bridge.A2A_IGNORED


def test_a2a_status_envelope_uses_repo_alias_target():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleClaudeBot"

    envelope = bridge._a2a_status_envelope(
        "MacMiniCodex", "task-1", "Working silently."
    )

    assert envelope.startswith("/handoff@ExampleCodexBot ")
    assert '"type"' in envelope and "status" in envelope
    assert '"requires_response"' in envelope and "false" in envelope


def test_valid_status_message_does_not_receive_guidance():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleClaudeBot"
    bridge.BOT_ID = 1000000003
    bridge.ALLOWED_BOT_IDS = {1000000001}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    message = {
        "chat": {"id": -1000000000000, "type": "supergroup"},
        "from": {"id": 1000000001, "username": "ExampleCodexBot", "is_bot": True},
    }
    status = (
        '/handoff@ExampleClaudeBot {"from":"ExampleCodexBot","to":"ExampleClaudeBot",'
        '"task_id":"task-1:status","ttl":1,"requires_response":false,'
        '"type":"status","body":"Accepted and working silently."}'
    )

    assert bridge.should_process_group_message(message, status, "") == (
        False,
        status,
        "",
        None,
    )


def test_repo_registry_trusts_known_peer_bots_by_default():
    bridge = load_bridge_module()

    assert 1000000001 in bridge.ALLOWED_BOT_IDS
    assert 1000000002 in bridge.ALLOWED_BOT_IDS
    assert bridge._canonical_handoff_target("MacMiniClaude") == "ExampleClaudeBot"


def test_accepts_current_bot_alias_handoff_from_registry():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleClaude2Bot"
    bridge.state = {"processed_handoffs": {}}
    bridge.save_state = lambda: None

    ok, prompt = bridge._parse_handoff(
        '/handoff@iMacClaude {"from":"ExampleCodexBot","to":"iMacClaude",'
        '"task_id":"alias-task-1","ttl":1,"requires_response":true,'
        '"type":"task","body":"Review the bridge docs."}'
    )

    assert ok is True
    assert "A2A handoff from ExampleCodexBot" in prompt
    assert "Review the bridge docs." in prompt


def test_peer_guidance_does_not_trigger_guidance_loop():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleClaudeBot"
    bridge.BOT_ID = 1000000003
    bridge.ALLOWED_BOT_IDS = {1000000001}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    guidance = bridge._a2a_guidance_message()
    should_process, text, caption, auto_reply = bridge.should_process_group_message(
        {
            "chat": {"id": -1000000000000, "type": "supergroup"},
            "from": {"id": 1000000001, "username": "ExampleCodexBot", "is_bot": True},
        },
        guidance,
        "",
    )

    assert should_process is False
    assert text == guidance
    assert caption == ""
    assert auto_reply is None


def test_rejects_raw_a2a_response_with_repo_skill_link():
    bridge = load_bridge_module()

    ok, reason = bridge._validate_handoff_envelope(
        "Audit #673 first.", "ExampleCodexBot"
    )
    rejection = bridge._a2a_response_rejection("ExampleCodexBot", reason)

    assert ok is False
    assert "response must start with /handoff@ExampleCodexBot" in reason
    assert rejection.startswith("A2A handoff syntax required for bot-to-bot work.")
    assert "invalid bot-to-bot response" in rejection
    assert "skills/telegram-a2a-handoff/SKILL.md" in rejection
    assert "services/telegram-a2a/agents.json" in rejection
    assert "github.com/ellaaicare/telegram-bridge" in rejection
    assert "/Users/" not in rejection


def test_validates_structured_a2a_response_to_source_bot():
    bridge = load_bridge_module()

    ok, reason = bridge._validate_handoff_envelope(
        '/handoff@ExampleCodexBot {"from":"ExampleClaudeBot","to":"ExampleCodexBot",'
        '"task_id":"result-1","ttl":0,"requires_response":false,'
        '"type":"result","body":"Audit #673 first."}',
        "ExampleCodexBot",
    )

    assert ok is True
    assert reason == ""


def test_validates_structured_a2a_response_to_source_alias():
    bridge = load_bridge_module()

    ok, reason = bridge._validate_handoff_envelope(
        '/handoff@MacMiniCodex {"from":"ExampleClaudeBot","to":"MacMiniCodex",'
        '"task_id":"result-1","ttl":0,"requires_response":false,'
        '"type":"result","body":"Audit #673 first."}',
        "ExampleCodexBot",
    )

    assert ok is True
    assert reason == ""


def test_a2a_runs_suppress_footer_in_final_response():
    bridge = load_bridge_module()

    async def exercise():
        class FakeStdout:
            def __aiter__(self):
                self._lines = iter(
                    [
                        b'{"type":"system","subtype":"init","session_id":"session-1"}\n',
                        b'{"type":"result","session_id":"session-1","duration_ms":17000,"result":"/handoff@ExampleCodexBot {\\"from\\":\\"ExampleClaudeBot\\",\\"to\\":\\"ExampleCodexBot\\",\\"task_id\\":\\"result-1\\",\\"ttl\\":0,\\"requires_response\\":false,\\"type\\":\\"result\\",\\"body\\":\\"Smoke ok\\"}","is_error":false}\n',
                    ]
                )
                return self

            async def __anext__(self):
                try:
                    return next(self._lines)
                except StopIteration:
                    raise StopAsyncIteration

        class FakeStderr:
            async def read(self):
                return b""

        class FakeProc:
            def __init__(self):
                self.stdout = FakeStdout()
                self.stderr = FakeStderr()

            async def wait(self):
                return 0

        async def fake_create_subprocess_exec(*args, **kwargs):
            return FakeProc()

        bridge.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        return await bridge.run_harness(
            "Return strict A2A envelope only.",
            1,
            suppress_footer=True,
        )

    response, session_id = __import__("asyncio").run(exercise())

    assert session_id == "session-1"
    assert response.startswith("/handoff@ExampleCodexBot ")
    assert "_(" not in response


def test_opencode_run_uses_model_and_agent_flags():
    bridge = load_bridge_module(
        {
            "HARNESS_CLI": "opencode",
            "HARNESS_LABEL": "OpenCode",
            "HARNESS_SERVICE_NAME": "opencode-telegram-bridge",
            "HARNESS_AGENT": "builder",
            "BRIDGE_MODEL": "gpt-5.4-mini",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "BRIDGE_STATE_DIR": "/tmp/opencode-telegram-bridge-test-state",
        }
    )
    bridge.state["active_folder"] = "/tmp"
    captured = {}

    async def exercise():
        class FakeStdout:
            def __aiter__(self):
                self._lines = iter(
                    [
                        b'{"type":"thread.init","sessionID":"op-123"}\n',
                        b'{"type":"message","message":{"role":"assistant","content":"Model switched cleanly."}}\n',
                        b'{"type":"result","result":"Model switched cleanly.","sessionID":"op-123","duration_ms":1200}\n',
                    ]
                )
                return self

            async def __anext__(self):
                try:
                    return next(self._lines)
                except StopIteration:
                    raise StopAsyncIteration

        class FakeStderr:
            async def read(self):
                return b""

        class FakeProc:
            def __init__(self):
                self.stdout = FakeStdout()
                self.stderr = FakeStderr()
                self.returncode = 0

            async def wait(self):
                return 0

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProc()

        bridge.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        return await bridge.run_harness("Summarize status.", 1, suppress_footer=True)

    response, session_id = __import__("asyncio").run(exercise())

    assert response == "Model switched cleanly."
    assert session_id == "op-123"
    assert captured["args"] == (
        "opencode",
        "run",
        "--format",
        "json",
        "--dir",
        "/tmp",
        "-m",
        "gpt-5.4-mini",
        "--agent",
        "builder",
        "Summarize status.",
    )


def test_kilo_run_uses_model_and_agent_flags():
    bridge = load_bridge_module(
        {
            "HARNESS_CLI": "kilo",
            "HARNESS_LABEL": "Kilo Code",
            "HARNESS_SERVICE_NAME": "kilo-telegram-bridge",
            "HARNESS_AGENT": "planner",
            "BRIDGE_MODEL": "kimi-k2.6:cloud",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "BRIDGE_STATE_DIR": "/tmp/kilo-telegram-bridge-test-state",
        }
    )
    bridge.state["active_folder"] = "/tmp"
    captured = {}

    async def exercise():
        class FakeStdout:
            def __aiter__(self):
                self._lines = iter(
                    [
                        b'{"type":"thread.init","sessionID":"ki-456"}\n',
                        b'{"type":"message","message":{"role":"assistant","content":"Kilo session ready."}}\n',
                        b'{"type":"result","result":"Kilo session ready.","sessionID":"ki-456","duration_ms":800}\n',
                    ]
                )
                return self

            async def __anext__(self):
                try:
                    return next(self._lines)
                except StopIteration:
                    raise StopAsyncIteration

        class FakeStderr:
            async def read(self):
                return b""

        class FakeProc:
            def __init__(self):
                self.stdout = FakeStdout()
                self.stderr = FakeStderr()
                self.returncode = 0

            async def wait(self):
                return 0

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProc()

        bridge.asyncio.create_subprocess_exec = fake_create_subprocess_exec
        return await bridge.run_harness("Check kilo status.", 1, suppress_footer=True)

    response, session_id = __import__("asyncio").run(exercise())

    assert response == "Kilo session ready."
    assert session_id == "ki-456"
    assert captured["args"] == (
        "kilo",
        "run",
        "--format",
        "json",
        "--dir",
        "/tmp",
        "-m",
        "kimi-k2.6:cloud",
        "--agent",
        "planner",
        "Check kilo status.",
    )


def test_health_uses_harness_metadata():
    bridge = load_bridge_module(
        {
            "HARNESS_CLI": "kilo",
            "HARNESS_LABEL": "Kilo Code",
            "HARNESS_SERVICE_NAME": "kilo-telegram-bridge",
            "BRIDGE_MODEL": "kimi-k2",
            "HARNESS_AGENT": "planner",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "BRIDGE_STATE_DIR": "/tmp/kilo-telegram-bridge-test-state",
        }
    )

    payload = __import__("asyncio").run(bridge.health())

    assert payload["service"] == "kilo-telegram-bridge"
    assert payload["harness_cli"] == "kilo"
    assert payload["harness_label"] == "Kilo Code"
    assert payload["model_override"] == "kimi-k2"
    assert payload["agent_profile"] == "planner"


def test_parse_handoff_returns_ignored_for_non_target():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "imackilocode_bot"
    ok, prompt = bridge._parse_handoff(
        '/handoff@EllaCodexBot {"from":"EllaCodexBot","to":"EllaCodexBot","task_id":"x","ttl":1,"requires_response":true,"type":"task","body":"Do work."}'
    )
    assert ok is False
    assert prompt == bridge.A2A_IGNORED


def test_parse_handoff_processes_targeted_handoff():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "imackilocode_bot"
    ok, prompt = bridge._parse_handoff(
        '/handoff@imackilocode_bot {"from":"EllaCodexBot","to":"imackilocode_bot","task_id":"t-123","ttl":1,"requires_response":true,"type":"task","body":"Fix A2A noise."}'
    )
    assert ok is True
    assert "task_id=t-123" in prompt
    assert "Fix A2A noise." in prompt


def test_group_message_ignores_non_target_bot_handoff():
    bridge = load_bridge_module(
        {
            "HARNESS_CLI": "kilo",
            "HARNESS_LABEL": "Kilo Code",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "ALLOWED_BOT_IDS": "8217119702",
        }
    )
    bridge.BOT_USERNAME = "imackilocode_bot"
    bridge.BOT_ID = 8763402136

    msg = {
        "chat": {"id": -1003765875927, "type": "supergroup"},
        "from": {"id": 8217119702, "is_bot": True, "username": "EllaCodexBot"},
        "text": '/handoff@EllaCodexBot {"from":"EllaCodexBot","to":"EllaCodexBot","task_id":"x","ttl":1,"requires_response":true,"type":"task","body":"Do work."}',
    }
    ok, text, caption, reply = bridge.should_process_group_message(msg, msg["text"], "")
    assert ok is False
    assert reply is None  # silently ignored, no guidance spam


def test_group_message_silently_ignores_bot_raw_text():
    bridge = load_bridge_module(
        {
            "HARNESS_CLI": "kilo",
            "HARNESS_LABEL": "Kilo Code",
            "TELEGRAM_BOT_TOKEN": "test-token",
            "ALLOWED_BOT_IDS": "8217119702",
        }
    )
    bridge.BOT_USERNAME = "imackilocode_bot"
    bridge.BOT_ID = 8763402136

    msg = {
        "chat": {"id": -1003765875927, "type": "supergroup"},
        "from": {"id": 8217119702, "is_bot": True, "username": "EllaCodexBot"},
        "text": "Standing by for next dispatch.",
    }
    ok, text, caption, reply = bridge.should_process_group_message(msg, msg["text"], "")
    assert ok is False
    assert reply is None  # no guidance spam to bots


def test_is_a2a_guidance_detects_rejection_message():
    bridge = load_bridge_module()
    rejection = bridge._a2a_response_rejection("TargetBot", "missing body")
    assert bridge._is_a2a_guidance(rejection) is True


def test_task_id_deduplication_rejects_duplicate():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "imackilocode_bot"

    handoff = '/handoff@imackilocode_bot {"from":"EllaCodexBot","to":"imackilocode_bot","task_id":"dup-001","ttl":1,"requires_response":true,"type":"task","body":"First."}'
    ok1, _ = bridge._parse_handoff(handoff)
    assert ok1 is True

    ok2, prompt2 = bridge._parse_handoff(handoff)
    assert ok2 is False
    assert prompt2 == bridge.A2A_IGNORED


def test_accepts_targeted_handoff_from_unregistered_bot():
    """Valid /handoff@ThisBot from an unknown bot should be accepted."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleClaudeBot"
    bridge.BOT_ID = 1000000003
    bridge.ALLOWED_BOT_IDS = set()
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    handoff_text = (
        '/handoff@ExampleClaudeBot {"from":"UnknownBot","to":"ExampleClaudeBot",'
        '"task_id":"unreg-task-1","ttl":1,"requires_response":true,'
        '"type":"task","body":"Please run the tests."}'
    )

    should_process, text, caption, auto_reply = bridge.should_process_group_message(
        {
            "chat": {"id": -1000000000000, "type": "supergroup"},
            "from": {"id": 9999999999, "username": "UnknownBot", "is_bot": True},
        },
        handoff_text,
        "",
    )

    assert should_process is True
    assert "A2A handoff from UnknownBot" in text
    assert "Please run the tests." in text
    assert auto_reply is None


def test_rejects_non_handoff_from_unregistered_bot():
    """Non-handoff messages from unregistered bots should still be rejected."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleClaudeBot"
    bridge.BOT_ID = 1000000003
    bridge.ALLOWED_BOT_IDS = set()
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    should_process, text, caption, auto_reply = bridge.should_process_group_message(
        {
            "chat": {"id": -1000000000000, "type": "supergroup"},
            "from": {"id": 9999999999, "username": "UnknownBot", "is_bot": True},
        },
        "Hey everyone, just chatting",
        "",
    )

    assert should_process is False
    assert auto_reply is None
