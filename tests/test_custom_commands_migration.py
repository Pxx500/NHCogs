import importlib.util
import inspect
import json
import sqlite3
import sys
import types
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "custom_commands"


def load_migration_modules():
    package_name = "custom_commands_migration_subject"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules[package_name] = package
    discord = types.ModuleType("discord")
    commands = types.ModuleType("redbot.core.commands")
    commands.Parameter = inspect.Parameter
    core = types.ModuleType("redbot.core")
    core.commands = commands
    temporary = {
        "discord": discord,
        "redbot": types.ModuleType("redbot"),
        "redbot.core": core,
        "redbot.core.commands": commands,
    }
    previous = {name: sys.modules.get(name) for name in temporary}
    sys.modules.update(temporary)
    try:
        for module_name in ("migration_state", "arguments", "catalog", "migration"):
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
        sys.modules[f"{package_name}.catalog"],
        sys.modules[f"{package_name}.migration"],
        sys.modules[f"{package_name}.migration_state"],
    )


catalog, migration, migration_state = load_migration_modules()


class _LegacyConfig:
    def __init__(self, guilds):
        self.guilds = guilds
        self.clear_all = mock.AsyncMock(side_effect=self._clear)

    async def all_guilds(self):
        return self.guilds

    def _clear(self):
        self.guilds = {}


class LegacyDataCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_reports_every_target_without_changing_data(self):
        with TemporaryDirectory() as directory:
            data_root = Path(directory)
            database_path = data_root / "custom_commands.sqlite"
            store = catalog.CustomCommandCatalog(database_path)
            await store.initialize()
            state_store = migration_state.MigrationStateStore(database_path)
            await state_store.initialize()
            artifact_root = data_root / "migration" / "digest"
            artifact_root.mkdir(parents=True)
            (artifact_root / "legacy-backup.json").write_text(
                "backup",
                encoding="utf-8",
            )
            (artifact_root / "migration-report.json").write_text(
                "report",
                encoding="utf-8",
            )
            legacy_config = _LegacyConfig(
                {
                    100: {
                        "commands": {
                            "first": {"response": "one"},
                            "second": {"response": "two"},
                        }
                    }
                }
            )

            status = await migration.inspect_legacy_data(
                legacy_config,
                data_root,
                database_path,
            )

            self.assertEqual(status.active_command_count, 0)
            self.assertEqual(status.legacy_command_count, 2)
            self.assertEqual(status.artifact_file_count, 2)
            self.assertEqual(status.artifact_bytes, len(b"backup") + len(b"report"))
            self.assertTrue(status.migration_state_present)
            legacy_config.clear_all.assert_not_awaited()
            self.assertTrue((artifact_root / "legacy-backup.json").exists())
            with closing(sqlite3.connect(database_path)) as connection:
                table = connection.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type = 'table' AND name = 'custom_command_migration_state'"""
                ).fetchone()
            self.assertIsNotNone(table)

    async def test_confirmed_cleanup_preserves_active_catalog_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            data_root = Path(directory)
            database_path = data_root / "custom_commands.sqlite"
            store = catalog.CustomCommandCatalog(database_path)
            await store.initialize()
            await store.create(
                guild_id=100,
                name="hello",
                author_id=200,
                author_name="Creator",
                responses=[catalog.ResponseDraft("  exact response  ")],
            )
            state_store = migration_state.MigrationStateStore(database_path)
            await state_store.initialize()
            artifact_root = data_root / "migration" / "digest"
            artifact_root.mkdir(parents=True)
            (artifact_root / "legacy-backup.json").write_text(
                "backup",
                encoding="utf-8",
            )
            legacy_config = _LegacyConfig(
                {100: {"commands": {"hello": {"response": "old"}}}}
            )

            first = await migration.purge_legacy_data(
                legacy_config,
                data_root,
                database_path,
            )
            second = await migration.purge_legacy_data(
                legacy_config,
                data_root,
                database_path,
            )

            self.assertEqual(first.legacy_command_count, 0)
            self.assertEqual(first.artifact_file_count, 0)
            self.assertFalse(first.migration_state_present)
            self.assertEqual(second, first)
            active = await store.get(100, "hello")
            self.assertIsNotNone(active)
            self.assertEqual(active.responses[0].content, "  exact response  ")
            self.assertFalse((data_root / "migration").exists())
            self.assertEqual(legacy_config.clear_all.await_count, 2)

    async def test_cleanup_refuses_nonempty_legacy_when_active_catalog_is_empty(self):
        with TemporaryDirectory() as directory:
            data_root = Path(directory)
            database_path = data_root / "custom_commands.sqlite"
            store = catalog.CustomCommandCatalog(database_path)
            await store.initialize()
            state_store = migration_state.MigrationStateStore(database_path)
            await state_store.initialize()
            artifact_root = data_root / "migration" / "digest"
            artifact_root.mkdir(parents=True)
            artifact = artifact_root / "legacy-backup.json"
            artifact.write_text("backup", encoding="utf-8")
            legacy_config = _LegacyConfig(
                {100: {"commands": {"hello": {"response": "old"}}}}
            )

            with self.assertRaisesRegex(
                migration.LegacyCleanupPreconditionError,
                "active catalog is empty",
            ):
                await migration.purge_legacy_data(
                    legacy_config,
                    data_root,
                    database_path,
                )

            legacy_config.clear_all.assert_not_awaited()
            self.assertTrue(artifact.exists())


class LegacyMigrationPlannerTests(unittest.TestCase):
    def test_plan_canonicalizes_legacy_commands_without_losing_metadata(self):
        legacy = {
            100: {
                "commands": {
                    "Hello": {
                        "author": {"id": 200, "name": "Creator"},
                        "command": "Hello",
                        "cooldowns": {"server": 30, "user": 5},
                        "created_at": "20/08/2026 12:00:00",
                        "edited_at": "20/08/2026 13:00:00",
                        "editors": [300],
                        "response": ["first {1}", "second {1}"],
                    }
                }
            }
        }

        plan = migration.LegacyMigrationPlanner().plan(legacy)

        self.assertEqual(plan.issues, ())
        self.assertEqual(len(plan.commands), 1)
        command = plan.commands[0]
        self.assertEqual(command.name, "hello")
        self.assertEqual(command.cooldowns, {"guild": 30, "member": 5})
        self.assertEqual([response.weight for response in command.responses], [100, 100])
        self.assertEqual(command.editors[0].user_id, 300)
        self.assertEqual(len(plan.source_digest), 64)
        self.assertIn(b'"Hello"', plan.backup_json)

    def test_any_invalid_record_blocks_the_complete_plan(self):
        legacy = {
            100: {
                "commands": {
                    "empty": {
                        "author": {"id": 200, "name": "Creator"},
                        "command": "empty",
                        "cooldowns": {},
                        "created_at": "20/08/2026 12:00:00",
                        "editors": [],
                        "response": "",
                    },
                    "valid": {
                        "author": {"id": 200, "name": "Creator"},
                        "command": "valid",
                        "cooldowns": {},
                        "created_at": "20/08/2026 12:00:00",
                        "editors": [],
                        "response": "valid response",
                    },
                }
            }
        }

        plan = migration.LegacyMigrationPlanner().plan(legacy)

        self.assertFalse(plan.can_apply)
        self.assertEqual([command.name for command in plan.commands], ["valid"])
        self.assertEqual(plan.issues[0].command_name, "empty")
        self.assertEqual(plan.issues[0].code, "empty_response")
        self.assertIsNotNone(plan.errors_text)


class MigrationImportIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_legacy_editors_survive_import_read_back(self):
        legacy = {
            100: {
                "commands": {
                    "editors": {
                        "author": {"id": 200, "name": "Creator"},
                        "command": "editors",
                        "cooldowns": {},
                        "created_at": "20/08/2026 12:00:00",
                        "edited_at": "20/08/2026 13:00:00",
                        "editors": [845186841556418560, 164041088229703680],
                        "response": "response",
                    }
                }
            }
        }
        plan = migration.LegacyMigrationPlanner().plan(legacy)

        with TemporaryDirectory() as directory:
            database = Path(directory) / "commands.sqlite"
            command_store = catalog.CustomCommandCatalog(database)
            state_store = migration_state.MigrationStateStore(database)
            await command_store.initialize()
            await state_store.initialize()
            await state_store.save(
                migration_state.MigrationPhase.PLANNED,
                source_digest=plan.source_digest,
                destination_digest=plan.destination_digest,
            )

            await command_store.import_migration(
                plan.commands,
                source_digest=plan.source_digest,
                destination_digest=plan.destination_digest,
            )

            stored = await command_store.list_commands(100)

        self.assertEqual(stored, plan.commands)


class LegacyPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_privacy_deletion_redacts_legacy_config_and_artifacts(self):
        legacy = {
            100: {
                "commands": {
                    "hello": {
                        "author": {"id": 42, "name": "User"},
                        "editors": [7, 42],
                    }
                }
            }
        }

        class CommandsValue:
            def __init__(self, guild_data):
                self.guild_data = guild_data

            async def set(self, commands):
                self.guild_data["commands"] = commands

        class GuildValue:
            def __init__(self, guild_data):
                self.commands = CommandsValue(guild_data)

        class Config:
            async def all_guilds(self):
                return legacy

            def guild_from_id(self, guild_id):
                return GuildValue(legacy[guild_id])

        with TemporaryDirectory() as directory:
            migration_root = Path(directory)
            artifact = migration_root / "digest"
            artifact.mkdir()
            (artifact / "legacy-backup.json").write_text(
                json.dumps(legacy),
                encoding="utf-8",
            )
            (artifact / "migration-report.json").write_text(
                json.dumps(
                    {
                        "target": [
                            {
                                "author": {"id": 42, "name": "User"},
                                "editors": [
                                    {
                                        "user_id": 42,
                                        "display_name": "User",
                                        "first_edited_at": "date",
                                        "last_edited_at": "date",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            await migration.redact_legacy_config(Config(), 42)
            migration.redact_migration_artifacts(migration_root, 42)

            backup = json.loads(
                (artifact / "legacy-backup.json").read_text(encoding="utf-8")
            )
            report = json.loads(
                (artifact / "migration-report.json").read_text(encoding="utf-8")
            )

        config_record = legacy[100]["commands"]["hello"]
        backup_record = backup["100"]["commands"]["hello"]
        report_record = report["target"][0]
        for record in (config_record, backup_record):
            self.assertEqual(record["author"]["id"], migration.DELETED_USER_ID)
            self.assertEqual(record["author"]["name"], migration.DELETED_USER_NAME)
            self.assertNotIn(42, record["editors"])
        self.assertEqual(
            report_record["author"]["id"],
            migration.DELETED_USER_ID,
        )
        self.assertEqual(
            report_record["editors"][0]["user_id"],
            migration.DELETED_USER_ID,
        )
        self.assertTrue(report["privacy_redacted"])


class MigrationStateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_migration_is_durable_and_cannot_run_twice(self):
        with TemporaryDirectory() as directory:
            store = migration_state.MigrationStateStore(
                Path(directory) / "commands.sqlite"
            )
            await store.initialize()
            self.assertEqual(
                (await store.get()).phase,
                migration_state.MigrationPhase.NOT_PLANNED,
            )
            await store.save(
                migration_state.MigrationPhase.PLANNED,
                source_digest="source",
                destination_digest="destination",
            )
            await store.save(
                migration_state.MigrationPhase.IMPORTED_NOT_ACTIVE,
                source_digest="source",
                destination_digest="destination",
            )
            await store.save(
                migration_state.MigrationPhase.COMPLETE,
                source_digest="source",
                destination_digest="destination",
            )

            reopened = migration_state.MigrationStateStore(
                Path(directory) / "commands.sqlite"
            )
            state = await reopened.get()
            with self.assertRaises(migration_state.MigrationApplyError):
                await reopened.save(
                    migration_state.MigrationPhase.PLANNED,
                    source_digest="new",
                    destination_digest="new",
                )

        self.assertEqual(state.phase, migration_state.MigrationPhase.COMPLETE)
        self.assertEqual(state.source_digest, "source")

    async def test_transition_rejects_a_stale_expected_phase(self):
        with TemporaryDirectory() as directory:
            store = migration_state.MigrationStateStore(
                Path(directory) / "commands.sqlite"
            )
            await store.initialize()
            await store.save(
                migration_state.MigrationPhase.IMPORTED_NOT_ACTIVE,
                source_digest="source",
                destination_digest="destination",
            )

            await store.transition(
                migration_state.MigrationPhase.IMPORTED_NOT_ACTIVE,
                migration_state.MigrationPhase.COMPLETE,
                source_digest="source",
                destination_digest="destination",
            )
            with self.assertRaises(migration_state.MigrationApplyError):
                await store.transition(
                    migration_state.MigrationPhase.IMPORTED_NOT_ACTIVE,
                    migration_state.MigrationPhase.COMPLETE,
                    source_digest="source",
                    destination_digest="destination",
                )

            state = await store.get()

        self.assertEqual(state.phase, migration_state.MigrationPhase.COMPLETE)


if __name__ == "__main__":
    unittest.main()
