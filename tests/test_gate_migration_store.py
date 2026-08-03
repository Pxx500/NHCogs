import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

NHMISC_PATH = Path(__file__).parents[1] / "NHMisc"
PACKAGE_NAME = "nhmisc_gate_migration_store_tests"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(NHMISC_PATH)]
sys.modules[PACKAGE_NAME] = package


def load_module(name):
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.{name}", NHMISC_PATH / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate_migration = load_module("gate_migration")
gate_migration_store = load_module("gate_migration_store")


class GateMigrationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "gate_migration.sqlite"
        self.store = gate_migration_store.GateMigrationStore(self.path)
        await self.store.initialize()

    @staticmethod
    def migration_plan():
        return gate_migration.plan_migration(
            (
                gate_migration.MemberSnapshot(
                    user_id=20,
                    username="SP user",
                    role_ids=(1348078496710135888, 222),
                ),
                gate_migration.MemberSnapshot(
                    user_id=10,
                    username="MP user",
                    role_ids=(798700443979087892, 111),
                ),
            )
        )

    async def test_active_run_and_immutable_member_plans_survive_restart(self):
        plan = self.migration_plan()
        created = await self.store.create_run(
            run_id="run-1",
            guild_id=123,
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            snapshot_sha256="abc123",
            plan=plan,
        )

        restarted = gate_migration_store.GateMigrationStore(self.path)
        await restarted.initialize()
        loaded = await restarted.get_run("run-1")
        members = await restarted.get_members("run-1")

        self.assertEqual(loaded, created)
        self.assertEqual(await restarted.get_active_run(123), created)
        self.assertEqual(tuple(member.plan for member in members), plan.members)
        self.assertTrue(all(member.status == "pending" for member in members))
        self.assertTrue(all(member.attempts == 0 for member in members))
        with self.assertRaises(gate_migration_store.ActiveMigrationExistsError):
            await restarted.create_run(
                run_id="run-2",
                guild_id=123,
                operator_id=456,
                channel_id=789,
                created_at="2026-08-03T12:01:00+00:00",
                snapshot_sha256="def456",
                plan=plan,
            )

    async def test_run_and_member_transitions_are_durable_and_reject_reexecution(self):
        await self.store.create_run(
            run_id="run-1",
            guild_id=123,
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            snapshot_sha256="abc123",
            plan=self.migration_plan(),
        )

        prepared = await self.store.transition_run(
            "run-1", gate_migration_store.RunState.PREPARED
        )
        applying = await self.store.transition_run(
            "run-1", gate_migration_store.RunState.APPLYING
        )
        first_attempt = await self.store.begin_member_attempt("run-1", 20)
        failed = await self.store.set_member_status(
            "run-1",
            20,
            gate_migration_store.MemberStatus.FAILED,
            error_code="discord_http_error",
        )
        second_attempt = await self.store.begin_member_attempt("run-1", 20)
        completed = await self.store.set_member_status(
            "run-1", 20, gate_migration_store.MemberStatus.COMPLETED
        )

        self.assertEqual(prepared.state, gate_migration_store.RunState.PREPARED)
        self.assertEqual(applying.state, gate_migration_store.RunState.APPLYING)
        self.assertEqual(
            await self.store.get_schema_state(123),
            gate_migration_store.SchemaState.MIGRATING,
        )
        self.assertEqual(first_attempt.status, gate_migration_store.MemberStatus.IN_PROGRESS)
        self.assertEqual(first_attempt.attempts, 1)
        self.assertEqual(failed.status, gate_migration_store.MemberStatus.FAILED)
        self.assertEqual(failed.error_code, "discord_http_error")
        self.assertEqual(second_attempt.attempts, 2)
        self.assertEqual(completed.status, gate_migration_store.MemberStatus.COMPLETED)
        self.assertIsNone(completed.error_code)
        with self.assertRaises(gate_migration_store.InvalidStateTransitionError):
            await self.store.begin_member_attempt("run-1", 20)

    async def test_restore_progress_is_restart_safe_and_rejects_reexecution(self):
        await self.store.create_run(
            run_id="run-restore",
            guild_id=123,
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            snapshot_sha256="abc123",
            plan=self.migration_plan(),
        )
        await self.store.begin_member_attempt("run-restore", 20)
        await self.store.set_member_status(
            "run-restore", 20, gate_migration_store.MemberStatus.COMPLETED
        )

        first_attempt = await self.store.begin_restore_attempt("run-restore", 20)
        failed = await self.store.set_restore_status(
            "run-restore",
            20,
            gate_migration_store.RestoreStatus.FAILED,
            error_code="discord_api_error",
        )
        second_attempt = await self.store.begin_restore_attempt("run-restore", 20)
        completed = await self.store.set_restore_status(
            "run-restore", 20, gate_migration_store.RestoreStatus.COMPLETED
        )

        restarted = gate_migration_store.GateMigrationStore(self.path)
        await restarted.initialize()
        restored_member = next(
            member
            for member in await restarted.get_members("run-restore")
            if member.plan.snapshot.user_id == 20
        )
        self.assertEqual(first_attempt.restore_attempts, 1)
        self.assertEqual(failed.restore_status, gate_migration_store.RestoreStatus.FAILED)
        self.assertEqual(failed.restore_error_code, "discord_api_error")
        self.assertEqual(second_attempt.restore_attempts, 2)
        self.assertEqual(completed.restore_status, gate_migration_store.RestoreStatus.COMPLETED)
        self.assertIsNone(completed.restore_error_code)
        self.assertEqual(restored_member, completed)
        with self.assertRaises(gate_migration_store.InvalidStateTransitionError):
            await restarted.begin_restore_attempt("run-restore", 20)

    async def test_conflicted_member_can_be_retried_after_manual_repair(self):
        await self.store.create_run(
            run_id="run-conflict-retry",
            guild_id=123,
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            snapshot_sha256="abc123",
            plan=self.migration_plan(),
        )
        await self.store.begin_member_attempt("run-conflict-retry", 20)
        await self.store.set_member_status(
            "run-conflict-retry",
            20,
            gate_migration_store.MemberStatus.CONFLICT,
            error_code="member_drift",
        )

        retried = await self.store.begin_member_attempt("run-conflict-retry", 20)

        self.assertEqual(retried.status, gate_migration_store.MemberStatus.IN_PROGRESS)
        self.assertEqual(retried.attempts, 2)
        self.assertIsNone(retried.error_code)

    async def test_uploaded_artifact_references_and_hashes_survive_restart(self):
        await self.store.create_run(
            run_id="run-1",
            guild_id=123,
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            snapshot_sha256="abc123",
            plan=self.migration_plan(),
        )
        artifacts = (
            gate_migration_store.StoredArtifact(
                run_id="run-1",
                kind="backup_part",
                part_index=1,
                filename="backup.part-001.jsonl.gz",
                sha256="part-hash",
                size=1234,
                channel_id=789,
                message_id=1001,
            ),
            gate_migration_store.StoredArtifact(
                run_id="run-1",
                kind="manifest",
                part_index=0,
                filename="backup.manifest.json",
                sha256="manifest-hash",
                size=321,
                channel_id=789,
                message_id=1002,
            ),
        )
        for artifact in artifacts:
            await self.store.record_artifact(artifact)

        restarted = gate_migration_store.GateMigrationStore(self.path)
        await restarted.initialize()

        self.assertEqual(await restarted.get_artifacts("run-1"), artifacts)

    async def test_finalize_keeps_only_completion_receipt_and_current_schema(self):
        await self.store.create_run(
            run_id="run-1",
            guild_id=123,
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            snapshot_sha256="abc123",
            plan=self.migration_plan(),
        )
        for state in (
            gate_migration_store.RunState.PREPARED,
            gate_migration_store.RunState.APPLYING,
            gate_migration_store.RunState.APPLIED,
            gate_migration_store.RunState.VERIFIED,
        ):
            await self.store.transition_run("run-1", state)
        await self.store.record_artifact(
            gate_migration_store.StoredArtifact(
                run_id="run-1",
                kind="backup_part",
                part_index=1,
                filename="backup.jsonl.gz",
                sha256="part-hash",
                size=1234,
                channel_id=789,
                message_id=1001,
            )
        )
        receipt = gate_migration_store.CompletionReceipt(
            run_id="run-1",
            guild_id=123,
            completed_at="2026-08-03T13:00:00+00:00",
            snapshot_sha256="abc123",
            backup_channel_id=789,
            backup_message_ids=(1001,),
        )

        await self.store.finalize(receipt)

        self.assertEqual(await self.store.get_receipt("run-1"), receipt)
        self.assertIsNone(await self.store.get_run("run-1"))
        self.assertEqual(await self.store.get_members("run-1"), ())
        self.assertEqual(await self.store.get_artifacts("run-1"), ())
        self.assertEqual(
            await self.store.get_schema_state(123),
            gate_migration_store.SchemaState.CURRENT,
        )
        with self.assertRaises(gate_migration_store.ActiveMigrationExistsError):
            await self.store.create_run(
                run_id="run-2",
                guild_id=123,
                operator_id=456,
                channel_id=789,
                created_at="2026-08-03T14:00:00+00:00",
                snapshot_sha256="def456",
                plan=self.migration_plan(),
            )


if __name__ == "__main__":
    unittest.main()
