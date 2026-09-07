import asyncio
import importlib.util
import json
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs"
_MISSING = object()


def _record_construction(bot, name):
    bot.constructed.append(name)
    if bot.failure == (name, "construct"):
        raise RuntimeError(f"failed constructing {name}")


class StubLifecycle:
    async def cog_load(self):
        self.bot.started.append(self.qualified_name)
        if self.bot.failure == (self.qualified_name, "cog_load"):
            raise RuntimeError(f"failed loading {self.qualified_name}")
        if self.bot.failure == (self.qualified_name, "cancel"):
            raise asyncio.CancelledError

    async def cog_unload(self):
        self.bot.unloaded.append(self.qualified_name)


class StubConsoleDump(StubLifecycle):
    qualified_name = "ConsoleDump"

    def __init__(self, bot, support):
        self.bot = bot
        _record_construction(bot, self.qualified_name)


class StubOperationalSupport(StubLifecycle):
    qualified_name = "OperationalSupport"

    def __init__(self, bot):
        self.bot = bot
        _record_construction(bot, self.qualified_name)

    async def report_global_error(self, **failure):
        self.bot.error_reports.append(failure)


class StubNHMisc(StubLifecycle):
    qualified_name = "NHMisc"
    CONFIG_IDENTIFIER = 8597423150612235807

    def __init__(self, bot, support):
        self.bot = bot
        self.support = support
        _record_construction(bot, self.qualified_name)


class StubHoneypot(StubLifecycle):
    qualified_name = "Honeypot"
    CONFIG_IDENTIFIER = 205192943327321000143939875896557571750

    def __init__(self, bot, support):
        self.bot = bot
        _record_construction(bot, self.qualified_name)


class StubCleanup(StubLifecycle):
    qualified_name = "Cleanup"

    def __init__(self, bot):
        self.bot = bot
        _record_construction(bot, self.qualified_name)


StubCleanup.__module__ = "NHCogs.cleanup.cog"


class StubGitHubTickets(StubLifecycle):
    qualified_name = "GitHubTickets"
    CONFIG_IDENTIFIER = 228724500916148494760637198509440112622

    def __init__(self, bot, support):
        self.bot = bot
        _record_construction(bot, self.qualified_name)


class StubNHModeration(StubLifecycle):
    qualified_name = "NHModeration"
    CONFIG_IDENTIFIER = 205192943327321000143939875896557571751

    def __init__(self, bot, support):
        self.bot = bot
        _record_construction(bot, self.qualified_name)


class StubCustomCommandsMigration(StubLifecycle):
    qualified_name = "CustomCommandsMigration"

    def __init__(self, bot):
        self.bot = bot


StubCustomCommandsMigration.__module__ = "NHCogs.custom_commands.migration_controller"


class StubCustomCommands(StubCustomCommandsMigration):
    qualified_name = "CustomCommands"


def _stub_module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


@contextmanager
def load_suite_module():
    names = (
        "redbot",
        "redbot.core",
        "redbot.core.bot",
        "redbot.core.utils",
        "NHCogs",
        "NHCogs.consoledump",
        "NHCogs.operational_support",
        "NHCogs.nhmisc",
        "NHCogs.honeypot",
        "NHCogs.cleanup",
        "NHCogs.githubtickets",
        "NHCogs.nhmoderation",
        "NHCogs.custom_commands",
    )
    previous = {name: sys.modules.get(name, _MISSING) for name in names}
    redbot = types.ModuleType("redbot")
    redbot_core = types.ModuleType("redbot.core")
    redbot_bot = types.ModuleType("redbot.core.bot")
    redbot_bot.Red = object
    redbot_utils = types.ModuleType("redbot.core.utils")
    redbot_utils.get_end_user_data_statement = lambda **_kwargs: "data statement"
    console_dump = _stub_module("NHCogs.consoledump", ConsoleDump=StubConsoleDump)
    support = _stub_module("NHCogs.operational_support", OperationalSupport=StubOperationalSupport)
    nhmisc = _stub_module("NHCogs.nhmisc", NHMisc=StubNHMisc)
    honeypot = _stub_module("NHCogs.honeypot", Honeypot=StubHoneypot)
    cleanup = types.ModuleType("NHCogs.cleanup")

    async def build_cleanup_component(bot, _nhmisc, _honeypot):
        return StubCleanup(bot)

    def assert_cleanup_safe_to_replace(bot):
        bot.cleanup_preflight_calls += 1
        if bot.failure == ("cleanup_preflight", "before"):
            raise RuntimeError("cleanup ownership conflict")

    cleanup.build_cleanup_component = build_cleanup_component
    cleanup.assert_safe_to_replace = assert_cleanup_safe_to_replace
    cleanup.Cleanup = StubCleanup
    githubtickets = _stub_module("NHCogs.githubtickets", GitHubTickets=StubGitHubTickets)
    nhmoderation = types.ModuleType("NHCogs.nhmoderation")
    nhmoderation.NHModeration = StubNHModeration
    custom_commands = types.ModuleType("NHCogs.custom_commands")

    async def build_custom_commands_component(bot, support):
        cog_type = StubCustomCommands if bot.migrated else StubCustomCommandsMigration
        cog = cog_type(bot)
        cog.support = support
        return cog

    def assert_safe_to_replace(bot):
        bot.preflight_calls += 1
        if bot.failure == ("preflight", "before"):
            raise RuntimeError("custom commands ownership conflict")

    custom_commands.build_custom_commands_component = build_custom_commands_component
    custom_commands.assert_safe_to_replace = assert_safe_to_replace
    custom_commands.CustomCommands = StubCustomCommandsMigration
    spec = importlib.util.spec_from_file_location(
        "NHCogs",
        PACKAGE_PATH / "__init__.py",
        submodule_search_locations=[str(PACKAGE_PATH)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    try:
        sys.modules.update(
            {
                "redbot": redbot,
                "redbot.core": redbot_core,
                "redbot.core.bot": redbot_bot,
                "redbot.core.utils": redbot_utils,
                "NHCogs": module,
                "NHCogs.consoledump": console_dump,
                "NHCogs.operational_support": support,
                "NHCogs.nhmisc": nhmisc,
                "NHCogs.honeypot": honeypot,
                "NHCogs.cleanup": cleanup,
                "NHCogs.githubtickets": githubtickets,
                "NHCogs.nhmoderation": nhmoderation,
                "NHCogs.custom_commands": custom_commands,
            }
        )
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, old_module in previous.items():
            if old_module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


class FakeBot:
    def __init__(self, failure=None):
        self.failure = failure
        self.migrated = False
        self.error_reports = []
        self.cogs = {}
        self.added = []
        self.removed = []
        self.constructed = []
        self.started = []
        self.unloaded = []
        self.preflight_calls = 0
        self.cleanup_preflight_calls = 0

    async def add_cog(self, cog):
        name = cog.qualified_name
        self.added.append(name)
        if self.failure == (name, "before"):
            raise RuntimeError(f"failed before adding {name}")
        if name in self.cogs:
            raise RuntimeError(f"cog already loaded: {name}")
        await cog.cog_load()
        if self.failure == (name, "framework_cleanup"):
            await cog.cog_unload()
            raise RuntimeError(f"framework cleaned {name} before failing")
        self.cogs[name] = cog
        if self.failure == (name, "after"):
            raise RuntimeError(f"failed after adding {name}")

    async def remove_cog(self, name):
        self.removed.append(name)
        cog = self.cogs.pop(name, None)
        if cog is not None:
            await cog.cog_unload()
        return cog

    def get_cog(self, name):
        return self.cogs.get(name)


class NHCogsSuiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_nhmisc_startup_failure_does_not_disable_custom_commands(self):
        for phase in ("construct", "cog_load", "before", "after"):
            for migrated in (False, True):
                with self.subTest(phase=phase, migrated=migrated), load_suite_module() as suite:
                    bot = FakeBot(("NHMisc", phase))
                    bot.migrated = migrated
                    with self.assertLogs("red.NHCogs", level="ERROR"):
                        await suite.setup(bot)

                    self.assertNotIn("NHMisc", bot.cogs)
                    name = "CustomCommands" if migrated else "CustomCommandsMigration"
                    self.assertIs(bot.cogs[name].support, bot.cogs["OperationalSupport"])

    async def test_support_failure_skips_consumers_but_keeps_unrelated_cogs(self):
        with load_suite_module() as suite:
            bot = FakeBot(("OperationalSupport", "cog_load"))
            with self.assertLogs("red.NHCogs", level="ERROR"):
                await suite.setup(bot)

        self.assertEqual(set(bot.cogs), set())
        self.assertEqual(bot.unloaded, ["OperationalSupport"])

    async def test_setup_registers_the_complete_nhcogs_suite(self):
        with load_suite_module() as suite:
            bot = FakeBot()

            await suite.setup(bot)

        self.assertEqual(
            bot.added,
            [
                "OperationalSupport",
                "ConsoleDump",
                "NHMisc",
                "Honeypot",
                "Cleanup",
                "GitHubTickets",
                "NHModeration",
                "CustomCommandsMigration",
            ],
        )
        self.assertEqual(
            set(bot.cogs),
            {
                "OperationalSupport",
                "ConsoleDump",
                "NHMisc",
                "Honeypot",
                "Cleanup",
                "GitHubTickets",
                "NHModeration",
                "CustomCommandsMigration",
            },
        )
        self.assertEqual(bot.removed, [])
        self.assertEqual(bot.preflight_calls, 1)
        self.assertEqual(bot.cleanup_preflight_calls, 1)
        self.assertIs(bot.cogs["NHMisc"].support, bot.cogs["CustomCommandsMigration"].support)

    async def test_custom_commands_conflict_does_not_block_other_subcogs(self):
        with load_suite_module() as suite:
            bot = FakeBot(("preflight", "before"))

            with self.assertLogs("red.NHCogs", level="ERROR") as captured:
                await suite.setup(bot)

        self.assertEqual(bot.preflight_calls, 1)
        self.assertEqual(
            set(bot.cogs),
            {
                "OperationalSupport",
                "ConsoleDump",
                "NHMisc",
                "Honeypot",
                "Cleanup",
                "GitHubTickets",
                "NHModeration",
            },
        )
        self.assertIn("custom commands ownership conflict", "\n".join(captured.output))
        self.assertEqual(bot.removed, [])

    async def test_cleanup_conflict_does_not_block_other_subcogs(self):
        with load_suite_module() as suite:
            bot = FakeBot(("cleanup_preflight", "before"))

            with self.assertLogs("red.NHCogs", level="ERROR") as captured:
                await suite.setup(bot)

        self.assertEqual(bot.cleanup_preflight_calls, 1)
        self.assertEqual(bot.preflight_calls, 1)
        self.assertEqual(
            set(bot.cogs),
            {
                "OperationalSupport",
                "ConsoleDump",
                "NHMisc",
                "Honeypot",
                "GitHubTickets",
                "NHModeration",
                "CustomCommandsMigration",
            },
        )
        self.assertIn("cleanup ownership conflict", "\n".join(captured.output))
        self.assertEqual(bot.removed, [])

    async def test_each_subcog_add_failure_is_isolated_and_logged(self):
        failures = (
            ("ConsoleDump", "before"),
            ("ConsoleDump", "after"),
            ("NHMisc", "before"),
            ("NHMisc", "after"),
            ("Honeypot", "before"),
            ("Honeypot", "after"),
            ("Cleanup", "before"),
            ("Cleanup", "after"),
            ("GitHubTickets", "before"),
            ("GitHubTickets", "after"),
            ("NHModeration", "before"),
            ("NHModeration", "after"),
            ("CustomCommandsMigration", "before"),
            ("CustomCommandsMigration", "after"),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                with load_suite_module() as suite:
                    bot = FakeBot(failure)
                    expected_error = f"failed {failure[1]} adding {failure[0]}"

                    with self.assertLogs("red.NHCogs", level="ERROR") as captured:
                        await suite.setup(bot)

                self.assertNotIn(failure[0], bot.cogs)
                if failure[0] != "CustomCommandsMigration":
                    for name in (
                        "OperationalSupport",
                        "ConsoleDump",
                        "NHMisc",
                        "Honeypot",
                        "Cleanup",
                        "GitHubTickets",
                        "NHModeration",
                    ):
                        if name != failure[0]:
                            if name == "Cleanup" and failure[0] in {"OperationalSupport", "Honeypot"}:
                                continue
                            if failure[0] == "OperationalSupport" and name == "NHMisc":
                                continue
                            self.assertIn(name, bot.cogs)
                self.assertIn(expected_error, "\n".join(captured.output))
                self.assertEqual(
                    bot.removed,
                    [failure[0]] if failure[1] == "after" else [],
                )

    async def test_each_subcog_construction_failure_is_isolated_and_logged(self):
        for name in (
            "OperationalSupport",
            "ConsoleDump",
            "NHMisc",
            "Honeypot",
            "Cleanup",
            "GitHubTickets",
            "NHModeration",
        ):
            with self.subTest(name=name):
                with load_suite_module() as suite:
                    bot = FakeBot((name, "construct"))

                    with self.assertLogs("red.NHCogs", level="ERROR") as captured:
                        await suite.setup(bot)

                self.assertNotIn(name, bot.cogs)
                for other in (
                    "OperationalSupport",
                    "ConsoleDump",
                    "NHMisc",
                    "Honeypot",
                    "Cleanup",
                    "GitHubTickets",
                    "NHModeration",
                ):
                    if other != name:
                        if other == "Cleanup" and name in {"OperationalSupport", "Honeypot"}:
                            continue
                        if name == "OperationalSupport":
                            continue
                        self.assertIn(other, bot.cogs)
                self.assertIn(
                    f"failed constructing {name}",
                    "\n".join(captured.output),
                )

    async def test_each_subcog_cog_load_failure_is_cleaned_up_and_isolated(self):
        for name in (
            "OperationalSupport",
            "ConsoleDump",
            "NHMisc",
            "Honeypot",
            "Cleanup",
            "GitHubTickets",
            "NHModeration",
        ):
            with self.subTest(name=name):
                with load_suite_module() as suite:
                    bot = FakeBot((name, "cog_load"))

                    with self.assertLogs("red.NHCogs", level="ERROR") as captured:
                        await suite.setup(bot)

                self.assertNotIn(name, bot.cogs)
                self.assertEqual(bot.unloaded.count(name), 1)
                self.assertIn(f"failed loading {name}", "\n".join(captured.output))
                for other in (
                    "OperationalSupport",
                    "ConsoleDump",
                    "NHMisc",
                    "Honeypot",
                    "Cleanup",
                    "GitHubTickets",
                    "NHModeration",
                ):
                    if other != name:
                        if other == "Cleanup" and name in {"OperationalSupport", "Honeypot"}:
                            continue
                        if name == "OperationalSupport":
                            continue
                        self.assertIn(other, bot.cogs)

    async def test_setup_cancellation_cleans_loaded_and_partial_cogs_then_reraises(self):
        with load_suite_module() as suite:
            bot = FakeBot(("Honeypot", "cancel"))

            with self.assertRaises(asyncio.CancelledError):
                await suite.setup(bot)

        self.assertEqual(bot.cogs, {})
        self.assertEqual(bot.removed, ["NHMisc", "ConsoleDump", "OperationalSupport"])
        self.assertEqual(bot.unloaded, ["Honeypot", "NHMisc", "ConsoleDump", "OperationalSupport"])

    async def test_framework_cleanup_is_not_repeated_by_supervisor(self):
        with load_suite_module() as suite:
            bot = FakeBot(("GitHubTickets", "framework_cleanup"))

            with self.assertLogs("red.NHCogs", level="ERROR"):
                await suite.setup(bot)

        self.assertEqual(bot.unloaded.count("GitHubTickets"), 1)
        self.assertNotIn("GitHubTickets", bot.cogs)

    async def test_import_failure_is_isolated_and_logs_complete_traceback(self):
        with load_suite_module() as suite:
            bot = FakeBot()
            real_import = suite.import_module

            def import_with_githubtickets_failure(name, package=None):
                if name == ".githubtickets":
                    raise RuntimeError("githubtickets import exploded")
                return real_import(name, package)

            suite.import_module = import_with_githubtickets_failure
            with self.assertLogs("red.NHCogs", level="ERROR") as captured:
                await suite.setup(bot)

        self.assertNotIn("GitHubTickets", bot.cogs)
        self.assertEqual(
            set(bot.cogs),
            {
                "OperationalSupport",
                "ConsoleDump",
                "NHMisc",
                "Honeypot",
                "Cleanup",
                "NHModeration",
                "CustomCommandsMigration",
            },
        )
        log_output = "\n".join(captured.output)
        self.assertIn("githubtickets import exploded", log_output)
        self.assertIn("Traceback (most recent call last)", log_output)

    async def test_existing_cog_conflict_does_not_block_other_subcogs(self):
        with load_suite_module() as suite:
            bot = FakeBot()
            existing = StubGitHubTickets(bot, object())
            bot.cogs[existing.qualified_name] = existing

            with self.assertLogs("red.NHCogs", level="ERROR") as captured:
                await suite.setup(bot)

        self.assertIs(bot.cogs["GitHubTickets"], existing)
        self.assertEqual(
            set(bot.cogs),
            {
                "OperationalSupport",
                "ConsoleDump",
                "NHMisc",
                "Honeypot",
                "Cleanup",
                "GitHubTickets",
                "NHModeration",
                "CustomCommandsMigration",
            },
        )
        self.assertIn("cog already loaded", "\n".join(captured.output))
        self.assertEqual(bot.removed, [])

    def test_combined_metadata_preserves_both_data_contracts(self):
        metadata = json.loads((PACKAGE_PATH / "info.json").read_text("utf-8"))

        self.assertEqual(metadata["name"], "NHCogs")
        self.assertEqual(
            metadata["description"],
            "Loads ConsoleDump, NHMisc, Honeypot, Cleanup, GitHubTickets, NHModeration, and Custom Commands together "
            "while preserving their separate commands, configuration, and stored data",
        )
        self.assertEqual(metadata["min_bot_version"], "3.5.23")
        self.assertEqual(metadata["min_python_version"], [3, 10, 0])
        self.assertEqual(
            metadata["requirements"],
            [
                "matplotlib",
                "git+https://github.com/AAA3A-AAA3A/AAA3A_utils.git",
                "Pillow>=11.3.0",
                "pillow-avif-plugin>=1.6.0",
            ],
        )
        statement = metadata["end_user_data_statement"]
        self.assertIn("Activity tracking stores user IDs", statement)
        self.assertIn("moderation case metadata", statement)
        self.assertIn("Custom Commands stores guild IDs", statement)
        self.assertIn("Operational error records store the guild", statement)
        self.assertIn("GitHubTickets stores guild and user IDs", statement)
        self.assertIn("NHModeration stores source observations", statement)

    async def test_teardown_removes_late_registered_replacement_cog(self):
        with load_suite_module() as suite:
            bot = FakeBot()
            await suite.setup(bot)

            await suite.teardown(bot)

        self.assertIn("CustomCommandsMigration", bot.removed)
        self.assertNotIn("CustomCommandsMigration", bot.cogs)
        self.assertIn("Cleanup", bot.removed)
        self.assertNotIn("Cleanup", bot.cogs)
