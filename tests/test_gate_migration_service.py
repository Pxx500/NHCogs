import csv
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

NHMISC_PATH = Path(__file__).parents[1] / "NHMisc"
PACKAGE_NAME = "nhmisc_gate_migration_service_tests"
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
role_analytics_service = load_module("role_analytics_service")
gate_migration_service = load_module("gate_migration_service")


class FakeRole:
    def __init__(self, role_id, position):
        self.id = role_id
        self.position = position


class FakeForbidden(RuntimeError):
    status = 403


class FakeMember:
    def __init__(self, user_id, username, roles, *, bot=False, top_position=200):
        self.id = user_id
        self.name = username
        self.roles = roles
        self.bot = bot
        self.top_role = FakeRole(user_id * 10_000, top_position)
        self.edit_calls = []
        self.edit_error = None

    async def edit(self, *, roles, reason):
        self.edit_calls.append((tuple(roles), reason))
        if self.edit_error is not None:
            raise self.edit_error
        self.roles = list(roles)


class FakeGuild:
    def __init__(self, members):
        self.id = 123
        self.default_role = FakeRole(123, 0)
        self._gate_roles = {
            role_id: FakeRole(role_id, position)
            for position, role_id in enumerate(
                sorted(gate_migration.ALL_GATE_ROLE_IDS), start=1
            )
        }
        self._extra_roles = {}
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=True),
            top_role=FakeRole(999_999, 100),
        )
        self.members = members

    def get_role(self, role_id):
        gate_role = self._gate_roles.get(role_id)
        if gate_role is not None:
            return gate_role
        extra_role = self._extra_roles.get(role_id)
        if extra_role is not None:
            return extra_role
        return next(
            (
                role
                for member in self.members
                for role in member.roles
                if role.id == role_id
            ),
            None,
        )

    def get_member(self, user_id):
        return next((member for member in self.members if member.id == user_id), None)

    def member_roles(self, *role_ids):
        return [self.default_role, *(self._gate_roles[role_id] for role_id in role_ids)]


class FakeAnalytics:
    def __init__(self):
        self.calls = []
        self.errors = []

    async def sync_guild(self, guild, *, manual, force_fresh=False):
        self.calls.append((guild.id, manual, force_fresh))
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(source="gateway-chunk", member_count=len(guild.members))


class FakeStore:
    def __init__(self, active_run=None, schema_state=gate_migration_store.SchemaState.LEGACY):
        self.active_run = active_run
        self.schema_state = schema_state
        self.created = []
        self.artifacts = []
        self.transitions = []

    async def get_active_run(self, guild_id):
        return self.active_run

    async def get_schema_state(self, guild_id):
        return self.schema_state

    async def create_run(self, **values):
        self.created.append(values)
        return SimpleNamespace(
            **{
                key: values[key]
                for key in ("run_id", "guild_id", "operator_id", "channel_id")
            }
        )

    async def record_artifact(self, artifact):
        self.artifacts.append(artifact)

    async def transition_run(self, run_id, state):
        self.transitions.append((run_id, state))
        return SimpleNamespace(run_id=run_id, state=state)


class GateMigrationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_reports_only_duplicate_legacy_categories(self):
        guild = FakeGuild([])
        valid_member = FakeMember(
            10,
            "Valid SP and MP",
            guild.member_roles(
                gate_migration.LEGACY_SP_ROLE_IDS[0],
                gate_migration.LEGACY_MP_ROLE_IDS[0],
            ),
            top_position=500,
        )
        duplicate_member = FakeMember(
            20,
            "Duplicate SP",
            guild.member_roles(
                gate_migration.LEGACY_SP_ROLE_IDS[0],
                gate_migration.LEGACY_SP_ROLE_IDS[2],
                gate_migration.LEGACY_MP_ROLE_IDS[1],
            ),
            bot=True,
            top_position=500,
        )
        guild.members = [duplicate_member, valid_member]
        analytics = FakeAnalytics()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            analytics,
            SimpleNamespace(),
        )

        result = await service.audit_legacy_users(guild)

        self.assertEqual(analytics.calls, [(123, True, True)])
        self.assertEqual(
            tuple(member.snapshot.user_id for member in result.members),
            (20,),
        )
        rows = list(csv.DictReader(io.StringIO(result.csv_data.decode("utf-8"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "Duplicate SP")
        self.assertEqual(rows[0]["user_id"], "20")
        self.assertEqual(
            rows[0]["selected_sp_role_id"],
            str(gate_migration.LEGACY_SP_ROLE_IDS[2]),
        )
        self.assertEqual(
            rows[0]["selected_mp_role_id"],
            str(gate_migration.LEGACY_MP_ROLE_IDS[1]),
        )

    async def test_prepare_refuses_active_run_before_requesting_members(self):
        guild = FakeGuild([])
        analytics = FakeAnalytics()
        store = FakeStore(active_run=SimpleNamespace(run_id="existing"))
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)), analytics, store
        )

        with self.assertRaisesRegex(
            gate_migration_service.MigrationPreflightError,
            "A gate migration is already active",
        ):
            await service.prepare_run(
                guild,
                run_id="new-run",
                operator_id=456,
                channel_id=789,
                created_at="2026-08-03T12:00:00+00:00",
                max_part_size=10_000,
            )

        self.assertEqual(analytics.calls, [])
        self.assertEqual(store.created, [])

    async def test_prepare_refuses_completed_migration_before_requesting_members(self):
        guild = FakeGuild([])
        analytics = FakeAnalytics()
        store = FakeStore(schema_state=gate_migration_store.SchemaState.CURRENT)
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)), analytics, store
        )

        with self.assertRaisesRegex(
            gate_migration_service.MigrationPreflightError,
            "has already started",
        ):
            await service.prepare_run(
                guild,
                run_id="second-run",
                operator_id=456,
                channel_id=789,
                created_at="2026-08-03T14:00:00+00:00",
                max_part_size=10_000,
            )

        self.assertEqual(analytics.calls, [])
        self.assertEqual(store.created, [])

    async def test_prepare_persists_the_same_snapshot_used_by_verified_backup(self):
        guild = FakeGuild([])
        guild.members = [
            FakeMember(
                10,
                "Prepared member",
                [
                    guild.default_role,
                    guild.get_role(gate_migration.LEGACY_SP_ROLE_IDS[1]),
                    FakeRole(777, 500),
                ],
            )
        ]
        analytics = FakeAnalytics()
        store = FakeStore()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)), analytics, store
        )

        prepared = await service.prepare_run(
            guild,
            run_id="run-1",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )

        gate_migration.verify_backup(prepared.backup)
        self.assertEqual(len(store.created), 1)
        persisted = store.created[0]
        self.assertEqual(persisted["plan"], prepared.collection.plan)
        self.assertEqual(
            persisted["snapshot_sha256"], prepared.backup.snapshot_sha256
        )
        self.assertEqual(prepared.collection.summary.total_members, 1)
        self.assertEqual(
            prepared.collection.plan.members[0].snapshot.role_ids,
            (777, 1348078496710135888),
        )

    async def test_publish_uploads_artifacts_before_summary_and_marks_prepared_last(self):
        guild = FakeGuild([])
        guild.members = [
            FakeMember(
                20,
                "Duplicate SP",
                guild.member_roles(
                    gate_migration.LEGACY_SP_ROLE_IDS[0],
                    gate_migration.LEGACY_SP_ROLE_IDS[2],
                ),
            )
        ]
        store = FakeStore()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        prepared = await service.prepare_run(
            guild,
            run_id="run-1",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        events = []

        async def send_attachment(filename, data):
            events.append(("attachment", filename, len(data)))
            return SimpleNamespace(id=1000 + len(events))

        async def send_text(content):
            events.append(("text", content))
            return SimpleNamespace(id=1000 + len(events))

        published = await service.publish_preparation(
            prepared,
            send_attachment=send_attachment,
            send_text=send_text,
        )

        event_kinds = [event[0] for event in events]
        self.assertIn("text", event_kinds)
        first_text = event_kinds.index("text")
        self.assertTrue(all(kind == "attachment" for kind in event_kinds[:first_text]))
        self.assertTrue(all(kind == "text" for kind in event_kinds[first_text:]))
        self.assertGreaterEqual(len(store.artifacts), 2)
        self.assertTrue(any(artifact.kind == "anomaly" for artifact in store.artifacts))
        self.assertEqual(
            store.transitions,
            [("run-1", gate_migration_store.RunState.PREPARED)],
        )
        self.assertEqual(len(published.summary_message_ids), len(published.pages))
        complete_summary = "\n".join(published.pages)
        self.assertIn("Members backed up: 1", complete_summary)
        self.assertIn("Members to change: 1", complete_summary)
        self.assertIn("Tier 10", complete_summary)

    async def test_publish_failure_never_marks_run_prepared(self):
        guild = FakeGuild([])
        guild.members = [
            FakeMember(
                10,
                "Backup member",
                guild.member_roles(gate_migration.LEGACY_MP_ROLE_IDS[0]),
            )
        ]
        store = FakeStore()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        prepared = await service.prepare_run(
            guild,
            run_id="run-1",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )

        async def fail_attachment(filename, data):
            del filename, data
            raise RuntimeError("Discord upload failed")

        async def send_text(content):
            self.fail(f"Summary sent after failed backup: {content}")

        with self.assertRaisesRegex(RuntimeError, "Discord upload failed"):
            await service.publish_preparation(
                prepared,
                send_attachment=fail_attachment,
                send_text=send_text,
            )

        self.assertEqual(store.transitions, [])

    async def test_apply_edits_only_changed_members_and_preserves_non_gate_roles(self):
        guild = FakeGuild([])
        unchanged = FakeMember(
            10,
            "Already tier one",
            [
                guild.default_role,
                guild.get_role(gate_migration.LEGACY_MP_ROLE_IDS[0]),
                FakeRole(888, 500),
            ],
            top_position=500,
        )
        changed = FakeMember(
            20,
            "SP plus MP",
            [
                guild.default_role,
                guild.get_role(gate_migration.LEGACY_SP_ROLE_IDS[1]),
                guild.get_role(gate_migration.LEGACY_MP_ROLE_IDS[0]),
                FakeRole(777, 500),
            ],
            top_position=500,
        )
        guild.members = [changed, unchanged]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        analytics = FakeAnalytics()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            analytics,
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-1",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run("run-1", gate_migration_store.RunState.PREPARED)
        changed.roles.append(FakeRole(666, 500))
        analytics.errors.append(
            role_analytics_service.FullMemberRequestCooldownError(2.5)
        )

        sleep = mock.AsyncMock()
        with mock.patch("asyncio.sleep", sleep):
            result = await service.apply_run(guild, "run-1")

        self.assertEqual(result.completed, 2)
        self.assertEqual(result.tier_role_counts, (1, 0, 1, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(
            analytics.calls,
            [(123, True, True), (123, True, True), (123, True, True)],
        )
        sleep.assert_awaited_once_with(2.5)
        self.assertEqual(unchanged.edit_calls, [])
        self.assertEqual(len(changed.edit_calls), 1)
        edited_role_ids = {role.id for role in changed.edit_calls[0][0]}
        self.assertEqual(
            edited_role_ids,
            {
                666,
                777,
                gate_migration.TARGET_TIER_ROLE_IDS[2],
                gate_migration.SINGLEPLAYER_COMPLETED_ROLE_ID,
            },
        )
        members = await store.get_members("run-1")
        self.assertTrue(
            all(
                member.status is gate_migration_store.MemberStatus.COMPLETED
                for member in members
            )
        )
        attempts = {
            member.plan.snapshot.user_id: member.attempts for member in members
        }
        self.assertEqual(attempts[unchanged.id], 0)
        self.assertEqual(attempts[changed.id], 1)
        self.assertEqual(
            (await store.get_run("run-1")).state,
            gate_migration_store.RunState.APPLIED,
        )

    async def test_apply_refuses_member_drift_before_any_role_edit(self):
        guild = FakeGuild([])
        member = FakeMember(
            20,
            "Drifted member",
            guild.member_roles(
                gate_migration.LEGACY_SP_ROLE_IDS[1],
                gate_migration.LEGACY_MP_ROLE_IDS[0],
            ),
        )
        guild.members = [member]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-drift",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-drift", gate_migration_store.RunState.PREPARED
        )
        member.roles = guild.member_roles(gate_migration.TARGET_TIER_ROLE_IDS[4])

        with self.assertRaisesRegex(
            gate_migration_service.MigrationPreflightError,
            "changed since preparation",
        ):
            await service.apply_run(guild, "run-drift")

        self.assertEqual(member.edit_calls, [])
        self.assertEqual(
            (await store.get_run("run-drift")).state,
            gate_migration_store.RunState.PREPARED,
        )

    async def test_apply_resumes_unknown_outcome_without_duplicate_edit(self):
        guild = FakeGuild([])
        member = FakeMember(
            20,
            "Interrupted member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        guild.members = [member]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-restart",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-restart", gate_migration_store.RunState.PREPARED
        )
        await store.transition_run(
            "run-restart", gate_migration_store.RunState.APPLYING
        )
        await store.begin_member_attempt("run-restart", member.id)
        member.roles = guild.member_roles(
            gate_migration.TARGET_TIER_ROLE_IDS[0],
            gate_migration.SINGLEPLAYER_COMPLETED_ROLE_ID,
        )

        result = await service.apply_run(guild, "run-restart")

        self.assertEqual(result.completed, 1)
        self.assertEqual(member.edit_calls, [])
        stored_member = (await store.get_members("run-restart"))[0]
        self.assertEqual(stored_member.status, gate_migration_store.MemberStatus.COMPLETED)
        self.assertEqual(stored_member.attempts, 1)
        self.assertEqual(
            (await store.get_run("run-restart")).state,
            gate_migration_store.RunState.APPLIED,
        )

    async def test_resume_checks_all_pending_members_before_any_new_edit(self):
        guild = FakeGuild([])
        first = FakeMember(
            10,
            "First pending member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        drifted = FakeMember(
            20,
            "Later drifted member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        guild.members = [first, drifted]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-resume-drift",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-resume-drift", gate_migration_store.RunState.PREPARED
        )
        await store.transition_run(
            "run-resume-drift", gate_migration_store.RunState.APPLYING
        )
        drifted.roles = guild.member_roles(gate_migration.TARGET_TIER_ROLE_IDS[4])

        with self.assertRaisesRegex(
            gate_migration_service.MigrationPreflightError,
            "changed since preparation",
        ):
            await service.apply_run(guild, "run-resume-drift")

        self.assertEqual(first.edit_calls, [])
        self.assertEqual(drifted.edit_calls, [])

    async def test_apply_stops_when_in_progress_member_has_unexpected_gate_roles(self):
        guild = FakeGuild([])
        member = FakeMember(
            20,
            "Conflicted member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        guild.members = [member]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-conflict",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-conflict", gate_migration_store.RunState.PREPARED
        )
        await store.transition_run(
            "run-conflict", gate_migration_store.RunState.APPLYING
        )
        await store.begin_member_attempt("run-conflict", member.id)
        member.roles = guild.member_roles(gate_migration.TARGET_TIER_ROLE_IDS[4])

        with self.assertRaisesRegex(
            gate_migration_service.MigrationPreflightError,
            "unexpected Gate roles",
        ):
            await service.apply_run(guild, "run-conflict")

        self.assertEqual(member.edit_calls, [])
        stored_member = (await store.get_members("run-conflict"))[0]
        self.assertEqual(stored_member.status, gate_migration_store.MemberStatus.CONFLICT)
        self.assertEqual(stored_member.error_code, "member_drift")
        self.assertEqual(
            (await store.get_run("run-conflict")).state,
            gate_migration_store.RunState.APPLY_FAILED,
        )

        member.roles = guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0])
        result = await service.apply_run(guild, "run-conflict")

        self.assertEqual(result.completed, 1)
        stored_member = (await store.get_members("run-conflict"))[0]
        self.assertEqual(stored_member.status, gate_migration_store.MemberStatus.COMPLETED)
        self.assertEqual(stored_member.attempts, 2)

    async def test_apply_reports_periodic_progress_for_large_runs(self):
        guild = FakeGuild([])
        guild.members = [
            FakeMember(user_id, f"Member {user_id}", guild.member_roles())
            for user_id in range(1, 1002)
        ]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-progress",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=1_000_000,
        )
        await store.transition_run(
            "run-progress", gate_migration_store.RunState.PREPARED
        )
        updates = []

        async def record_progress(progress):
            updates.append(progress)

        result = await service.apply_run(
            guild, "run-progress", progress_callback=record_progress
        )

        self.assertEqual(result.completed, 1001)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].processed, 1000)
        self.assertEqual(updates[0].remaining, 1)
        self.assertEqual(updates[0].completed, 1000)

    async def test_apply_skips_member_forbidden_and_continues_batch(self):
        guild = FakeGuild([])
        skipped = FakeMember(
            10,
            "Unmodifiable member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        skipped.edit_error = FakeForbidden("role hierarchy")
        changed = FakeMember(
            20,
            "Modifiable member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[1]),
        )
        guild.members = [skipped, changed]
        original_skipped_roles = tuple(skipped.roles)
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-forbidden",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-forbidden", gate_migration_store.RunState.PREPARED
        )

        result = await service.apply_run(guild, "run-forbidden")

        self.assertEqual(result.completed, 1)
        self.assertEqual(result.skipped_unmodifiable, 1)
        self.assertEqual(tuple(skipped.roles), original_skipped_roles)
        self.assertEqual(len(changed.edit_calls), 1)
        statuses = {
            member.plan.snapshot.user_id: member.status
            for member in await store.get_members("run-forbidden")
        }
        self.assertEqual(
            statuses,
            {
                skipped.id: gate_migration_store.MemberStatus.SKIPPED_UNMODIFIABLE,
                changed.id: gate_migration_store.MemberStatus.COMPLETED,
            },
        )
        self.assertEqual(
            (await store.get_run("run-forbidden")).state,
            gate_migration_store.RunState.APPLIED,
        )

    async def test_resume_retries_skipped_member_after_hierarchy_is_fixed(self):
        guild = FakeGuild([])
        member = FakeMember(
            10,
            "Retry member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        member.edit_error = FakeForbidden("role hierarchy")
        guild.members = [member]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-retry",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-retry", gate_migration_store.RunState.PREPARED
        )
        await service.apply_run(guild, "run-retry")
        member.edit_error = None

        result = await service.apply_run(guild, "run-retry")

        self.assertEqual(result.completed, 1)
        self.assertEqual(result.skipped_unmodifiable, 0)
        stored_member = (await store.get_members("run-retry"))[0]
        self.assertEqual(stored_member.status, gate_migration_store.MemberStatus.COMPLETED)
        self.assertEqual(stored_member.attempts, 2)
        self.assertEqual(
            (await store.get_run("run-retry")).state,
            gate_migration_store.RunState.APPLIED,
        )

    async def test_apply_stops_and_journals_other_discord_errors(self):
        guild = FakeGuild([])
        failed = FakeMember(
            10,
            "API failure",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        failed.edit_error = RuntimeError("Discord unavailable")
        later = FakeMember(
            20,
            "Must not be touched",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[1]),
        )
        guild.members = [failed, later]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-api-failure",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-api-failure", gate_migration_store.RunState.PREPARED
        )

        with self.assertRaisesRegex(RuntimeError, "Discord unavailable"):
            await service.apply_run(guild, "run-api-failure")

        self.assertEqual(later.edit_calls, [])
        statuses = {
            member.plan.snapshot.user_id: member
            for member in await store.get_members("run-api-failure")
        }
        self.assertEqual(
            statuses[failed.id].status,
            gate_migration_store.MemberStatus.FAILED,
        )
        self.assertEqual(statuses[failed.id].error_code, "discord_api_error")
        self.assertEqual(
            statuses[later.id].status,
            gate_migration_store.MemberStatus.PENDING,
        )
        self.assertEqual(
            (await store.get_run("run-api-failure")).state,
            gate_migration_store.RunState.APPLY_FAILED,
        )

    async def test_apply_records_departed_member_and_continues(self):
        guild = FakeGuild([])
        departed = FakeMember(
            10,
            "Departed member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        remaining = FakeMember(
            20,
            "Remaining member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[1]),
        )
        unchanged_departed = FakeMember(
            30,
            "Unchanged departed member",
            guild.member_roles(),
        )
        guild.members = [departed, remaining, unchanged_departed]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-departed",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-departed", gate_migration_store.RunState.PREPARED
        )
        guild.members = [remaining]

        result = await service.apply_run(guild, "run-departed")

        self.assertEqual(result.departed, 2)
        self.assertEqual(result.completed, 1)
        stored_members = {
            member.plan.snapshot.user_id: member
            for member in await store.get_members("run-departed")
        }
        self.assertEqual(
            stored_members[departed.id].status,
            gate_migration_store.MemberStatus.DEPARTED,
        )
        self.assertEqual(
            stored_members[remaining.id].status,
            gate_migration_store.MemberStatus.COMPLETED,
        )
        self.assertEqual(
            stored_members[unchanged_departed.id].status,
            gate_migration_store.MemberStatus.DEPARTED,
        )
        self.assertEqual(stored_members[unchanged_departed.id].attempts, 0)

    async def test_verify_forces_fresh_sync_and_marks_matching_run_current(self):
        guild = FakeGuild([])
        member = FakeMember(
            10,
            "Verified member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        guild.members = [member]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        analytics = FakeAnalytics()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)), analytics, store
        )
        await service.prepare_run(
            guild,
            run_id="run-verify",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-verify", gate_migration_store.RunState.PREPARED
        )
        await service.apply_run(guild, "run-verify")

        result = await service.verify_run(guild, "run-verify")

        self.assertEqual(result.matched, 1)
        self.assertEqual(result.departed, 0)
        self.assertEqual(analytics.calls[-1], (guild.id, True, True))
        self.assertEqual(
            (await store.get_run("run-verify")).state,
            gate_migration_store.RunState.VERIFIED,
        )
        self.assertEqual(
            await store.get_schema_state(guild.id),
            gate_migration_store.SchemaState.CURRENT,
        )

    async def test_restore_stays_retryable_when_a_member_is_unmodifiable(self):
        guild = FakeGuild([])
        member = FakeMember(
            10,
            "Restore retry member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        guild.members = [member]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        await service.prepare_run(
            guild,
            run_id="run-restore-retry",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-restore-retry", gate_migration_store.RunState.PREPARED
        )
        await service.apply_run(guild, "run-restore-retry")
        member.edit_error = FakeForbidden("role hierarchy")

        first_result = await service.restore_run(guild, "run-restore-retry")

        self.assertEqual(first_result.skipped_unmodifiable, 1)
        self.assertEqual(
            (await store.get_run("run-restore-retry")).state,
            gate_migration_store.RunState.RESTORE_FAILED,
        )

        member.edit_error = None
        second_result = await service.restore_run(guild, "run-restore-retry")

        self.assertEqual(second_result.completed, 1)
        self.assertEqual(second_result.skipped_unmodifiable, 0)
        self.assertEqual(
            (await store.get_run("run-restore-retry")).state,
            gate_migration_store.RunState.RESTORED,
        )

    async def test_verify_refuses_unresolved_skipped_member(self):
        guild = FakeGuild([])
        member = FakeMember(
            10,
            "Skipped member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        member.edit_error = FakeForbidden("role hierarchy")
        guild.members = [member]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        analytics = FakeAnalytics()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)), analytics, store
        )
        await service.prepare_run(
            guild,
            run_id="run-unverified",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-unverified", gate_migration_store.RunState.PREPARED
        )
        await service.apply_run(guild, "run-unverified")

        with self.assertRaisesRegex(
            gate_migration_service.MigrationPreflightError,
            "unresolved members",
        ):
            await service.verify_run(guild, "run-unverified")

        self.assertEqual(analytics.calls[-1], (guild.id, True, True))
        self.assertEqual(
            (await store.get_run("run-unverified")).state,
            gate_migration_store.RunState.APPLIED,
        )
        self.assertEqual(
            await store.get_schema_state(guild.id),
            gate_migration_store.SchemaState.MIGRATING,
        )

    async def test_restore_recovers_backup_roles_without_removing_new_non_gate_roles(self):
        guild = FakeGuild([])
        backed_up_role = FakeRole(777, 20)
        later_role = FakeRole(888, 21)
        guild._extra_roles = {777: backed_up_role, 888: later_role}
        member = FakeMember(
            10,
            "Restored member",
            [
                guild.default_role,
                guild.get_role(gate_migration.LEGACY_SP_ROLE_IDS[0]),
                backed_up_role,
            ],
        )
        guild.members = [member]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        analytics = FakeAnalytics()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)), analytics, store
        )
        await service.prepare_run(
            guild,
            run_id="run-restore",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.transition_run(
            "run-restore", gate_migration_store.RunState.PREPARED
        )
        await service.apply_run(guild, "run-restore")
        member.roles = [
            role
            for role in member.roles
            if role.id != backed_up_role.id
        ] + [later_role]

        result = await service.restore_run(guild, "run-restore")

        self.assertEqual(result.completed, 1)
        self.assertEqual(result.departed, 0)
        restored_role_ids = {role.id for role in member.roles}
        self.assertEqual(
            restored_role_ids,
            {
                gate_migration.LEGACY_SP_ROLE_IDS[0],
                backed_up_role.id,
                later_role.id,
            },
        )
        stored_member = (await store.get_members("run-restore"))[0]
        self.assertEqual(
            stored_member.restore_status,
            gate_migration_store.RestoreStatus.COMPLETED,
        )
        self.assertEqual(
            (await store.get_run("run-restore")).state,
            gate_migration_store.RunState.RESTORED,
        )
        self.assertEqual(
            await store.get_schema_state(guild.id),
            gate_migration_store.SchemaState.LEGACY,
        )
        self.assertEqual(analytics.calls[-1], (guild.id, True, True))

    async def test_status_export_and_finalize_preserve_verified_receipt(self):
        guild = FakeGuild([])
        member = FakeMember(
            10,
            "Finalized member",
            guild.member_roles(gate_migration.LEGACY_SP_ROLE_IDS[0]),
        )
        guild.members = [member]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = gate_migration_store.GateMigrationStore(
            Path(temp_dir.name) / "gate_migration.sqlite"
        )
        await store.initialize()
        service = gate_migration_service.GateMigrationService(
            SimpleNamespace(intents=SimpleNamespace(members=True)),
            FakeAnalytics(),
            store,
        )
        prepared = await service.prepare_run(
            guild,
            run_id="run-finalize",
            operator_id=456,
            channel_id=789,
            created_at="2026-08-03T12:00:00+00:00",
            max_part_size=10_000,
        )
        await store.record_artifact(
            gate_migration_store.StoredArtifact(
                run_id="run-finalize",
                kind="backup_part",
                part_index=1,
                filename=prepared.backup.parts[0].filename,
                sha256=prepared.backup.parts[0].sha256,
                size=len(prepared.backup.parts[0].data),
                channel_id=789,
                message_id=999,
            )
        )
        await store.record_artifact(
            gate_migration_store.StoredArtifact(
                run_id="run-finalize",
                kind="manifest",
                part_index=0,
                filename="gate-migration-run-finalize-manifest.json",
                sha256="manifest-sha256",
                size=100,
                channel_id=789,
                message_id=1000,
            )
        )
        await store.transition_run(
            "run-finalize", gate_migration_store.RunState.PREPARED
        )
        await service.apply_run(guild, "run-finalize")
        await service.verify_run(guild, "run-finalize")

        status = await service.status_run(guild, "run-finalize")
        exported = await service.export_run(guild, "run-finalize", max_part_size=100)

        self.assertTrue(status.backup_verified)
        self.assertEqual(status.member_counts[gate_migration_store.MemberStatus.COMPLETED], 1)
        gate_migration.verify_backup(exported)
        self.assertEqual(exported.snapshot_sha256, prepared.backup.snapshot_sha256)

        receipt = await service.finalize_run(
            guild,
            "run-finalize",
            completed_at="2026-08-03T13:00:00+00:00",
        )

        self.assertEqual(receipt.backup_channel_id, 789)
        self.assertEqual(receipt.backup_message_ids, (999, 1000))
        self.assertIsNone(await store.get_run("run-finalize"))
        self.assertEqual(await store.get_receipt("run-finalize"), receipt)
        self.assertIsNone(await store.get_active_run(guild.id))
