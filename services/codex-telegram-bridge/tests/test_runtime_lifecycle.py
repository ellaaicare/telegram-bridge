import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class RuntimeConfigurationTests(unittest.TestCase):
    def test_runtime_and_kill_grace_defaults_are_safely_bounded(self):
        self.assertGreaterEqual(main.CODEX_MAX_RUNTIME, 60)
        self.assertGreaterEqual(main.CODEX_KILL_GRACE, 1)
        self.assertLessEqual(main.CODEX_KILL_GRACE, 60)

    def test_environment_values_are_clamped_and_invalid_values_use_default(self):
        with patch.dict(os.environ, {"LIMIT": "999999999"}):
            self.assertEqual(100, main._bounded_int_env("LIMIT", 50, 1, 100))
        with patch.dict(os.environ, {"LIMIT": "-50"}):
            self.assertEqual(1, main._bounded_int_env("LIMIT", 50, 1, 100))
        with patch.dict(os.environ, {"LIMIT": "invalid"}):
            self.assertEqual(50, main._bounded_int_env("LIMIT", 50, 1, 100))


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
        if self.mode == "delayed":
            await asyncio.sleep(0.02)
            self.mode = "lines"
        if self.lines:
            return self.lines.pop(0)
        return b""


class _Process:
    _next_pid = 41000

    def __init__(
        self,
        stdout,
        *,
        completed=False,
        ignore_term=False,
        ignore_kill=False,
        lookup_on_kill=False,
    ):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.stdout = stdout
        self.stderr = _Stderr()
        self.returncode = 0 if completed else None
        self.terminated = False
        self.killed = False
        self.ignore_term = ignore_term
        self.ignore_kill = ignore_kill
        self.lookup_on_kill = lookup_on_kill
        self._done = asyncio.Event()
        if completed:
            self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.ignore_term:
            return
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.killed = True
        if self.lookup_on_kill:
            self.returncode = -9
            self._done.set()
            raise ProcessLookupError
        if self.ignore_kill:
            return
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
        self.old_notifier_stop_timeout = main.PROGRESS_NOTIFIER_STOP_TIMEOUT
        main.CODEX_MAX_RUNTIME = 0.03
        main.CODEX_KILL_GRACE = 0.01

    async def asyncTearDown(self):
        main.CODEX_MAX_RUNTIME = self.old_runtime
        main.CODEX_KILL_GRACE = self.old_grace
        main.CODEX_PROGRESS_UPDATE_INTERVAL = self.old_interval
        main.PROGRESS_NOTIFIER_STOP_TIMEOUT = self.old_notifier_stop_timeout
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

    async def test_run_cancellation_reaps_child_before_releasing_ownership(self):
        main.CODEX_MAX_RUNTIME = 10
        process = _Process(_Stdout("silent"))
        task = asyncio.create_task(self._run(process))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.2)
        self.assertTrue(process.terminated)
        self.assertIsNotNone(process.returncode)
        self.assertIsNone(main._active_codex_proc)

    async def test_sigterm_timeout_escalates_to_sigkill_and_reaps(self):
        process = _Process(_Stdout("silent"), ignore_term=True)
        await main._terminate_child(process, "test")
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertIsNotNone(process.returncode)

    async def test_unreaped_sigkill_is_reported_not_swallowed(self):
        process = _Process(
            _Stdout("silent"), ignore_term=True, ignore_kill=True
        )
        with self.assertRaisesRegex(RuntimeError, "unreaped_after_sigkill"):
            await main._terminate_child(process, "test")
        self.assertIsNone(process.returncode)

    async def test_sigkill_process_lookup_race_is_still_reaped(self):
        process = _Process(
            _Stdout("silent"), ignore_term=True, lookup_on_kill=True
        )
        await main._terminate_child(process, "test")
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertIsNotNone(process.returncode)

    async def test_unreaped_child_keeps_bridge_ownership(self):
        process = _Process(
            _Stdout("error"), ignore_term=True, ignore_kill=True
        )
        with self.assertRaisesRegex(RuntimeError, "unreaped_after_sigkill"):
            await self._run(process)
        self.assertIs(main._active_codex_proc, process)
        self.assertIsNone(process.returncode)

    async def test_suppressed_run_does_not_create_progress_notifier(self):
        process = _Process(_Stdout("lines", [_agent_message()]), completed=True)
        with patch.object(
            main, "_notify_turn_progress", new=AsyncMock()
        ) as notifier:
            response, _ = await self._run(process)
        self.assertEqual("done", response)
        notifier.assert_not_awaited()

    async def test_blocked_notifier_cannot_delay_completed_turn(self):
        main.CODEX_PROGRESS_UPDATE_INTERVAL = 0.005
        process = _Process(
            _Stdout("delayed", [_agent_message()]), completed=True
        )

        async def blocked_send(*args, **kwargs):
            await asyncio.Future()

        with patch.object(main, "send_message", side_effect=blocked_send), patch.object(
            main.asyncio, "create_subprocess_exec", return_value=process
        ):
            response, _ = await asyncio.wait_for(
                main.run_codex(
                    "test", 1, ".", suppress_footer=True
                ),
                0.2,
            )
        self.assertEqual("done", response)
        self.assertIsNone(main._active_codex_proc)

    async def test_cancellation_resistant_notifier_cannot_delay_finalizer(self):
        main.PROGRESS_NOTIFIER_STOP_TIMEOUT = 0.01
        release = asyncio.Event()

        async def cancellation_resistant_notifier():
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        notifier = asyncio.create_task(cancellation_resistant_notifier())
        await asyncio.sleep(0)
        process = _Process(_Stdout("lines"), completed=True)
        started = time.monotonic()
        await asyncio.wait_for(
            main._finalize_codex_child(process, asyncio.Event(), notifier), 0.1
        )
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertFalse(notifier.done())

        release.set()
        await asyncio.wait_for(notifier, 0.1)

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
