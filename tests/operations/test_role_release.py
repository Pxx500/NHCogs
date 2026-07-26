import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tests.test_detection_pipeline import _Bot, _isolated_honeypot_modules


class _GuildConfig:
    async def joinwatch_pending_roles(self):
        return {}


class _Config:
    def guild(self, guild):
        return _GuildConfig()

    def guild_from_id(self, guild_id):
        return self

    async def all(self):
        return {}


class _Member:
    def __init__(self, role):
        self.id = 20
        self.roles = [role]
        self.removals = []
        self.guild = None

    async def remove_roles(self, role, *, reason):
        self.roles.remove(role)
        self.removals.append((role.id, reason))


class RoleReleaseHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _append_case(honeypot, cog, created_at, *, message_id=40):
        cog._case_store.initialize()
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=10,
                user_id=20,
                channel_id=30,
                message_id=message_id,
                content="evidence",
                created_at=created_at,
                jump_url=f"https://discord.test/messages/{message_id}",
                attachments=(),
            ),
            (),
        )

    @staticmethod
    def _record_ownership(honeypot, cog, case_id, role_id, now):
        ownership = cog._case_store.ensure_operation(
            case_id,
            honeypot.OperationType.ROLE_APPLY,
            f"role-apply:{case_id}:{role_id}",
        )
        ownership = cog._case_store.claim_operation(ownership.operation_id, now)
        cog._case_store.start_operation_effect(
            ownership.operation_id,
            ownership.claim_token,
            now,
        )
        result = cog._case_store.record_operation_role_ownership(
            ownership.operation_id,
            ownership.claim_token,
            case_id,
            10,
            20,
            role_id=role_id,
            now=now,
        )
        if result != "owned":
            raise AssertionError(f"unexpected ownership result: {result}")
        cog._case_store.complete_operation(
            ownership.operation_id,
            ownership.claim_token,
            now,
        )

    async def test_registered_unowned_release_completes_as_ownership_transferred(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                try:
                    handler_module = import_module("Honeypot.operations.role_release")
                except ModuleNotFoundError:
                    self.fail("role_release has no dedicated handler module")
                operations = import_module("Honeypot.operations")
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(id=20, roles=[role], removals=[])
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                expected_types = {
                    honeypot.OperationType.MESSAGE_PROCESS,
                    honeypot.OperationType.REVIEW_UPDATE,
                    honeypot.OperationType.REVIEW_PUBLISH,
                    honeypot.OperationType.CACHED_PURGE,
                    honeypot.OperationType.SOURCE_DELETE,
                    honeypot.OperationType.EVIDENCE_CLEANUP,
                    honeypot.OperationType.ROLE_RELEASE,
                    honeypot.OperationType.ROLE_APPLY,
                    honeypot.OperationType.MODERATION_ACTION,
                    honeypot.OperationType.MODERATOR_BAN,
                    honeypot.OperationType.MODERATOR_KICK,
                }
                self.assertEqual(set(operations.HANDLERS), expected_types)
                self.assertIs(
                    cog._detection_operation_handlers.resolve(
                        honeypot.OperationType.ROLE_RELEASE
                    ),
                    handler_module.role_release_handler,
                )
                for operation_type in honeypot.OperationType:
                    if operation_type not in expected_types:
                        self.assertIsNone(
                            cog._detection_operation_handlers.resolve(operation_type)
                        )

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(persisted.result, "ownership_transferred")
                self.assertEqual(member.roles, [role])
                self.assertEqual(member.removals, [])

    async def test_owned_release_removes_role_and_releases_ownership(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(role)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                self._record_ownership(
                    honeypot, cog, appended.case.case_id, role.id, now
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertIsNone(persisted.result)
                self.assertEqual(member.roles, [])
                self.assertEqual(
                    member.removals,
                    [
                        (
                            role.id,
                            "Detection case resolved; removing pending mute.",
                        )
                    ],
                )
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), ()
                )

    async def test_missing_role_or_member_or_membership_still_releases_ownership(self):
        for scenario in ("role_absent", "member_absent", "member_lacks_role"):
            with (
                self.subTest(scenario=scenario),
                TemporaryDirectory() as directory,
            ):
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    now = datetime.now(timezone.utc)
                    role = SimpleNamespace(id=55)
                    member = SimpleNamespace(
                        id=20,
                        roles=[] if scenario == "member_lacks_role" else [role],
                        removals=[],
                    )

                    async def fetch_member(user_id):
                        raise honeypot.discord.NotFound("member departed")

                    guild = SimpleNamespace(
                        id=10,
                        get_member=lambda user_id, scenario=scenario, member=member: (
                            None if scenario == "member_absent" else member
                        ),
                        fetch_member=fetch_member,
                        get_role=lambda role_id, scenario=scenario, role=role: (
                            None if scenario == "role_absent" else role
                        ),
                    )
                    bot = _Bot()
                    bot.get_guild = lambda guild_id, guild=guild: guild
                    cog = honeypot.Honeypot(bot)
                    cog.config = _Config()
                    appended = self._append_case(honeypot, cog, now)
                    self._record_ownership(
                        honeypot, cog, appended.case.case_id, role.id, now
                    )
                    operation = cog._case_store.ensure_operation(
                        appended.case.case_id,
                        honeypot.OperationType.ROLE_RELEASE,
                        f"role-release:{appended.case.case_id}:{role.id}",
                    )
                    claimed = cog._case_store.claim_operation(
                        operation.operation_id, now
                    )

                    await cog._execute_detection_case_operation(claimed, now)

                    snapshot = cog._case_store.get_case(appended.case.case_id)
                    persisted = next(
                        item
                        for item in snapshot.operations
                        if item.operation_id == operation.operation_id
                    )
                    self.assertIs(
                        persisted.status, honeypot.OperationStatus.SUCCEEDED
                    )
                    self.assertIsNone(persisted.result)
                    self.assertEqual(member.removals, [])
                    self.assertEqual(
                        cog._case_store.owned_role_ids(appended.case.case_id), ()
                    )

    async def test_unavailable_member_lookup_retries_with_exact_error(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: None,
                    get_role=lambda role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                self._record_ownership(
                    honeypot, cog, appended.case.case_id, role.id, now
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(persisted.result)
                self.assertIsNotNone(persisted.retry_at)
                self.assertEqual(
                    persisted.last_error,
                    "RuntimeError: detection case member lookup is unavailable",
                )
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id),
                    (role.id,),
                )

    async def test_http_member_lookup_is_wrapped_and_retried(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                lookup_error = honeypot.discord.HTTPException("lookup failed")

                async def fetch_member(user_id):
                    raise lookup_error

                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: None,
                    fetch_member=fetch_member,
                    get_role=lambda role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                self._record_ownership(
                    honeypot, cog, appended.case.case_id, role.id, now
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                context = honeypot.OperationContext(
                    operation=claimed,
                    snapshot=cog._case_store.get_case(appended.case.case_id),
                    lease=honeypot.OperationLease(
                        operation_id=claimed.operation_id,
                        claim_token=claimed.claim_token,
                    ),
                    now=now,
                )
                handler = cog._detection_operation_handlers.resolve(
                    honeypot.OperationType.ROLE_RELEASE
                )

                try:
                    await handler(cog, context)
                except Exception as error:
                    captured = error
                else:
                    self.fail("HTTP member lookup failure was not propagated")

                self.assertIs(type(captured), RuntimeError)
                self.assertEqual(
                    str(captured), "detection case member lookup failed"
                )
                self.assertIs(captured.__cause__, lookup_error)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(persisted.result)
                self.assertIsNotNone(persisted.retry_at)
                self.assertEqual(
                    persisted.last_error,
                    "RuntimeError: detection case member lookup failed",
                )
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id),
                    (role.id,),
                )

    async def test_failed_discord_removal_retries_without_releasing_ownership(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(role)

                async def reject_removal(rejected_role, *, reason):
                    raise honeypot.discord.HTTPException("remove failed")

                member.remove_roles = reject_removal
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                self._record_ownership(
                    honeypot, cog, appended.case.case_id, role.id, now
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(persisted.result)
                self.assertIsNotNone(persisted.retry_at)
                self.assertEqual(
                    persisted.last_error,
                    "RuntimeError: failed to release detection case role",
                )
                self.assertEqual(member.roles, [role])
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id),
                    (role.id,),
                )

    async def test_missing_cached_guild_retries_with_exact_error(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:55",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(persisted.result)
                self.assertIsNotNone(persisted.retry_at)
                self.assertEqual(
                    persisted.last_error,
                    "RuntimeError: detection case guild is unavailable",
                )

    async def test_invalid_role_identity_preserves_native_int_failure(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                guild = SimpleNamespace(id=10)
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:not-a-role",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(persisted.result)
                self.assertIsNotNone(persisted.retry_at)
                self.assertEqual(
                    persisted.last_error,
                    "ValueError: invalid literal for int() with base 10: "
                    "'not-a-role'",
                )

    async def test_start_fence_rejection_with_same_owner_retries_as_lease_lost(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(role)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                self._record_ownership(
                    honeypot, cog, appended.case.case_id, role.id, now
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                real_start = cog._case_store.start_role_release_effect

                def reject_mismatched_claim(
                    operation_id, claim_token, case_id, role_id, started_at
                ):
                    return real_start(
                        operation_id,
                        f"stale-{claim_token}",
                        case_id,
                        role_id,
                        started_at,
                    )

                cog._case_store.start_role_release_effect = reject_mismatched_claim

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(persisted.result)
                self.assertIsNotNone(persisted.retry_at)
                self.assertEqual(
                    persisted.last_error,
                    "RuntimeError: detection operation lease was lost",
                )
                self.assertEqual(member.roles, [role])
                self.assertEqual(member.removals, [])
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id),
                    (role.id,),
                )

    async def test_start_fence_observes_concurrent_owner_handoff_as_success(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(role)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                first = self._append_case(honeypot, cog, now)
                self._record_ownership(
                    honeypot, cog, first.case.case_id, role.id, now
                )
                resolution_time = now + timedelta(seconds=1)
                resolution = cog._case_store.claim_resolution(
                    first.case.case_id, resolution_time
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        resolution,
                        honeypot.CaseStatus.EXPIRED,
                        "expired",
                        None,
                        resolution_time,
                        final_operations=(
                            (
                                honeypot.OperationType.ROLE_RELEASE,
                                f"role-release:{first.case.case_id}:{role.id}",
                            ),
                            (
                                honeypot.OperationType.EVIDENCE_CLEANUP,
                                f"evidence-cleanup:{first.case.case_id}",
                            ),
                        ),
                    )
                )
                second = self._append_case(
                    honeypot, cog, resolution_time, message_id=41
                )
                second_apply = cog._case_store.ensure_operation(
                    second.case.case_id,
                    honeypot.OperationType.ROLE_APPLY,
                    f"role-apply:{second.case.case_id}:{role.id}",
                )
                second_apply = cog._case_store.claim_operation(
                    second_apply.operation_id, resolution_time
                )
                self.assertTrue(
                    cog._case_store.start_operation_effect(
                        second_apply.operation_id,
                        second_apply.claim_token,
                        resolution_time,
                    )
                )
                first_snapshot = cog._case_store.get_case(first.case.case_id)
                release = next(
                    item
                    for item in first_snapshot.operations
                    if item.operation_type == honeypot.OperationType.ROLE_RELEASE
                )
                claimed = cog._case_store.claim_operation(
                    release.operation_id, resolution_time
                )
                real_start = cog._case_store.start_role_release_effect

                def hand_off_before_start(
                    operation_id, claim_token, case_id, role_id, started_at
                ):
                    if not cog._case_store.release_role_ownership(case_id, role_id):
                        raise AssertionError("original owner was not released")
                    ownership_result = (
                        cog._case_store.record_operation_role_ownership(
                            second_apply.operation_id,
                            second_apply.claim_token,
                            second.case.case_id,
                            guild.id,
                            member.id,
                            role_id=role_id,
                            now=started_at,
                        )
                    )
                    if ownership_result != "owned":
                        raise AssertionError(
                            f"unexpected handoff result: {ownership_result}"
                        )
                    cog._case_store.complete_operation(
                        second_apply.operation_id,
                        second_apply.claim_token,
                        started_at,
                    )
                    return real_start(
                        operation_id,
                        claim_token,
                        case_id,
                        role_id,
                        started_at,
                    )

                cog._case_store.start_role_release_effect = hand_off_before_start

                await cog._execute_detection_case_operation(claimed, resolution_time)

                refreshed = cog._case_store.get_case(first.case.case_id)
                persisted = next(
                    item
                    for item in refreshed.operations
                    if item.operation_id == release.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(persisted.result, "ownership_transferred")
                self.assertEqual(
                    cog._case_store.owned_role_ids(first.case.case_id), ()
                )
                self.assertEqual(
                    cog._case_store.owned_role_ids(second.case.case_id),
                    (role.id,),
                )
                self.assertEqual(member.roles, [role])
                self.assertEqual(member.removals, [])

    async def test_cancellation_propagates_without_releasing_ownership(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(role)

                async def cancel_removal(cancelled_role, *, reason):
                    raise asyncio.CancelledError

                member.remove_roles = cancel_removal
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                self._record_ownership(
                    honeypot, cog, appended.case.case_id, role.id, now
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                with self.assertRaises(asyncio.CancelledError):
                    await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.RUNNING)
                self.assertEqual(member.roles, [role])
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id),
                    (role.id,),
                )

    async def test_terminal_compaction_runs_after_completion_fence_loss(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(role)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                self._record_ownership(
                    honeypot, cog, appended.case.case_id, role.id, now
                )
                terminal_at = now + timedelta(seconds=1)
                resolution = cog._case_store.claim_resolution(
                    appended.case.case_id, terminal_at
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        resolution,
                        honeypot.CaseStatus.EXPIRED,
                        "expired",
                        None,
                        terminal_at,
                        final_operations=((
                            honeypot.OperationType.ROLE_RELEASE,
                            f"role-release:{appended.case.case_id}:{role.id}",
                        ),),
                    )
                )
                terminal = cog._case_store.get_case(appended.case.case_id)
                release = next(
                    item
                    for item in terminal.operations
                    if item.operation_type == honeypot.OperationType.ROLE_RELEASE
                )
                claimed = cog._case_store.claim_operation(
                    release.operation_id, terminal_at
                )

                async def remove_during_case_deletion(removed_role, *, reason):
                    member.roles.remove(removed_role)
                    member.removals.append((removed_role.id, reason))
                    await cog.red_delete_data_for_user(
                        requester="discord_deleted_user",
                        user_id=appended.case.user_id,
                    )

                member.remove_roles = remove_during_case_deletion
                compaction_attempts = []
                real_compact = cog._case_store.compact_terminal_case

                def record_compaction(case_id):
                    compaction_attempts.append(case_id)
                    return real_compact(case_id)

                cog._case_store.compact_terminal_case = record_compaction

                await cog._execute_detection_case_operation(claimed, terminal_at)

                self.assertEqual(
                    compaction_attempts, [appended.case.case_id]
                )
                self.assertIsNone(
                    cog._case_store.get_case(appended.case.case_id)
                )
                self.assertIsNone(
                    cog._case_store.get_case_deletion_job(
                        appended.case.case_id
                    )
                )
                self.assertEqual(member.roles, [])
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), ()
                )
