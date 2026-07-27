import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


PACKAGE_NAME = "nhmisc_forum_autopin_test_package"
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


class FakeTextChannel:
    def __init__(self, channel_id=555):
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.sent = []

    async def send(self, content, allowed_mentions=None):
        self.sent.append(content)
        return types.SimpleNamespace(id=1)


class FakeForumChannel:
    def __init__(self, channel_id, *, guild=None, permissions=None, name="forum"):
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.mention = f"<#{channel_id}>"
        self._permissions = permissions

    def permissions_for(self, target):
        return self._permissions


ALLOWED_MENTIONS_NONE = object()


def load_nhmisc_module():
    discord = types.ModuleType("discord")
    discord.HTTPException = type("HTTPException", (Exception,), {})
    # Mirror the real discord.py hierarchy so except ordering is exercised.
    discord.Forbidden = type("Forbidden", (discord.HTTPException,), {})
    discord.NotFound = type("NotFound", (discord.HTTPException,), {})
    discord.File = object
    discord.AllowedMentions = types.SimpleNamespace(
        none=lambda: ALLOWED_MENTIONS_NONE
    )
    discord.MessageType = types.SimpleNamespace(default=0, reply=1)
    discord.Color = types.SimpleNamespace(
        blue=lambda: 0, green=lambda: 0, orange=lambda: 0, red=lambda: 0
    )
    discord.Embed = object
    discord.TextChannel = FakeTextChannel
    discord.ForumChannel = FakeForumChannel

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
            raise AssertionError("Config should not be constructed in unit tests")

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
        return module, discord
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


nhmisc, discord = load_nhmisc_module()


def make_permissions(
    *, view_channel=True, read_message_history=True, pin_messages=True
):
    return types.SimpleNamespace(
        view_channel=view_channel,
        read_message_history=read_message_history,
        pin_messages=pin_messages,
    )


class FakeConfigValue:
    def __init__(self, store, key):
        self._store = store
        self._key = key

    async def __call__(self):
        return self._store[self._key]

    async def set(self, value):
        self._store[self._key] = value


class FakeGuildConfig:
    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return FakeConfigValue(self._store, name)


class FakeConfigRoot:
    def __init__(self):
        self.stores = {}

    def store_for(self, guild):
        return self.stores.setdefault(
            guild.id, {"forum_autopin_channel_ids": [], "alert_channel": None}
        )

    def guild(self, guild):
        return FakeGuildConfig(self.store_for(guild))


class FakeGuild:
    def __init__(self, guild_id=123):
        self.id = guild_id
        self.me = object()
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


class FakeThread:
    def __init__(self, guild, parent_id, *, thread_id=777, results=()):
        self.id = thread_id
        self.guild = guild
        self.parent_id = parent_id
        self._results = list(results)
        self.fetch_calls = 0

    async def fetch_message(self, message_id):
        self.fetch_calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeMessage:
    def __init__(self, *, pin_error=None):
        self.pin_error = pin_error
        self.pin_reasons = []

    async def pin(self, reason=None):
        self.pin_reasons.append(reason)
        if self.pin_error is not None:
            raise self.pin_error


def make_context(guild, config):
    return types.SimpleNamespace(
        guild=guild,
        author=types.SimpleNamespace(id=999),
        send=mock.AsyncMock(),
    )


class ForumAutopinCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = types.SimpleNamespace(guilds=[], get_channel=lambda _id: None)
        cog.config = FakeConfigRoot()
        cog._forum_autopin_alerted = set()
        return cog

    def test_group_requires_manage_guild_via_decorator(self):
        callback = nhmisc.NHMisc.nhmisc_forumautopin.callback
        self.assertEqual(callback.admin_or_permissions, {"manage_guild": True})

    async def test_add_rejects_forum_without_pin_messages_permission(self):
        cog = self.make_cog()
        guild = FakeGuild()
        forum = FakeForumChannel(
            42, guild=guild, permissions=make_permissions(pin_messages=False)
        )
        ctx = make_context(guild, cog.config)

        with self.assertRaises(UserFeedbackCheckFailure) as caught:
            await nhmisc.NHMisc.nhmisc_forumautopin_add.callback(cog, ctx, forum)

        self.assertIn("Pin Messages", str(caught.exception))
        self.assertEqual(cog.config.store_for(guild)["forum_autopin_channel_ids"], [])

    async def test_add_rejects_forum_without_read_message_history(self):
        cog = self.make_cog()
        guild = FakeGuild()
        forum = FakeForumChannel(
            42, guild=guild, permissions=make_permissions(read_message_history=False)
        )
        ctx = make_context(guild, cog.config)

        with self.assertRaises(UserFeedbackCheckFailure) as caught:
            await nhmisc.NHMisc.nhmisc_forumautopin_add.callback(cog, ctx, forum)

        self.assertIn("Read Message History", str(caught.exception))

    async def test_add_stores_forum_and_is_idempotent(self):
        cog = self.make_cog()
        guild = FakeGuild()
        forum = FakeForumChannel(42, guild=guild, permissions=make_permissions())
        ctx = make_context(guild, cog.config)

        await nhmisc.NHMisc.nhmisc_forumautopin_add.callback(cog, ctx, forum)
        self.assertEqual(
            cog.config.store_for(guild)["forum_autopin_channel_ids"], [42]
        )

        await nhmisc.NHMisc.nhmisc_forumautopin_add.callback(cog, ctx, forum)
        self.assertEqual(
            cog.config.store_for(guild)["forum_autopin_channel_ids"], [42]
        )
        self.assertIn("already enabled", ctx.send.await_args_list[-1].args[0])

    async def test_remove_deletes_only_the_requested_forum(self):
        cog = self.make_cog()
        guild = FakeGuild()
        cog.config.store_for(guild)["forum_autopin_channel_ids"] = [11, 42]
        forum = FakeForumChannel(42, guild=guild, permissions=make_permissions())
        ctx = make_context(guild, cog.config)

        await nhmisc.NHMisc.nhmisc_forumautopin_remove.callback(cog, ctx, forum)

        self.assertEqual(
            cog.config.store_for(guild)["forum_autopin_channel_ids"], [11]
        )

    async def test_remove_reports_forum_that_was_not_configured(self):
        cog = self.make_cog()
        guild = FakeGuild()
        forum = FakeForumChannel(42, guild=guild, permissions=make_permissions())
        ctx = make_context(guild, cog.config)

        await nhmisc.NHMisc.nhmisc_forumautopin_remove.callback(cog, ctx, forum)

        self.assertIn("not enabled", ctx.send.await_args_list[-1].args[0])


class ForumAutopinListenerTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, guild, *, configured=(), alert_channel=None):
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = types.SimpleNamespace(guilds=[], get_channel=lambda _id: None)
        cog.config = FakeConfigRoot()
        cog._forum_autopin_alerted = set()
        store = cog.config.store_for(guild)
        store["forum_autopin_channel_ids"] = list(configured)
        if alert_channel is not None:
            store["alert_channel"] = alert_channel.id
            guild.channels[alert_channel.id] = alert_channel
        return cog

    async def test_unconfigured_forum_is_ignored_without_fetching(self):
        guild = FakeGuild()
        cog = self.make_cog(guild, configured=[99])
        thread = FakeThread(guild, parent_id=42)

        await nhmisc.NHMisc.on_thread_create(cog, thread)

        self.assertEqual(thread.fetch_calls, 0)

    async def test_starter_message_is_pinned_with_audit_reason(self):
        guild = FakeGuild()
        cog = self.make_cog(guild, configured=[42])
        message = FakeMessage()
        thread = FakeThread(guild, parent_id=42, results=[message])

        await nhmisc.NHMisc.on_thread_create(cog, thread)

        self.assertEqual(message.pin_reasons, [nhmisc.FORUM_AUTOPIN_AUDIT_REASON])

    async def test_missing_starter_message_is_retried_once_then_pinned(self):
        guild = FakeGuild()
        cog = self.make_cog(guild, configured=[42])
        message = FakeMessage()
        thread = FakeThread(
            guild, parent_id=42, results=[discord.NotFound(), message]
        )

        with mock.patch.object(nhmisc.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            await nhmisc.NHMisc.on_thread_create(cog, thread)

        sleep.assert_awaited_once_with(nhmisc.FORUM_AUTOPIN_RETRY_SECONDS)
        self.assertEqual(thread.fetch_calls, 2)
        self.assertEqual(message.pin_reasons, [nhmisc.FORUM_AUTOPIN_AUDIT_REASON])

    async def test_starter_message_missing_twice_gives_up(self):
        guild = FakeGuild()
        cog = self.make_cog(guild, configured=[42])
        thread = FakeThread(
            guild, parent_id=42, results=[discord.NotFound(), discord.NotFound()]
        )

        with mock.patch.object(nhmisc.asyncio, "sleep", new=mock.AsyncMock()):
            await nhmisc.NHMisc.on_thread_create(cog, thread)

        self.assertEqual(thread.fetch_calls, 2)

    async def test_forbidden_pin_alerts_once_per_forum(self):
        guild = FakeGuild()
        alert_channel = FakeTextChannel()
        forum = FakeForumChannel(42, guild=guild)
        guild.channels[42] = forum
        cog = self.make_cog(guild, configured=[42], alert_channel=alert_channel)

        for _ in range(3):
            thread = FakeThread(
                guild,
                parent_id=42,
                results=[FakeMessage(pin_error=discord.Forbidden())],
            )
            await nhmisc.NHMisc.on_thread_create(cog, thread)

        self.assertEqual(len(alert_channel.sent), 1)
        self.assertIn("pin messages", alert_channel.sent[0])
        self.assertIn(forum.mention, alert_channel.sent[0])

    async def test_successful_pin_rearms_the_permission_alert(self):
        guild = FakeGuild()
        alert_channel = FakeTextChannel()
        guild.channels[42] = FakeForumChannel(42, guild=guild)
        cog = self.make_cog(guild, configured=[42], alert_channel=alert_channel)

        failing = FakeThread(
            guild, parent_id=42, results=[FakeMessage(pin_error=discord.Forbidden())]
        )
        await nhmisc.NHMisc.on_thread_create(cog, failing)

        recovered = FakeThread(guild, parent_id=42, results=[FakeMessage()])
        await nhmisc.NHMisc.on_thread_create(cog, recovered)
        self.assertEqual(cog._forum_autopin_alerted, set())

        failing_again = FakeThread(
            guild, parent_id=42, results=[FakeMessage(pin_error=discord.Forbidden())]
        )
        await nhmisc.NHMisc.on_thread_create(cog, failing_again)

        self.assertEqual(len(alert_channel.sent), 2)

    async def test_forbidden_fetch_alerts_about_read_permissions(self):
        guild = FakeGuild()
        alert_channel = FakeTextChannel()
        guild.channels[42] = FakeForumChannel(42, guild=guild)
        cog = self.make_cog(guild, configured=[42], alert_channel=alert_channel)
        thread = FakeThread(guild, parent_id=42, results=[discord.Forbidden()])

        await nhmisc.NHMisc.on_thread_create(cog, thread)

        self.assertEqual(len(alert_channel.sent), 1)
        self.assertIn("Read Message History", alert_channel.sent[0])

    async def test_permission_failure_without_alert_channel_is_not_recorded(self):
        guild = FakeGuild()
        cog = self.make_cog(guild, configured=[42])
        thread = FakeThread(
            guild, parent_id=42, results=[FakeMessage(pin_error=discord.Forbidden())]
        )

        await nhmisc.NHMisc.on_thread_create(cog, thread)

        self.assertEqual(cog._forum_autopin_alerted, set())


class ForumAutopinChannelDeleteTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, guild, *, configured=(), alert_channel=None):
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = types.SimpleNamespace(guilds=[], get_channel=lambda _id: None)
        cog.config = FakeConfigRoot()
        cog._forum_autopin_alerted = set()
        store = cog.config.store_for(guild)
        store["forum_autopin_channel_ids"] = list(configured)
        if alert_channel is not None:
            store["alert_channel"] = alert_channel.id
            guild.channels[alert_channel.id] = alert_channel
        return cog

    async def test_deleted_forum_is_dropped_from_configuration(self):
        guild = FakeGuild()
        alert_channel = FakeTextChannel()
        cog = self.make_cog(
            guild, configured=[11, 42], alert_channel=alert_channel
        )
        cog._forum_autopin_alerted.add((guild.id, 42))
        forum = FakeForumChannel(42, guild=guild, name="announcements")

        await nhmisc.NHMisc.on_guild_channel_delete(cog, forum)

        self.assertEqual(
            cog.config.store_for(guild)["forum_autopin_channel_ids"], [11]
        )
        self.assertEqual(cog._forum_autopin_alerted, set())
        self.assertEqual(len(alert_channel.sent), 1)
        self.assertIn("announcements", alert_channel.sent[0])

    async def test_unconfigured_channel_delete_is_ignored(self):
        guild = FakeGuild()
        alert_channel = FakeTextChannel()
        cog = self.make_cog(guild, configured=[11], alert_channel=alert_channel)
        channel = FakeForumChannel(42, guild=guild)

        await nhmisc.NHMisc.on_guild_channel_delete(cog, channel)

        self.assertEqual(
            cog.config.store_for(guild)["forum_autopin_channel_ids"], [11]
        )
        self.assertEqual(alert_channel.sent, [])


if __name__ == "__main__":
    unittest.main()
