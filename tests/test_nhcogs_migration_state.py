import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from NHCogsMigrator.state import (
    MigrationState,
    MigrationStateError,
    MigrationStateStore,
)


class MigrationStateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "migration.sqlite"
        self.store = MigrationStateStore(self.path)
        await self.store.initialize()

    async def test_run_survives_store_reconstruction_with_artifacts(self):
        await self.store.create_run(
            "run-1",
            original_packages=("NHMisc", "Honeypot", "OtherCog"),
            source_commit="abc123",
            validations={"sqlite": {"ok": True, "databases": 8}},
        )
        await self.store.transition(
            "run-1",
            MigrationState.PLANNED,
            MigrationState.QUIESCING,
        )
        await self.store.transition(
            "run-1",
            MigrationState.QUIESCING,
            MigrationState.BACKUP_COMPLETE,
            artifacts={"backup": "C:/backups/run-1"},
            checksums={"manifest.json": "deadbeef"},
            validations={"backup": {"ok": True}},
        )

        reopened = MigrationStateStore(self.path)
        await reopened.initialize()
        run = await reopened.latest_run()
        events = await reopened.events("run-1")

        self.assertIsNotNone(run)
        self.assertEqual(run.run_id, "run-1")
        self.assertEqual(run.state, MigrationState.BACKUP_COMPLETE)
        self.assertEqual(run.original_packages, ("NHMisc", "Honeypot", "OtherCog"))
        self.assertEqual(run.source_commit, "abc123")
        self.assertEqual(run.artifacts, {"backup": "C:/backups/run-1"})
        self.assertEqual(run.checksums, {"manifest.json": "deadbeef"})
        self.assertEqual(
            run.validations,
            {
                "sqlite": {"ok": True, "databases": 8},
                "backup": {"ok": True},
            },
        )
        self.assertEqual(
            tuple(event.state for event in events),
            (
                MigrationState.PLANNED,
                MigrationState.QUIESCING,
                MigrationState.BACKUP_COMPLETE,
            ),
        )

    async def test_transition_rejects_stale_or_invalid_state(self):
        await self.store.create_run(
            "run-1",
            original_packages=("NHMisc", "Honeypot"),
            source_commit="abc123",
        )
        await self.store.transition(
            "run-1",
            MigrationState.PLANNED,
            MigrationState.QUIESCING,
        )

        with self.assertRaises(MigrationStateError):
            await self.store.transition(
                "run-1",
                MigrationState.PLANNED,
                MigrationState.QUIESCING,
            )
        with self.assertRaises(MigrationStateError):
            await self.store.transition(
                "run-1",
                MigrationState.QUIESCING,
                MigrationState.COMMITTED,
            )

        run = await self.store.latest_run()
        self.assertEqual(run.state, MigrationState.QUIESCING)

    async def test_second_run_is_allowed_only_after_complete_rollback(self):
        await self.store.create_run(
            "run-1",
            original_packages=("NHMisc", "Honeypot"),
            source_commit="abc123",
        )
        with self.assertRaises(MigrationStateError):
            await self.store.create_run(
                "run-2",
                original_packages=("NHMisc", "Honeypot"),
                source_commit="def456",
            )

        await self.store.transition(
            "run-1",
            MigrationState.PLANNED,
            MigrationState.QUIESCING,
        )
        await self.store.transition(
            "run-1",
            MigrationState.QUIESCING,
            MigrationState.ROLLING_BACK,
        )
        await self.store.transition(
            "run-1",
            MigrationState.ROLLING_BACK,
            MigrationState.ROLLED_BACK,
        )

        run = await self.store.create_run(
            "run-2",
            original_packages=("NHMisc", "Honeypot"),
            source_commit="def456",
        )
        self.assertEqual(run.state, MigrationState.PLANNED)

    async def test_failed_rollback_can_record_manual_intervention(self):
        await self.store.create_run(
            "run-1",
            original_packages=("NHMisc", "Honeypot"),
            source_commit="abc123",
        )
        await self.store.transition(
            "run-1",
            MigrationState.PLANNED,
            MigrationState.QUIESCING,
        )
        await self.store.transition(
            "run-1",
            MigrationState.QUIESCING,
            MigrationState.ROLLING_BACK,
        )

        run = await self.store.transition(
            "run-1",
            MigrationState.ROLLING_BACK,
            MigrationState.MANUAL_INTERVENTION,
            validations={"rollback_error": "restore failed"},
        )

        self.assertEqual(run.state, MigrationState.MANUAL_INTERVENTION)
        self.assertEqual(run.validations["rollback_error"], "restore failed")

        retrying = await self.store.transition(
            "run-1",
            MigrationState.MANUAL_INTERVENTION,
            MigrationState.ROLLING_BACK,
        )

        self.assertEqual(retrying.state, MigrationState.ROLLING_BACK)
