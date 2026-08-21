import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "custom_commands"


class _Command:
    def __init__(self, callback, *, parent=None, **attrs):
        self.callback = callback
        self.parent = parent
        self.name = attrs.get("name", callback.__name__)
        self.aliases = attrs.get("aliases", [])
        self.hidden = attrs.get("hidden", False)
        self.commands = []
        self.qualified_name = (
            f"{parent.qualified_name} {self.name}" if parent is not None else self.name
        )
        self.signature = ""
        doc = (callback.__doc__ or "").strip().splitlines()
        self.short_doc = doc[0] if doc else ""
        if parent is not None:
            parent.commands.append(self)

    def command(self, **attrs):
        return lambda callback: _Command(callback, parent=self, **attrs)

    def group(self, **attrs):
        return lambda callback: _Command(callback, parent=self, **attrs)


def _tag(name, value=True):
    def decorator(target):
        callback = target.callback if isinstance(target, _Command) else target
        setattr(callback, name, value)
        return target

    return decorator


def load_cog_module():  # noqa: PLR0915
    package_name = "custom_commands_cog_subject"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules[package_name] = package

    discord = types.ModuleType("discord")

    class View:
        def __init__(self, *, timeout=None):
            self.timeout = timeout
            self.children = []

        def add_item(self, item):
            self.children.append(item)

        def stop(self):
            return None

    class Button:
        def __init__(self, *, label, style):
            self.label = label
            self.style = style
            self.disabled = False
            self.callback = None

    class Embed:
        def __init__(self, *, title=None, description=None, **_kwargs):
            self.title = title
            self.description = description
            self.fields = []

        def add_field(self, *, name, value, inline):
            self.fields.append(types.SimpleNamespace(name=name, value=value, inline=inline))

        def set_footer(self, *, text):
            self.footer = text

    discord.ui = types.SimpleNamespace(View=View, Button=Button)
    discord.ButtonStyle = types.SimpleNamespace(
        green=1,
        secondary=2,
        danger=3,
    )
    discord.Embed = Embed
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.PartialMessageable = type("PartialMessageable", (), {})
    discord.Interaction = object
    discord.Message = object
    discord.Thread = object
    discord.Member = type("Member", (), {})
    discord.File = object
    discord.utils = types.SimpleNamespace(format_dt=lambda value: value.isoformat())

    class Cog:
        @staticmethod
        def listener(_event=None):
            return _tag("listener")

    commands = types.ModuleType("redbot.core.commands")
    commands.Cog = Cog
    commands.Context = object
    commands.Parameter = inspect.Parameter
    commands.MemberConverter = object
    commands.UserFeedbackCheckFailure = type(
        "UserFeedbackCheckFailure",
        (Exception,),
        {},
    )
    commands.RESERVED_COMMAND_NAMES = set()
    commands.command = lambda **attrs: lambda callback: _Command(callback, **attrs)
    commands.group = commands.command
    commands.guild_only = lambda: _tag("guild_only")
    commands.mod_or_permissions = lambda **permissions: _tag(
        "required_permissions",
        permissions,
    )

    core = types.ModuleType("redbot.core")
    core.commands = commands
    core.Config = types.SimpleNamespace(get_conf=lambda *_args, **_kwargs: object())
    data_manager = types.ModuleType("redbot.core.data_manager")
    data_manager.cog_data_path = lambda **_kwargs: Path(".")
    menus = types.ModuleType("redbot.core.utils.menus")
    menus.menu = lambda *_args, **_kwargs: None
    utils = types.ModuleType("redbot.core.utils")
    utils.menus = menus
    formatting = types.ModuleType("redbot.core.utils.chat_formatting")
    formatting.pagify = lambda text, **_kwargs: (text,)

    def humanize_list(values):
        return " and ".join(values)

    formatting.humanize_list = humanize_list

    rapidfuzz = types.ModuleType("rapidfuzz")
    rapidfuzz.process = types.SimpleNamespace(extract=lambda *_args, **_kwargs: [])
    rapidfuzz.utils = types.SimpleNamespace(default_process=lambda value: value)
    temporary = {
        "discord": discord,
        "rapidfuzz": rapidfuzz,
        "redbot": types.ModuleType("redbot"),
        "redbot.core": core,
        "redbot.core.commands": commands,
        "redbot.core.data_manager": data_manager,
        "redbot.core.utils": utils,
        "redbot.core.utils.menus": menus,
        "redbot.core.utils.chat_formatting": formatting,
    }
    previous = {name: sys.modules.get(name) for name in temporary}
    sys.modules.update(temporary)
    try:
        for module_name in (
            "arguments",
            "migration_state",
            "catalog",
            "runtime",
            "workflows",
            "cog",
            "migration",
            "lifecycle",
            "migration_controller",
        ):
            qualified_name = f"{package_name}.{module_name}"
            spec = importlib.util.spec_from_file_location(
                qualified_name,
                PACKAGE_PATH / f"{module_name}.py",
            )
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[qualified_name] = module
            spec.loader.exec_module(module)
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module
    return (
        sys.modules[f"{package_name}.cog"],
        sys.modules[f"{package_name}.migration_controller"],
    )


cog, migration_controller = load_cog_module()


class CustomCommandsSurfaceTests(unittest.TestCase):
    def test_management_and_read_only_paths_preserve_customcom_interface(self):
        root = cog.CustomCommands.customcom
        self.assertEqual(root.name, "customcom")
        self.assertEqual(root.aliases, ["cc"])
        children = {command.name: command for command in root.commands}
        self.assertEqual(
            set(children),
            {"raw", "search", "list", "show", "create", "edit", "cooldown", "delete"},
        )
        self.assertEqual(
            {command.name for command in children["create"].commands},
            {"simple", "random"},
        )
        for name in ("create", "edit", "cooldown", "delete"):
            self.assertEqual(
                children[name].callback.required_permissions,
                {"manage_messages": True},
            )
        for name in ("raw", "search", "list", "show"):
            self.assertFalse(hasattr(children[name].callback, "required_permissions"))

    def test_migration_uses_a_separate_hidden_management_path(self):
        root = migration_controller.CustomCommandsMigration.nhcustomcom
        self.assertEqual(root.name, "nhcustomcom")
        self.assertTrue(root.hidden)
        migrate = next(command for command in root.commands if command.name == "migrate")
        self.assertEqual(
            {command.name for command in migrate.commands},
            {"plan", "apply"},
        )


class CustomCommandsMigrationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_mode_participates_in_user_data_deletion(self):
        migration_cog = object.__new__(migration_controller.CustomCommandsMigration)
        migration_cog.catalog = object()
        migration_cog._legacy_config = object()
        with mock.patch.object(
            migration_controller,
            "redact_custom_command_user_data",
            new=mock.AsyncMock(),
        ) as redact:
            await migration_cog.red_delete_data_for_user(
                requester="discord_deleted_user",
                user_id=42,
            )

        redact.assert_awaited_once_with(
            migration_cog.catalog,
            migration_cog._legacy_config,
            mock.ANY,
            42,
        )

    async def test_verified_plan_imports_once_before_entering_cutover_state(self):
        plan = migration_controller.LegacyMigrationPlanner().plan(
            {
                100: {
                    "commands": {
                        "hello": {
                            "author": {"id": 200, "name": "Creator"},
                            "command": "hello",
                            "cooldowns": {},
                            "created_at": "20/08/2026 12:00:00",
                            "editors": [],
                            "response": "hello",
                        }
                    }
                }
            }
        )
        imported_state = migration_controller.MigrationState(
            migration_controller.MigrationPhase.IMPORTED_NOT_ACTIVE,
            source_digest=plan.source_digest,
            destination_digest=plan.destination_digest,
        )
        state_store = types.SimpleNamespace(
            get=mock.AsyncMock(return_value=imported_state)
        )
        catalog = types.SimpleNamespace(
            import_migration=mock.AsyncMock(),
            list_commands=mock.AsyncMock(return_value=plan.commands),
        )
        migration_cog = object.__new__(migration_controller.CustomCommandsMigration)
        migration_cog.catalog = catalog
        migration_cog.state_store = state_store
        migration_cog._build_plan = mock.AsyncMock(return_value=plan)
        planned_state = migration_controller.MigrationState(
            migration_controller.MigrationPhase.PLANNED,
            source_digest=plan.source_digest,
            destination_digest=plan.destination_digest,
        )

        result = await migration_cog._import_planned(planned_state)

        self.assertIs(result, imported_state)
        catalog.import_migration.assert_awaited_once_with(
            plan.commands,
            source_digest=plan.source_digest,
            destination_digest=plan.destination_digest,
        )
        state_store.get.assert_awaited_once()

    async def test_apply_quiesces_official_before_final_snapshot_import(self):
        planned = migration_controller.MigrationState(
            migration_controller.MigrationPhase.PLANNED,
            source_digest="source",
            destination_digest="destination",
        )
        imported = migration_controller.MigrationState(
            migration_controller.MigrationPhase.IMPORTED_NOT_ACTIVE,
            source_digest="source",
            destination_digest="destination",
        )
        calls = mock.Mock()
        controller = types.SimpleNamespace(
            quiesce_official=mock.AsyncMock(
                side_effect=lambda: calls("quiesce")
            ),
            activate_imported=mock.AsyncMock(
                side_effect=lambda: calls("activate")
            ),
            restore_official=mock.AsyncMock(),
        )
        migration_cog = object.__new__(migration_controller.CustomCommandsMigration)
        migration_cog.state_store = types.SimpleNamespace(
            get=mock.AsyncMock(return_value=planned)
        )
        migration_cog.controller = controller

        async def import_planned(_state):
            calls("import")
            return imported

        migration_cog._import_planned = import_planned
        migration_cog.bot = types.SimpleNamespace(remove_cog=mock.AsyncMock())
        migration_cog.__cog_name__ = "CustomCommandsMigration"
        migration_cog.nhmisc = types.SimpleNamespace(
            report_operational_error=mock.AsyncMock()
        )
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            channel=types.SimpleNamespace(id=200),
            message=types.SimpleNamespace(id=300),
            send=mock.AsyncMock(),
        )

        await migration_cog._apply_confirmed(ctx)

        self.assertEqual(
            calls.call_args_list,
            [mock.call("quiesce"), mock.call("import"), mock.call("activate")],
        )
        controller.restore_official.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
