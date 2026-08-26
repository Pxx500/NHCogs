import asyncio
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
            self.refreshed_components = None

        def add_item(self, item):
            self.children.append(item)

        def clear_items(self):
            self.children.clear()

        def stop(self):
            return None

        def _refresh(self, components):
            self.refreshed_components = components

    class Button:
        def __init__(self, *, label=None, emoji=None, style, row=None):
            self.label = label
            self.emoji = emoji
            self.style = style
            self.row = row
            self.disabled = False
            self.callback = None

    class ViewStore:
        @staticmethod
        def update_from_message(view, components):
            view._refresh(components)

    class Select:
        def __init__(self, *, placeholder, options, row=None):
            self.placeholder = placeholder
            self.options = options
            self.row = row
            self.values = []
            self.disabled = False
            self.callback = None

    class SelectOption:
        def __init__(self, *, label, value, default=False):
            self.label = label
            self.value = value
            self.default = default

    class TextInput:
        def __init__(self, *, default=None, **kwargs):
            self.default = default
            self.value = default or ""
            for key, value in kwargs.items():
                setattr(self, key, value)

    class Modal:
        def __init__(self, *, title):
            self.title = title
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class File:
        def __init__(self, fp, *, filename):
            self.fp = fp
            self.filename = filename

    class Embed:
        def __init__(self, *, title=None, description=None, **_kwargs):
            self.title = title
            self.description = description
            self.fields = []

        def add_field(self, *, name, value, inline):
            self.fields.append(types.SimpleNamespace(name=name, value=value, inline=inline))

        def set_footer(self, *, text):
            self.footer = text

    discord.ui = types.SimpleNamespace(
        View=View,
        ViewStore=ViewStore,
        Button=Button,
        Select=Select,
        TextInput=TextInput,
        Modal=Modal,
    )
    discord.SelectOption = SelectOption
    discord.TextStyle = types.SimpleNamespace(paragraph=1, short=2)
    discord.ButtonStyle = types.SimpleNamespace(
        green=1,
        secondary=2,
        danger=3,
        primary=4,
    )
    discord.Embed = Embed
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.PartialMessageable = type("PartialMessageable", (), {})
    discord.Interaction = object
    discord.Message = object
    discord.Thread = object
    discord.Member = type("Member", (), {})
    discord.File = File
    discord.utils = types.SimpleNamespace(
        escape_markdown=lambda value: value.replace("*", r"\*"),
        format_dt=lambda value: value.isoformat(),
    )

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
    commands.UserInputError = type("UserInputError", (Exception,), {})
    commands.RESERVED_COMMAND_NAMES = set()
    commands.command = lambda **attrs: lambda callback: _Command(callback, **attrs)
    commands.group = commands.command
    commands.guild_only = lambda: _tag("guild_only")
    commands.mod_or_permissions = lambda **permissions: _tag(
        "required_permissions",
        permissions,
    )
    commands.has_permissions = lambda **permissions: _tag(
        "direct_permissions",
        permissions,
    )

    core = types.ModuleType("redbot.core")
    core.commands = commands
    core.Config = types.SimpleNamespace(get_conf=lambda *_args, **_kwargs: object())
    data_manager = types.ModuleType("redbot.core.data_manager")
    data_manager.cog_data_path = lambda **_kwargs: Path(".")
    menus = types.ModuleType("redbot.core.utils.menus")
    menus.menu = mock.AsyncMock()
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


class CustomCommandsStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_reports_catalog_initialization_failure(self):
        failure = OSError("database is unavailable")
        bot = types.SimpleNamespace(guilds=[types.SimpleNamespace(id=100)])
        nhmisc = types.SimpleNamespace(report_operational_error=mock.AsyncMock())
        catalog = types.SimpleNamespace(
            initialize=mock.AsyncMock(side_effect=failure)
        )

        with mock.patch.object(
            migration_controller,
            "CustomCommandCatalog",
            return_value=catalog,
        ):
            with self.assertRaisesRegex(OSError, "database is unavailable"):
                await migration_controller.build_custom_commands_component(bot, nhmisc)

        nhmisc.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="CustomCommands",
            action="activate replacement startup",
            error=failure,
        )

    async def test_startup_activates_replacement_without_reading_migration_state(self):
        bot = types.SimpleNamespace(guilds=[types.SimpleNamespace(id=100)])
        nhmisc = types.SimpleNamespace(report_operational_error=mock.AsyncMock())
        catalog = types.SimpleNamespace(initialize=mock.AsyncMock())
        runtime = object()
        activator = types.SimpleNamespace(activate=mock.AsyncMock(return_value=runtime))

        with (
            mock.patch.object(
                migration_controller,
                "CustomCommandCatalog",
                return_value=catalog,
            ),
            mock.patch.object(
                migration_controller,
                "MigrationStateStore",
                side_effect=AssertionError("migration state must not be read at startup"),
            ),
            mock.patch.object(
                migration_controller,
                "ReplacementActivator",
                return_value=activator,
                create=True,
            ),
        ):
            result = await migration_controller.build_custom_commands_component(
                bot,
                nhmisc,
            )

        self.assertIs(result, runtime)
        catalog.initialize.assert_awaited_once()
        activator.activate.assert_awaited_once()
        nhmisc.report_operational_error.assert_not_awaited()

    async def test_startup_reports_activation_failure_and_never_returns_migrator(self):
        failure = RuntimeError("replacement registration failed")
        bot = types.SimpleNamespace(guilds=[types.SimpleNamespace(id=100)])
        nhmisc = types.SimpleNamespace(report_operational_error=mock.AsyncMock())
        catalog = types.SimpleNamespace(initialize=mock.AsyncMock())
        activator = types.SimpleNamespace(
            activate=mock.AsyncMock(side_effect=failure)
        )

        with (
            mock.patch.object(
                migration_controller,
                "CustomCommandCatalog",
                return_value=catalog,
            ),
            mock.patch.object(
                migration_controller,
                "ReplacementActivator",
                return_value=activator,
                create=True,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "replacement registration failed",
            ):
                await migration_controller.build_custom_commands_component(bot, nhmisc)

        nhmisc.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="CustomCommands",
            action="activate replacement startup",
            error=failure,
        )


class CustomCommandsSurfaceTests(unittest.TestCase):
    def test_management_and_read_only_paths_preserve_customcom_interface(self):
        root = cog.CustomCommands.customcom
        self.assertEqual(root.name, "customcom")
        self.assertEqual(root.aliases, ["cc"])
        children = {command.name: command for command in root.commands}
        self.assertEqual(
            set(children),
            {
                "raw",
                "search",
                "list",
                "show",
                "create",
                "edit",
                "cooldown",
                "delete",
                "purgelegacy",
            },
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
        self.assertTrue(children["purgelegacy"].hidden)
        self.assertTrue(children["purgelegacy"].callback.guild_only)
        self.assertEqual(
            children["purgelegacy"].callback.direct_permissions,
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
            {"plan", "apply", "forgetguild"},
        )


class CustomCommandsLegacyPurgeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _subject():
        subject = object.__new__(cog.CustomCommands)
        subject._data_root = Path("custom-commands-data")
        subject._legacy_config = object()
        subject._log_moderation_action = mock.AsyncMock()
        command = types.SimpleNamespace(cog=subject)
        bot = types.SimpleNamespace(
            extensions={},
            _config=types.SimpleNamespace(
                packages=mock.AsyncMock(return_value=["NHCogs"])
            ),
        )
        bot.get_cog = lambda name: subject if name == "CustomCommands" else None
        bot.get_command = lambda name: command if name in {"customcom", "cc"} else None
        subject.bot = bot
        return subject

    @staticmethod
    def _ctx():
        return types.SimpleNamespace(
            clean_prefix="!",
            guild=types.SimpleNamespace(id=100),
            author=types.SimpleNamespace(id=200, __str__=lambda self: "Moderator"),
            send=mock.AsyncMock(),
        )

    async def test_plan_reports_targets_without_purging(self):
        subject = self._subject()
        ctx = self._ctx()
        status = types.SimpleNamespace(
            active_command_count=12,
            legacy_command_count=3,
            artifact_file_count=2,
            artifact_bytes=1536,
            migration_state_present=True,
            is_clean=False,
        )
        inspect = mock.AsyncMock(return_value=status)
        purge = mock.AsyncMock()

        with (
            mock.patch.object(cog, "inspect_legacy_data", inspect, create=True),
            mock.patch.object(cog, "purge_legacy_data", purge, create=True),
        ):
            await cog.CustomCommands.cc_purgelegacy.callback(subject, ctx, None)

        purge.assert_not_awaited()
        sent = ctx.send.await_args.kwargs
        description = sent["embed"].description
        self.assertIn("Active SQLite commands: 12", description)
        self.assertIn("Legacy Config commands: 3", description)
        self.assertIn("Migration artifact files: 2", description)
        self.assertIn("Migration state table: present", description)
        self.assertIn("!customcom purgelegacy confirm", description)

    async def test_confirmation_purges_and_logs_only_after_exact_token(self):
        subject = self._subject()
        ctx = self._ctx()
        clean = types.SimpleNamespace(
            active_command_count=12,
            legacy_command_count=0,
            artifact_file_count=0,
            artifact_bytes=0,
            migration_state_present=False,
            is_clean=True,
        )
        purge = mock.AsyncMock(return_value=clean)

        with mock.patch.object(cog, "purge_legacy_data", purge, create=True):
            with self.assertRaises(cog.commands.UserFeedbackCheckFailure):
                await cog.CustomCommands.cc_purgelegacy.callback(
                    subject,
                    ctx,
                    "CONFIRM",
                )
            await cog.CustomCommands.cc_purgelegacy.callback(
                subject,
                ctx,
                "confirm",
            )

        purge.assert_awaited_once_with(
            subject._legacy_config,
            subject._data_root,
            subject._data_root / "custom_commands.sqlite",
        )
        subject._log_moderation_action.assert_awaited_once()
        self.assertEqual(ctx.send.await_count, 1)
        self.assertEqual(
            ctx.send.await_args.kwargs["embed"].title,
            "Legacy CustomCom data removed",
        )

    async def test_confirmation_exposes_only_safety_refusal_as_user_feedback(self):
        subject = self._subject()
        ctx = self._ctx()
        safety_refusal = cog.LegacyCleanupPreconditionError(
            "active catalog is empty"
        )
        operational_failure = cog.LegacyCleanupError(
            "database failed its integrity check"
        )

        with mock.patch.object(
            cog,
            "purge_legacy_data",
            mock.AsyncMock(side_effect=safety_refusal),
            create=True,
        ):
            with self.assertRaisesRegex(
                cog.commands.UserFeedbackCheckFailure,
                "active catalog is empty",
            ):
                await cog.CustomCommands.cc_purgelegacy.callback(
                    subject,
                    ctx,
                    "confirm",
                )

        with mock.patch.object(
            cog,
            "purge_legacy_data",
            mock.AsyncMock(side_effect=operational_failure),
            create=True,
        ):
            with self.assertRaises(cog.LegacyCleanupError) as raised:
                await cog.CustomCommands.cc_purgelegacy.callback(
                    subject,
                    ctx,
                    "confirm",
                )

        self.assertIs(raised.exception, operational_failure)

    async def test_plan_refuses_when_replacement_does_not_own_both_commands(self):
        subject = self._subject()
        ctx = self._ctx()
        subject.bot.get_command = lambda name: types.SimpleNamespace(
            cog=subject if name == "customcom" else object()
        )
        inspect = mock.AsyncMock()

        with mock.patch.object(cog, "inspect_legacy_data", inspect, create=True):
            with self.assertRaisesRegex(
                cog.commands.UserFeedbackCheckFailure,
                "does not own the cc command",
            ):
                await cog.CustomCommands.cc_purgelegacy.callback(subject, ctx, None)

        inspect.assert_not_awaited()


class CustomCommandsCommandErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_reports_only_unexpected_errors_from_this_cog(self):
        listener = getattr(cog.CustomCommands, "on_command_error", None)
        self.assertIsNotNone(listener)

        subject = object.__new__(cog.CustomCommands)
        subject.nhmisc = types.SimpleNamespace(
            report_operational_error=mock.AsyncMock()
        )
        command = types.SimpleNamespace(qualified_name="customcom raw")
        ctx = types.SimpleNamespace(
            cog=subject,
            command=command,
            guild=types.SimpleNamespace(id=100),
            channel=types.SimpleNamespace(id=200),
            message=types.SimpleNamespace(id=300),
        )

        await listener(
            subject,
            ctx,
            cog.commands.UserFeedbackCheckFailure("expected"),
        )
        subject.nhmisc.report_operational_error.assert_not_awaited()

        await listener(subject, ctx, cog.commands.UserInputError("invalid input"))
        subject.nhmisc.report_operational_error.assert_not_awaited()

        failure = RuntimeError("paginator failed")
        await listener(subject, ctx, types.SimpleNamespace(original=failure))
        subject.nhmisc.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="CustomCommands",
            action="customcom raw",
            error=failure,
            channel_id=200,
            message_id=300,
        )

        subject.nhmisc.report_operational_error.reset_mock()
        ctx.cog = object()
        await listener(subject, ctx, failure)
        subject.nhmisc.report_operational_error.assert_not_awaited()


class CustomCommandsCopyTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_share_one_not_found_message(self):
        subject = object.__new__(cog.CustomCommands)
        subject.catalog = types.SimpleNamespace(get=mock.AsyncMock(return_value=None))
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            send=mock.AsyncMock(),
        )
        callbacks = (
            cog.CustomCommands.cc_raw.callback,
            cog.CustomCommands.cc_show.callback,
            cog.CustomCommands.cc_edit.callback,
            cog.CustomCommands.cc_cooldown.callback,
            cog.CustomCommands.cc_delete.callback,
        )

        for callback in callbacks:
            with self.subTest(command=callback.__name__):
                ctx.send.reset_mock()
                await callback(subject, ctx, "missing")
                ctx.send.assert_awaited_once_with(
                    "That custom command doesn't exist"
                )

    async def test_show_does_not_repeat_the_command_name_in_its_body(self):
        command = types.SimpleNamespace(
            name="spoodie",
            author_id=200,
            author_name="Moderator",
            created_at=types.SimpleNamespace(isoformat=lambda: "created"),
            edited_at=None,
            revision=3,
            cooldowns={},
            responses=(types.SimpleNamespace(weight=100, content="response"),),
        )
        subject = object.__new__(cog.CustomCommands)
        subject.catalog = types.SimpleNamespace(get=mock.AsyncMock(return_value=command))
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(
                id=100,
                get_member=lambda _user_id: None,
            ),
            send=mock.AsyncMock(),
        )
        cog.menus.menu.reset_mock()

        await cog.CustomCommands.cc_show.callback(subject, ctx, "spoodie")

        embed = cog.menus.menu.await_args.args[1][0]
        self.assertEqual(embed.title, "Custom command: spoodie")
        self.assertNotIn("Command: spoodie", embed.description)

    async def test_create_and_cooldown_validation_use_concise_copy(self):
        subject = object.__new__(cog.CustomCommands)
        subject.bot = types.SimpleNamespace(all_commands={"hello": object()})
        subject.catalog = types.SimpleNamespace(
            normalize_name=lambda _name: "hello",
            get=mock.AsyncMock(
                return_value=types.SimpleNamespace(cooldowns={})
            ),
        )
        subject.workflows = types.SimpleNamespace(open=mock.AsyncMock())
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            send=mock.AsyncMock(),
        )

        await subject._open_create_workflow(ctx, "hello", None)
        ctx.send.assert_awaited_once_with("A bot command already uses that name")

        ctx.send.reset_mock()
        await cog.CustomCommands.cc_cooldown.callback(
            subject,
            ctx,
            "hello",
            10,
            per="invalid",
        )
        ctx.send.assert_awaited_once_with(
            "Cooldown scope must be member, channel, or guild"
        )


class CustomCommandsListTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _subject_and_ctx(command_count=16, *, stored=None):
        if stored is None:
            stored = tuple(
                types.SimpleNamespace(
                    name=f"command{index:02}",
                    responses=(
                        types.SimpleNamespace(
                            content="**first**\n\tsecond   " + "x" * 80
                        ),
                    ),
                )
                for index in range(command_count)
            )
        subject = object.__new__(cog.CustomCommands)
        subject.catalog = types.SimpleNamespace(
            list_commands=mock.AsyncMock(return_value=stored)
        )
        message = types.SimpleNamespace(
            edit=mock.AsyncMock(),
            delete=mock.AsyncMock(),
        )
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            author=types.SimpleNamespace(id=200),
            clean_prefix="!",
            send=mock.AsyncMock(return_value=message),
        )
        return subject, ctx, message

    async def test_list_sends_fifteen_single_line_entries_per_page(self):
        subject, ctx, message = self._subject_and_ctx()

        await cog.CustomCommands.cc_list.callback(subject, ctx)

        ctx.send.assert_awaited_once()
        sent = ctx.send.await_args.kwargs
        first_page = sent["embed"]
        self.assertEqual(first_page.title, "Custom Command List")
        self.assertEqual(len(first_page.description.splitlines()), 15)
        self.assertEqual(first_page.footer, "Page 1/2")
        self.assertTrue(
            first_page.description.startswith(
                r"**!command00** - \*\*first\*\* second "
            )
        )
        self.assertNotIn("simple", first_page.description)
        self.assertNotIn("random", first_page.description)
        self.assertNotIn("·", first_page.description)
        self.assertEqual(
            [(item.label, item.emoji) for item in sent["view"].children],
            [("Previous", None), (None, "❌"), ("Next", None)],
        )
        self.assertIs(sent["view"].message, message)
        self.assertIsNone(sent["allowed_mentions"])

    async def test_list_buttons_navigate_and_close_the_message(self):
        subject, ctx, message = self._subject_and_ctx()
        await cog.CustomCommands.cc_list.callback(subject, ctx)
        view = ctx.send.await_args.kwargs["view"]
        previous, close, next_button = view.children
        self.assertTrue(previous.disabled)
        self.assertFalse(close.disabled)
        self.assertFalse(next_button.disabled)
        interaction = types.SimpleNamespace(
            response=types.SimpleNamespace(
                defer=mock.AsyncMock(),
                edit_message=mock.AsyncMock(),
            )
        )

        await next_button.callback(interaction)

        edited = interaction.response.edit_message.await_args.kwargs
        self.assertEqual(edited["embed"].footer, "Page 2/2")
        self.assertEqual(len(edited["embed"].description.splitlines()), 1)
        self.assertFalse(previous.disabled)
        self.assertTrue(next_button.disabled)

        await close.callback(interaction)

        interaction.response.defer.assert_awaited_once()
        message.delete.assert_awaited_once()

    async def test_list_controls_are_invoker_owned_and_disappear_on_timeout(self):
        subject, ctx, message = self._subject_and_ctx()
        subject._report_view_timeout_error = mock.AsyncMock()
        await cog.CustomCommands.cc_list.callback(subject, ctx)
        view = ctx.send.await_args.kwargs["view"]
        denied_response = types.SimpleNamespace(send_message=mock.AsyncMock())
        denied = types.SimpleNamespace(
            user=types.SimpleNamespace(id=201),
            response=denied_response,
        )

        self.assertFalse(await view.interaction_check(denied))
        denied_response.send_message.assert_awaited_once_with(
            "Only the person who ran this command can use these controls.",
            ephemeral=True,
        )
        self.assertTrue(
            await view.interaction_check(
                types.SimpleNamespace(user=types.SimpleNamespace(id=200))
            )
        )

        await view.on_timeout()

        message.edit.assert_awaited_once_with(view=None)
        subject._report_view_timeout_error.assert_not_awaited()

    async def test_list_timeout_edit_failure_is_reported(self):
        subject, ctx, message = self._subject_and_ctx()
        failure = RuntimeError("message edit failed")
        message.edit.side_effect = failure
        subject._report_view_timeout_error = mock.AsyncMock()
        await cog.CustomCommands.cc_list.callback(subject, ctx)
        view = ctx.send.await_args.kwargs["view"]

        await view.on_timeout()

        subject._report_view_timeout_error.assert_awaited_once_with(
            message,
            action="expire custom command list",
            error=failure,
        )

    async def test_list_keeps_escaped_long_entries_within_embed_limits(self):
        stored = tuple(
            types.SimpleNamespace(
                name=f"cmd{index:02}" + "*" * 95,
                responses=(types.SimpleNamespace(content="*" * 52),),
            )
            for index in range(30)
        )
        subject, ctx, _message = self._subject_and_ctx(stored=stored)
        await cog.CustomCommands.cc_list.callback(subject, ctx)
        sent = ctx.send.await_args.kwargs
        view = sent["view"]
        next_button = view.children[2]
        descriptions = [sent["embed"].description]
        interaction = types.SimpleNamespace(
            response=types.SimpleNamespace(edit_message=mock.AsyncMock())
        )

        while not next_button.disabled:
            await next_button.callback(interaction)
            descriptions.append(
                interaction.response.edit_message.await_args.kwargs[
                    "embed"
                ].description
            )

        self.assertTrue(all(len(page) <= 3_800 for page in descriptions))
        self.assertTrue(
            all(len(page.splitlines()) <= 15 for page in descriptions)
        )
        rendered = "\n".join(descriptions)
        for index in range(30):
            self.assertEqual(rendered.count(f"cmd{index:02}"), 1)


class CustomCommandsMessageListenerTests(unittest.IsolatedAsyncioTestCase):
    def _subject(self):
        subject = object.__new__(cog.CustomCommands)
        subject.bot = types.SimpleNamespace(
            cog_disabled_in_guild=mock.AsyncMock(return_value=False)
        )
        subject.nhmisc = types.SimpleNamespace(
            report_operational_error=mock.AsyncMock()
        )
        subject.workflows = types.SimpleNamespace(
            on_message=mock.AsyncMock(return_value=False)
        )
        subject.runtime = types.SimpleNamespace(handle_message=mock.AsyncMock())
        return subject

    @staticmethod
    def _message():
        return types.SimpleNamespace(
            id=300,
            guild=types.SimpleNamespace(id=100),
            channel=types.SimpleNamespace(id=200, parent=object()),
        )

    async def test_listener_reports_unexpected_workflow_dispatch_failure(self):
        subject = self._subject()
        message = self._message()
        failure = RuntimeError("workflow dispatch failed")
        subject.workflows.on_message.side_effect = failure

        await subject.on_message_without_command(message)

        subject.runtime.handle_message.assert_not_awaited()
        subject.nhmisc.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="CustomCommands",
            action="process custom command message",
            error=failure,
            channel_id=200,
            thread_id=200,
            message_id=300,
        )

    async def test_listener_reports_unexpected_runtime_dispatch_failure(self):
        subject = self._subject()
        message = self._message()
        failure = RuntimeError("runtime dispatch failed")
        subject.runtime.handle_message.side_effect = failure

        await subject.on_message_without_command(message)

        subject.nhmisc.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="CustomCommands",
            action="process custom command message",
            error=failure,
            channel_id=200,
            thread_id=200,
            message_id=300,
        )


class CustomCommandsRawTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_view_accepts_discord_message_component_updates(self):
        view = cog.RawResponseView(
            object(),
            requester_id=200,
            pages=(cog.discord.Embed(description="page"),),
        )
        components = [object()]

        cog.discord.ui.ViewStore.update_from_message(view, components)

        self.assertIs(view.refreshed_components, components)

    async def test_raw_uses_an_invoker_owned_button_view_and_exact_code_block(self):
        stored = types.SimpleNamespace(
            name="ben",
            responses=(
                types.SimpleNamespace(content="first   response  "),
                types.SimpleNamespace(content="second response"),
            ),
        )
        subject = object.__new__(cog.CustomCommands)
        subject.catalog = types.SimpleNamespace(
            get=mock.AsyncMock(return_value=stored)
        )
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            author=types.SimpleNamespace(id=200),
            send=mock.AsyncMock(return_value=types.SimpleNamespace()),
        )

        await cog.CustomCommands.cc_raw.callback(subject, ctx, "ben")

        ctx.send.assert_awaited_once()
        call = ctx.send.await_args
        self.assertEqual(call.kwargs["embed"].description, "```\nfirst   response  \n```")
        self.assertEqual(
            [item.label for item in call.kwargs["view"].children],
            ["Previous", "Next"],
        )
        view = call.kwargs["view"]
        previous, next_button = view.children
        self.assertTrue(previous.disabled)
        self.assertFalse(next_button.disabled)
        interaction = types.SimpleNamespace(
            response=types.SimpleNamespace(edit_message=mock.AsyncMock())
        )

        await next_button.callback(interaction)

        edited = interaction.response.edit_message.await_args.kwargs
        self.assertEqual(edited["embed"].description, "```\nsecond response\n```")
        self.assertFalse(previous.disabled)
        self.assertTrue(next_button.disabled)

    async def test_raw_timeout_reports_a_failed_message_edit(self):
        subject = object.__new__(cog.CustomCommands)
        subject.nhmisc = types.SimpleNamespace(
            report_operational_error=mock.AsyncMock()
        )
        view = cog.RawResponseView(
            subject,
            requester_id=200,
            pages=(cog.discord.Embed(description="page"),),
        )
        failure = RuntimeError("message edit failed")
        view.message = types.SimpleNamespace(
            id=300,
            guild=types.SimpleNamespace(id=100),
            channel=types.SimpleNamespace(id=200),
            edit=mock.AsyncMock(side_effect=failure),
        )

        await view.on_timeout()

        subject.nhmisc.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="CustomCommands",
            action="expire raw custom command response browser",
            error=failure,
            channel_id=200,
            message_id=300,
        )

    async def test_raw_pagination_error_is_reported_to_the_user(self):
        subject = object.__new__(cog.CustomCommands)
        subject.nhmisc = types.SimpleNamespace(
            report_operational_error=mock.AsyncMock()
        )
        view = cog.RawResponseView(
            subject,
            requester_id=200,
            pages=(cog.discord.Embed(description="page"),),
        )
        failure = RuntimeError("page failed")
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            channel=types.SimpleNamespace(id=200),
            response=types.SimpleNamespace(
                is_done=lambda: False,
                send_message=mock.AsyncMock(),
            ),
            followup=types.SimpleNamespace(send=mock.AsyncMock()),
        )

        await view.on_error(interaction, failure, object())

        interaction.response.send_message.assert_awaited_once_with(
            "Could not change the page. The error was reported",
            ephemeral=True,
        )

    async def test_raw_uses_one_exact_transcript_when_a_response_has_a_code_fence(self):
        responses = (
            types.SimpleNamespace(content="before  "),
            types.SimpleNamespace(content="```py\nvalue = 1\n```  "),
        )
        stored = types.SimpleNamespace(name="ben", responses=responses)
        subject = object.__new__(cog.CustomCommands)
        subject.catalog = types.SimpleNamespace(
            get=mock.AsyncMock(return_value=stored)
        )
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            author=types.SimpleNamespace(id=200),
            send=mock.AsyncMock(return_value=types.SimpleNamespace()),
        )

        await cog.CustomCommands.cc_raw.callback(subject, ctx, "ben")

        sent_file = ctx.send.await_args.kwargs["file"]
        self.assertEqual(sent_file.filename, "ben-responses.txt")
        transcript = sent_file.fp.getvalue()
        for index, response in enumerate(responses, start=1):
            encoded = response.content.encode()
            marker = f"===== Response {index}: {len(encoded)} bytes =====\n".encode()
            start = transcript.index(marker) + len(marker)
            self.assertEqual(transcript[start : start + len(encoded)], encoded)


class CustomCommandsDeleteViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_delete_replaces_prompt_with_compact_result(self):
        subject = object.__new__(cog.CustomCommands)
        subject.catalog = types.SimpleNamespace(delete=mock.AsyncMock())
        subject._log_moderation_action = mock.AsyncMock()
        command = types.SimpleNamespace(
            guild_id=100,
            name="spoodie",
            revision=3,
        )
        view = cog.DeleteConfirmationView(subject, command=command, opener_id=200)
        view.message = types.SimpleNamespace(edit=mock.AsyncMock())
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=100),
            user="moderator",
            response=types.SimpleNamespace(defer=mock.AsyncMock()),
        )

        await view.children[0].callback(interaction)

        edited = view.message.edit.await_args.kwargs
        self.assertEqual(edited["embed"].title, "Deleted")
        self.assertEqual(edited["embed"].description, "`spoodie`")
        self.assertIsNone(edited["view"])

    async def test_cancelled_and_timed_out_delete_prompts_remove_controls(self):
        for status in ("Cancelled", "Timed out"):
            with self.subTest(status=status):
                command = types.SimpleNamespace(name="spoodie")
                view = cog.DeleteConfirmationView(
                    object(),
                    command=command,
                    opener_id=200,
                )
                view.message = types.SimpleNamespace(edit=mock.AsyncMock())

                if status == "Cancelled":
                    interaction = types.SimpleNamespace(
                        response=types.SimpleNamespace(defer=mock.AsyncMock())
                    )
                    await view.children[1].callback(interaction)
                else:
                    await view.on_timeout()

                edited = view.message.edit.await_args.kwargs
                self.assertEqual(edited["embed"].title, status)
                self.assertEqual(edited["embed"].description, "`spoodie`")
                self.assertIsNone(edited["view"])


class CustomCommandsDeleteTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_timeout_reports_a_failed_message_edit(self):
        subject = object.__new__(cog.CustomCommands)
        subject.nhmisc = types.SimpleNamespace(
            report_operational_error=mock.AsyncMock()
        )
        command = types.SimpleNamespace(name="ben")
        view = cog.DeleteConfirmationView(subject, command=command, opener_id=200)
        failure = RuntimeError("message edit failed")
        view.message = types.SimpleNamespace(
            id=300,
            guild=types.SimpleNamespace(id=100),
            channel=types.SimpleNamespace(id=200),
            edit=mock.AsyncMock(side_effect=failure),
        )

        await view.on_timeout()

        subject.nhmisc.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="CustomCommands",
            action="expire custom command delete prompt",
            error=failure,
            channel_id=200,
            message_id=300,
        )


class CustomCommandsMigrationFlowTests(unittest.IsolatedAsyncioTestCase):
    def _forgetguild_subject(
        self,
        *,
        guilds,
        phase=migration_controller.MigrationPhase.PLANNED,
        connected_guild_ids=(),
    ):
        class GuildScope:
            def __init__(self, guild_id):
                self.guild_id = guild_id

            async def clear(self):
                guilds.pop(self.guild_id)

        migration_cog = object.__new__(migration_controller.CustomCommandsMigration)
        migration_cog._apply_lock = asyncio.Lock()
        migration_cog._require_private_migration_context = mock.AsyncMock()
        migration_cog.state_store = types.SimpleNamespace(
            get=mock.AsyncMock(
                return_value=migration_controller.MigrationState(
                    phase,
                    source_digest="source",
                    destination_digest="destination",
                )
            ),
            save=mock.AsyncMock(),
        )
        migration_cog._legacy_config = types.SimpleNamespace(
            all_guilds=mock.AsyncMock(return_value=guilds),
            guild_from_id=mock.Mock(side_effect=GuildScope),
        )
        migration_cog.bot = types.SimpleNamespace(
            get_guild=lambda guild_id: (
                object() if guild_id in connected_guild_ids else None
            )
        )
        ctx = types.SimpleNamespace(send=mock.AsyncMock(), clean_prefix="!")
        return migration_cog, ctx

    async def test_forgetguild_clears_only_orphaned_legacy_scope_and_invalidates_plan(self):
        guilds = {
            100: {"commands": {"active": {"response": "keep"}}},
            754: {
                "commands": {
                    "first": {"response": "remove"},
                    "second": {"response": "remove"},
                }
            },
        }
        migration_cog, ctx = self._forgetguild_subject(
            guilds=guilds,
            connected_guild_ids={100},
        )
        command = migration_controller.CustomCommandsMigration.nhcustomcom_migrate_forgetguild

        await command.callback(migration_cog, ctx, 754, "confirm")

        self.assertEqual(tuple(guilds), (100,))
        migration_cog.state_store.save.assert_awaited_once_with(
            migration_controller.MigrationPhase.NOT_PLANNED,
            source_digest=None,
            destination_digest=None,
        )
        self.assertIn("Forgot 2 legacy CustomCom commands", ctx.send.await_args.args[0])
        self.assertIn("migrate plan", ctx.send.await_args.args[0])

    async def test_forgetguild_rejects_a_guild_the_bot_is_still_connected_to(self):
        guilds = {754: {"commands": {"first": {"response": "keep"}}}}
        migration_cog, ctx = self._forgetguild_subject(
            guilds=guilds,
            connected_guild_ids={754},
        )
        command = migration_controller.CustomCommandsMigration.nhcustomcom_migrate_forgetguild

        with self.assertRaisesRegex(
            migration_controller.commands.UserFeedbackCheckFailure,
            "still connected",
        ):
            await command.callback(migration_cog, ctx, 754, "confirm")

        self.assertIn(754, guilds)
        migration_cog._legacy_config.guild_from_id.assert_not_called()
        migration_cog.state_store.save.assert_not_awaited()

    async def test_forgetguild_requires_a_current_migration_plan(self):
        guilds = {754: {"commands": {"first": {"response": "keep"}}}}
        migration_cog, ctx = self._forgetguild_subject(
            guilds=guilds,
            phase=migration_controller.MigrationPhase.NOT_PLANNED,
        )
        command = migration_controller.CustomCommandsMigration.nhcustomcom_migrate_forgetguild

        with self.assertRaisesRegex(
            migration_controller.commands.UserFeedbackCheckFailure,
            "Run the migration plan",
        ):
            await command.callback(migration_cog, ctx, 754, "confirm")

        self.assertIn(754, guilds)
        migration_cog._legacy_config.all_guilds.assert_not_awaited()
        migration_cog.state_store.save.assert_not_awaited()

    async def test_forgetguild_rejects_missing_or_empty_legacy_guild_data(self):
        command = migration_controller.CustomCommandsMigration.nhcustomcom_migrate_forgetguild
        for guilds in ({}, {754: {"commands": {}}}):
            with self.subTest(guilds=guilds):
                migration_cog, ctx = self._forgetguild_subject(guilds=guilds)

                with self.assertRaisesRegex(
                    migration_controller.commands.UserFeedbackCheckFailure,
                    "no active legacy CustomCom commands",
                ):
                    await command.callback(migration_cog, ctx, 754, "confirm")

                migration_cog._legacy_config.guild_from_id.assert_not_called()
                migration_cog.state_store.save.assert_not_awaited()

    async def test_forgetguild_requires_exact_confirmation(self):
        guilds = {754: {"commands": {"first": {"response": "keep"}}}}
        migration_cog, ctx = self._forgetguild_subject(guilds=guilds)
        command = migration_controller.CustomCommandsMigration.nhcustomcom_migrate_forgetguild

        with self.assertRaisesRegex(
            migration_controller.commands.UserFeedbackCheckFailure,
            "confirm",
        ):
            await command.callback(migration_cog, ctx, 754, "yes")

        self.assertIn(754, guilds)
        migration_cog.state_store.get.assert_not_awaited()
        migration_cog._legacy_config.all_guilds.assert_not_awaited()

    async def test_forgetguild_does_not_delete_when_plan_invalidation_fails(self):
        guilds = {754: {"commands": {"first": {"response": "keep"}}}}
        migration_cog, ctx = self._forgetguild_subject(guilds=guilds)
        migration_cog.state_store.save.side_effect = OSError("storage unavailable")
        command = migration_controller.CustomCommandsMigration.nhcustomcom_migrate_forgetguild

        with self.assertRaisesRegex(OSError, "storage unavailable"):
            await command.callback(migration_cog, ctx, 754, "confirm")

        self.assertIn(754, guilds)
        migration_cog._legacy_config.guild_from_id.assert_not_called()

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
