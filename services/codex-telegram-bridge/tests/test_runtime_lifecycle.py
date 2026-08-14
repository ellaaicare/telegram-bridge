import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class RuntimeConfigurationTests(unittest.TestCase):
    def test_runtime_and_kill_grace_defaults_are_safely_bounded(self):
        self.assertGreaterEqual(main.CODEX_MAX_RUNTIME, 60)
        self.assertGreaterEqual(main.CODEX_KILL_GRACE, 1)
        self.assertLessEqual(main.CODEX_KILL_GRACE, 60)


class _Stderr:
    async def read(self):
        return b""


class _Stdout:
    def __init__(self, mode, lines=None):
        self.mode = mode
        self.lines = list(lines or [])

    async def readline(self):
        if self.mode == "silent":
            await asyncio.Future()
        if self.mode == "stream":
            await asyncio.sleep(0)
            return b'{"type":"thread.started","thread_id":"test"}\n'
        if self.mode == "error":
            raise RuntimeError("stream failed")
        if self.lines:
            return self.lines.pop(0)
        return b""


class _Process:
    _next_pid = 41000

    def __init__(self, stdout, *, completed=False):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.stdout = stdout
        self.stderr = _Stderr()
        self.returncode = 0 if completed else None
        self.terminated = False
        self.killed = False
        self._done = asyncio.Event()
        if completed:
            self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._done.set()


def _agent_message(text="done"):
    return (
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": text},
            }
        ).encode()
        + b"\n"
    )


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_runtime = main.CODEX_MAX_RUNTIME
        self.old_grace = main.CODEX_KILL_GRACE
        self.old_interval = main.CODEX_PROGRESS_UPDATE_INTERVAL
        main.CODEX_MAX_RUNTIME = 0.03
        main.CODEX_KILL_GRACE = 0.01

    async def asyncTearDown(self):
        main.CODEX_MAX_RUNTIME = self.old_runtime
        main.CODEX_KILL_GRACE = self.old_grace
        main.CODEX_PROGRESS_UPDATE_INTERVAL = self.old_interval
        main._active_codex_proc = None
        main._active_codex_started = None
        main._active_codex_started_monotonic = None

    async def _run(self, process):
        with patch.object(
            main.asyncio, "create_subprocess_exec", return_value=process
        ):
            return await main.run_codex(
                "test",
                1,
                ".",
                suppress_progress_messages=True,
                suppress_footer=True,
            )

    async def test_silent_child_hits_absolute_ceiling_without_watchdog(self):
        process = _Process(_Stdout("silent"))
        response, _ = await asyncio.wait_for(self._run(process), 0.3)
        self.assertTrue(process.terminated)
        self.assertIn("Turn stopped", response)

    async def test_streaming_child_hits_same_absolute_ceiling(self):
        process = _Process(_Stdout("stream"))
        response, _ = await asyncio.wait_for(self._run(process), 0.3)
        self.assertTrue(process.terminated)
        self.assertIn("Turn stopped", response)

    async def test_normal_completion_is_not_terminated(self):
        process = _Process(_Stdout("lines", [_agent_message()]), completed=True)
        response, _ = await self._run(process)
        self.assertEqual("done", response)
        self.assertFalse(process.terminated)

    async def test_stream_exception_reaps_live_child(self):
        process = _Process(_Stdout("error"))
        response, _ = await self._run(process)
        self.assertTrue(process.terminated)
        self.assertIsNotNone(process.returncode)
        self.assertIn("Error", response)

    async def test_progress_notifier_stops_without_false_short_turn_update(self):
        sent = []

        async def fake_send(chat_id, message, **kwargs):
            sent.append((chat_id, message))

        main.CODEX_PROGRESS_UPDATE_INTERVAL = 0.03
        stop = asyncio.Event()
        with patch.object(main, "send_message", side_effect=fake_send):
            task = asyncio.create_task(
                main._notify_turn_progress(7, time.monotonic(), stop)
            )
            await asyncio.sleep(0.005)
            stop.set()
            await asyncio.wait_for(task, 0.1)
        self.assertEqual([], sent)

    async def test_progress_notifier_uses_continuing_checkpoint(self):
        sent = []

        async def fake_send(chat_id, message, **kwargs):
            sent.append((chat_id, message))

        main.CODEX_PROGRESS_UPDATE_INTERVAL = 0.01
        stop = asyncio.Event()
        with patch.object(main, "send_message", side_effect=fake_send):
            task = asyncio.create_task(
                main._notify_turn_progress(7, time.monotonic() - 60, stop)
            )
            await asyncio.sleep(0.025)
            stop.set()
            await asyncio.wait_for(task, 0.1)
        self.assertGreaterEqual(len(sent), 2)
        self.assertTrue(all("turn is continuing" in text for _, text in sent))


if __name__ == "__main__":
    unittest.main()
