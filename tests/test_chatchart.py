import importlib.util
import io
import math
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from matplotlib.figure import Figure
from PIL import Image

PACKAGE_NAME = "nhmisc_chatchart_test_package"
PACKAGE_PATH = Path(__file__).parents[1] / "NHMisc"


class UserFeedbackCheckFailure(Exception):
    pass


class FakeCommand:
    def __init__(self, callback, **attrs):
        self.callback = callback
        self.attrs = attrs

    def command(self, **attrs):
        return lambda callback: FakeCommand(callback, **attrs)

    def group(self, **attrs):
        return lambda callback: FakeCommand(callback, **attrs)


def _tag(name, value=True):
    def decorator(target):
        callback = target.callback if isinstance(target, FakeCommand) else target
        setattr(callback, name, value)
        return target

    return decorator


def _command(**attrs):
    return lambda callback: FakeCommand(callback, **attrs)


class FakeCog:
    @staticmethod
    def listener(event_name=None):
        return _tag("listener_event", event_name)


class FakeFile:
    def __init__(self, fp, *, filename):
        self.filename = filename
        self.data = fp.read()


ALLOWED_MENTIONS_NONE = object()


def load_nhmisc_module():
    discord = types.ModuleType("discord")
    discord.Forbidden = type("Forbidden", (Exception,), {})
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.File = FakeFile
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: ALLOWED_MENTIONS_NONE)
    discord.MessageType = types.SimpleNamespace(default=0, reply=1)
    discord.Color = types.SimpleNamespace(
        blue=lambda: 0, green=lambda: 0, orange=lambda: 0, red=lambda: 0
    )
    discord.Embed = object

    commands = types.ModuleType("redbot.core.commands")
    commands.Cog = FakeCog
    commands.Context = object
    commands.UserFeedbackCheckFailure = UserFeedbackCheckFailure
    commands.BucketType = types.SimpleNamespace(user="user", guild="guild")
    commands.command = _command
    commands.group = _command
    commands.guild_only = lambda: _tag("guild_only")
    commands.admin_or_permissions = lambda **permissions: _tag(
        "admin_or_permissions", permissions
    )
    commands.has_permissions = lambda **permissions: _tag(
        "required_permissions", permissions
    )
    commands.cooldown = lambda rate, per, bucket: _tag(
        "cooldown", (rate, per, bucket)
    )

    class FakeConfig:
        @staticmethod
        def get_conf(*args, **kwargs):
            raise AssertionError("Config should not be constructed in command unit tests")

    redbot = types.ModuleType("redbot")
    core = types.ModuleType("redbot.core")
    core.Config = FakeConfig
    core.commands = commands
    data_manager = types.ModuleType("redbot.core.data_manager")
    data_manager.cog_data_path = lambda cog: Path(".")

    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PACKAGE_PATH)]
    module_names = (
        "discord",
        "redbot",
        "redbot.core",
        "redbot.core.commands",
        "redbot.core.data_manager",
        PACKAGE_NAME,
        f"{PACKAGE_NAME}.nhmisc",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    sys.modules.update(
        {
            "discord": discord,
            "redbot": redbot,
            "redbot.core": core,
            "redbot.core.commands": commands,
            "redbot.core.data_manager": data_manager,
            PACKAGE_NAME: package,
        }
    )
    try:
        qualified_name = f"{PACKAGE_NAME}.nhmisc"
        spec = importlib.util.spec_from_file_location(
            qualified_name, PACKAGE_PATH / "nhmisc.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


nhmisc = load_nhmisc_module()


class FakeContext:
    def __init__(self, guild):
        self.guild = guild
        self.channel = types.SimpleNamespace(id=456, name="test-channel")
        self.sent = []

    async def send(self, content=None, **kwargs):
        if content is not None:
            kwargs["content"] = content
        self.sent.append(kwargs)


class ChatChartCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_renders_wide_hybrid_activity_chart(self):
        guild = types.SimpleNamespace(
            id=123,
            get_member=lambda user_id: types.SimpleNamespace(
                display_name=f"User {user_id}"
            ),
        )
        ctx = FakeContext(guild)
        counts = [
            types.SimpleNamespace(user_id=index, message_count=10_000 - index * 400)
            for index in range(1, 13)
        ]
        cog = object.__new__(nhmisc.NHMisc)
        cog._require_activity_staff = mock.AsyncMock()
        cog._close_stale_activity_days_for_guild = mock.AsyncMock()
        cog._cap_detail_days = mock.AsyncMock(return_value=31)
        cog._activity_parent_channel_id = lambda channel: channel.id
        cog._activity_thread_id = lambda channel: None
        cog._utc_today = lambda: None
        cog._activity_store = types.SimpleNamespace(
            get_channel_user_counts=mock.AsyncMock(return_value=counts)
        )
        captured_figures = []
        original_savefig = Figure.savefig

        def savefig_and_capture(figure, *args, **kwargs):
            captured_figures.append(figure)
            return original_savefig(figure, *args, **kwargs)

        with mock.patch.object(Figure, "savefig", savefig_and_capture):
            await nhmisc.NHMisc.nhmisc_chatchart.callback(cog, ctx, 31)

        attachment = ctx.sent[0]["file"]
        with Image.open(io.BytesIO(attachment.data)) as image:
            width, height = image.size

        self.assertEqual(attachment.filename, "chatchart.png")
        self.assertGreaterEqual(width, 1_700)
        self.assertGreater(width / height, 1.6)

        figure_text = {text.get_text() for text in captured_figures[0].texts}
        self.assertIn("#test-channel", figure_text)
        self.assertIn("Messages by user - last 31 days", figure_text)
        channel_label = next(
            text
            for text in captured_figures[0].texts
            if text.get_text() == "#test-channel"
        )
        self.assertEqual(channel_label.get_horizontalalignment(), "left")
        self.assertLess(channel_label.get_position()[0], 0.1)
        self.assertGreater(channel_label.get_position()[1], 0.9)

        named = len(nhmisc.CHATCHART_SERIES_COLORS)
        ranking_axis, donut_axis = captured_figures[0].axes
        self.assertEqual(
            [bar.get_width() for bar in ranking_axis.patches],
            [count.message_count for count in counts[:named]],
        )
        # Percentages stay relative to every user, not just the ranked ones.
        ranking_annotations = {text.get_text() for text in ranking_axis.texts}
        self.assertIn("9,600 · 10.8%", ranking_annotations)
        self.assertIn("6,800 · 7.7%", ranking_annotations)

        # One distinct hue per ranked user, so no bar shares the neutral tone.
        ranking_colors = [bar.get_facecolor() for bar in ranking_axis.patches]
        self.assertEqual(len(set(ranking_colors)), named)

        # Every hue slot plus exactly one neutral wedge for everyone else.
        self.assertEqual(len(donut_axis.patches), named + 1)
        donut_colors = [wedge.get_facecolor() for wedge in donut_axis.patches]
        self.assertEqual(donut_colors[:named], ranking_colors)
        self.assertNotIn(donut_colors[named], ranking_colors)

        donut_text = {text.get_text() for text in donut_axis.texts if text.get_text()}
        self.assertIn("88,800\nmessages", donut_text)
        self.assertIn("Other", donut_text)
        percentage_labels = [
            text for text in donut_axis.texts if text.get_text().endswith("%")
        ]
        self.assertTrue(percentage_labels)
        for label in percentage_labels:
            self.assertAlmostEqual(math.hypot(*label.get_position()), 0.79, places=2)
        self.assertIsNone(donut_axis.get_legend())

    def test_location_label_names_channels_threads_and_missing_parents(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog._activity_thread_id = lambda channel: getattr(channel, "thread_id", None)

        channel = types.SimpleNamespace(name="general", thread_id=None)
        self.assertEqual(cog._chatchart_location_label(channel), "#general")

        thread = types.SimpleNamespace(
            name="side-quest",
            thread_id=99,
            parent=types.SimpleNamespace(name="general"),
        )
        self.assertEqual(
            cog._chatchart_location_label(thread), "#general / side-quest"
        )

        orphan_thread = types.SimpleNamespace(
            name="side-quest", thread_id=99, parent=None
        )
        self.assertEqual(cog._chatchart_location_label(orphan_thread), "side-quest")

        unnamed = types.SimpleNamespace(thread_id=None)
        self.assertEqual(cog._chatchart_location_label(unnamed), "#unknown-channel")


class YapperCommandTests(unittest.IsolatedAsyncioTestCase):
    def build_fixture(self, counts):
        members = {
            10: types.SimpleNamespace(display_name="Alpha"),
            20: types.SimpleNamespace(display_name="Beta"),
        }
        guild = types.SimpleNamespace(
            id=123,
            get_member=members.get,
        )
        ctx = FakeContext(guild)
        store = types.SimpleNamespace(
            get_guild_user_counts=mock.AsyncMock(return_value=counts),
            get_channel_user_counts=mock.AsyncMock(return_value=counts),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._require_activity_staff = mock.AsyncMock()
        cog._close_stale_activity_days_for_guild = mock.AsyncMock()
        cog._cap_detail_days = mock.AsyncMock(return_value=7)
        cog._activity_parent_channel_id = lambda channel: channel.id
        cog._activity_thread_id = lambda channel: 789
        cog._utc_today = lambda: date(2026, 7, 27)
        cog._activity_store = store
        return cog, ctx, store

    async def test_topyapper_returns_requested_guild_ranking_without_mentions(self):
        counts = [
            types.SimpleNamespace(user_id=10, message_count=120),
            types.SimpleNamespace(user_id=20, message_count=80),
        ]
        cog, ctx, store = self.build_fixture(counts)

        await nhmisc.NHMisc.nhmisc_topyapper.callback(cog, ctx, 30, 2)

        store.get_guild_user_counts.assert_awaited_once_with(
            123, date(2026, 7, 27), 7, 2
        )
        cog._require_activity_staff.assert_awaited_once_with(ctx)
        content = ctx.sent[0]["content"]
        self.assertIn("1. Alpha (10) — 120 messages", content)
        self.assertIn("2. Beta (20) — 80 messages", content)
        self.assertIs(ctx.sent[0]["allowed_mentions"], ALLOWED_MENTIONS_NONE)

    async def test_channelyapper_uses_current_thread_and_limits_output(self):
        counts = [
            types.SimpleNamespace(user_id=10, message_count=120),
            types.SimpleNamespace(user_id=20, message_count=80),
            types.SimpleNamespace(user_id=30, message_count=40),
        ]
        cog, ctx, store = self.build_fixture(counts)

        await nhmisc.NHMisc.nhmisc_channelyapper.callback(cog, ctx, 30, 2)

        store.get_channel_user_counts.assert_awaited_once_with(
            123, 456, 789, date(2026, 7, 27), 7
        )
        content = ctx.sent[0]["content"]
        self.assertIn("1. Alpha (10) — 120 messages", content)
        self.assertIn("2. Beta (20) — 80 messages", content)
        self.assertNotIn("30", content)
        self.assertIs(ctx.sent[0]["allowed_mentions"], ALLOWED_MENTIONS_NONE)

    async def test_yapper_commands_reject_invalid_days_and_amount(self):
        commands_to_test = (
            nhmisc.NHMisc.nhmisc_topyapper,
            nhmisc.NHMisc.nhmisc_channelyapper,
        )
        for command in commands_to_test:
            for days, amount in ((0, 1), (1, 0), (1, 21)):
                with self.subTest(command=command, days=days, amount=amount):
                    cog, ctx, _ = self.build_fixture([])
                    with self.assertRaises(UserFeedbackCheckFailure):
                        await command.callback(cog, ctx, days, amount)

    async def test_yapper_commands_report_when_no_activity_is_retained(self):
        commands_to_test = (
            (nhmisc.NHMisc.nhmisc_topyapper, "server"),
            (nhmisc.NHMisc.nhmisc_channelyapper, "channel"),
        )
        for command, scope in commands_to_test:
            with self.subTest(command=command):
                cog, ctx, _ = self.build_fixture([])

                await command.callback(cog, ctx, 7, 10)

                self.assertIn(
                    f"No retained activity data for this {scope}",
                    ctx.sent[0]["content"],
                )
                self.assertIs(
                    ctx.sent[0]["allowed_mentions"], ALLOWED_MENTIONS_NONE
                )


if __name__ == "__main__":
    unittest.main()
