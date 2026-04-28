import importlib.util
import os
import sys
from pathlib import Path


def load_bridge_module():
    repo_root = Path(__file__).resolve().parents[2]
    bridge_path = repo_root / "services" / "codex-telegram-bridge" / "main.py"
    os.environ.setdefault("CODEX_BRIDGE_LOG_FILE", "/tmp/codex-telegram-bridge-test.log")
    spec = importlib.util.spec_from_file_location("codex_telegram_bridge_main", bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_allowed_bot_bad_syntax_receives_guidance():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"
    bridge.BOT_ID = 1000000001
    bridge.ALLOWED_BOT_IDS = {1000000003}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    should_process, text, caption, auto_reply = bridge.should_process_group_message(
        {
            "chat": {"id": -1000000000000, "type": "supergroup"},
            "from": {"id": 1000000003, "username": "ExampleClaudeBot", "is_bot": True},
        },
        "MacMiniClaude standing by",
        "",
    )

    assert should_process is False
    assert text == "MacMiniClaude standing by"
    assert caption == ""
    assert auto_reply is not None
    assert auto_reply.startswith("A2A handoff syntax required for bot-to-bot work.")
    assert "skills/telegram-a2a-handoff/SKILL.md" in auto_reply
    assert "github.com/ellaaicare/telegram-bridge" in auto_reply
    assert "/Users/" not in auto_reply


def test_bridge_version_metadata_is_exposed():
    bridge = load_bridge_module()

    assert bridge.BRIDGE_VERSION == "0.3.0"
    assert bridge.BRIDGE_BUILD == "a2a-autowrap-silent-ignore"
    assert bridge.app.version == bridge.BRIDGE_VERSION


def test_repeated_bad_bot_syntax_guidance_is_rate_limited():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"
    bridge.BOT_ID = 1000000001
    bridge.ALLOWED_BOT_IDS = {1000000003}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}
    bridge._a2a_guidance_last_sent = {}

    message = {
        "chat": {"id": -1000000000000, "type": "supergroup"},
        "from": {"id": 1000000003, "username": "ExampleClaudeBot", "is_bot": True},
    }

    first = bridge.should_process_group_message(message, "Using TodoWrite", "")
    second = bridge.should_process_group_message(message, "Reading page.dart", "")

    assert first[3] is not None
    assert second == (False, "Reading page.dart", "", None)


def test_a2a_status_envelope_is_non_actionable_and_ignored():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"
    bridge.state = {"processed_handoffs": {}}

    ok, prompt = bridge._parse_handoff(
        '/handoff@ExampleCodexBot {"from":"ExampleClaudeBot","to":"ExampleCodexBot",'
        '"task_id":"task-1:status","ttl":1,"requires_response":false,'
        '"type":"status","body":"Accepted and working silently."}'
    )

    assert ok is False
    assert prompt == bridge.A2A_IGNORED


def test_valid_status_message_does_not_receive_guidance():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"
    bridge.BOT_ID = 1000000001
    bridge.ALLOWED_BOT_IDS = {1000000003}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    message = {
        "chat": {"id": -1000000000000, "type": "supergroup"},
        "from": {"id": 1000000003, "username": "ExampleClaudeBot", "is_bot": True},
    }
    status = (
        '/handoff@ExampleCodexBot {"from":"ExampleClaudeBot","to":"ExampleCodexBot",'
        '"task_id":"task-1:status","ttl":1,"requires_response":false,'
        '"type":"status","body":"Accepted and working silently."}'
    )

    assert bridge.should_process_group_message(message, status, "") == (False, status, "", None)


def test_repo_registry_trusts_known_peer_bots_by_default():
    bridge = load_bridge_module()

    assert 1000000002 in bridge.ALLOWED_BOT_IDS
    assert 1000000003 in bridge.ALLOWED_BOT_IDS
    assert bridge._canonical_handoff_target("iMacCodex") == "ExampleCodex2Bot"


def test_accepts_current_bot_alias_handoff_from_registry():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodex2Bot"
    bridge.state = {"processed_handoffs": {}}
    bridge.save_state = lambda: None

    ok, prompt = bridge._parse_handoff(
        '/handoff@iMacCodex {"from":"ExampleCodexBot","to":"iMacCodex",'
        '"task_id":"alias-task-1","ttl":1,"requires_response":true,'
        '"type":"task","body":"Create the issue."}'
    )

    assert ok is True
    assert "A2A handoff from ExampleCodexBot" in prompt
    assert "Create the issue." in prompt


def test_peer_guidance_does_not_trigger_guidance_loop():
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"
    bridge.BOT_ID = 1000000001
    bridge.ALLOWED_BOT_IDS = {1000000003}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    guidance = bridge._a2a_guidance_message()
    should_process, text, caption, auto_reply = bridge.should_process_group_message(
        {
            "chat": {"id": -1000000000000, "type": "supergroup"},
            "from": {"id": 1000000003, "username": "ExampleClaudeBot", "is_bot": True},
        },
        guidance,
        "",
    )

    assert should_process is False
    assert text == guidance
    assert caption == ""
    assert auto_reply is None


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
        '/handoff@MacMiniCodex {"from":"ExampleCodex2Bot","to":"MacMiniCodex",'
        '"task_id":"result-1","ttl":0,"requires_response":false,'
        '"type":"result","body":"Issue #680 created."}',
        "ExampleCodexBot",
    )

    assert ok is True
    assert reason == ""


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


def test_a2a_runs_suppress_footer_in_final_response():
    bridge = load_bridge_module()

    async def exercise():
        bridge.build_codex_command = lambda *args, **kwargs: ["fake-codex"]

        class FakeStdout:
            def __aiter__(self):
                self._lines = iter(
                    [
                        b'{"type":"thread.started","thread_id":"thread-1"}\n',
                        b'{"type":"item.completed","item":{"type":"agent_message","text":"/handoff@ExampleCodexBot {\\"from\\":\\"ExampleClaudeBot\\",\\"to\\":\\"ExampleCodexBot\\",\\"task_id\\":\\"result-1\\",\\"ttl\\":0,\\"requires_response\\":false,\\"type\\":\\"result\\",\\"body\\":\\"Smoke ok\\"}"}}\n',
                        b'{"type":"turn.completed","usage":{"output_tokens":17}}\n',
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
        return await bridge.run_codex(
            "Return strict A2A envelope only.",
            1,
            cwd="/tmp",
            suppress_footer=True,
        )

    response, session_id = __import__("asyncio").run(exercise())

    assert session_id == "thread-1"
    assert response.startswith("/handoff@ExampleCodexBot ")
    assert "_(" not in response


# --- Regression tests for issue #2 (auto-wrap) and #6 (silent ignore) ---


def test_a2a_result_envelope_produces_valid_handoff():
    """Issue #2: _a2a_result_envelope wraps raw text as a valid result envelope."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"

    envelope = bridge._a2a_result_envelope("ExampleClaudeBot", "task-123", "All done. Found 3 items.")

    assert envelope.startswith("/handoff@ExampleClaudeBot ")
    payload_str = envelope[len("/handoff@ExampleClaudeBot "):]
    payload = __import__("json").loads(payload_str)
    assert payload["type"] == "result"
    assert payload["from"] == "ExampleCodexBot"
    assert payload["to"] == "ExampleClaudeBot"
    assert payload["task_id"] == "task-123"
    assert payload["ttl"] == 0
    assert payload["requires_response"] is False
    assert payload["body"] == "All done. Found 3 items."


def test_a2a_result_envelope_validates_against_handoff_schema():
    """Issue #2: Auto-wrapped result envelopes pass _validate_handoff_envelope."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"

    envelope = bridge._a2a_result_envelope("ExampleClaudeBot", "task-456", "Result text")
    ok, reason = bridge._validate_handoff_envelope(envelope, "ExampleClaudeBot")

    assert ok is True
    assert reason == ""


def test_a2a_result_envelope_uses_registry_alias():
    """Issue #2: Auto-wrap resolves canonical target via registry alias."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"

    # "MacMiniClaude" is an alias that resolves via the registry.
    # The canonical username may differ between example and live registries,
    # so we just verify it resolves to *something* and produces a valid envelope.
    envelope = bridge._a2a_result_envelope("MacMiniClaude", "task-789", "Done")
    assert envelope.startswith("/handoff@")
    assert '"type":"result"' in envelope
    assert '"task_id":"task-789"' in envelope
    # The envelope must validate against the resolved target
    target_username = bridge._canonical_handoff_target("MacMiniClaude")
    ok, reason = bridge._validate_handoff_envelope(envelope, target_username)
    assert ok is True


def test_is_valid_handoff_to_other_bot_recognizes_valid_handoff():
    """Issue #6: _is_valid_handoff_to_other_bot returns True for a valid
    handoff addressed to a different bot."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"

    handoff_to_claude = (
        '/handoff@ExampleClaudeBot {"from":"ExampleCodexBot","to":"ExampleClaudeBot",'
        '"task_id":"task-1","ttl":1,"requires_response":true,'
        '"type":"task","body":"Do work."}'
    )

    assert bridge._is_valid_handoff_to_other_bot(handoff_to_claude) is True


def test_is_valid_handoff_to_other_bot_returns_false_for_own_handoff():
    """Issue #6: A handoff addressed to THIS bot is not 'other bot'."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"

    handoff_to_self = (
        '/handoff@ExampleCodexBot {"from":"ExampleClaudeBot","to":"ExampleCodexBot",'
        '"task_id":"task-1","ttl":1,"requires_response":true,'
        '"type":"task","body":"Do work."}'
    )

    assert bridge._is_valid_handoff_to_other_bot(handoff_to_self) is False


def test_is_valid_handoff_to_other_bot_returns_false_for_garbage():
    """Issue #6: Non-handoff text is not a valid handoff to another bot."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"

    assert bridge._is_valid_handoff_to_other_bot("standing by") is False
    assert bridge._is_valid_handoff_to_other_bot("hello world") is False
    assert bridge._is_valid_handoff_to_other_bot("") is False


def test_is_valid_handoff_to_other_bot_returns_false_for_invalid_json():
    """Issue #6: Handoff with broken JSON is not a valid handoff to another bot."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"

    assert bridge._is_valid_handoff_to_other_bot("/handoff@ExampleClaudeBot not-json") is False


def test_is_valid_handoff_to_other_bot_returns_false_for_missing_fields():
    """Issue #6: Handoff missing task_id/body is not a valid handoff."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"

    # Missing task_id
    assert bridge._is_valid_handoff_to_other_bot(
        '/handoff@ExampleClaudeBot {"from":"ExampleCodexBot","to":"ExampleClaudeBot",'
        '"body":"Do work."}'
    ) is False

    # Missing body
    assert bridge._is_valid_handoff_to_other_bot(
        '/handoff@ExampleClaudeBot {"from":"ExampleCodexBot","to":"ExampleClaudeBot",'
        '"task_id":"task-1"}'
    ) is False


def test_non_target_bridge_silently_ignores_valid_handoff():
    """Issue #6: A bridge should NOT send A2A guidance when it sees a valid
    handoff addressed to another bot in the group."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"
    bridge.BOT_ID = 1000000001
    bridge.ALLOWED_BOT_IDS = {1000000003}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    # ClaudeBot sends a valid handoff addressed to Claude2Bot (not CodexBot)
    handoff_to_other = (
        '/handoff@ExampleClaude2Bot {"from":"ExampleClaudeBot","to":"ExampleClaude2Bot",'
        '"task_id":"task-other-1","ttl":1,"requires_response":true,'
        '"type":"task","body":"Review the docs."}'
    )

    should_process, text, caption, auto_reply = bridge.should_process_group_message(
        {
            "chat": {"id": -1000000000000, "type": "supergroup"},
            "from": {"id": 1000000003, "username": "ExampleClaudeBot", "is_bot": True},
        },
        handoff_to_other,
        "",
    )

    assert should_process is False
    assert auto_reply is None  # No guidance sent — silently ignored


def test_non_target_bridge_silently_ignores_result_handoff():
    """Issue #6: Result handoffs to other bots are also silently ignored."""
    bridge = load_bridge_module()
    bridge.BOT_USERNAME = "ExampleCodexBot"
    bridge.BOT_ID = 1000000001
    bridge.ALLOWED_BOT_IDS = {1000000003}
    bridge.ALLOWED_CHAT_IDS = {-1000000000000}

    result_to_other = (
        '/handoff@ExampleClaudeBot {"from":"ExampleClaude2Bot","to":"ExampleClaudeBot",'
        '"task_id":"result-1","ttl":0,"requires_response":false,'
        '"type":"result","body":"Done."}'
    )

    should_process, text, caption, auto_reply = bridge.should_process_group_message(
        {
            "chat": {"id": -1000000000000, "type": "supergroup"},
            "from": {"id": 1000000004, "username": "ExampleClaude2Bot", "is_bot": True},
        },
        result_to_other,
        "",
    )

    assert should_process is False
    assert auto_reply is None
