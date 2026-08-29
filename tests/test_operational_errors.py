import importlib
import inspect
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

PACKAGE_ROOT = Path(__file__).parents[1] / "NHCogs"
ROOT_PACKAGE = "operational_errors_test_root"
SUBJECT_PACKAGE = f"{ROOT_PACKAGE}.operationalerrors"
_MISSING = object()


class _FakeCommand:
    def __init__(self, callback, *, kind="command", parent=None, **attrs):
        self.callback = callback
        self.kind = kind
        self.parent = parent
        self.name = attrs.get("name", callback.__name__)
        self.aliases = attrs.get("aliases", [])
        self.invoke_without_command = attrs.get("invoke_without_command", False)
        self.commands = []
        if parent is not None:
            parent.commands.append(self)

    @property
    def qualified_name(self):
        if self.parent is None:
            return self.name
        return f"{self.parent.qualified_name} {self.name}"

    @property
    def short_doc(self):
        lines = (self.callback.__doc__ or "").strip().splitlines()
        return lines[0] if lines else ""

    @property
    def signature(self):
        parameters = list(inspect.signature(self.callback).parameters.values())[2:]
        return " ".join(
            f"<{parameter.name}>"
            if parameter.default is inspect.Parameter.empty
            else f"[{parameter.name}]"
            for parameter in parameters
        )

    def command(self, **attrs):
        return lambda callback: _FakeCommand(callback, parent=self, **attrs)

    def group(self, **attrs):
        return lambda callback: _FakeCommand(
            callback,
            kind="group",
            parent=self,
            **attrs,
        )


def _tag(name, value=True):
    def decorator(target):
        callback = target.callback if isinstance(target, _FakeCommand) else target
        setattr(callback, name, value)
        return target

    return decorator


class _FakeCog:
    @staticmethod
    def listener(event_name=None):
        return _tag("listener_event", event_name)


class _Setting:
    def __init__(self, value=None):
        self.value = value
        self.read_count = 0
        self.set_count = 0

    def __call__(self):
        async def read():
            self.read_count += 1
            return self.value

        return read()

    async def set(self, value):
        self.set_count += 1
        self.value = value

    async def clear(self):
        self.value = None


class _FakeConfig:
    last = None

    def __init__(self):
        self.error_channel = _Setting()
        self.error_maintainer_id = _Setting()
        self.registered = None

    @classmethod
    def get_conf(cls, *_args, **_kwargs):
        cls.last = cls()
        return cls.last

    def register_global(self, **values):
        self.registered = values


class _Embed:
    def __init__(self, *, title=None, description=None):
        self.title = title
        self.description = description
        self.fields = []

    def add_field(self, *, name, value, inline):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))


class _AllowedMentions:
    none_marker = object()

    def __init__(self, **values):
        self.__dict__.update(values)

    @classmethod
    def none(cls):
        return cls.none_marker


class _File:
    def __init__(self, fp, *, filename):
        self.data = fp.read()
        self.filename = filename


class _Channel:
    def __init__(self, channel_id, guild, *, public=False):
        self.id = channel_id
        self.name = "operational-errors"
        self.guild = guild
        self.public = public
        self.send = mock.AsyncMock()

    def permissions_for(self, target):
        if target is self.guild.default_role:
            return SimpleNamespace(view_channel=self.public)
        return SimpleNamespace(
            view_channel=True,
            send_messages=True,
            attach_files=True,
        )


class _Guild:
    def __init__(self, guild_id=100):
        self.id = guild_id
        self.default_role = object()
        self.me = object()
        self.channel = None
        self.maintainer = SimpleNamespace(
            id=300,
            mention="<@300>",
            display_name="maintainer",
        )

    def get_channel(self, channel_id):
        if self.channel is not None and channel_id == self.channel.id:
            return self.channel
        return None

    def get_member(self, member_id):
        return self.maintainer if member_id == self.maintainer.id else None


class _Bot:
    def __init__(self, guild):
        self.guild = guild
        self.cog = None

    def get_channel(self, channel_id):
        return self.guild.get_channel(channel_id)

    def get_cog(self, name):
        if name == "OperationalErrors":
            return self.cog
        return None


@contextmanager
def _isolated_operational_errors(data_path: Path):
    discord = types.ModuleType("discord")
    discord.AllowedMentions = _AllowedMentions
    discord.Embed = _Embed
    discord.File = _File
    discord.Guild = _Guild
    discord.Member = type("Member", (), {})
    discord.Object = lambda *, id: SimpleNamespace(id=id)
    discord.TextChannel = type("TextChannel", (), {})

    commands = types.ModuleType("redbot.core.commands")
    commands.Cog = _FakeCog
    commands.Context = object
    commands.Group = _FakeCommand
    commands.UserFeedbackCheckFailure = type(
        "UserFeedbackCheckFailure",
        (Exception,),
        {},
    )
    commands.group = lambda **attrs: lambda callback: _FakeCommand(
        callback,
        kind="group",
        **attrs,
    )
    commands.guild_only = lambda: _tag("guild_only")
    commands.has_permissions = lambda **permissions: _tag(
        "required_permissions",
        permissions,
    )

    redbot = types.ModuleType("redbot")
    core = types.ModuleType("redbot.core")
    core.Config = _FakeConfig
    core.commands = commands
    data_manager = types.ModuleType("redbot.core.data_manager")
    data_manager.cog_data_path = lambda _cog: data_path

    root = types.ModuleType(ROOT_PACKAGE)
    root.__path__ = [str(PACKAGE_ROOT)]
    names = (
        "discord",
        "redbot",
        "redbot.core",
        "redbot.core.commands",
        "redbot.core.data_manager",
        ROOT_PACKAGE,
        f"{ROOT_PACKAGE}.command_overview",
        SUBJECT_PACKAGE,
        f"{SUBJECT_PACKAGE}.cog",
    )
    previous = {name: sys.modules.get(name, _MISSING) for name in names}
    sys.modules.update(
        {
            "discord": discord,
            "redbot": redbot,
            "redbot.core": core,
            "redbot.core.commands": commands,
            "redbot.core.data_manager": data_manager,
            ROOT_PACKAGE: root,
        }
    )
    try:
        yield importlib.import_module(SUBJECT_PACKAGE)
    finally:
        for name, old_module in previous.items():
            if old_module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


def _reporting_fixture(module):
    guild = _Guild()
    channel = _Channel(200, guild)
    guild.channel = channel
    bot = _Bot(guild)
    cog = module.OperationalErrors(bot)
    bot.cog = cog
    cog.config.error_channel.value = channel.id
    cog.config.error_maintainer_id.value = guild.maintainer.id
    return cog, bot, guild, channel


class OperationalErrorReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_persists_occurrences_and_alerts_each_time(self):
        with TemporaryDirectory() as directory:
            with _isolated_operational_errors(Path(directory)) as module:
                cog, _bot, guild, channel = _reporting_fixture(module)
                await cog.cog_load()

                error = ValueError("Discord rejected the message")
                first = await cog.report(
                    guild_id=guild.id,
                    source="CustomCommands",
                    action="send response",
                    error=error,
                    channel_id=400,
                    message_id=500,
                )
                second = await cog.report(
                    guild_id=guild.id,
                    source="CustomCommands",
                    action="send response",
                    error=error,
                    channel_id=400,
                    message_id=500,
                )

                self.assertEqual(first.occurrences, 1)
                self.assertEqual(second.occurrences, 2)
                self.assertEqual(first.fingerprint, second.fingerprint)
                self.assertEqual(channel.send.await_count, 2)

    async def test_correlation_key_groups_retries_and_recovers_only_matching_work(self):
        with TemporaryDirectory() as directory:
            with _isolated_operational_errors(Path(directory)) as module:
                cog, bot, guild, _channel = _reporting_fixture(module)
                await cog.cog_load()

                try:
                    first = await module.report_operational_error(
                        bot,
                        guild_id=guild.id,
                        source="Honeypot",
                        action="role_apply",
                        error=RuntimeError("first attempt"),
                        correlation_key="operation-1",
                    )
                    retry = await module.report_operational_error(
                        bot,
                        guild_id=guild.id,
                        source="Honeypot",
                        action="role_apply",
                        error=RuntimeError("different retry error"),
                        correlation_key="operation-1",
                    )
                    unrelated = await module.report_operational_error(
                        bot,
                        guild_id=guild.id,
                        source="Honeypot",
                        action="role_apply",
                        error=RuntimeError("other operation"),
                        correlation_key="operation-2",
                    )
                except TypeError as error:
                    self.fail(f"shared reporter rejected correlation_key: {error}")

                self.assertEqual(first.fingerprint, retry.fingerprint)
                self.assertEqual(retry.occurrences, 2)
                self.assertNotEqual(first.fingerprint, unrelated.fingerprint)
                try:
                    recovered = await module.mark_operational_error_recovered(
                        bot,
                        guild_id=guild.id,
                        source="Honeypot",
                        action="role_apply",
                        correlation_key="operation-1",
                    )
                except TypeError as error:
                    self.fail(f"shared recovery rejected correlation_key: {error}")

                self.assertEqual(recovered, 1)
                self.assertEqual(await cog.active_count(guild.id), 1)

    async def test_persistence_failure_still_attempts_the_alert(self):
        with TemporaryDirectory() as directory:
            with _isolated_operational_errors(Path(directory)) as module:
                cog, _bot, guild, channel = _reporting_fixture(module)
                cog._record_sync = mock.Mock(side_effect=RuntimeError("disk full"))

                result = await cog.report(
                    guild_id=guild.id,
                    source="GitHubTickets",
                    action="accept webhook",
                    error=RuntimeError("delivery failed"),
                )

                self.assertIsNone(result)
                channel.send.assert_awaited_once()

    async def test_shared_entry_point_never_raises_when_reporter_is_missing_or_broken(self):
        with TemporaryDirectory() as directory:
            with _isolated_operational_errors(Path(directory)) as module:
                guild = _Guild()
                bot = _Bot(guild)
                error = RuntimeError("failed work")

                missing = await module.report_operational_error(
                    bot,
                    guild_id=guild.id,
                    source="GitHubTickets",
                    action="recover delivery",
                    error=error,
                )
                cog = module.OperationalErrors(bot)
                bot.cog = cog
                cog.report = mock.AsyncMock(side_effect=BaseException("reporter broke"))
                broken = await module.report_operational_error(
                    bot,
                    guild_id=guild.id,
                    source="GitHubTickets",
                    action="recover delivery",
                    error=error,
                )

                self.assertIsNone(missing)
                self.assertIsNone(broken)


class OperationalErrorCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_registered_command_tree_is_moderator_only_and_complete(self):
        with TemporaryDirectory() as directory:
            with _isolated_operational_errors(Path(directory)) as module:
                root = module.OperationalErrors.nhcogs

                self.assertEqual(
                    root.callback.required_permissions,
                    {"manage_messages": True},
                )
                self.assertTrue(root.callback.guild_only)
                paths = set()

                def collect(command):
                    if not command.commands:
                        paths.add(command.qualified_name)
                    for child in command.commands:
                        collect(child)

                collect(root)

                self.assertEqual(
                    paths,
                    {
                        "nhcogs errors channel set",
                        "nhcogs errors channel clear",
                        "nhcogs errors maintainer set",
                        "nhcogs errors maintainer clear",
                    },
                )

    async def test_public_overview_does_not_read_global_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_operational_errors(Path(directory)) as module:
                cog, _bot, guild, _channel = _reporting_fixture(module)
                public_channel = _Channel(900, guild, public=True)
                ctx = SimpleNamespace(
                    guild=guild,
                    channel=public_channel,
                    command=module.OperationalErrors.nhcogs_errors,
                    clean_prefix="!",
                    send=mock.AsyncMock(),
                )

                await module.OperationalErrors.nhcogs_errors.callback(cog, ctx)

                self.assertEqual(cog.config.error_channel.read_count, 0)
                self.assertEqual(cog.config.error_maintainer_id.read_count, 0)
                rendered = "\n".join(
                    field.value
                    for call in ctx.send.await_args_list
                    for field in call.kwargs["embed"].fields
                )
                self.assertIn("Current values are hidden", rendered)
                self.assertIn("!nhcogs errors channel set <channel>", rendered)
                self.assertIn("!nhcogs errors maintainer clear", rendered)

    async def test_private_overview_reads_and_renders_global_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_operational_errors(Path(directory)) as module:
                cog, _bot, guild, channel = _reporting_fixture(module)
                await cog.cog_load()
                ctx = SimpleNamespace(
                    guild=guild,
                    channel=channel,
                    command=module.OperationalErrors.nhcogs_errors,
                    clean_prefix="?",
                    send=mock.AsyncMock(),
                )

                await module.OperationalErrors.nhcogs_errors.callback(cog, ctx)

                self.assertGreater(cog.config.error_channel.read_count, 0)
                config_embed = ctx.send.await_args_list[0].kwargs["embed"]
                values = {field.name: field.value for field in config_embed.fields}
                self.assertEqual(values["Channel"], "#operational-errors")
                self.assertEqual(values["Maintainer"], "@maintainer")
                self.assertEqual(values["Active failures"], "0")
                self.assertTrue(
                    all(
                        call.kwargs["allowed_mentions"] is _AllowedMentions.none_marker
                        for call in ctx.send.await_args_list
                    )
                )

    async def test_public_mutation_commands_reject_before_configuration_access(self):
        with TemporaryDirectory() as directory:
            with _isolated_operational_errors(Path(directory)) as module:
                cog, _bot, guild, private_channel = _reporting_fixture(module)
                public_channel = _Channel(900, guild, public=True)
                ctx = SimpleNamespace(
                    guild=guild,
                    channel=public_channel,
                    command=module.OperationalErrors.nhcogs_errors_channel_set,
                    clean_prefix="!",
                    send=mock.AsyncMock(),
                )
                feedback_error = sys.modules[
                    "redbot.core.commands"
                ].UserFeedbackCheckFailure

                with self.assertRaisesRegex(
                    feedback_error,
                    "hidden from @everyone",
                ):
                    await module.OperationalErrors.nhcogs_errors_channel_set.callback(
                        cog,
                        ctx,
                        private_channel,
                    )
                ctx.command = module.OperationalErrors.nhcogs_errors_maintainer_set
                with self.assertRaisesRegex(
                    feedback_error,
                    "hidden from @everyone",
                ):
                    await module.OperationalErrors.nhcogs_errors_maintainer_set.callback(
                        cog,
                        ctx,
                        guild.maintainer,
                    )
                ctx.command = module.OperationalErrors.nhcogs_errors_channel_clear
                with self.assertRaisesRegex(
                    feedback_error,
                    "hidden from @everyone",
                ):
                    await module.OperationalErrors.nhcogs_errors_channel_clear.callback(
                        cog,
                        ctx,
                    )
                ctx.command = module.OperationalErrors.nhcogs_errors_maintainer_clear
                with self.assertRaisesRegex(
                    feedback_error,
                    "hidden from @everyone",
                ):
                    await module.OperationalErrors.nhcogs_errors_maintainer_clear.callback(
                        cog,
                        ctx,
                    )

                self.assertEqual(cog.config.error_channel.read_count, 0)
                self.assertEqual(cog.config.error_channel.set_count, 0)
                self.assertEqual(cog.config.error_maintainer_id.read_count, 0)
                self.assertEqual(cog.config.error_maintainer_id.set_count, 0)
                self.assertEqual(cog.config.error_channel.value, private_channel.id)
                self.assertEqual(
                    cog.config.error_maintainer_id.value,
                    guild.maintainer.id,
                )

    async def test_private_set_commands_write_protected_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_operational_errors(Path(directory)) as module:
                cog, _bot, guild, private_channel = _reporting_fixture(module)
                cog.config.error_channel.value = None
                cog.config.error_maintainer_id.value = None
                ctx = SimpleNamespace(
                    guild=guild,
                    channel=private_channel,
                    command=module.OperationalErrors.nhcogs_errors_channel_set,
                    clean_prefix="!",
                    send=mock.AsyncMock(),
                )

                await module.OperationalErrors.nhcogs_errors_channel_set.callback(
                    cog,
                    ctx,
                    private_channel,
                )
                ctx.command = module.OperationalErrors.nhcogs_errors_maintainer_set
                await module.OperationalErrors.nhcogs_errors_maintainer_set.callback(
                    cog,
                    ctx,
                    guild.maintainer,
                )

                self.assertEqual(cog.config.error_channel.value, private_channel.id)
                self.assertEqual(
                    cog.config.error_maintainer_id.value,
                    guild.maintainer.id,
                )


if __name__ == "__main__":
    unittest.main()
