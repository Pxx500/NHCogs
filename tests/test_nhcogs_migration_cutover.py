import asyncio
import copy
import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from NHCogsMigrator import controller as controller_module
from NHCogsMigrator.backup import create_verified_backup
from NHCogsMigrator.controller import MigrationApplyError, MigrationController
from NHCogsMigrator.inventory import SuiteInventory
from NHCogsMigrator.plan import MigrationPreflightPlan
from NHCogsMigrator.preflight import DatabaseInspection, PersistedDataReport
from NHCogsMigrator.state import MigrationState, MigrationStateStore


class FakeGroup:
    def __init__(self, config, guild_id):
        self.config = config
        self.guild_id = guild_id

    async def set(self, values):
        self.config.guilds[self.guild_id] = copy.deepcopy(values)


class FakeConfig:
    def __init__(self, guilds):
        self.guilds = copy.deepcopy(guilds)

    async def all_guilds(self):
        return copy.deepcopy(self.guilds)

    async def clear_all_guilds(self):
        self.guilds.clear()

    def guild_from_id(self, guild_id):
        return FakeGroup(self, guild_id)


def make_cog(name, module, config):
    identifiers = {
        "NHMisc": 8597423150612235807,
        "Honeypot": 205192943327321000143939875896557571750,
    }
    cog_type = type(
        name,
        (),
        {
            "CONFIG_IDENTIFIER": identifiers[name],
            "QUIESCENT_UNLOAD_VERSION": 1,
        },
    )
    cog_type.__module__ = module
    cog = cog_type()
    cog.config = config
    return cog


class FakeRuntime:
    def __init__(
        self,
        paths,
        inventory,
        *,
        fail_validation=False,
        leave_legacy_loaded=False,
        lose_config=False,
        lose_database=None,
        unhealthy=False,
        legacy_inventory=None,
    ):
        self.paths = paths
        self.expected_inventory = inventory
        self.fail_validation = fail_validation
        self.leave_legacy_loaded = leave_legacy_loaded
        self.lose_config = lose_config
        self.lose_database = lose_database
        self.unhealthy = unhealthy
        self.legacy_inventory = legacy_inventory
        self.packages = ["NHMisc", "OtherCog", "Honeypot", "NHCogsMigrator"]
        self.configs = {
            "NHMisc": FakeConfig({1: {"enabled": True}}),
            "Honeypot": FakeConfig({1: {"enabled": True}}),
        }
        self.cogs = {}
        self.extensions = {}
        self._load_legacy("NHMisc")
        self._load_legacy("Honeypot")

    async def persisted_packages(self):
        return tuple(self.packages)

    async def replace_persisted_packages(self, expected, replacement):
        if tuple(self.packages) != expected:
            raise RuntimeError("package compare-and-swap failed")
        self.packages[:] = replacement

    def loaded_cog(self, name):
        return self.cogs[name]

    def config_for_cog(self, name, _identifier):
        return self.configs[name]

    def extension_key_for_cog(self, name):
        return "NHCogs" if type(self.cogs[name]).__module__.startswith("NHCogs.") else name

    def extension_key_for_module(self, name):
        return name if name in self.extensions else None

    async def unload_extension(self, name):
        self.extensions.pop(name, None)
        if name == "NHCogs":
            self.cogs.pop("NHMisc", None)
            self.cogs.pop("Honeypot", None)
        else:
            self.cogs.pop(name, None)

    async def load_extension(self, name):
        if name == "NHCogs":
            self.extensions[name] = object()
            if self.leave_legacy_loaded:
                self.extensions["NHMisc"] = object()
            module_root = "Broken" if self.fail_validation else "NHCogs"
            self.cogs["NHMisc"] = make_cog(
                "NHMisc",
                f"{module_root}.nhmisc.nhmisc",
                self.configs["NHMisc"],
            )
            self.cogs["Honeypot"] = make_cog(
                "Honeypot",
                f"{module_root}.honeypot.honeypot",
                self.configs["Honeypot"],
            )
            if self.lose_config:
                self.configs["NHMisc"].guilds.clear()
            if self.lose_database is not None:
                self.lose_database.unlink()
            if self.fail_validation:
                (self.paths["NHMisc"] / "state.bin").write_bytes(b"mutated")
        else:
            self._load_legacy(name)
        return name

    def suite_inventory(self, _names):
        if self.fail_validation and "NHCogs" in self.extensions:
            return SuiteInventory(("wrong",), (), (), ())
        return self.expected_inventory

    def legacy_global_inventory(self, _names):
        return self.legacy_inventory or self.expected_inventory

    def background_health_issues(self, _names):
        if self.unhealthy:
            return ("NHMisc: activity midnight task is not running",)
        return ()

    def data_path_for_cog(self, name):
        return self.paths[name]

    def _load_legacy(self, name):
        self.extensions[name] = object()
        self.cogs[name] = make_cog(
            name,
            f"{name}.{name.casefold()}",
            self.configs[name],
        )


class MigrationCutoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.paths = {
            "NHMisc": self.root / "NHMisc",
            "Honeypot": self.root / "Honeypot",
        }
        for path in self.paths.values():
            path.mkdir()
        (self.paths["NHMisc"] / "state.bin").write_bytes(b"before")
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.inventory = SuiteInventory(("command",), ("listener",), ("app",), ("view",))
        self.store = MigrationStateStore(self.root / "migration.sqlite")
        await self.store.initialize()

    async def test_success_commits_package_authority_after_validation(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        controller = MigrationController(runtime, self.store, self.backups)

        result = await controller.apply("run-1", plan)

        self.assertEqual(result.state, MigrationState.COMMITTED)
        self.assertEqual(
            tuple(runtime.packages),
            ("NHCogs", "OtherCog", "NHCogsMigrator"),
        )
        self.assertIn("NHCogs", runtime.extensions)
        self.assertNotIn("NHMisc", runtime.extensions)
        self.assertNotIn("Honeypot", runtime.extensions)
        metadata_path = Path(result.artifacts["backup_path"]) / "metadata.json"
        metadata = json.loads(metadata_path.read_text("utf-8"))
        self.assertEqual(metadata["dependency_versions"], plan.dependency_versions)

    async def test_concurrent_apply_has_one_transition_owner(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        original_latest_run = self.store.latest_run
        first_read_started = asyncio.Event()
        release_first_read = asyncio.Event()
        read_count = 0

        async def block_first_read():
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                first_read_started.set()
                await release_first_read.wait()
            return await original_latest_run()

        self.store.latest_run = block_first_read
        controller = MigrationController(runtime, self.store, self.backups)

        first = asyncio.create_task(controller.apply("run-1", plan))
        await first_read_started.wait()
        second = asyncio.create_task(controller.apply("run-1", plan))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            self.assertEqual(read_count, 1)
        finally:
            release_first_read.set()
            results = await asyncio.gather(first, second, return_exceptions=True)

        committed = [
            result
            for result in results
            if not isinstance(result, BaseException)
            and result.state is MigrationState.COMMITTED
        ]
        rejected = [
            result for result in results if isinstance(result, MigrationApplyError)
        ]
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual((await original_latest_run()).state, MigrationState.COMMITTED)

    async def test_validation_failure_restores_data_config_and_legacy_runtime(self):
        runtime = FakeRuntime(self.paths, self.inventory, fail_validation=True)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        controller = MigrationController(runtime, self.store, self.backups)

        with self.assertRaises(MigrationApplyError):
            await controller.apply("run-1", plan)

        run = await self.store.latest_run()
        self.assertEqual(run.state, MigrationState.ROLLED_BACK)
        self.assertEqual((self.paths["NHMisc"] / "state.bin").read_bytes(), b"before")
        self.assertEqual(tuple(runtime.packages), plan.original_packages)
        self.assertIn("NHMisc", runtime.extensions)
        self.assertIn("Honeypot", runtime.extensions)
        self.assertNotIn("NHCogs", runtime.extensions)
        self.assertEqual(await runtime.configs["NHMisc"].all_guilds(), {1: {"enabled": True}})

    async def test_loaded_legacy_extension_blocks_commit_and_rolls_back(self):
        runtime = FakeRuntime(
            self.paths,
            self.inventory,
            leave_legacy_loaded=True,
        )
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        controller = MigrationController(runtime, self.store, self.backups)

        with self.assertRaises(MigrationApplyError):
            await controller.apply("run-1", plan)

        run = await self.store.latest_run()
        self.assertEqual(run.state, MigrationState.ROLLED_BACK)
        self.assertEqual(tuple(runtime.packages), plan.original_packages)
        self.assertIn("NHMisc", runtime.extensions)
        self.assertIn("Honeypot", runtime.extensions)
        self.assertNotIn("NHCogs", runtime.extensions)

    async def test_config_guild_loss_blocks_commit_and_rolls_back(self):
        runtime = FakeRuntime(self.paths, self.inventory, lose_config=True)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        controller = MigrationController(runtime, self.store, self.backups)

        with self.assertRaises(MigrationApplyError):
            await controller.apply("run-1", plan)

        run = await self.store.latest_run()
        self.assertEqual(run.state, MigrationState.ROLLED_BACK)
        self.assertEqual(runtime.configs["NHMisc"].guilds, {1: {"enabled": True}})

    async def test_persisted_database_loss_blocks_commit_and_restores_backup(self):
        database = self.paths["NHMisc"] / "state.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE state (value TEXT)")
            connection.execute("INSERT INTO state VALUES ('before')")
            connection.commit()
        runtime = FakeRuntime(
            self.paths,
            self.inventory,
            lose_database=database,
        )
        plan = self._plan(runtime)
        inspection = DatabaseInspection(
            path=str(database.resolve()),
            size_bytes=database.stat().st_size,
            integrity_result="ok",
            table_rows={"state": 1},
        )
        plan = replace(
            plan,
            persisted_data=replace(plan.persisted_data, databases=(inspection,)),
        )
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        controller = MigrationController(runtime, self.store, self.backups)

        with self.assertRaises(MigrationApplyError):
            await controller.apply("run-1", plan)

        self.assertTrue(database.is_file())
        with closing(sqlite3.connect(database)) as connection:
            value = connection.execute("SELECT value FROM state").fetchone()[0]
        self.assertEqual(value, "before")

    async def test_background_worker_failure_blocks_commit_and_rolls_back(self):
        runtime = FakeRuntime(self.paths, self.inventory, unhealthy=True)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        controller = MigrationController(runtime, self.store, self.backups)

        with self.assertRaisesRegex(MigrationApplyError, "background health"):
            await controller.apply("run-1", plan)

        run = await self.store.latest_run()
        self.assertEqual(run.state, MigrationState.ROLLED_BACK)

    async def test_background_worker_failure_blocks_restart_verification(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        first = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-a",
        )
        await first.apply("run-1", plan)
        runtime.unhealthy = True
        restarted = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-b",
        )

        with self.assertRaisesRegex(MigrationApplyError, "background health"):
            await restarted.verify_restart("run-1")

        run = await self.store.latest_run()
        self.assertEqual(run.state, MigrationState.COMMITTED)

    async def test_restart_verification_requires_a_new_process(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        first_process = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-a",
        )
        await first_process.apply("run-1", plan)

        with self.assertRaisesRegex(MigrationApplyError, "normal bot restart"):
            await first_process.verify_restart("run-1")
        restarted = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-b",
        )
        result = await restarted.verify_restart("run-1")

        self.assertEqual(result.state, MigrationState.RESTART_VERIFIED)

        finalized = await restarted.finalize("run-1")

        self.assertEqual(finalized.state, MigrationState.FINALIZED)
        self.assertEqual(tuple(runtime.packages), ("NHCogs", "OtherCog"))

    async def test_restart_ignores_unrelated_views_from_pre_scope_run(self):
        suite_view = "honeypot:case:case-id:moderate:ban"
        scoped_inventory = SuiteInventory(
            ("command",),
            ("listener",),
            ("app",),
            (suite_view,),
        )
        pre_scope_inventory = SuiteInventory(
            scoped_inventory.prefix_commands,
            scoped_inventory.listeners,
            scoped_inventory.application_commands,
            (suite_view, *(f"other:view:{index}" for index in range(8))),
        )
        runtime = FakeRuntime(
            self.paths,
            scoped_inventory,
            legacy_inventory=scoped_inventory,
        )
        plan = replace(self._plan(runtime), inventory=scoped_inventory)
        validations = plan.validations()
        stored_inventory = pre_scope_inventory.as_dict()
        stored_inventory.pop("scope")
        validations["inventory"] = stored_inventory
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=validations,
        )
        first_process = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-a",
        )
        await first_process.apply("run-1", plan)
        restarted = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-b",
        )

        with mock.patch.object(
            controller_module.asyncio,
            "sleep",
            new=mock.AsyncMock(),
        ):
            result = await restarted.verify_restart("run-1")

        self.assertEqual(result.state, MigrationState.RESTART_VERIFIED)

    async def test_restart_still_requires_honeypot_views_from_pre_scope_run(self):
        suite_view = "honeypot:case:case-id:moderate:ban"
        scoped_inventory = SuiteInventory(("command",), ("listener",), ("app",), ())
        pre_scope_inventory = SuiteInventory(
            scoped_inventory.prefix_commands,
            scoped_inventory.listeners,
            scoped_inventory.application_commands,
            (suite_view,),
        )
        runtime = FakeRuntime(
            self.paths,
            scoped_inventory,
            legacy_inventory=scoped_inventory,
        )
        plan = replace(self._plan(runtime), inventory=scoped_inventory)
        validations = plan.validations()
        stored_inventory = pre_scope_inventory.as_dict()
        stored_inventory.pop("scope")
        validations["inventory"] = stored_inventory
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=validations,
        )
        first_process = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-a",
        )
        await first_process.apply("run-1", plan)
        restarted = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-b",
        )

        with mock.patch.object(
            controller_module.asyncio,
            "sleep",
            new=mock.AsyncMock(),
        ):
            with self.assertRaisesRegex(
                MigrationApplyError,
                "persistent_view_custom_ids missing=1 extra=0",
            ):
                await restarted.verify_restart("run-1")

    async def test_restart_waits_for_suite_inventory_to_settle(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        first_process = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-a",
        )
        await first_process.apply("run-1", plan)
        restarted = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-b",
        )
        runtime.suite_inventory = mock.Mock(
            side_effect=[
                SuiteInventory(("command",), ("listener",), (), ()),
                self.inventory,
            ]
        )

        with mock.patch.object(
            controller_module.asyncio,
            "sleep",
            new=mock.AsyncMock(),
        ) as sleep:
            result = await restarted.verify_restart("run-1")

        self.assertEqual(result.state, MigrationState.RESTART_VERIFIED)
        self.assertEqual(sleep.await_args_list.count(mock.call(1)), 1)

    async def test_finalization_intent_survives_package_update_failure(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        first = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-a",
        )
        await first.apply("run-1", plan)
        restarted = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-b",
        )
        await restarted.verify_restart("run-1")
        original_replace = runtime.replace_persisted_packages
        failed_once = False

        async def fail_first_removal(expected, replacement):
            nonlocal failed_once
            if not failed_once and "NHCogsMigrator" not in replacement:
                failed_once = True
                raise RuntimeError("process stopped before package removal")
            await original_replace(expected, replacement)

        runtime.replace_persisted_packages = fail_first_removal

        with self.assertRaises(RuntimeError):
            await restarted.finalize("run-1")

        interrupted = await self.store.latest_run()
        self.assertEqual(interrupted.state, MigrationState.FINALIZED)
        self.assertIn("NHCogsMigrator", runtime.packages)

        result = await restarted.finalize("run-1")

        self.assertEqual(result.state, MigrationState.FINALIZED)
        self.assertNotIn("NHCogsMigrator", runtime.packages)

    async def test_restart_recovers_interrupted_precommit_run_from_backup(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        await self.store.transition(
            "run-1",
            MigrationState.PLANNED,
            MigrationState.QUIESCING,
        )
        backup = await create_verified_backup(
            "run-1",
            data_directories=self.paths,
            backup_root=self.backups,
            config_exports={
                name: await config.all_guilds()
                for name, config in runtime.configs.items()
            },
            metadata={},
        )
        await self.store.transition(
            "run-1",
            MigrationState.QUIESCING,
            MigrationState.BACKUP_COMPLETE,
            artifacts={"backup_path": str(backup.path)},
        )
        await self.store.transition(
            "run-1",
            MigrationState.BACKUP_COMPLETE,
            MigrationState.LOADING_SUITE,
        )
        (self.paths["NHMisc"] / "state.bin").write_bytes(b"mutated before crash")
        runtime.configs["NHMisc"].guilds = {2: {"enabled": False}}
        controller = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="restarted-process",
        )

        result = await controller.recover_interrupted("run-1")

        self.assertEqual(result.state, MigrationState.ROLLED_BACK)
        self.assertEqual((self.paths["NHMisc"] / "state.bin").read_bytes(), b"before")
        self.assertEqual(runtime.configs["NHMisc"].guilds, {1: {"enabled": True}})

    async def test_manual_intervention_can_retry_verified_recovery(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        await self._create_manual_intervention(runtime)
        (self.paths["NHMisc"] / "state.bin").write_bytes(b"mutated")
        runtime.configs["NHMisc"].guilds.clear()
        controller = MigrationController(runtime, self.store, self.backups)

        result = await controller.recover_interrupted("run-1")

        self.assertEqual(result.state, MigrationState.ROLLED_BACK)
        self.assertEqual(
            (self.paths["NHMisc"] / "state.bin").read_bytes(),
            b"before",
        )
        self.assertEqual(runtime.configs["NHMisc"].guilds, {1: {"enabled": True}})

    async def test_manual_recovery_unloads_surviving_suite_before_restore(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        await self._create_manual_intervention(runtime)
        await runtime.unload_extension("Honeypot")
        await runtime.unload_extension("NHMisc")
        await runtime.load_extension("NHCogs")
        controller = MigrationController(runtime, self.store, self.backups)

        result = await controller.recover_interrupted("run-1")

        self.assertEqual(result.state, MigrationState.ROLLED_BACK)
        self.assertNotIn("NHCogs", runtime.extensions)
        self.assertIn("NHMisc", runtime.extensions)
        self.assertIn("Honeypot", runtime.extensions)

    async def test_manual_recovery_works_without_loaded_cogs(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        await self._create_manual_intervention(runtime)
        await runtime.unload_extension("Honeypot")
        await runtime.unload_extension("NHMisc")
        runtime.configs["NHMisc"].guilds.clear()
        controller = MigrationController(runtime, self.store, self.backups)

        result = await controller.recover_interrupted("run-1")

        self.assertEqual(result.state, MigrationState.ROLLED_BACK)
        self.assertEqual(runtime.configs["NHMisc"].guilds, {1: {"enabled": True}})
        self.assertIn("NHMisc", runtime.extensions)
        self.assertIn("Honeypot", runtime.extensions)

    async def test_concurrent_manual_recovery_has_one_operation_owner(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        await self._create_manual_intervention(runtime)
        controller = MigrationController(runtime, self.store, self.backups)
        original_restore = controller_module.restore_verified_backup
        first_restore_started = asyncio.Event()
        release_restore = asyncio.Event()
        second_invocation_started = asyncio.Event()
        restore_calls = 0

        async def blocked_restore(*args, **kwargs):
            nonlocal restore_calls
            restore_calls += 1
            first_restore_started.set()
            await release_restore.wait()
            return await original_restore(*args, **kwargs)

        async def invoke_second_recovery():
            second_invocation_started.set()
            return await controller.recover_interrupted("run-1")

        with mock.patch.object(
            controller_module,
            "restore_verified_backup",
            side_effect=blocked_restore,
        ):
            first = asyncio.create_task(controller.recover_interrupted("run-1"))
            await first_restore_started.wait()
            second = asyncio.create_task(invoke_second_recovery())
            await second_invocation_started.wait()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            try:
                self.assertEqual(restore_calls, 1)
            finally:
                release_restore.set()
                results = await asyncio.gather(first, second, return_exceptions=True)

        recovered = [
            result
            for result in results
            if not isinstance(result, BaseException)
            and result.state is MigrationState.ROLLED_BACK
        ]
        rejected = [
            result for result in results if isinstance(result, MigrationApplyError)
        ]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(len(rejected), 1)

    async def test_restart_recovers_package_commit_before_state_commit(self):
        runtime = FakeRuntime(self.paths, self.inventory)
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        await self.store.transition(
            "run-1", MigrationState.PLANNED, MigrationState.QUIESCING
        )
        await self.store.transition(
            "run-1", MigrationState.QUIESCING, MigrationState.BACKUP_COMPLETE
        )
        await self.store.transition(
            "run-1", MigrationState.BACKUP_COMPLETE, MigrationState.LOADING_SUITE
        )
        await runtime.unload_extension("Honeypot")
        await runtime.unload_extension("NHMisc")
        await runtime.load_extension("NHCogs")
        await self.store.transition(
            "run-1", MigrationState.LOADING_SUITE, MigrationState.VALIDATED
        )
        runtime.packages[:] = ("NHCogs", "OtherCog", "NHCogsMigrator")
        restarted = MigrationController(
            runtime,
            self.store,
            self.backups,
            process_token="process-after-crash",
        )

        result = await restarted.recover_interrupted("run-1")

        self.assertEqual(result.state, MigrationState.RESTART_VERIFIED)
        self.assertTrue(result.validations["recovered_commit"])

    async def _create_manual_intervention(self, runtime):
        plan = self._plan(runtime)
        await self.store.create_run(
            "run-1",
            original_packages=plan.original_packages,
            source_commit=plan.source_commit,
            validations=plan.validations(),
        )
        backup = await create_verified_backup(
            "run-1",
            data_directories=self.paths,
            backup_root=self.backups,
            config_exports={
                name: await config.all_guilds()
                for name, config in runtime.configs.items()
            },
            metadata={},
        )
        await self.store.transition(
            "run-1",
            MigrationState.PLANNED,
            MigrationState.QUIESCING,
            artifacts={"backup_path": str(backup.path)},
        )
        await self.store.transition(
            "run-1",
            MigrationState.QUIESCING,
            MigrationState.ROLLING_BACK,
        )
        await self.store.transition(
            "run-1",
            MigrationState.ROLLING_BACK,
            MigrationState.MANUAL_INTERVENTION,
            validations={"rollback_error": "temporary restore failure"},
        )

    def _plan(self, runtime):
        data = PersistedDataReport(
            data_directories={name: str(path) for name, path in self.paths.items()},
            databases=(),
            file_count=1,
            total_bytes=6,
            required_backup_bytes=6,
            free_bytes=10_000,
            blocking_issues=(),
        )
        return MigrationPreflightPlan(
            original_packages=tuple(runtime.packages),
            source_commit="abc123",
            installed_commits={
                "NHMisc": "abc123",
                "Honeypot": "abc123",
                "NHCogs": "abc123",
                "NHCogsMigrator": "abc123",
            },
            dependency_versions={
                "matplotlib": "3.10.0",
                "AAA3A_utils": "0.0.0",
                "Pillow": "11.3.0",
                "pillow-avif-plugin": "1.6.0",
            },
            suite_identity={
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
            },
            legacy_extension_keys={"NHMisc": "NHMisc", "Honeypot": "Honeypot"},
            data_directories={name: str(path) for name, path in self.paths.items()},
            config_guild_counts={"NHMisc": 1, "Honeypot": 1},
            inventory=self.inventory,
            persisted_data=data,
            blocking_issues=(),
        )
