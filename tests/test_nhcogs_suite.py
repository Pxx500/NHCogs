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

    def __init__(self, bot):
        self.bot = bot


class StubHoneypot:
    qualified_name = "Honeypot"

    def __init__(self, bot):
        self.bot = bot


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

    async def add_cog(self, cog):
        name = cog.qualified_name
        self.added.append(name)
        if self.failure == (name, "before"):
            raise RuntimeError(f"failed before adding {name}")
        self.cogs[name] = cog
        if self.failure == (name, "after"):
            raise RuntimeError(f"failed after adding {name}")

    async def remove_cog(self, name):
        self.removed.append(name)
        return self.cogs.pop(name, None)


class NHCogsSuiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_registers_nhmisc_then_honeypot(self):
        with load_suite_module() as suite:
            bot = FakeBot()

            await suite.setup(bot)

        self.assertEqual(bot.added, ["NHMisc", "Honeypot"])
        self.assertEqual(set(bot.cogs), {"NHMisc", "Honeypot"})
        self.assertEqual(bot.removed, [])

    async def test_setup_compensates_every_add_failure(self):
        failures = (
            ("NHMisc", "before"),
            ("NHMisc", "after"),
            ("Honeypot", "before"),
            ("Honeypot", "after"),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                with load_suite_module() as suite:
                    bot = FakeBot(failure)
                    expected_error = f"failed {failure[1]} adding {failure[0]}"

                    with self.assertRaisesRegex(RuntimeError, expected_error):
                        await suite.setup(bot)

                self.assertEqual(bot.cogs, {})
                self.assertEqual(bot.removed, list(reversed(bot.added)))

    def test_combined_metadata_preserves_both_data_contracts(self):
        metadata = json.loads((PACKAGE_PATH / "info.json").read_text("utf-8"))

        self.assertEqual(metadata["name"], "NHCogs")
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
