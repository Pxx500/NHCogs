import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from NHCogsMigrator.inventory import SuiteInventory
from NHCogsMigrator.plan import build_preflight_plan


class FakeConfig:
    def __init__(self, guilds):
        self.guilds = guilds

    async def all_guilds(self):
        return self.guilds


class FakeRuntime:
    def __init__(self, nhmisc_path, honeypot_path):
        self.packages = ("NHMisc", "OtherCog", "Honeypot", "NHCogsMigrator")
        self.cogs = {
            "NHMisc": self._cog(
                "NHMisc",
                "NHMisc.nhmisc",
                {1: {"enabled": True}},
            ),
            "Honeypot": self._cog(
                "Honeypot",
                "Honeypot.honeypot",
                {1: {"enabled": True}, 2: {"enabled": False}},
            ),
        }
        self.paths = {"NHMisc": nhmisc_path, "Honeypot": honeypot_path}
        self.commits = {
            "NHMisc": "abc123",
            "Honeypot": "abc123",
            "NHCogs": "abc123",
            "NHCogsMigrator": "abc123",
        }

    async def persisted_packages(self):
        return self.packages

    def loaded_cog(self, name):
        return self.cogs[name]

    def extension_key_for_cog(self, name):
        return name

    async def find_extension_spec(self, name):
        if name in {"NHCogs", "NHCogsMigrator"}:
            return types.SimpleNamespace(name=name)
        return None

    async def installed_module(self, name):
        return types.SimpleNamespace(name=name, commit=self.commits[name])

    async def probe_suite_identity(self):
        return {
            "NHMisc": {
                "class_name": "NHMisc",
                "module": "NHCogs.nhmisc.nhmisc",
                "config_identifier": 8597423150612235807,
                "quiescent_unload_version": 1,
                "runtime_health_version": 1,
            },
            "Honeypot": {
                "class_name": "Honeypot",
                "module": "NHCogs.honeypot.honeypot",
                "config_identifier": 205192943327321000143939875896557571750,
                "quiescent_unload_version": 1,
                "runtime_health_version": 1,
            },
        }

    def suite_inventory(self, _names):
        return SuiteInventory(("command",), ("listener",), ("app",), ("view",))

    def data_path_for_cog(self, name):
        return self.paths[name]

    @staticmethod
    def _cog(name, module, guilds):
        cog_type = type(name, (), {"QUIESCENT_UNLOAD_VERSION": 1})
        cog_type.__module__ = module
        cog = cog_type()
        cog.config = FakeConfig(guilds)
        return cog


class PreflightPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_runtime_produces_non_blocking_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            nhmisc = root / "NHMisc"
            honeypot = root / "Honeypot"
            backups = root / "backups"
            nhmisc.mkdir()
            honeypot.mkdir()
            backups.mkdir()
            runtime = FakeRuntime(nhmisc, honeypot)

            with (
                mock.patch(
                    "NHCogsMigrator.plan.importlib.util.find_spec",
                    return_value=object(),
                ),
                mock.patch(
                    "NHCogsMigrator.plan.metadata.version",
                    return_value="1.0",
                ),
            ):
                plan = await build_preflight_plan(runtime, backup_root=backups)

        self.assertEqual(plan.blocking_issues, ())
        self.assertEqual(plan.original_packages, runtime.packages)
        self.assertEqual(plan.source_commit, "abc123")
        self.assertEqual(set(plan.installed_commits.values()), {"abc123"})
        self.assertEqual(set(plan.dependency_versions.values()), {"1.0"})
        self.assertEqual(plan.config_guild_counts, {"NHMisc": 1, "Honeypot": 2})
        self.assertEqual(plan.inventory.prefix_commands, ("command",))

    async def test_wrong_package_authority_and_data_path_are_blocking(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            nhmisc = root / "wrong-name"
            honeypot = root / "Honeypot"
            backups = root / "backups"
            nhmisc.mkdir()
            honeypot.mkdir()
            backups.mkdir()
            runtime = FakeRuntime(nhmisc, honeypot)
            runtime.packages = ("NHCogs", "NHCogsMigrator")

            with (
                mock.patch(
                    "NHCogsMigrator.plan.importlib.util.find_spec",
                    return_value=object(),
                ),
                mock.patch(
                    "NHCogsMigrator.plan.metadata.version",
                    return_value="1.0",
                ),
            ):
                plan = await build_preflight_plan(runtime, backup_root=backups)

        self.assertTrue(any("package list" in issue for issue in plan.blocking_issues))
        self.assertTrue(any("NHMisc data path" in issue for issue in plan.blocking_issues))

    async def test_legacy_runtime_without_quiescent_unload_is_blocking(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            nhmisc = root / "NHMisc"
            honeypot = root / "Honeypot"
            backups = root / "backups"
            nhmisc.mkdir()
            honeypot.mkdir()
            backups.mkdir()
            runtime = FakeRuntime(nhmisc, honeypot)
            delattr(type(runtime.cogs["NHMisc"]), "QUIESCENT_UNLOAD_VERSION")

            with (
                mock.patch(
                    "NHCogsMigrator.plan.importlib.util.find_spec",
                    return_value=object(),
                ),
                mock.patch(
                    "NHCogsMigrator.plan.metadata.version",
                    return_value="1.0",
                ),
            ):
                plan = await build_preflight_plan(runtime, backup_root=backups)

        self.assertTrue(
            any("quiescent unload" in issue for issue in plan.blocking_issues)
        )

    async def test_mixed_release_commits_are_blocking(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            nhmisc = root / "NHMisc"
            honeypot = root / "Honeypot"
            backups = root / "backups"
            nhmisc.mkdir()
            honeypot.mkdir()
            backups.mkdir()
            runtime = FakeRuntime(nhmisc, honeypot)
            runtime.commits["Honeypot"] = "older"

            with (
                mock.patch(
                    "NHCogsMigrator.plan.importlib.util.find_spec",
                    return_value=object(),
                ),
                mock.patch(
                    "NHCogsMigrator.plan.metadata.version",
                    return_value="1.0",
                ),
            ):
                plan = await build_preflight_plan(runtime, backup_root=backups)

        self.assertTrue(
            any("same release commit" in issue for issue in plan.blocking_issues)
        )
