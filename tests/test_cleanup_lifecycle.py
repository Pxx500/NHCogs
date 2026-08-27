import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

CLEANUP_PATH = Path(__file__).parents[1] / "NHCogs" / "cleanup"


def load_lifecycle_module():
    package_name = "cleanup_lifecycle_subject"
    package = types.ModuleType(package_name)
    package.__path__ = [str(CLEANUP_PATH)]
    sys.modules[package_name] = package

    cog = types.ModuleType(f"{package_name}.cog")

    class Cleanup:
        qualified_name = "Cleanup"

        def __init__(self, bot, nhmisc, honeypot):
            self.bot = bot
            self.nhmisc = nhmisc
            self.honeypot = honeypot

    cog.Cleanup = Cleanup
    sys.modules[cog.__name__] = cog

    name = f"{package_name}.lifecycle"
    spec = importlib.util.spec_from_file_location(name, CLEANUP_PATH / "lifecycle.py")
    assert spec is not None
    assert spec.loader is not None
    lifecycle = importlib.util.module_from_spec(spec)
    sys.modules[name] = lifecycle
    spec.loader.exec_module(lifecycle)
    return cog, lifecycle


cog, lifecycle = load_lifecycle_module()


class PackageConfig:
    def __init__(self, packages):
        self.packages_value = packages

    async def packages(self):
        return list(self.packages_value)


class Bot:
    def __init__(self):
        official_extension = types.ModuleType("redbot.cogs.cleanup")
        official_type = type("OfficialCleanup", (), {})
        official_type.__module__ = "redbot.cogs.cleanup.cleanup"
        official = official_type()
        self.extensions = {"cleanup": official_extension}
        self.cogs = {"Cleanup": official}
        self.commands = {"cleanup": SimpleNamespace(cog=official)}
        self.packages = ["NHCogs", "cleanup"]
        self._config = PackageConfig(self.packages)
        self.events = []

    async def add_loaded_package(self, name):
        self.events.append(("add package", name))
        if name not in self.packages:
            self.packages.append(name)

    async def remove_loaded_package(self, name):
        self.events.append(("remove package", name))
        while name in self.packages:
            self.packages.remove(name)

    async def unload_extension(self, name):
        self.events.append(("unload", name))
        self.extensions.pop(name, None)
        self.cogs.pop("Cleanup", None)
        self.commands.pop("cleanup", None)

    async def add_cog(self, runtime):
        self.events.append(("add cog", runtime.qualified_name))
        self.cogs[runtime.qualified_name] = runtime
        self.commands["cleanup"] = SimpleNamespace(cog=runtime)

    async def remove_cog(self, name):
        self.cogs.pop(name, None)
        self.commands.pop("cleanup", None)

    def get_cog(self, name):
        return self.cogs.get(name)

    def get_command(self, name):
        return self.commands.get(name)


class CleanupReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def test_activation_removes_official_package_before_registering_replacement(self):
        bot = Bot()
        runtime = await lifecycle.ReplacementActivator(
            bot,
            object(),
            object(),
        ).activate()

        self.assertIsInstance(runtime, cog.Cleanup)
        self.assertNotIn("cleanup", bot.packages)
        self.assertIs(bot.get_cog("Cleanup"), runtime)
        self.assertIs(bot.get_command("cleanup").cog, runtime)
        self.assertEqual(
            bot.events,
            [
                ("add package", "NHCogs"),
                ("remove package", "cleanup"),
                ("unload", "cleanup"),
                ("add cog", "Cleanup"),
            ],
        )

    async def test_activation_is_idempotent_for_the_managed_owner(self):
        bot = Bot()
        activator = lifecycle.ReplacementActivator(bot, object(), object())

        first = await activator.activate()
        second = await activator.activate()

        self.assertIs(second, first)
        self.assertIs(bot.get_command("cleanup").cog, first)

    async def test_unknown_cleanup_owner_is_never_removed(self):
        bot = Bot()
        bot.extensions.clear()
        unknown = object()
        bot.cogs["Cleanup"] = unknown
        bot.commands["cleanup"] = SimpleNamespace(cog=unknown)

        with self.assertRaisesRegex(
            lifecycle.CleanupReplacementError,
            "Another cog owns the Cleanup name",
        ):
            await lifecycle.ReplacementActivator(bot, object(), object()).activate()

        self.assertIs(bot.get_cog("Cleanup"), unknown)
        self.assertEqual(bot.events, [])

    async def test_missing_registered_command_removes_partial_replacement(self):
        bot = Bot()

        async def add_without_command(runtime):
            bot.cogs[runtime.qualified_name] = runtime

        bot.add_cog = add_without_command

        with self.assertRaisesRegex(
            lifecycle.CleanupReplacementError,
            "Replacement does not own the cleanup command",
        ):
            await lifecycle.ReplacementActivator(bot, object(), object()).activate()

        self.assertIsNone(bot.get_cog("Cleanup"))


if __name__ == "__main__":
    unittest.main()
