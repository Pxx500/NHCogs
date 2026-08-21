import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "custom_commands"


def load_lifecycle_module():
    package_name = "custom_commands_lifecycle_subject"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules[package_name] = package

    catalog = types.ModuleType(f"{package_name}.catalog")
    catalog.CustomCommandCatalog = object

    cog = types.ModuleType(f"{package_name}.cog")

    class CustomCommands:
        qualified_name = "CustomCommands"

        def __init__(self, bot, nhmisc, *, catalog):
            self.bot = bot
            self.nhmisc = nhmisc
            self.catalog = catalog

    cog.CustomCommands = CustomCommands
    sys.modules[catalog.__name__] = catalog
    sys.modules[cog.__name__] = cog

    state_name = f"{package_name}.migration_state"
    state_spec = importlib.util.spec_from_file_location(
        state_name,
        PACKAGE_PATH / "migration_state.py",
    )
    assert state_spec is not None
    assert state_spec.loader is not None
    state = importlib.util.module_from_spec(state_spec)
    sys.modules[state_name] = state
    state_spec.loader.exec_module(state)

    lifecycle_name = f"{package_name}.lifecycle"
    lifecycle_spec = importlib.util.spec_from_file_location(
        lifecycle_name,
        PACKAGE_PATH / "lifecycle.py",
    )
    assert lifecycle_spec is not None
    assert lifecycle_spec.loader is not None
    lifecycle = importlib.util.module_from_spec(lifecycle_spec)
    sys.modules[lifecycle_name] = lifecycle
    lifecycle_spec.loader.exec_module(lifecycle)
    return cog, lifecycle, state


cog, lifecycle, migration_state = load_lifecycle_module()


class _PackageConfig:
    def __init__(self, packages):
        self._packages = packages

    async def packages(self):
        return list(self._packages)


class _Bot:
    def __init__(self):
        official = types.ModuleType("redbot.cogs.customcom")
        self.extensions = {"customcom": official}
        official_cog_type = type("OfficialCustomCommands", (), {})
        official_cog_type.__module__ = "redbot.cogs.customcom.customcom"
        self.cogs = {"CustomCommands": official_cog_type()}
        self.packages = ["NHCogs", "customcom"]
        self._config = _PackageConfig(self.packages)
        self.events = []
        self.commands = {}
        self._cog_mgr = SimpleNamespace(
            find_cog=mock.AsyncMock(
                return_value=SimpleNamespace(name="redbot.cogs.customcom")
            )
        )

    async def unload_extension(self, name):
        self.events.append(("unload", name))
        self.extensions.pop(name, None)
        self.cogs.pop("CustomCommands", None)

    async def remove_loaded_package(self, name):
        self.events.append(("remove package", name))
        while name in self.packages:
            self.packages.remove(name)

    async def add_cog(self, runtime):
        self.events.append(("add cog", runtime.qualified_name))
        self.cogs[runtime.qualified_name] = runtime
        self.commands = {
            "customcom": SimpleNamespace(cog=runtime),
            "cc": SimpleNamespace(cog=runtime),
        }

    async def remove_cog(self, name):
        self.cogs.pop(name, None)

    async def load_extension(self, spec):
        self.events.append(("load", spec))

    async def add_loaded_package(self, name):
        self.packages.append(name)

    def get_cog(self, name):
        return self.cogs.get(name)

    def get_command(self, name):
        return self.commands.get(name)


class CutoverControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cutover_unloads_official_before_registering_replacement(self):
        bot = _Bot()
        state_store = SimpleNamespace(
            get=mock.AsyncMock(
                return_value=migration_state.MigrationState(
                    migration_state.MigrationPhase.IMPORTED_NOT_ACTIVE,
                    source_digest="source",
                    destination_digest="destination",
                )
            ),
            transition=mock.AsyncMock(),
        )
        controller = lifecycle.CutoverController(
            bot,
            object(),
            object(),
            state_store,
        )

        runtime = await controller.activate_imported()

        self.assertIsInstance(runtime, cog.CustomCommands)
        self.assertEqual(
            bot.events,
            [
                ("unload", "customcom"),
                ("remove package", "customcom"),
                ("add cog", "CustomCommands"),
            ],
        )
        self.assertNotIn("customcom", bot.packages)
        state_store.transition.assert_awaited_once_with(
            migration_state.MigrationPhase.IMPORTED_NOT_ACTIVE,
            migration_state.MigrationPhase.COMPLETE,
            source_digest="source",
            destination_digest="destination",
        )

    async def test_failed_activation_restores_official_package_and_keeps_import_retryable(self):
        bot = _Bot()

        async def fail_add(runtime):
            bot.events.append(("add cog", runtime.qualified_name))
            raise RuntimeError("registration failed")

        bot.add_cog = fail_add
        state_store = SimpleNamespace(
            get=mock.AsyncMock(
                return_value=migration_state.MigrationState(
                    migration_state.MigrationPhase.IMPORTED_NOT_ACTIVE,
                    source_digest="source",
                    destination_digest="destination",
                )
            ),
            transition=mock.AsyncMock(),
        )
        controller = lifecycle.CutoverController(
            bot,
            object(),
            object(),
            state_store,
        )

        with self.assertRaises(migration_state.MigrationApplyError):
            await controller.activate_imported()

        self.assertTrue(
            any(
                event == "load" and spec.name == "redbot.cogs.customcom"
                for event, spec in bot.events
                if event == "load"
            )
        )
        self.assertIn("customcom", bot.packages)
        state_store.transition.assert_not_awaited()

    async def test_concurrent_cutover_has_one_owner_and_keeps_replacement_active(self):
        bot = _Bot()

        class StateStore:
            def __init__(self):
                self.state = migration_state.MigrationState(
                    migration_state.MigrationPhase.IMPORTED_NOT_ACTIVE,
                    source_digest="source",
                    destination_digest="destination",
                )

            async def get(self):
                return self.state

            async def transition(
                self,
                expected_phase,
                phase,
                *,
                source_digest,
                destination_digest,
            ):
                if self.state.phase is not expected_phase:
                    raise migration_state.MigrationApplyError("stale transition")
                await asyncio.sleep(0)
                self.state = migration_state.MigrationState(
                    phase,
                    source_digest=source_digest,
                    destination_digest=destination_digest,
                )
                return self.state

        state_store = StateStore()
        controller = lifecycle.CutoverController(bot, object(), object(), state_store)

        first, second = await asyncio.gather(
            controller.activate_imported(),
            controller.activate_imported(),
        )

        self.assertIs(first, second)
        self.assertIs(bot.get_cog("CustomCommands"), first)
        self.assertEqual(
            state_store.state.phase,
            migration_state.MigrationPhase.COMPLETE,
        )
        self.assertNotIn("customcom", bot.packages)


if __name__ == "__main__":
    unittest.main()
