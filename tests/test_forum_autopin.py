import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT_PACKAGE_NAME = "nhmisc_forum_autopin_test_root"
PACKAGE_NAME = f"{ROOT_PACKAGE_NAME}.nhmisc"
ROOT_PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs"
PACKAGE_PATH = ROOT_PACKAGE_PATH / "nhmisc"


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
    def __init__(self, channel_id=555, *, public=False):
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.public = public
        self.sent = []
        self.allowed_mentions = []

    def permissions_for(self, _target):
        return types.SimpleNamespace(view_channel=self.public)

    async def send(self, content, allowed_mentions=None):
        self.sent.append(content)
        self.allowed_mentions.append(allowed_mentions)
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


def load_nhmisc_modules():
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
    commands.mod_or_permissions = lambda **permissions: _tag(
        "mod_or_permissions", permissions
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

    root_package = types.ModuleType(ROOT_PACKAGE_NAME)
    root_package.__path__ = [str(ROOT_PACKAGE_PATH)]
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PACKAGE_PATH)]
    module_names = (
        "discord",
        "redbot",
        "redbot.core",
        "redbot.core.commands",
        "redbot.core.data_manager",
        ROOT_PACKAGE_NAME,
        PACKAGE_NAME,
        f"{PACKAGE_NAME}.nhmisc",
        f"{PACKAGE_NAME}.forum_autopin",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    sys.modules.update(
        {
            "discord": discord,
            "redbot": redbot,
            "redbot.core": core,
            "redbot.core.commands": commands,
            "redbot.core.data_manager": data_manager,
            ROOT_PACKAGE_NAME: root_package,
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
        return module, sys.modules[f"{PACKAGE_NAME}.forum_autopin"], discord
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


nhmisc, forum_autopin, discord = load_nhmisc_modules()


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

    async def clear(self):
        self._store[self._key] = None


class FakeGuildConfig:
    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return FakeConfigValue(self._store, name)


class FakeConfigRoot:
    def __init__(self):
        self.stores = {}
        self.defaults = {"forum_autopin_channel_ids": [], "alert_channel": None}

    def register_guild(self, **defaults):
        self.defaults.update(defaults)

    def store_for(self, guild):
        return self.stores.setdefault(
            guild.id, self.defaults.copy()
        )

    def guild(self, guild):
        return FakeGuildConfig(self.store_for(guild))

    def guild_from_id(self, guild_id):
        return self.guild(types.SimpleNamespace(id=guild_id))

    async def all_guilds(self):
        return self.stores


def make_support(
    bot, config, data_path=Path("unused-operational-test-data"), *, module=nhmisc, error_config=None
):
    namespace = module.OperationalSupport.__init__.__globals__
    error_config = error_config if error_config is not None else FakeConfigRoot()
    with (
        mock.patch.object(
            namespace["Config"], "get_conf",
            side_effect=lambda *_args, **kwargs: (
                config if kwargs.get("cog_name") == "NHMisc" else error_config
            ),
        ),
        mock.patch.dict(namespace, {"cog_data_path": lambda **_kwargs: data_path}),
        mock.patch.object(
            config, "register_guild", getattr(config, "register_guild", mock.Mock()), create=True
        ),
    ):
        return module.OperationalSupport(bot)


class AlertRecorder:
    """Stands in for the cog's alert-channel transport."""

    def __init__(self, *, delivered=True):
        self.messages = []
        self.delivered = delivered

    async def __call__(self, guild, content):
        self.messages.append(content)
        return self.delivered


class FakeGuild:
    def __init__(self, guild_id=123):
        self.id = guild_id
        self.me = object()
        self.default_role = object()
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


def make_service(*, alerts=None):
    config = FakeConfigRoot()
    alerts = alerts if alerts is not None else AlertRecorder()
    service = forum_autopin.ForumAutopinService(config, alert_sender=alerts)
    return service, config, alerts


def make_context(guild):
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
        cog._support = make_support(cog.bot, cog.config)
        cog._forum_autopin = forum_autopin.ForumAutopinService(
            cog.config, alert_sender=AlertRecorder()
        )
        return cog

    def test_group_requires_manage_messages_via_decorator(self):
        callback = nhmisc.NHMisc.nhmisc_forumautopin.callback
        self.assertEqual(callback.required_permissions, {"manage_messages": True})

    async def test_add_rejects_forum_without_pin_messages_permission(self):
        cog = self.make_cog()
        guild = FakeGuild()
        forum = FakeForumChannel(
            42, guild=guild, permissions=make_permissions(pin_messages=False)
        )
        ctx = make_context(guild)

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
        ctx = make_context(guild)

        with self.assertRaises(UserFeedbackCheckFailure) as caught:
            await nhmisc.NHMisc.nhmisc_forumautopin_add.callback(cog, ctx, forum)

        self.assertIn("Read Message History", str(caught.exception))

    async def test_add_stores_forum_and_is_idempotent(self):
        cog = self.make_cog()
        guild = FakeGuild()
        forum = FakeForumChannel(42, guild=guild, permissions=make_permissions())
        ctx = make_context(guild)

        await nhmisc.NHMisc.nhmisc_forumautopin_add.callback(cog, ctx, forum)
        self.assertEqual(
            cog.config.store_for(guild)["forum_autopin_channel_ids"], [42]
        )
        self.assertIn("is now enabled", ctx.send.await_args_list[-1].args[0])

        await nhmisc.NHMisc.nhmisc_forumautopin_add.callback(cog, ctx, forum)
        self.assertEqual(
            cog.config.store_for(guild)["forum_autopin_channel_ids"], [42]
        )
        self.assertIn("is already enabled", ctx.send.await_args_list[-1].args[0])

    async def test_remove_deletes_only_the_requested_forum(self):
        cog = self.make_cog()
        guild = FakeGuild()
        cog.config.store_for(guild)["forum_autopin_channel_ids"] = [11, 42]
        forum = FakeForumChannel(42, guild=guild, permissions=make_permissions())
        ctx = make_context(guild)

        await nhmisc.NHMisc.nhmisc_forumautopin_remove.callback(cog, ctx, forum)

        self.assertEqual(
            cog.config.store_for(guild)["forum_autopin_channel_ids"], [11]
        )
        self.assertIn("is disabled", ctx.send.await_args_list[-1].args[0])

    async def test_remove_reports_forum_that_was_not_configured(self):
        cog = self.make_cog()
        guild = FakeGuild()
        forum = FakeForumChannel(42, guild=guild, permissions=make_permissions())
        ctx = make_context(guild)

        await nhmisc.NHMisc.nhmisc_forumautopin_remove.callback(cog, ctx, forum)

        self.assertIn("is not enabled", ctx.send.await_args_list[-1].args[0])

    async def test_send_guild_alert_reports_missing_alert_channel(self):
        cog = self.make_cog()
        guild = FakeGuild()

        self.assertFalse(await cog._send_guild_alert(guild, "anything"))

        alert_channel = FakeTextChannel()
        guild.channels[alert_channel.id] = alert_channel
        cog.config.store_for(guild)["alert_channel"] = alert_channel.id

        self.assertTrue(await cog._send_guild_alert(guild, "delivered"))
        self.assertEqual(alert_channel.sent, ["delivered"])

    async def test_moderation_log_disables_all_mentions(self):
        cog = self.make_cog()
        guild = FakeGuild()
        channel = FakeTextChannel()
        guild.channels[channel.id] = channel
        cog.config.store_for(guild)["moderation_log_channel"] = channel.id

        self.assertTrue(
            await cog._send_moderation_log(
                guild,
                "Moderator: <@42>; Role: <@&123>",
            )
        )

        allowed_mentions = channel.allowed_mentions[-1]
        self.assertIs(allowed_mentions, ALLOWED_MENTIONS_NONE)


class ForumAutopinServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_forum_is_ignored_without_fetching(self):
        service, config, _ = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [99]
        thread = FakeThread(guild, parent_id=42)

        await service.handle_thread_create(thread)

        self.assertEqual(thread.fetch_calls, 0)

    async def test_starter_message_is_pinned_with_audit_reason(self):
        service, config, _ = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42]
        message = FakeMessage()
        thread = FakeThread(guild, parent_id=42, results=[message])

        await service.handle_thread_create(thread)

        self.assertEqual(message.pin_reasons, [forum_autopin.AUDIT_REASON])

    async def test_missing_starter_message_is_retried_once_then_pinned(self):
        service, config, _ = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42]
        message = FakeMessage()
        thread = FakeThread(
            guild, parent_id=42, results=[discord.NotFound(), message]
        )

        with mock.patch.object(
            forum_autopin.asyncio, "sleep", new=mock.AsyncMock()
        ) as sleep:
            await service.handle_thread_create(thread)

        sleep.assert_awaited_once_with(forum_autopin.RETRY_SECONDS)
        self.assertEqual(thread.fetch_calls, 2)
        self.assertEqual(message.pin_reasons, [forum_autopin.AUDIT_REASON])

    async def test_starter_message_missing_twice_gives_up(self):
        service, config, alerts = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42]
        thread = FakeThread(
            guild, parent_id=42, results=[discord.NotFound(), discord.NotFound()]
        )

        with mock.patch.object(forum_autopin.asyncio, "sleep", new=mock.AsyncMock()):
            await service.handle_thread_create(thread)

        self.assertEqual(thread.fetch_calls, 2)
        self.assertEqual(alerts.messages, [])

    async def test_forbidden_pin_alerts_once_per_forum(self):
        service, config, alerts = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42]
        forum = FakeForumChannel(42, guild=guild)
        guild.channels[42] = forum

        for _ in range(3):
            thread = FakeThread(
                guild,
                parent_id=42,
                results=[FakeMessage(pin_error=discord.Forbidden())],
            )
            await service.handle_thread_create(thread)

        self.assertEqual(len(alerts.messages), 1)
        self.assertIn("pin messages", alerts.messages[0])
        self.assertIn(forum.mention, alerts.messages[0])

    async def test_successful_pin_rearms_the_permission_alert(self):
        service, config, alerts = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42]
        guild.channels[42] = FakeForumChannel(42, guild=guild)

        await service.handle_thread_create(
            FakeThread(
                guild,
                parent_id=42,
                results=[FakeMessage(pin_error=discord.Forbidden())],
            )
        )
        await service.handle_thread_create(
            FakeThread(guild, parent_id=42, results=[FakeMessage()])
        )
        await service.handle_thread_create(
            FakeThread(
                guild,
                parent_id=42,
                results=[FakeMessage(pin_error=discord.Forbidden())],
            )
        )

        self.assertEqual(len(alerts.messages), 2)

    async def test_forbidden_fetch_alerts_about_read_permissions(self):
        service, config, alerts = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42]
        guild.channels[42] = FakeForumChannel(42, guild=guild)
        thread = FakeThread(guild, parent_id=42, results=[discord.Forbidden()])

        await service.handle_thread_create(thread)

        self.assertEqual(len(alerts.messages), 1)
        self.assertIn("Read Message History", alerts.messages[0])

    async def test_undelivered_alert_is_retried_on_the_next_failure(self):
        alerts = AlertRecorder(delivered=False)
        service, config, _ = make_service(alerts=alerts)
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42]

        for _ in range(2):
            await service.handle_thread_create(
                FakeThread(
                    guild,
                    parent_id=42,
                    results=[FakeMessage(pin_error=discord.Forbidden())],
                )
            )

        self.assertEqual(len(alerts.messages), 2)

    async def test_unknown_forum_is_labelled_by_id_in_the_alert(self):
        service, config, alerts = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42]
        thread = FakeThread(guild, parent_id=42, results=[discord.Forbidden()])

        await service.handle_thread_create(thread)

        self.assertIn("`42`", alerts.messages[0])

    async def test_get_forum_ids_is_sorted_and_deduplicated(self):
        service, config, _ = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42, 11, 42]

        self.assertEqual(await service.get_forum_ids(guild), [11, 42])


class ForumAutopinChannelDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_forum_is_dropped_from_configuration(self):
        service, config, alerts = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [11, 42]
        forum = FakeForumChannel(42, guild=guild, name="announcements")

        self.assertTrue(await service.handle_channel_delete(forum))

        self.assertEqual(config.store_for(guild)["forum_autopin_channel_ids"], [11])
        self.assertEqual(len(alerts.messages), 1)
        self.assertIn("announcements", alerts.messages[0])

    async def test_unconfigured_channel_delete_is_ignored(self):
        service, config, alerts = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [11]
        channel = FakeForumChannel(42, guild=guild)

        self.assertFalse(await service.handle_channel_delete(channel))

        self.assertEqual(config.store_for(guild)["forum_autopin_channel_ids"], [11])
        self.assertEqual(alerts.messages, [])

    async def test_deleting_a_forum_clears_its_pending_alert_state(self):
        service, config, alerts = make_service()
        guild = FakeGuild()
        config.store_for(guild)["forum_autopin_channel_ids"] = [42]
        guild.channels[42] = FakeForumChannel(42, guild=guild)

        await service.handle_thread_create(
            FakeThread(
                guild,
                parent_id=42,
                results=[FakeMessage(pin_error=discord.Forbidden())],
            )
        )
        await service.handle_channel_delete(FakeForumChannel(42, guild=guild))

        # Re-adding the same forum id must be able to alert again.
        await service.enable(guild, 42)
        await service.handle_thread_create(
            FakeThread(
                guild,
                parent_id=42,
                results=[FakeMessage(pin_error=discord.Forbidden())],
            )
        )

        permission_alerts = [m for m in alerts.messages if "cannot pin messages" in m]
        self.assertEqual(len(permission_alerts), 2)


if __name__ == "__main__":
    unittest.main()
