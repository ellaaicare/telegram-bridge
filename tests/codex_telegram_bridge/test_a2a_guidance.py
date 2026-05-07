import importlib.util
import os
import sys
from pathlib import Path


def load_bridge_module():
    repo_root = Path(__file__).resolve().parents[2]
    bridge_path = repo_root / "services" / "codex-telegram-bridge" / "main.py"
    registry_path = repo_root / "services" / "telegram-a2a" / "agents.json"
    os.environ.setdefault("CODEX_BRIDGE_LOG_FILE", "/tmp/codex-telegram-bridge-test.log")
    # Force test registry so live env doesn't leak into tests
    os.environ["A2A_BOT_REGISTRY_PATH"] = str(registry_path)
    spec = importlib.util.spec_from_file_location("codex_telegram_bridge_main", bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_allowed_bot_raw_chatter_is_silently_ignored():
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
    assert auto_reply is None


def test_bridge_version_metadata_is_exposed():
    bridge = load_bridge_module()

    assert bridge.BRIDGE_VERSION == "0.2.0"
    assert bridge.BRIDGE_BUILD == "a2a-noise-harden-pr6.9c8bcef"
    assert bridge.app.version == bridge.BRIDGE_VERSION


def test_repeated_bad_bot_syntax_is_silently_ignored():
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

    assert first == (False, "Using TodoWrite", "", None)
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
