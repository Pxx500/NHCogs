"""Shared isolation harness and Red/discord stubs for the Honeypot test suite.

Extracted verbatim from `tests/test_detection_pipeline.py`. Names keep their
leading underscore even though they are now imported across test modules; a
rename belongs in its own commit.
"""

import ast
import asyncio
import inspect
import sys
import unittest
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from importlib import util
from pathlib import Path
from types import MethodType, ModuleType, SimpleNamespace
from unittest import mock

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "NHCogs" / "honeypot"
_MISSING = object()

def _load_module(name: str, path: Path):
    spec = util.spec_from_file_location(name, path)
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _listener(*args, **kwargs):
    def apply(function):
        function.__cog_listener__ = True
        return function

    return apply


class _BoundLoop:
    def __init__(self, function, instance, options, before):
        self.function = function
        self.instance = instance
        self.options = options
        self.before = before
        self.started = False
        self.cancelled = False
        self.task = None

    def start(self):
        if self.started:
            raise RuntimeError("loop already started")
        self.started = True

    def cancel(self):
        self.cancelled = True
        if self.task is not None:
            self.task.cancel()

    def get_task(self):
        return self.task

    async def wait_before_start(self):
        await self.before(self.instance)


class _LoopStub:
    def __init__(self, function, options):
        self.function = function
        self.options = options
        self.before = None
        self.bound = {}

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return self.bound.setdefault(
            instance,
            _BoundLoop(self.function, instance, self.options, self.before),
        )

    def before_loop(self, function):
        self.before = function
        return function


class _GuildConfig:
    def __init__(self, defaults, values, stats):
        self._defaults = defaults
        self._values = values
        self._stats = stats

    async def all(self):
        return {**self._defaults, **self._values}

    async def clear(self):
        self._values.clear()

    async def clear_raw(self, key):
        self._values.pop(str(key), None)

    async def get_raw(self, key, *, default=None):
        key = str(key)
        return self._values.get(key, self._defaults.get(key, default))

    async def set_raw(self, key, *, value):
        self._values[str(key)] = value

    @asynccontextmanager
    async def stats(self):
        yield self._stats


class _Config:
    def __init__(self):
        self.defaults = {}
        self.global_defaults = {}
        self.global_values = {}
        self._guilds = {}
        self._stats = {}

    def register_guild(self, **defaults):
        self.defaults = defaults

    def register_global(self, **defaults):
        self.global_defaults = defaults

    async def all(self):
        return {**self.global_defaults, **self.global_values}

    async def get_raw(self, key, *, default=None):
        key = str(key)
        return self.global_values.get(key, self.global_defaults.get(key, default))

    async def set_raw(self, key, *, value):
        self.global_values[str(key)] = value

    async def clear_raw(self, key):
        self.global_values.pop(str(key), None)

    def guild(self, guild):
        return self.guild_from_id(guild.id)

    def guild_from_id(self, guild_id):
        return _GuildConfig(
            self.defaults,
            self._guilds.setdefault(guild_id, {}),
            self._stats,
        )

    async def all_guilds(self):
        return {
            guild_id: {**self.defaults, **values}
            for guild_id, values in self._guilds.items()
        }


class _Cog:
    @staticmethod
    def listener(*args, **kwargs):
        return _listener(*args, **kwargs)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        commands = []
        listeners = []
        seen_commands = set()
        seen_listeners = set()
        for base in reversed(cls.__mro__[:-1]):
            for name, value in base.__dict__.items():
                if isinstance(value, _CommandStub) and id(value) not in seen_commands:
                    seen_commands.add(id(value))
                    commands.append(value)
                elif getattr(value, "__cog_listener__", False) and name not in seen_listeners:
                    seen_listeners.add(name)
                    listeners.append(name)
        cls.__cog_commands__ = tuple(commands)
        cls.__cog_listeners__ = tuple(listeners)

    def __init__(self, *, bot):
        self.bot = bot

    def format_help_for_context(self, ctx):
        return self.__class__.__doc__

    async def cog_load(self):
        self.base_loaded = True

    async def cog_unload(self):
        self.base_unloaded = True


class _CommandStub:
    def __init__(self, callback, *, kind, name, parent, invoke_without_command=False):
        self.callback = callback
        self.kind = kind
        self.name = name
        self.parent = parent
        self.invoke_without_command = invoke_without_command
        self.commands = []
        self.usage = None
        self.qualified_name = (
            name if parent is None else f"{parent.qualified_name} {name}"
        )

    def __call__(self, *args, **kwargs):
        return self.callback(*args, **kwargs)

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return MethodType(self, instance)

    async def can_run(self, ctx):
        command = self
        while command is not None:
            callback = command.callback
            direct_permissions = getattr(callback, "has_permissions", None)
            if direct_permissions is not None and not self._passes_permissions(
                ctx,
                direct_permissions,
            ):
                return False
            mod_permissions = getattr(callback, "mod_or_permissions", None)
            if mod_permissions is not None and not self._passes_permissions(
                ctx,
                mod_permissions,
                "is_red_mod",
                "is_red_admin",
            ):
                return False
            admin_permissions = getattr(callback, "admin_or_permissions", None)
            if admin_permissions is not None and not self._passes_permissions(
                ctx,
                admin_permissions,
                "is_red_admin",
            ):
                return False
            command = command.parent
        return True

    @staticmethod
    def _passes_permissions(ctx, permissions, *privilege_flags):
        if any(getattr(ctx, flag, False) for flag in privilege_flags):
            return True
        guild_permissions = ctx.author.guild_permissions
        return all(
            getattr(guild_permissions, name, False) is required
            for name, required in permissions.items()
        )

    def command(self, *args, **kwargs):
        return _command_decorator("command", parent=self, **kwargs)

    def group(self, *args, **kwargs):
        return _command_decorator("group", parent=self, **kwargs)


class _GroupStub(_CommandStub):
    pass


def _command_decorator(kind, *, parent=None, **options):
    def apply(function):
        command_type = _GroupStub if kind == "group" else _CommandStub
        command = command_type(
            function,
            kind=kind,
            name=options.get("name") or function.__name__,
            parent=parent,
            invoke_without_command=options.get("invoke_without_command", False),
        )
        command.usage = options.get("usage")
        if parent is not None:
            parent.commands.append(command)
        return command

    return apply


class _AAA3ACog(_Cog):
    def format_help_for_context(self, ctx):
        help_text = super().format_help_for_context(ctx)
        return (
            f"{help_text}\n\n"
            "Repo name: AAA3A-cogs\n"
            "Documentation: https://aaa3a-cogs.readthedocs.io\n"
            "Translations: https://crowdin.com/project/aaa3a-cogs"
        )


def _member_source_files(path: Path) -> list[Path]:
    if path.name == "__init__.py":
        return sorted(path.parent.rglob("*.py"))
    return [path]


def _runtime_import_targets(path: Path) -> set[str]:
    """Top-level package members this file imports when it executes.

    TYPE_CHECKING blocks are skipped: they never run, and several handlers
    import the cog from one, which would otherwise look like a cycle.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    type_checking_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            type_checking_lines.update(
                range(node.lineno, (node.end_lineno or node.lineno) + 1)
            )
    package_parts = path.relative_to(PACKAGE_DIR).parent.parts
    targets = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        if node.lineno in type_checking_lines:
            continue
        base = package_parts[: len(package_parts) - (node.level - 1)]
        if node.module:
            resolved = base + tuple(node.module.split("."))
            targets.add(resolved[0])
        else:
            targets.update((base + (alias.name,))[0] for alias in node.names)
    return targets


def _dependency_load_order(module_paths: dict[str, Path]) -> tuple[str, ...]:
    """Order top-level members so no module is imported before its dependencies.

    Computed rather than hand-listed: the plan's Phase 0.6 requires new modules
    to be picked up automatically, and a stale hand-written order silently
    leaves two live copies of a module in sys.modules.
    """
    members = sorted(
        name for name in module_paths if "." not in name and name != "honeypot"
    )
    dependencies = {}
    for name in members:
        required = set()
        for path in _member_source_files(module_paths[name]):
            required |= _runtime_import_targets(path)
        dependencies[name] = {
            target
            for target in required
            if target in members and target != name
        }
    order: list[str] = []
    remaining = list(members)
    while remaining:
        ready = [name for name in remaining if dependencies[name] <= set(order)]
        if not ready:
            # A runtime import cycle: stay deterministic and let Python's own
            # import machinery resolve the rest.
            ready = remaining[:1]
        order.extend(ready)
        remaining = [name for name in remaining if name not in set(ready)]
    return tuple(order)


@lru_cache(maxsize=1)
def _honeypot_module_layout() -> tuple[
    tuple[tuple[str, Path], ...], tuple[str, ...]
]:
    """Discover immutable source layout once per pytest worker."""
    module_paths = {}
    for path in PACKAGE_DIR.rglob("*.py"):
        relative = path.relative_to(PACKAGE_DIR)
        if relative == Path("__init__.py"):
            continue
        if relative.name == "__init__.py":
            qualified_name = ".".join(relative.parent.parts)
        else:
            qualified_name = ".".join(relative.with_suffix("").parts)
        module_paths[qualified_name] = path
    return tuple(module_paths.items()), _dependency_load_order(module_paths)


@contextmanager
def _isolated_honeypot_modules(data_path: Path):
    module_path_items, load_order = _honeypot_module_layout()
    module_paths = dict(module_path_items)
    package_name = "NHCogs.honeypot"
    preexisting_honeypot_names = tuple(
        name
        for name in sys.modules
        if name == package_name or name.startswith(f"{package_name}.")
    )
    names = tuple(dict.fromkeys((
        "discord",
        "discord.ext",
        "discord.ext.tasks",
        "redbot",
        "redbot.core",
        "redbot.core.commands",
        "redbot.core.bot",
        "redbot.core.data_manager",
        "redbot.core.i18n",
        "redbot.core.utils",
        "redbot.core.utils.chat_formatting",
        "AAA3A_utils",
        "NHCogs",
        "NHCogs.command_overview",
        "NHCogs.storage",
        package_name,
        *(f"{package_name}.{name}" for name in (*load_order, "honeypot")),
        *preexisting_honeypot_names,
    )))
    previous = {name: sys.modules.get(name, _MISSING) for name in names}

    discord = ModuleType("discord")
    for name in (
        "AllowedMentions",
        "Attachment",
        "ButtonStyle",
        "Color",
        "Embed",
        "File",
        "Guild",
        "Interaction",
        "Member",
        "Message",
        "Object",
        "PermissionOverwrite",
        "Role",
        "SelectOption",
        "TextChannel",
        "Thread",
        "User",
    ):
        setattr(discord, name, object)

    class _Object:
        def __init__(self, *, id):
            self.id = id

    discord.Object = _Object
    discord.ForumChannel = type("ForumChannel", (), {})
    discord.Forbidden = type("Forbidden", (Exception,), {})
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.NotFound = type("NotFound", (discord.HTTPException,), {})

    class _AllowedMentions:
        def __init__(self, **values):
            self.__dict__.update(values)

        @classmethod
        def none(cls):
            return cls(everyone=False, roles=False, users=False, replied_user=False)

    discord.AllowedMentions = _AllowedMentions
    discord.AppCommandType = SimpleNamespace(message="message", user="user")
    discord.ButtonStyle = SimpleNamespace(danger=1, secondary=2, success=3, primary=4)
    discord.TextStyle = SimpleNamespace(short=1, paragraph=2)
    discord.Permissions = SimpleNamespace
    discord.utils = SimpleNamespace(
        snowflake_time=lambda snowflake_id: datetime.fromtimestamp(
            ((snowflake_id >> 22) + 1_420_070_400_000) / 1000,
            timezone.utc,
        )
    )

    class _File:
        def __init__(self, fp, *, filename):
            self.fp = fp
            self.filename = filename

    discord.File = _File

    class _ContextMenu:
        def __init__(self, *, name, callback):
            self.name = name
            self.callback = callback
            self.default_permissions = None
            self.guild_only = bool(
                getattr(callback, "__discord_app_commands_guild_only__", False)
            )
            parameters = tuple(inspect.signature(callback).parameters.values())
            target_annotation = str(parameters[-1].annotation) if parameters else ""
            self.type = (
                discord.AppCommandType.user
                if "Member" in target_annotation or "User" in target_annotation
                else discord.AppCommandType.message
            )

    class _AppCommand:
        def __init__(self, *, name, description, callback):
            self.name = name
            self.description = description
            self.callback = callback
            self.default_permissions = None
            self.guild_only = bool(
                getattr(callback, "__discord_app_commands_guild_only__", False)
            )

    discord.AppCommandType.chat_input = "chat_input"
    def _app_guild_only():
        def decorator(callback):
            callback.__discord_app_commands_guild_only__ = True
            return callback

        return decorator

    discord.app_commands = SimpleNamespace(
        Command=_AppCommand,
        ContextMenu=_ContextMenu,
        guild_only=_app_guild_only,
    )

    class _View:
        def __init__(self, *, timeout=None):
            self.timeout = timeout
            self.children = []
            self.stopped = False

        def add_item(self, item):
            self.children.append(item)
            item.view = self

        def stop(self):
            self.stopped = True

        def clear_items(self):
            self.children.clear()

        def remove_item(self, item):
            self.children.remove(item)

    class _Button:
        def __init__(
            self,
            *,
            label=None,
            style=None,
            disabled=False,
            row=None,
            **values,
        ):
            self.label = label
            self.style = style
            self.disabled = disabled
            self.row = row
            self.callback = None
            self.__dict__.update(values)

    class _Select:
        def __init__(self, *, placeholder=None, options=(), row=None, **values):
            self.placeholder = placeholder
            self.options = options
            self.row = row
            self.values = []
            self.callback = None
            self.__dict__.update(values)

    class _SelectOption:
        def __init__(self, *, label, value=None, default=False, description=None):
            self.label = label
            self.value = label if value is None else value
            self.default = default
            self.description = description

    class _TextInput:
        def __init__(
            self,
            *,
            label=None,
            style=None,
            placeholder=None,
            required=True,
            max_length=None,
            default=None,
        ):
            self.label = label
            self.style = style
            self.placeholder = placeholder
            self.required = required
            self.max_length = max_length
            self.value = default or ""

    class _Modal:
        def __init__(self, *, title, timeout=None):
            self.title = title
            self.timeout = timeout
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class _Label:
        def __init__(self, *, text, component, description=None, id=None):
            self.text = text
            self.component = component
            self.description = description
            self.id = id

    class _Checkbox:
        def __init__(self, *, default=False, **values):
            self.default = default
            self.value = default
            self.__dict__.update(values)

    class _RadioGroup:
        def __init__(self, *, options, required=True, **values):
            self.options = options
            self.required = required
            self.value = None
            self.__dict__.update(values)

    discord.SelectOption = _SelectOption
    discord.RadioGroupOption = _SelectOption
    discord.abc = SimpleNamespace(GuildChannel=object)
    discord.ui = SimpleNamespace(
        Button=_Button,
        Checkbox=_Checkbox,
        Label=_Label,
        Modal=_Modal,
        RadioGroup=_RadioGroup,
        Select=_Select,
        TextInput=_TextInput,
        UserSelect=_Select,
        View=_View,
        button=lambda *args, **kwargs: (lambda function: function),
    )
    discord.ext = ModuleType("discord.ext")
    tasks = SimpleNamespace(
        loop=lambda *args, **kwargs: (lambda function: _LoopStub(function, kwargs))
    )
    discord.ext.tasks = tasks

    class _Converter:
        pass

    class _RawUserIdConverter(int):
        pass

    class _BadArgument(Exception):
        pass

    def _guild_only():
        def apply(function):
            function.guild_only = True
            return function

        return apply

    def _mod_or_permissions(**permissions):
        def apply(function):
            function.mod_or_permissions = permissions
            return function

        return apply

    def _admin_or_permissions(**permissions):
        def apply(function):
            function.admin_or_permissions = permissions
            return function

        return apply

    def _has_permissions(**permissions):
        def apply(function):
            function.has_permissions = permissions
            return function

        return apply

    commands = SimpleNamespace(
        BadArgument=_BadArgument,
        Cog=_Cog,
        Context=object,
        Converter=_Converter,
        Greedy=list,
        Group=_GroupStub,
        RawUserIdConverter=_RawUserIdConverter,
        UserFeedbackCheckFailure=Exception,
        group=lambda *args, **kwargs: _command_decorator("group", **kwargs),
        command=lambda *args, **kwargs: _command_decorator("command", **kwargs),
        guild_only=_guild_only,
        has_permissions=_has_permissions,
        admin_or_permissions=_admin_or_permissions,
        mod_or_permissions=_mod_or_permissions,
        bot_has_guild_permissions=lambda **kwargs: (lambda function: function),
        permissions_check=lambda predicate: (lambda function: function),
    )
    redbot = ModuleType("redbot")
    redbot.core = ModuleType("redbot.core")
    redbot.core.Config = SimpleNamespace(get_conf=lambda *args, **kwargs: _Config())
    redbot.core.commands = commands
    redbot.core.modlog = SimpleNamespace()
    redbot.core.bot = ModuleType("redbot.core.bot")
    redbot.core.bot.Red = object
    redbot.core.data_manager = ModuleType("redbot.core.data_manager")
    redbot.core.data_manager.cog_data_path = lambda cog: data_path
    redbot.core.i18n = ModuleType("redbot.core.i18n")
    redbot.core.i18n.Translator = lambda *args, **kwargs: (lambda text: text)
    redbot.core.i18n.cog_i18n = lambda translator: (lambda cls: cls)
    redbot.core.utils = ModuleType("redbot.core.utils")
    formatting = ModuleType("redbot.core.utils.chat_formatting")
    formatting.box = lambda text, *args, **kwargs: text

    def pagify(text, *, page_length=2000, **kwargs):
        pages = []
        start = 0
        while start < len(text):
            end = min(start + page_length, len(text))
            if end < len(text):
                boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
                end = boundary + 1 if boundary > start else end
            pages.append(text[start:end])
            start = end
        return pages

    formatting.pagify = pagify
    redbot.core.utils.chat_formatting = formatting
    aaa3a_utils = ModuleType("AAA3A_utils")
    aaa3a_utils.Cog = _AAA3ACog
    suite_package = ModuleType("NHCogs")
    suite_package.__path__ = [str(PACKAGE_DIR.parent)]
    package = ModuleType(package_name)
    package.__path__ = [str(PACKAGE_DIR)]

    try:
        for name in preexisting_honeypot_names:
            sys.modules.pop(name, None)
        sys.modules.pop("NHCogs.command_overview", None)
        sys.modules.update(
            {
                "discord": discord,
                "discord.ext": discord.ext,
                "discord.ext.tasks": tasks,
                "redbot": redbot,
                "redbot.core": redbot.core,
                "redbot.core.commands": commands,
                "redbot.core.bot": redbot.core.bot,
                "redbot.core.data_manager": redbot.core.data_manager,
                "redbot.core.i18n": redbot.core.i18n,
                "redbot.core.utils": redbot.core.utils,
                "redbot.core.utils.chat_formatting": formatting,
                "AAA3A_utils": aaa3a_utils,
                "NHCogs": suite_package,
                package_name: package,
            }
        )
        _load_module("NHCogs.storage", PACKAGE_DIR.parent / "storage.py")
        loaded = {
            name: _load_module(f"{package_name}.{name}", module_paths[name])
            for name in load_order
        }
        runtime = loaded["detection_runtime"]

        async def test_bounded_reader(attachment, max_bytes):
            data = await attachment.read(use_cached=True)
            return data[: max_bytes + 1]

        runtime.read_attachment_bounded = test_bounded_reader
        yield _load_module(f"{package_name}.honeypot", module_paths["honeypot"])
    finally:
        touched_names = set(previous)
        touched_names.update(
            name
            for name in sys.modules
            if name == package_name or name.startswith(f"{package_name}.")
        )
        for name in touched_names:
            module = previous.get(name, _MISSING)
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class _AppCommandTree:
    def __init__(self):
        self.commands = {}

    def get_command(self, name, *, type):
        return self.commands.get((name, type))

    def add_command(self, command, *, override=False):
        command_type = getattr(command, "type", "chat_input")
        self.commands[(command.name, command_type)] = command

    def remove_command(self, name, *, type):
        return self.commands.pop((name, type), None)


class _Bot:
    def __init__(self, ready=True):
        self.ready = asyncio.Event()
        self.tree = _AppCommandTree()
        self.guilds = []
        if ready:
            self.ready.set()

    async def wait_until_red_ready(self):
        await self.ready.wait()

    def add_view(self, view, *, message_id=None):
        self.restored_views = getattr(self, "restored_views", [])
        self.restored_views.append((view, message_id))

    def get_guild(self, guild_id):
        return None


async def _async_noop(*args, **kwargs):
    return None


def active_case(store, guild_id: int, user_id: int):
    return next(
        (
            snapshot
            for snapshot in store.list_open_cases()
            if snapshot.case.guild_id == guild_id
            and snapshot.case.user_id == user_id
        ),
        None,
    )


_DRAIN_PASSES = 5


async def drain_background_work(*cogs) -> None:
    """Wait for background work to finish before a temporary directory is torn down.

    A case review follow-up hands sqlite and filesystem work to an
    ``asyncio.to_thread`` worker. On Windows that worker still holds an open WAL
    connection to ``detection_cases.sqlite`` while ``TemporaryDirectory.__exit__``
    tries to remove the tree, and the removal then fails with
    ``PermissionError: [WinError 32]``. Call this as the last statement inside the
    temporary-directory block of any test that leaves such work running.

    ``cog_unload`` is deliberately not used: it cancels the tasks and returns while
    the worker still owns the file. The follow-up tasks are awaited without
    ``return_exceptions`` so a follow-up that fails still fails the test. Unrelated
    loop work the test abandoned on purpose is only quiesced, not asserted on.

    Awaiting the tasks is not sufficient on its own. When a test cancels the work
    mid-flight the thread that ``to_thread`` started keeps running with no task left
    to await it, so the executor is shut down as well; that call returns only once
    every worker has finished and released the store.
    """
    for cog in cogs:
        pending = tuple(getattr(cog, "_case_review_tasks", ()))
        if pending:
            await asyncio.gather(*pending)
    current = asyncio.current_task()
    for _ in range(_DRAIN_PASSES):
        outstanding = tuple(
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        )
        if not outstanding:
            break
        await asyncio.gather(*outstanding, return_exceptions=True)
    await asyncio.get_running_loop().shutdown_default_executor()


class DetectionPipelineTestCase(unittest.IsolatedAsyncioTestCase):
    """Message and public-boundary fixtures shared by the detection tests."""

    @staticmethod
    def _message(
        honeypot,
        *,
        attachment_count=3,
        delete_error=None,
        message_id=300,
        channel_id=400,
    ):
        attachments = [
            SimpleNamespace(
                filename=f"proof-{position}.png",
                size=len(f"image-{position}".encode()),
                content_type="image/png",
                width=10,
                height=20,
                description=None,
                is_spoiler=lambda: False,
                url=f"https://cdn.test/proof-{position}.png",
                read=mock.AsyncMock(return_value=f"image-{position}".encode()),
            )
            for position in range(1, attachment_count + 1)
        ]
        for attachment in attachments:
            async def read_bounded(max_bytes, *, _attachment=attachment):
                data = await _attachment.read(use_cached=True)
                return data[: max_bytes + 1]

            attachment.read_bounded = read_bounded
        guild = SimpleNamespace(
            id=100,
            name="Guild",
            icon=None,
            get_channel=lambda channel_id: None,
            get_thread=lambda channel_id: None,
        )
        author = SimpleNamespace(
            id=200,
            bot=False,
            roles=[],
            display_name="User",
            display_avatar=None,
            created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            joined_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        )
        message = SimpleNamespace(
            id=message_id,
            guild=guild,
            author=author,
            channel=SimpleNamespace(id=channel_id),
            content="forward evidence",
            attachments=attachments,
            created_at=datetime(2026, 7, 13, 12, tzinfo=timezone.utc),
            jump_url="https://discord.test/channels/100/400/300",
            webhook_id=None,
        )
        message.delete = mock.AsyncMock(side_effect=delete_error)
        return message

    @staticmethod
    def _configure_public_boundary(cog, config):
        cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
        cog._message_registry._initialize_sync()
        cog.config = SimpleNamespace(
            guild=lambda guild: SimpleNamespace(all=mock.AsyncMock(return_value=config)),
            guild_from_id=lambda guild_id: SimpleNamespace(
                all=mock.AsyncMock(return_value=config)
            ),
        )
        cog._is_protected_member = mock.AsyncMock(return_value=False)
        cog._is_forward_purge_active = mock.Mock(return_value=True)
        cog._handle_spam_message = mock.AsyncMock()
        cog._handle_firstpost_message = mock.AsyncMock()
        cog._handle_imagescan_detector_message = mock.AsyncMock()
        cog._increment_stat = mock.AsyncMock()
        cog._purge_detection_case_cached_messages = mock.AsyncMock(return_value=0)


class CaseExpiryTestCase(unittest.IsolatedAsyncioTestCase):
    """Config, case-append and operation fixtures shared by the case tests."""

    @staticmethod
    def _config(values):
        config = _Config()
        config.register_guild(**values)
        return config

    @staticmethod
    def _append_case(honeypot, cog, created_at, *, message_id=40):
        cog._case_store.initialize()
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=10,
                user_id=20,
                channel_id=30,
                message_id=message_id,
                content="evidence",
                created_at=created_at,
                jump_url=f"https://discord.test/messages/{message_id}",
                attachments=(),
            ),
            (),
        )

    @staticmethod
    def _complete_case_operation(cog, case_id, result, now):
        operation = cog._case_store.ensure_operation(
            case_id,
            "moderation_action",
            f"moderation-action:{case_id}:1",
            1,
        )
        claimed = cog._case_store.claim_operation(operation.operation_id, now)
        cog._case_store.complete_operation(
            claimed.operation_id,
            claimed.claim_token,
            now,
            result,
        )
