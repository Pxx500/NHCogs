import importlib
import inspect
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

_MISSING = object()


class FakeVersionInfo:
    def __init__(self, major, minor, micro):
        self.major = major
        self.minor = minor
        self.micro = micro

    @classmethod
    def from_str(cls, value):
        return cls(*(int(part) for part in value.split(".")))

    def __lt__(self, other):
        return (self.major, self.minor, self.micro) < (
            other.major,
            other.minor,
            other.micro,
        )


class FakeCommand:
    def __init__(self, callback, *, name=None, hidden=False, **_kwargs):
        self.callback = callback
        self.name = name or callback.__name__
        self.hidden = hidden
        self.required_permissions = getattr(callback, "required_permissions", {})
        self.guild_only = getattr(callback, "guild_only", False)

    def command(self, *args, **kwargs):
        return lambda callback: FakeCommand(callback, **kwargs)


def tag(name, value=True):
    def apply(callback):
        setattr(callback, name, value)
        return callback

    return apply


@contextmanager
def load_migrator():
    names = tuple(
        name
        for name in sys.modules
        if name == "NHCogsMigrator" or name.startswith("NHCogsMigrator.")
    ) + (
        "discord",
        "redbot",
        "redbot.core",
        "redbot.core.commands",
        "redbot.core.bot",
        "redbot.core.data_manager",
        "redbot.core.utils",
    )
    previous = {name: sys.modules.get(name, _MISSING) for name in names}
    discord = types.ModuleType("discord")
    discord.File = object
    discord.Embed = object
    discord.Color = types.SimpleNamespace(
        blue=lambda: 0,
        green=lambda: 0,
        red=lambda: 0,
    )
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    commands = types.ModuleType("redbot.core.commands")
    commands.Cog = object
    commands.Context = object
    commands.UserFeedbackCheckFailure = RuntimeError
    commands.group = lambda *args, **kwargs: (
        lambda callback: FakeCommand(callback, **kwargs)
    )
    commands.guild_only = lambda: tag("guild_only")
    commands.mod_or_permissions = lambda **permissions: tag(
        "required_permissions",
        permissions,
    )
    redbot = types.ModuleType("redbot")
    redbot.VersionInfo = FakeVersionInfo
    core = types.ModuleType("redbot.core")
    core.commands = commands
    core.version_info = FakeVersionInfo.from_str("3.5.23")
    bot_module = types.ModuleType("redbot.core.bot")
    bot_module.Red = object
    data_manager = types.ModuleType("redbot.core.data_manager")
    data_manager.cog_data_path = lambda _cog: Path(".")
    utils = types.ModuleType("redbot.core.utils")
    utils.get_end_user_data_statement = lambda **_kwargs: "data"
    try:
        for name in previous:
            sys.modules.pop(name, None)
        sys.modules.update(
            {
                "discord": discord,
                "redbot": redbot,
                "redbot.core": core,
                "redbot.core.commands": commands,
                "redbot.core.bot": bot_module,
                "redbot.core.data_manager": data_manager,
                "redbot.core.utils": utils,
            }
        )
        yield importlib.import_module("NHCogsMigrator.migrator")
    finally:
        touched = tuple(
            name
            for name in sys.modules
            if name == "NHCogsMigrator" or name.startswith("NHCogsMigrator.")
        )
        for name in (*touched, *previous):
            old = previous.get(name, _MISSING)
            if old is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class MigratorCommandTests(unittest.TestCase):
    def test_all_migration_flows_are_hidden_manage_messages_commands(self):
        with load_migrator() as module:
            cog = module.NHCogsMigrator
            commands_to_check = (
                cog.nhcogsmigrate,
                cog.nhcogsmigrate_plan,
                cog.nhcogsmigrate_status,
                cog.nhcogsmigrate_apply,
                cog.nhcogsmigrate_finalize,
                cog.nhcogsmigrate_recover,
            )

            for command in commands_to_check:
                with self.subTest(command=command.name):
                    self.assertTrue(command.hidden)
                    self.assertTrue(command.guild_only)
                    self.assertEqual(
                        command.required_permissions,
                        {"manage_messages": True},
                    )

            signature = inspect.signature(cog.nhcogsmigrate_apply.callback)
            self.assertIn("confirm", signature.parameters)
            self.assertIs(signature.parameters["confirm"].default, inspect.Parameter.empty)
            recover_signature = inspect.signature(cog.nhcogsmigrate_recover.callback)
            self.assertIn("confirm", recover_signature.parameters)


class MigratorAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cog_load_accepts_the_minimum_supported_red_version(self):
        with load_migrator() as module:
            initialized = False

            class Store:
                async def initialize(self):
                    nonlocal initialized
                    initialized = True

            async def recover_after_ready():
                return None

            cog = object.__new__(module.NHCogsMigrator)
            cog._store = Store()
            cog._recover_after_ready = recover_after_ready

            await cog.cog_load()
            await cog._recovery_task

            self.assertTrue(initialized)

    async def test_red_moderator_without_manage_messages_is_rejected(self):
        with load_migrator() as module:
            default_role = object()
            author = object()
            channel = types.SimpleNamespace(
                permissions_for=lambda subject: types.SimpleNamespace(
                    view_channel=False,
                    manage_messages=subject is not author,
                )
            )
            ctx = types.SimpleNamespace(
                author=author,
                channel=channel,
                guild=types.SimpleNamespace(default_role=default_role),
            )

            with self.assertRaisesRegex(RuntimeError, "Manage Messages"):
                await module.NHCogsMigrator._require_private_channel(ctx)

    async def test_recovery_report_is_withheld_if_channel_became_public(self):
        with load_migrator() as module:
            sent = False

            async def send(**_kwargs):
                nonlocal sent
                sent = True

            default_role = object()
            channel = types.SimpleNamespace(
                id=123,
                guild=types.SimpleNamespace(default_role=default_role),
                permissions_for=lambda _role: types.SimpleNamespace(
                    view_channel=True
                ),
                send=send,
            )
            cog = object.__new__(module.NHCogsMigrator)
            cog.bot = types.SimpleNamespace(
                get_channel=lambda _channel_id: channel,
            )
            run = types.SimpleNamespace(
                validations={"plan_channel_id": 123},
            )
            module._status_embed = lambda _run: object()

            await cog._send_recovery_report(run)

            self.assertFalse(sent)
