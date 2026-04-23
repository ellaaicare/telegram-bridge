import importlib.util
import os
import sys
from pathlib import Path


def load_bridge_module():
    repo_root = Path(__file__).resolve().parents[2]
    bridge_path = repo_root / "services" / "claude-telegram-bridge" / "main.py"
    os.environ.setdefault("BRIDGE_STATE_DIR", "/tmp/claude-telegram-bridge-test-state")
    spec = importlib.util.spec_from_file_location("claude_telegram_bridge_main", bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_allowed_bot_bad_syntax_receives_guidance():
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
    assert auto_reply is not None
    assert auto_reply.startswith("A2A handoff syntax required for bot-to-bot work.")
    assert "skills/telegram-a2a-handoff/SKILL.md" in auto_reply
    assert "github.com/ellaaicare/telegram-bridge" in auto_reply
    assert "/Users/" not in auto_reply


def test_bridge_version_metadata_is_exposed():
    bridge = load_bridge_module()

    assert bridge.BRIDGE_VERSION == "3.5.0"
    assert bridge.BRIDGE_BUILD == "a2a-quiet-status-pr685.7681cf5"
    assert bridge.app.version == bridge.BRIDGE_VERSION


def test_repeated_bad_bot_syntax_guidance_is_rate_limited():
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

    assert first[3] is not None
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

    envelope = bridge._a2a_status_envelope("MacMiniCodex", "task-1", "Working silently.")

    assert envelope.startswith("/handoff@ExampleCodexBot ")
    assert '"type":"status"' in envelope
    assert '"requires_response":false' in envelope


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

    assert bridge.should_process_group_message(message, status, "") == (False, status, "", None)


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

    ok, reason = bridge._validate_handoff_envelope("Audit #673 first.", "ExampleCodexBot")
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
        return await bridge.run_claude(
            "Return strict A2A envelope only.",
            1,
            suppress_footer=True,
        )

    response, session_id = __import__("asyncio").run(exercise())

    assert session_id == "session-1"
    assert response.startswith("/handoff@ExampleCodexBot ")
    assert "_(" not in response
