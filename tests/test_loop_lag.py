import asyncio
import importlib.util
import logging
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.test_detection_cases import DetectionCaseStore
from tests.test_shared_error_configuration import context, shared_reporting


def _load_loop_lag():
    """Load the measurement module without executing the suite package init."""
    name = "NHCogs.loop_lag"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parents[1] / "NHCogs" / "loop_lag.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


loop_lag = _load_loop_lag()

_FRESH_TIMER_PROBE = """
import asyncio
import importlib.util
import sys
from pathlib import Path

path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("NHCogs.loop_lag", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

async def main():
    module._measurement.active = True
    module._install()
    fired = []
    asyncio.get_running_loop().call_later(0, fired.append, "later")
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    if fired != ["later"]:
        raise SystemExit("call_later did not fire: %r" % (fired,))
    print("ok")

asyncio.run(main())
"""

ORIGINAL_HANDLE_RUN = asyncio.events.Handle._run
ORIGINAL_TIMER_RUN = asyncio.events.TimerHandle._run


class Clock:
    def __init__(self):
        self.jump = 0.0
        self.now = 0.0

    def __call__(self):
        current = self.now
        self.now += self.jump
        return current


CLOCK = Clock()


def marker():
    CLOCK.jump = 0.0


def marker_raises():
    CLOCK.jump = 0.0
    raise RuntimeError("marker failed")


class Backup:
    qualified_name = "NHMisc"

    def backup(self):
        CLOCK.jump = 0.0


class Probe:
    qualified_name = "Honeypot"

    async def on_message(self):
        CLOCK.jump = 0.0
        await asyncio.sleep(0)


class CompletedDetection:
    qualified_name = "Honeypot"

    async def on_message(self):
        CLOCK.now += 0.15


class CompletedVoice:
    qualified_name = "NHMisc"

    async def on_message(self):
        CLOCK.now += 0.15


class EventClient:
    async def _run_event(self, coro, event_name, *args, **kwargs):
        try:
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass


class Deadline:
    def __init__(self, value=None):
        self.value = value

    async def __call__(self):
        return self.value

    async def set(self, value):
        self.value = value


class DeadlineConfig:
    def __init__(self, value=None):
        self.loop_lag_until = Deadline(value)


class History:
    def count_observations(self, guild_id):
        if guild_id == 12:
            raise RuntimeError("missing table")
        if guild_id == 10:
            return 4
        return 0

    def cutover_source(self, guild_id):
        return "sqlite"

    def counts(self, guild_id):
        if guild_id == 10:
            return {"verified": 2, "pending_role": 1, "pending_assignment": 0}
        return {"verified": 0, "pending_role": 0, "pending_assignment": 0}


def honeypot_bot():
    honeypot = SimpleNamespace(_case_store=History())
    return SimpleNamespace(
        get_cog=lambda name: honeypot if name == "Honeypot" else None,
        guilds=[SimpleNamespace(id=10), SimpleNamespace(id=12)],
    )


class LoopLagTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        CLOCK.jump = 0.0
        CLOCK.now = 0.0
        self.now = 1_700_000_000.0
        self._perf = loop_lag.perf_counter
        self._wall = loop_lag.wall_time
        loop_lag.perf_counter = CLOCK
        loop_lag.wall_time = lambda: self.now
        loop_lag._measurement.records.clear()
        loop_lag._measurement.sizes_logged = False
        self.logger = logging.getLogger("red.NHCogs.loop_lag")
        self._level = self.logger.level
        self._propagate = self.logger.propagate
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.messages = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.messages.append(record.getMessage())
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self._restore()

    async def asyncTearDown(self):
        loop_lag.pause()
        await asyncio.sleep(0)
        self._restore()

    def _restore(self):
        loop_lag.pause()
        asyncio.events.Handle._run = ORIGINAL_HANDLE_RUN
        asyncio.events.TimerHandle._run = ORIGINAL_TIMER_RUN
        if self._perf is not None:
            loop_lag.perf_counter = self._perf
            loop_lag.wall_time = self._wall
            self._perf = None
        if self.handler is not None:
            self.logger.removeHandler(self.handler)
            self.logger.setLevel(self._level)
            self.logger.propagate = self._propagate
            self.handler = None

    async def _enable(self, bot=None):
        bot = bot if bot is not None else SimpleNamespace(guilds=[])
        config = DeadlineConfig()
        deadline = await loop_lag.enable(bot, config)
        return bot, config, deadline

    async def _run_marked(self, callback, delay):
        loop = asyncio.get_running_loop()

        def arm():
            CLOCK.jump = delay

        loop.call_soon(arm)
        loop.call_soon(callback)
        await asyncio.sleep(0)

    def _lines_for(self, text):
        return [line for line in self.messages if text in line]

    def test_summary_keeps_the_slowest_callbacks(self):
        loop_lag._measurement.active = True
        loop_lag._measurement.active_until = self.now + loop_lag.WINDOW_SECONDS
        try:
            for index in range(6):
                loop_lag._remember(f"handler-{index}", float((index + 1) * 100))
            text = loop_lag.summary_text()
            self.assertIn("handler-5: 1, max 600 ms", text)
            self.assertNotIn("handler-0", text)
            self.assertEqual(text.count("max"), loop_lag.SUMMARY_LIMIT)
            for line in text.splitlines():
                self.assertFalse(line.endswith("."))
        finally:
            loop_lag._measurement.active = False
            loop_lag._measurement.records.clear()

    async def test_slow_callback_is_logged_and_the_threshold_stays_quiet(self):
        await self._enable()
        self.messages.clear()
        await self._run_marked(marker, loop_lag.THRESHOLD_SECONDS)
        self.assertFalse(self._lines_for("marker"))
        await self._run_marked(marker, loop_lag.THRESHOLD_SECONDS + 0.05)
        self.assertEqual(
            self._lines_for("tests.test_loop_lag.marker"),
            ["Loop lag: tests.test_loop_lag.marker took 150 ms"],
        )

    async def test_bound_method_names_its_cog(self):
        await self._enable()
        self.messages.clear()
        await self._run_marked(Backup().backup, 0.15)
        self.assertEqual(
            self._lines_for("backup"),
            ["Loop lag: Backup.backup cog=NHMisc took 150 ms"],
        )

    async def test_task_step_names_the_handler_not_the_scheduler(self):
        await self._enable()
        self.messages.clear()
        loop = asyncio.get_running_loop()

        def arm():
            CLOCK.jump = 0.15
            asyncio.create_task(Probe().on_message(), name="discord.py: on_message")

        loop.call_soon(arm)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        matched = self._lines_for("Probe.on_message")
        self.assertEqual(len(matched), 1)
        self.assertIn("cog=Honeypot", matched[0])
        self.assertIn("task=discord.py: on_message", matched[0])
        self.assertNotIn("sleep", matched[0])
        self.assertIn("took 150 ms", matched[0])

    async def test_completed_listeners_keep_separate_handlers_and_cogs(self):
        await self._enable()
        self.messages.clear()
        client = EventClient()
        tasks = [
            asyncio.create_task(
                client._run_event(cog.on_message, "on_message"), name="discord.py: on_message",
            )
            for cog in (CompletedDetection(), CompletedVoice())
        ]
        await asyncio.gather(*tasks)
        self.assertIn(
            "Loop lag: CompletedDetection.on_message cog=Honeypot "
            "task=discord.py: on_message took 150 ms",
            self.messages,
        )
        self.assertIn(
            "Loop lag: CompletedVoice.on_message cog=NHMisc "
            "task=discord.py: on_message took 150 ms",
            self.messages,
        )
        summary = loop_lag.summary_text()
        self.assertIn("CompletedDetection.on_message cog=Honeypot", summary)
        self.assertIn("CompletedVoice.on_message cog=NHMisc", summary)
        self.assertNotIn("_run_event", summary)

    async def test_unreadable_task_metadata_cannot_prevent_a_callback(self):
        await self._enable()
        called = []

        class Callback:
            @property
            def __self__(self):
                raise RuntimeError("metadata unavailable")

            def __call__(self):
                called.append(True)

        asyncio.get_running_loop().call_soon(Callback())
        await asyncio.sleep(0)
        self.assertEqual(called, [True])

    async def test_completed_task_keeps_its_cog_without_an_event_wrapper(self):
        await self._enable()
        self.messages.clear()
        await asyncio.create_task(CompletedDetection().on_message())
        self.assertEqual(self.messages, [
            "Loop lag: CompletedDetection.on_message cog=Honeypot took 150 ms",
        ])

    async def test_task_label_keeps_the_method_name_and_skips_the_event_wrapper(self):
        async def _run_event():
            await Probe().on_message()

        task = asyncio.create_task(_run_event(), name="discord.py: on_message")
        try:
            await asyncio.sleep(0)
            label = loop_lag._describe_task(task)
            self.assertIn("Probe.on_message", label)
            self.assertIn("cog=Honeypot", label)
            self.assertNotIn("_run_event", label)
            self.assertNotIn("sleep", label)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_timer_callback_is_timed(self):
        await self._enable()
        self.messages.clear()
        loop = asyncio.get_running_loop()
        CLOCK.jump = 0.15
        handle = asyncio.events.TimerHandle(loop.time(), marker, (), loop)
        handle._run()
        self.assertEqual(
            self._lines_for("tests.test_loop_lag.marker"),
            ["Loop lag: tests.test_loop_lag.marker took 150 ms"],
        )

    def test_fresh_interpreter_still_runs_timers_with_the_probe(self):
        path = Path(__file__).resolve().parents[1] / "NHCogs" / "loop_lag.py"
        completed = subprocess.run(
            [sys.executable, "-c", _FRESH_TIMER_PROBE, str(path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")

    async def test_raising_callback_is_recorded_without_escaping(self):
        await self._enable()
        self.messages.clear()
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, _context: None)

        def arm():
            CLOCK.jump = 0.15

        loop.call_soon(arm)
        loop.call_soon(marker_raises)
        await asyncio.sleep(0)
        self.assertEqual(
            self._lines_for("marker_raises"),
            ["Loop lag: tests.test_loop_lag.marker_raises took 150 ms"],
        )

    async def test_disable_restores_the_loop_and_stops_logging(self):
        _bot, config, _deadline = await self._enable()
        self.assertIs(asyncio.events.Handle._run, loop_lag._timed_run)
        self.assertIs(asyncio.events.TimerHandle._run, loop_lag._timed_run)
        await self._run_marked(marker, 0.15)
        await loop_lag.disable(config)
        self.messages.clear()
        await self._run_marked(marker, 0.15)
        self.assertFalse(self.messages)
        self.assertIs(asyncio.events.Handle._run, ORIGINAL_HANDLE_RUN)
        self.assertIs(asyncio.events.TimerHandle._run, ORIGINAL_TIMER_RUN)
        self.assertFalse(loop_lag.active())
        self.assertIsNone(config.loop_lag_until.value)

    async def test_joinwatch_sizes_are_logged_once_until_enabled_again(self):
        bot = honeypot_bot()
        _bot, _config, _deadline = await self._enable(bot)
        self.assertIn(
            "Loop lag sizes: guild 10 verified_members=2 pending_roles=1 "
            "pending_role_assignments=0 join_history_observations=4",
            self.messages,
        )
        self.assertIn(
            "Loop lag sizes: guild 12 verified_members=0 pending_roles=0 "
            "pending_role_assignments=0 join_history_observations=unavailable",
            self.messages,
        )
        self.assertEqual(len(self._lines_for("Loop lag sizes:")), 2)
        self.messages.clear()
        await loop_lag.note_suite_loaded(bot)
        self.assertFalse(self._lines_for("Loop lag sizes:"))
        await loop_lag.enable(bot, DeadlineConfig())
        self.assertEqual(len(self._lines_for("Loop lag sizes:")), 2)

    async def test_joinwatch_sizes_use_real_sqlite_counts_including_an_empty_guild(self):
        with TemporaryDirectory() as directory:
            store = DetectionCaseStore(Path(directory) / "cases.sqlite")
            store.initialize()
            joined = datetime(2026, 10, 8, tzinfo=timezone.utc)
            store.record_first_join(10, 1, joined)
            store.record_first_join(10, 2, joined)
            store.record_first_join(20, 3, joined)
            honeypot = SimpleNamespace(_case_store=store)
            bot = SimpleNamespace(
                get_cog=lambda name: honeypot if name == "Honeypot" else None,
                guilds=[SimpleNamespace(id=10), SimpleNamespace(id=11)],
            )
            with mock.patch.object(store, "all_observations", side_effect=AssertionError):
                await self._enable(bot)
            sizes = self._lines_for("Loop lag sizes:")
            self.assertEqual(sizes, [
                "Loop lag sizes: guild 10 verified_members=0 pending_roles=0 "
                "pending_role_assignments=0 join_history_observations=2",
                "Loop lag sizes: guild 11 verified_members=0 pending_roles=0 "
                "pending_role_assignments=0 join_history_observations=0",
            ])

    async def test_missing_honeypot_is_reported_once(self):
        await self._enable()
        self.assertIn(
            "Loop lag: Honeypot is not loaded, so joinwatch sizes were not read",
            self.messages,
        )
        self.messages.clear()
        await loop_lag.note_suite_loaded(SimpleNamespace(guilds=[]))
        self.assertFalse(self.messages)

    async def test_window_stops_itself_and_a_stale_stop_does_not_clear_a_new_one(self):
        bot = SimpleNamespace(guilds=[])
        _bot, config, first = await self._enable(bot)
        old_generation = loop_lag._measurement.generation
        self.now += 10
        _bot, config, second = await self._enable(bot)
        await loop_lag._wait_and_stop(old_generation, first, config)
        self.assertTrue(loop_lag.active())
        self.assertEqual(config.loop_lag_until.value, second)
        self.now = second
        await loop_lag._wait_and_stop(loop_lag._measurement.generation, second, config)
        self.assertFalse(loop_lag.active())
        self.assertIsNone(config.loop_lag_until.value)
        self.assertIs(asyncio.events.Handle._run, ORIGINAL_HANDLE_RUN)
        self.assertIn("Loop lag measurement stopped", self.messages)

    async def test_command_controls_the_window_without_a_trailing_period(self):
        with shared_reporting() as module:
            ctx, _member = context(module)
            ctx.bot.get_cog = lambda _name: None
            ctx.bot.guilds = []
            support = module.OperationalSupport(ctx.bot)

            with self.assertRaisesRegex(module.commands.UserFeedbackCheckFailure, "Use on or off"):
                await module.OperationalSupport.lag.callback(support, ctx, "later")
            self.assertFalse(loop_lag.active())

            ctx.channel.permissions_for = lambda _role: SimpleNamespace(view_channel=True)
            with self.assertRaisesRegex(
                module.commands.UserFeedbackCheckFailure,
                "Run this command in a private moderator channel",
            ):
                await module.OperationalSupport.lag.callback(support, ctx, "on")
            self.assertFalse(loop_lag.active())

            ctx.channel.permissions_for = lambda role: SimpleNamespace(
                view_channel=role is not ctx.guild.default_role
            )
            await module.OperationalSupport.lag.callback(support, ctx, "on")
            started = ctx.send.await_args.args[0]
            self.assertFalse(started.endswith("."))
            self.assertTrue(started.startswith("Loop lag measurement is on until "))
            self.assertEqual(
                await support.config.loop_lag_until(),
                self.now + loop_lag.WINDOW_SECONDS,
            )

            loop_lag._remember("Detection.on_message cog=Honeypot", 420)
            ctx.send.reset_mock()
            await module.OperationalSupport.lag.callback(support, ctx)
            summary = ctx.send.await_args.args[0]
            self.assertIn("Detection.on_message cog=Honeypot: 1, max 420 ms", summary)
            for line in summary.splitlines():
                self.assertFalse(line.endswith("."))

            ctx.send.reset_mock()
            await module.OperationalSupport.lag.callback(support, ctx, "off")
            self.assertEqual(ctx.send.await_args.args[0], "Loop lag measurement is off")
            self.assertFalse(loop_lag.active())
            self.assertIsNone(await support.config.loop_lag_until())
            self.assertEqual(module.OperationalSupport.lag.parent, module.OperationalSupport.nhcogs)
            self.assertEqual(module.OperationalSupport.lag.usage, "[on|off]")

    async def test_reload_keeps_an_open_window_and_drops_an_expired_one(self):
        with shared_reporting() as module:
            ctx, _member = context(module)
            ctx.bot.get_cog = lambda _name: None
            support = module.OperationalSupport(ctx.bot)
            await support.config.loop_lag_until.set(self.now - 1)
            await support.cog_load()
            self.assertFalse(loop_lag.active())
            self.assertIsNone(await support.config.loop_lag_until())

            deadline = self.now + 3600
            await support.config.loop_lag_until.set(deadline)
            self.messages.clear()
            await support.cog_load()
            self.assertTrue(loop_lag.active())
            self.assertEqual(await support.config.loop_lag_until(), deadline)
            self.assertTrue(any("resumed until" in line for line in self.messages))
            self.assertFalse(self._lines_for("joinwatch sizes"))
            await support.cog_unload()
            self.assertFalse(loop_lag.active())
            self.assertEqual(await support.config.loop_lag_until(), deadline)
            self.assertIs(asyncio.events.Handle._run, ORIGINAL_HANDLE_RUN)
