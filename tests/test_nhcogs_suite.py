import importlib.util
import json
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs"
_MISSING = object()


class StubNHMisc:
    qualified_name = "NHMisc"
    CONFIG_IDENTIFIER = 8597423150612235807

    def __init__(self, bot):
        self.bot = bot
        bot.constructed.append(self.qualified_name)


class StubHoneypot:
    qualified_name = "Honeypot"
    CONFIG_IDENTIFIER = 205192943327321000143939875896557571750

    def __init__(self, bot):
        self.bot = bot
        bot.constructed.append(self.qualified_name)


class StubGitHubTickets:
    qualified_name = "GitHubTickets"
    CONFIG_IDENTIFIER = 228724500916148494760637198509440112622

    def __init__(self, bot):
        self.bot = bot
        bot.constructed.append(self.qualified_name)


class StubCustomCommandsMigration:
    qualified_name = "CustomCommandsMigration"

    def __init__(self, bot):
        self.bot = bot


StubCustomCommandsMigration.__module__ = "NHCogs.custom_commands.migration_controller"


@contextmanager
def load_suite_module():
    names = (
        "redbot",
        "redbot.core",
        "redbot.core.bot",
        "redbot.core.utils",
        "NHCogs",
        "NHCogs.nhmisc",
        "NHCogs.honeypot",
        "NHCogs.githubtickets",
        "NHCogs.custom_commands",
    )
    previous = {name: sys.modules.get(name, _MISSING) for name in names}
    redbot = types.ModuleType("redbot")
    redbot_core = types.ModuleType("redbot.core")
    redbot_bot = types.ModuleType("redbot.core.bot")
    redbot_bot.Red = object
    redbot_utils = types.ModuleType("redbot.core.utils")
    redbot_utils.get_end_user_data_statement = lambda **_kwargs: "data statement"
    nhmisc = types.ModuleType("NHCogs.nhmisc")
    nhmisc.NHMisc = StubNHMisc
    honeypot = types.ModuleType("NHCogs.honeypot")
    honeypot.Honeypot = StubHoneypot
    githubtickets = types.ModuleType("NHCogs.githubtickets")
    githubtickets.GitHubTickets = StubGitHubTickets
    custom_commands = types.ModuleType("NHCogs.custom_commands")

    async def build_custom_commands_component(bot, _nhmisc):
        return StubCustomCommandsMigration(bot)

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
                "NHCogs.nhmisc": nhmisc,
                "NHCogs.honeypot": honeypot,
                "NHCogs.githubtickets": githubtickets,
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
        self.cogs = {}
        self.added = []
        self.removed = []
        self.constructed = []
        self.preflight_calls = 0

    async def add_cog(self, cog):
        name = cog.qualified_name
        self.added.append(name)
        if self.failure == (name, "before"):
            raise RuntimeError(f"failed before adding {name}")
        if name in self.cogs:
            raise RuntimeError(f"cog already loaded: {name}")
        self.cogs[name] = cog
        if self.failure == (name, "after"):
            raise RuntimeError(f"failed after adding {name}")

    async def remove_cog(self, name):
        self.removed.append(name)
        return self.cogs.pop(name, None)

    def get_cog(self, name):
        return self.cogs.get(name)


class NHCogsSuiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_registers_the_complete_nhcogs_suite(self):
        with load_suite_module() as suite:
            bot = FakeBot()

            await suite.setup(bot)

            self.assertEqual(suite.NHMisc.__name__, "StubNHMisc")
            self.assertEqual(suite.NHMisc.CONFIG_IDENTIFIER, 8597423150612235807)
            self.assertEqual(
                suite.Honeypot.CONFIG_IDENTIFIER,
                205192943327321000143939875896557571750,
            )
            self.assertEqual(
                suite.GitHubTickets.CONFIG_IDENTIFIER,
                228724500916148494760637198509440112622,
            )

        self.assertEqual(
            bot.added,
            ["NHMisc", "Honeypot", "GitHubTickets", "CustomCommandsMigration"],
        )
        self.assertEqual(
            set(bot.cogs),
            {"NHMisc", "Honeypot", "GitHubTickets", "CustomCommandsMigration"},
        )
        self.assertEqual(bot.removed, [])
        self.assertEqual(bot.preflight_calls, 1)

    async def test_setup_rejects_custom_commands_conflict_before_suite_effects(self):
        with load_suite_module() as suite:
            bot = FakeBot(("preflight", "before"))

            with self.assertRaisesRegex(
                RuntimeError,
                "custom commands ownership conflict",
            ):
                await suite.setup(bot)

        self.assertEqual(bot.preflight_calls, 1)
        self.assertEqual(bot.constructed, [])
        self.assertEqual(bot.added, [])
        self.assertEqual(bot.removed, [])

    async def test_setup_compensates_every_add_failure(self):
        failures = (
            ("NHMisc", "before"),
            ("NHMisc", "after"),
            ("Honeypot", "before"),
            ("Honeypot", "after"),
            ("GitHubTickets", "before"),
            ("GitHubTickets", "after"),
            ("CustomCommandsMigration", "before"),
            ("CustomCommandsMigration", "after"),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                with load_suite_module() as suite:
                    bot = FakeBot(failure)
                    expected_error = f"failed {failure[1]} adding {failure[0]}"

                    with self.assertRaisesRegex(RuntimeError, expected_error):
                        await suite.setup(bot)

                self.assertEqual(bot.cogs, {})
                expected_removed = (
                    list(reversed(bot.added[:-1]))
                    if failure[1] == "before"
                    else list(reversed(bot.added))
                )
                self.assertEqual(bot.removed, expected_removed)

    async def test_setup_failure_preserves_a_previously_loaded_cog(self):
        with load_suite_module() as suite:
            bot = FakeBot()
            existing = StubGitHubTickets(bot)
            bot.cogs[existing.qualified_name] = existing

            with self.assertRaisesRegex(RuntimeError, "cog already loaded"):
                await suite.setup(bot)

        self.assertIs(bot.cogs["GitHubTickets"], existing)
        self.assertEqual(bot.removed, ["Honeypot", "NHMisc"])

    def test_combined_metadata_preserves_both_data_contracts(self):
        metadata = json.loads((PACKAGE_PATH / "info.json").read_text("utf-8"))

        self.assertEqual(metadata["name"], "NHCogs")
        self.assertEqual(
            metadata["description"],
            "Loads NHMisc, Honeypot, GitHubTickets, and Custom Commands together "
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

    async def test_teardown_removes_late_registered_replacement_cog(self):
        with load_suite_module() as suite:
            bot = FakeBot()
            await suite.setup(bot)

            await suite.teardown(bot)

        self.assertIn("CustomCommandsMigration", bot.removed)
        self.assertNotIn("CustomCommandsMigration", bot.cogs)
