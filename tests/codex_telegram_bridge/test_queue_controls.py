import asyncio
import importlib.util
import os
import signal
import sys
import uuid
from pathlib import Path


def load_bridge_module():
    repo_root = Path(__file__).resolve().parents[2]
    bridge_path = repo_root / "services" / "codex-telegram-bridge" / "main.py"
    registry_path = repo_root / "services" / "telegram-a2a" / "agents.json"
    os.environ.setdefault("CODEX_BRIDGE_LOG_FILE", "/tmp/codex-telegram-bridge-test.log")
    os.environ.pop("CODEX_BRIDGE_VERSION", None)
    os.environ.pop("CODEX_BRIDGE_BUILD", None)
    os.environ["A2A_BOT_REGISTRY_PATH"] = str(registry_path)
    module_name = f"codex_telegram_bridge_queue_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def router_prompt(task_id: str, body: str) -> str:
    return (
        "A2A handoff from n8n-github-router "
        f"(task_id={task_id}, type=task, ttl=1, requires_response=True).\n\n{body}"
    )


def configure_queue(bridge):
    bridge.state = {
        "active_folder": "/tmp",
        "default_session_id": "session-1",
        "folders": {"tmp": "/tmp"},
        "folder_sessions": {},
        "sessions": {},
    }
    bridge._prompt_queue = bridge.PromptQueue()
    sent = []

    async def fake_send_message(chat_id, text, reply_to=None):
        sent.append((chat_id, text, reply_to))

    async def fake_send_plain_message(chat_id, text, reply_to=None):
        sent.append((chat_id, text, reply_to))

    bridge.send_message = fake_send_message
    bridge.send_plain_message = fake_send_plain_message
    return sent


def test_router_events_coalesce_into_one_pending_run():
    bridge = load_bridge_module()
    sent = configure_queue(bridge)

    async def exercise():
        await bridge.enqueue_prompt(
            -100,
            11,
            router_prompt("issue-comment-1", "Read issue #1051 comment one."),
            a2a_reply_target="Letta_moltbot",
        )
        await bridge.enqueue_prompt(
            -100,
            12,
            router_prompt("issue-comment-2", "Read issue #1051 comment two."),
            a2a_reply_target="Letta_moltbot",
        )
        item = await bridge._prompt_queue.get()
        return item

    item = asyncio.run(exercise())

    assert bridge._prompt_queue.qsize() == 0
    assert item["a2a_task_ids"] == ["issue-comment-1", "issue-comment-2"]
    assert len(item["a2a_events"]) == 2
    assert "Coalesced A2A backlog containing 2 automated GitHub events" in item["text"]
    assert "Read issue #1051 comment one." in item["text"]
    assert "Read issue #1051 comment two." in item["text"]
    assert sent == []


def test_health_keeps_dequeued_work_unfinished_until_task_done():
    bridge = load_bridge_module()
    configure_queue(bridge)
    item = {"text": "dequeued", "automated": False}

    async def exercise():
        await bridge._prompt_queue.put(item)
        dequeued = await bridge._prompt_queue.get()
        before = await bridge.health()
        bridge._prompt_queue.task_done()
        after = await bridge.health()
        return dequeued, before, after

    dequeued, before, after = asyncio.run(exercise())

    assert dequeued == item
    assert before["queue"] == {
        "runs": 0,
        "events": 0,
        "automated_runs": 0,
        "unfinished_runs": 1,
        "busy": False,
    }
    assert after["queue"]["unfinished_runs"] == 0


def test_log_redaction_masks_provider_keys_and_bot_tokens():
    bridge = load_bridge_module()

    secrets = [
        "sk-example_abcdefghijklmnopqrstuvwxyz",
        "gsk_abcdefghijklmnopqrstuvwxyz123456",
        "xai-abcdefghijklmnopqrstuvwxyz123456",
        "AIzaabcdefghijklmnopqrstuvwxyz1234567890",
        "github_pat_abcdefghijklmnopqrstuvwxyz1234567890",
        "eyJabcdefghijk.abcdefghijklmnop.abcdefghijklmnop",
        "123456789:abcdefghijklmnopqrstuvwxyz123456",
        "named-secret-value-123456",
    ]
    redacted = bridge._redact_log_text(
        "use "
        + " ".join(secrets[:5])
        + f" Authorization: Bearer {secrets[5]} {secrets[6]} "
        + f'OPENROUTER_API_KEY="{secrets[7]}"'
    )

    assert all(secret not in redacted for secret in secrets)
    assert "OPENROUTER_API_KEY=[REDACTED_SECRET]" in redacted
    assert redacted.count("[REDACTED_") == len(secrets)


def test_human_prompt_runs_before_pending_automation():
    bridge = load_bridge_module()
    configure_queue(bridge)

    async def exercise():
        await bridge.enqueue_prompt(
            -100,
            11,
            router_prompt("automated-1", "Read issue #1051."),
            a2a_reply_target="Letta_moltbot",
        )
        await bridge.enqueue_prompt(436052469, 12, "Please check the current task.")
        first = await bridge._prompt_queue.get()
        second = await bridge._prompt_queue.get()
        return first, second

    first, second = asyncio.run(exercise())

    assert first["text"] == "Please check the current task."
    assert second["a2a_task_ids"] == ["automated-1"]


def test_non_router_handoffs_remain_separate_runs():
    bridge = load_bridge_module()
    configure_queue(bridge)
    first = "A2A handoff from HenryBot (task_id=henry-1, type=task, ttl=1, requires_response=True).\n\nOne."
    second = "A2A handoff from HenryBot (task_id=henry-2, type=task, ttl=1, requires_response=True).\n\nTwo."

    async def exercise():
        await bridge.enqueue_prompt(-100, 11, first, a2a_reply_target="HenryBot")
        await bridge.enqueue_prompt(-100, 12, second, a2a_reply_target="HenryBot")

    asyncio.run(exercise())

    assert bridge._prompt_queue.qsize() == 2
    assert bridge._prompt_queue.event_count() == 2


def test_coalescing_starts_a_new_batch_at_configured_event_limit():
    bridge = load_bridge_module()
    configure_queue(bridge)
    bridge.A2A_QUEUE_COALESCE_MAX_EVENTS = 2

    async def exercise():
        for index in range(3):
            await bridge.enqueue_prompt(
                -100,
                11 + index,
                router_prompt(f"automated-{index}", f"Event {index}."),
                a2a_reply_target="Letta_moltbot",
            )

    asyncio.run(exercise())

    assert bridge._prompt_queue.qsize() == 2
    assert bridge._prompt_queue.event_count() == 3


def test_stop_interrupts_active_run_clears_backlog_and_prioritizes_replacement():
    bridge = load_bridge_module()
    sent = configure_queue(bridge)

    class FakeProc:
        returncode = None

        def __init__(self):
            self.signals = []

        def send_signal(self, value):
            self.signals.append(value)

    proc = FakeProc()
    bridge._active_codex_proc = proc

    async def exercise():
        await bridge.enqueue_prompt(
            -100,
            11,
            router_prompt("automated-1", "First event."),
            a2a_reply_target="Letta_moltbot",
        )
        await bridge.enqueue_prompt(
            -100,
            12,
            router_prompt("automated-2", "Second event."),
            a2a_reply_target="Letta_moltbot",
        )
        await bridge.cmd_interrupt(436052469, 13, "Replacement task", clear_pending=True)
        return await bridge._prompt_queue.get()

    next_item = asyncio.run(exercise())

    assert proc.signals == [signal.SIGINT]
    assert next_item["text"] == "Replacement task"
    assert bridge._prompt_queue.qsize() == 0
    assert any("cleared 1 queued runs" in text for _, text, _ in sent)
    assert any('"task_id": "automated-1"' in text for _, text, _ in sent)
    assert any('"task_id": "automated-2"' in text for _, text, _ in sent)


def test_interrupt_keeps_backlog_but_puts_follow_up_first():
    bridge = load_bridge_module()
    configure_queue(bridge)

    class FakeProc:
        returncode = None

        def send_signal(self, value):
            self.signal = value

    bridge._active_codex_proc = FakeProc()

    async def exercise():
        await bridge.enqueue_prompt(
            -100,
            11,
            router_prompt("automated-1", "Existing event."),
            a2a_reply_target="Letta_moltbot",
        )
        await bridge.cmd_interrupt(436052469, 12, "Urgent follow-up", clear_pending=False)
        first = await bridge._prompt_queue.get()
        second = await bridge._prompt_queue.get()
        return first, second

    first, second = asyncio.run(exercise())

    assert first["text"] == "Urgent follow-up"
    assert second["a2a_task_ids"] == ["automated-1"]
    assert bridge._active_codex_proc.signal == signal.SIGINT


def test_coalesced_response_body_can_be_reused_for_each_original_task():
    bridge = load_bridge_module()
    response = bridge._a2a_autowrap_result("ExampleCodexBot", "task-1", "Completed once.")

    body = bridge._a2a_response_body(response, "ExampleCodexBot")
    second = bridge._a2a_autowrap_result("ExampleCodexBot", "task-2", body)

    assert body == "Completed once."
    assert '"task_id": "task-2"' in second
    assert '"body": "Completed once."' in second


def test_processing_coalesced_batch_sends_terminal_result_for_every_task_id():
    bridge = load_bridge_module()
    configure_queue(bridge)
    bridge.A2A_PROGRESS_MODE = "off"
    bridge.WATCHDOG_ENABLED = False
    bridge.save_state = lambda: None
    sent = []

    async def fake_send_typing(chat_id):
        return None

    async def fake_send_plain_message(chat_id, text, reply_to=None):
        sent.append(text)

    async def fake_run_codex(*args, **kwargs):
        return "Completed the consolidated GitHub review.", "session-1"

    async def fake_cleanup():
        return None

    bridge.send_typing = fake_send_typing
    bridge.send_plain_message = fake_send_plain_message
    bridge.run_codex = fake_run_codex
    bridge.cleanup_old_media = fake_cleanup
    events = [
        bridge._coalescible_a2a_event(router_prompt("task-1", "First."), "Letta_moltbot"),
        bridge._coalescible_a2a_event(router_prompt("task-2", "Second."), "Letta_moltbot"),
    ]
    item = {
        "chat_id": -100,
        "msg_id": 11,
        "text": bridge._format_coalesced_a2a_prompt(events),
        "images": [],
        "folder": "/tmp",
        "session_id": "session-1",
        "context_id": None,
        "pending_label": None,
        "a2a_reply_target": "Letta_moltbot",
        "a2a_task_ids": ["task-1", "task-2"],
    }

    asyncio.run(bridge._process_prompt(item))

    assert len(sent) == 2
    assert '"task_id": "task-1"' in sent[0]
    assert '"task_id": "task-2"' in sent[1]
    assert "Completed the consolidated GitHub review." in sent[0]
    assert "Completed the consolidated GitHub review." in sent[1]


def test_queue_worker_failure_sends_terminal_result_for_every_coalesced_task_id():
    bridge = load_bridge_module()
    sent = configure_queue(bridge)
    bridge._shutting_down = False
    events = [
        bridge._coalescible_a2a_event(router_prompt("task-1", "First."), "Letta_moltbot"),
        bridge._coalescible_a2a_event(router_prompt("task-2", "Second."), "Letta_moltbot"),
    ]
    item = {
        "chat_id": -100,
        "msg_id": 11,
        "text": bridge._format_coalesced_a2a_prompt(events),
        "images": [],
        "folder": "/tmp",
        "session_id": "session-1",
        "context_id": None,
        "pending_label": None,
        "a2a_reply_target": "Letta_moltbot",
        "a2a_task_ids": ["task-1", "task-2"],
    }

    async def fake_process(_item):
        raise RuntimeError("OPENAI_API_KEY=must-not-leak")

    async def exercise():
        bridge._process_prompt = fake_process
        await bridge._prompt_queue.put(item)
        worker = asyncio.create_task(bridge.queue_worker())
        for _ in range(100):
            terminal = [text for _, text, _ in sent if '"type": "result"' in text]
            if len(terminal) == 2:
                break
            await asyncio.sleep(0.01)
        bridge._shutting_down = True
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())

    terminal = [text for _, text, _ in sent if '"type": "result"' in text]
    assert len(terminal) == 2
    assert '"task_id": "task-1"' in terminal[0]
    assert '"task_id": "task-2"' in terminal[1]
    assert all("Bridge processing failed before completion" in text for text in terminal)
    assert all("must-not-leak" not in text for _, text, _ in sent)


def test_queue_worker_cancellation_sends_only_missing_terminal_results():
    bridge = load_bridge_module()
    sent = configure_queue(bridge)
    bridge._shutting_down = False
    started = asyncio.Event()
    wait_forever = asyncio.Event()
    item = {
        "chat_id": -100,
        "msg_id": 11,
        "text": router_prompt("task-1", "First."),
        "images": [],
        "folder": "/tmp",
        "session_id": "session-1",
        "context_id": None,
        "pending_label": None,
        "a2a_reply_target": "Letta_moltbot",
        "a2a_task_ids": ["task-1", "task-2"],
        "terminal_a2a_task_ids": {"task-1"},
    }

    async def fake_process(_item):
        started.set()
        await wait_forever.wait()

    async def exercise():
        bridge._process_prompt = fake_process
        await bridge._prompt_queue.put(item)
        worker = asyncio.create_task(bridge.queue_worker())
        await started.wait()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(exercise())

    terminal = [text for _, text, _ in sent if '"type": "result"' in text]
    assert len(terminal) == 1
    assert '"task_id": "task-2"' in terminal[0]
    assert "Cancelled before completion" in terminal[0]
