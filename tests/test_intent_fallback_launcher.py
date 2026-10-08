from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import run_red_with_intent_fallback as launcher


class LauncherTests(unittest.TestCase):
    def test_confirmed_intent_denial_retries_once_and_preserves_original_disabled_intents(self):
        outcomes = iter((launcher.ChildResult(78, True), launcher.ChildResult(0)))
        commands, environments, delays = [], [], []

        def run(command, environment, _marker, restore):
            commands.append(command)
            environments.append(environment)
            self.assertEqual(restore, len(commands) == 2)
            return next(outcomes)

        args = ["instance", "--no-prompt", "--disable-intent=presences"]
        with tempfile.TemporaryDirectory() as directory:
            code = launcher.supervise(args, Path(directory) / "marker", runner=run,
                                      sleeper=delays.append)
        self.assertEqual(code, 0)
        self.assertEqual(commands[0], [sys.executable, "-m", "redbot", *args])
        self.assertEqual(commands[1][-4:], ["--disable-intent", "members",
                                          "--disable-intent", "message_content"])
        self.assertEqual(environments[1][launcher.RECOVERY_INTENTS_ENV], "members,message_content")
        self.assertEqual(delays, [launcher.MIN_RESTART_DELAY])

    def test_unrelated_error_or_success_never_triggers_fallback(self):
        for result in (launcher.ChildResult(78), launcher.ChildResult(0, True)):
            runner = mock.Mock(return_value=result)
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(launcher.supervise(["instance"], Path(directory) / "marker",
                                                    runner=runner), result.returncode)
            runner.assert_called_once()

    def test_red_error_output_drives_real_monitor_to_one_degraded_restart(self):
        denied = SimpleNamespace(
            returncode=78, poll=mock.Mock(return_value=78),
            stdout=io.BytesIO(b"Red requires all Privileged Intents to be enabled.\n"),
        )
        finished = SimpleNamespace(returncode=0, poll=mock.Mock(return_value=0),
                                   stdout=io.BytesIO())
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(launcher.subprocess, "Popen", side_effect=(denied, finished)) as popen, \
                    mock.patch.object(launcher.sys, "stdout", SimpleNamespace(
                        buffer=io.BytesIO(), write=lambda _text: None, flush=lambda: None,
                    )):
                code = launcher.supervise(
                    ["instance"], Path(directory) / "marker", runner=launcher.run_child,
                    sleeper=lambda _delay: None,
                )
        self.assertEqual(code, 0)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args_list[0].args[0], [sys.executable, "-m", "redbot", "instance"])
        self.assertEqual(popen.call_args_list[1].args[0].count("--disable-intent"), 3)

    def test_degraded_failure_is_not_retried(self):
        runner = mock.Mock(side_effect=(launcher.ChildResult(78, True),
                                        launcher.ChildResult(78, True)))
        with tempfile.TemporaryDirectory() as directory:
            code = launcher.supervise(["instance"], Path(directory) / "marker",
                                      runner=runner, sleeper=lambda _delay: None)
        self.assertEqual(code, 78)
        self.assertEqual(runner.call_count, 2)

    def test_approval_restores_original_arguments_with_backoff(self):
        args = ["instance", "--disable-intent", "members"]
        outcomes = iter((launcher.ChildResult(78, True),
                         launcher.ChildResult(0, restore_requested=True),
                         launcher.ChildResult(0)))
        commands, delays = [], []

        def run(command, environment, _marker, _restore):
            commands.append(command)
            self.assertEqual(environment[launcher.RECOVERY_INTENTS_ENV], "presences,message_content")
            return next(outcomes)

        with tempfile.TemporaryDirectory() as directory:
            launcher.supervise(args, Path(directory) / "marker", runner=run, sleeper=delays.append)
        self.assertEqual(commands[0], commands[2])
        self.assertNotEqual(commands[0], commands[1])
        self.assertEqual(delays, [launcher.MIN_RESTART_DELAY, 2 * launcher.MIN_RESTART_DELAY])

    def test_manual_degraded_and_disabled_auto_restore_are_explicit(self):
        runner = mock.Mock(return_value=launcher.ChildResult(0))
        with tempfile.TemporaryDirectory() as directory:
            launcher.supervise(["instance"], Path(directory) / "marker", start_degraded=True,
                               auto_restore=False, runner=runner)
        command, _env, _marker, restore = runner.call_args.args
        self.assertFalse(restore)
        self.assertEqual(command.count("--disable-intent"), 3)

    def test_output_detection_handles_chunks_and_does_not_misread_arbitrary_4014(self):
        for data, expected in (
            (b"Red requires all Privileged Intents to be enabled." + b"x" * 4096, True),
            (b"discord.errors.PrivilegedIntentsRequired", True),
            (b"gateway close code: 4014", True),
            (b"Processing guild 4014 failed due to invalid config", False),
        ):
            with self.subTest(data=data[:50]):
                denied = threading.Event()
                output = io.BytesIO()
                with mock.patch.object(launcher.sys, "stdout", SimpleNamespace(buffer=output)):
                    launcher._forward_output(io.BytesIO(data), denied)
                self.assertEqual(denied.is_set(), expected)
                self.assertEqual(output.getvalue(), data)

    def test_child_watches_marker_without_waiting_for_output_and_exits_before_restore(self):
        events = []
        process = SimpleNamespace(
            returncode=None, stdout=io.BytesIO(), poll=mock.Mock(return_value=None),
            terminate=mock.Mock(), kill=mock.Mock(),
        )

        def wait(**_kwargs):
            events.append("old child exited")
            process.returncode = 0
            process.poll.return_value = 0
            return 0

        process.wait = mock.Mock(side_effect=wait)
        process.send_signal = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            marker.touch()
            with mock.patch.object(launcher.subprocess, "Popen", return_value=process), \
                    mock.patch.object(launcher.sys, "stdout", SimpleNamespace(buffer=io.BytesIO())):
                result = launcher.run_child(["unused"], {}, marker, True)
            self.assertFalse(marker.exists())
        self.assertTrue(result.restore_requested)
        self.assertEqual(events, ["old child exited"])

    def test_keyboard_interrupt_stops_and_reaps_child(self):
        process = SimpleNamespace(
            returncode=None, stdout=io.BytesIO(), poll=mock.Mock(return_value=None),
            terminate=mock.Mock(), send_signal=mock.Mock(), kill=mock.Mock(),
        )

        def wait(**_kwargs):
            process.poll.return_value = 0
            process.returncode = 0
            return 0

        process.wait = mock.Mock(side_effect=wait)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(launcher.subprocess, "Popen", return_value=process), \
                    mock.patch.object(launcher.sys, "stdout", SimpleNamespace(buffer=io.BytesIO())), \
                    mock.patch.object(launcher.time, "sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    launcher.run_child(["unused"], {}, Path(directory) / "marker", True)
        process.wait.assert_called_once()
        if launcher.os.name == "nt":
            process.send_signal.assert_called_once()
        else:
            process.terminate.assert_called_once()

    def test_shutdown_timeout_kills_and_reaps_child(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = (subprocess.TimeoutExpired("unused", 20), 0)
        launcher.stop_child(process)
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
