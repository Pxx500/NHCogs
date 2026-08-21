import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules


class _StatsContext:
    def __init__(self, stats):
        self._stats = stats

    async def __aenter__(self):
        return self._stats

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _GuildConfig:
    def __init__(self, stats, settings):
        self._stats = stats
        self._settings = settings

    def stats(self):
        return _StatsContext(self._stats)

    async def all(self):
        return dict(self._settings)

    async def joinwatch_pending_roles(self):
        return {}


class _Config:
    def __init__(self):
        self.stats = {}
        self.settings = {}

    def guild(self, guild):
        return _GuildConfig(self.stats, self.settings)

    def guild_from_id(self, guild_id):
        return _GuildConfig(self.stats, self.settings)


class _Member:
    def __init__(self, user_id):
        self.id = user_id
        self.roles = []
        self.additions = []

    async def add_roles(self, role, *, reason):
        self.roles.append(role)
        self.additions.append((role.id, reason))


class RoleApplyHandlerTests(unittest.IsolatedAsyncioTestCase):
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
    def _claim_role_apply(honeypot, cog, case_id, now, *, role_id=55):
        operation = cog._case_store.ensure_operation(
            case_id,
            honeypot.OperationType.ROLE_APPLY,
            f"role-apply:{case_id}:{role_id}",
        )
        claimed = cog._case_store.claim_operation(operation.operation_id, now)
        if claimed is None:
            raise AssertionError("role apply operation was not claimable")
        return operation, claimed

    @staticmethod
    def _finish_case(
        honeypot, cog, case_id, now, *, final_operations=()
    ):
        lease = cog._case_store.claim_resolution(case_id, now)
        if lease is None:
            raise AssertionError("case resolution was not claimable")
        if not cog._case_store.finish_resolution(
            lease,
            honeypot.CaseStatus.EXPIRED,
            "expired",
            None,
            now,
            final_operations=final_operations,
        ):
            raise AssertionError("case resolution did not finish")

    @staticmethod
    def _persisted_operation(cog, case_id, operation_id):
        snapshot = cog._case_store.get_case(case_id)
        return next(
            operation
            for operation in snapshot.operations
            if operation.operation_id == operation_id
        )

    @staticmethod
    def _record_role_ownership(honeypot, cog, case_id, now, *, role_id=55):
        operation = cog._case_store.ensure_operation(
            case_id,
            honeypot.OperationType.ROLE_APPLY,
            f"role-apply:{case_id}:{role_id}",
        )
        claimed = cog._case_store.claim_operation(operation.operation_id, now)
        if not cog._case_store.start_role_apply_effect(
            claimed.operation_id, claimed.claim_token, now
        ):
            raise AssertionError("role ownership effect did not start")
        result = cog._case_store.record_operation_role_ownership(
            claimed.operation_id,
            claimed.claim_token,
            case_id,
            10,
            20,
            role_id=role_id,
            now=now,
        )
        if result != "owned":
            raise AssertionError(f"unexpected ownership result: {result}")
        if not cog._case_store.complete_operation(
            claimed.operation_id, claimed.claim_token, now
        ):
            raise AssertionError("role ownership operation did not complete")

    @staticmethod
    def _handler_context(honeypot, cog, claimed, now):
        return honeypot.OperationContext(
            operation=claimed,
            snapshot=cog._case_store.get_case(claimed.case_id),
            lease=honeypot.OperationLease(
                operation_id=claimed.operation_id,
                claim_token=claimed.claim_token,
            ),
            now=now,
        )

    async def test_registry_includes_automatic_moderation_handler(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                operations = import_module("NHCogs.honeypot.operations")
                cog = honeypot.Honeypot(_Bot())
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
                self.assertIsNotNone(
                    cog._detection_operation_handlers.resolve(
                        honeypot.OperationType.ROLE_APPLY
                    )
                )
                for operation_type in honeypot.OperationType:
                    if operation_type not in expected_types:
                        self.assertIsNone(
                            cog._detection_operation_handlers.resolve(operation_type)
                        )

    async def test_superseding_moderation_wins_before_terminal_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                moderation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.MODERATOR_BAN,
                    f"moderator-ban:{appended.case.case_id}",
                    actor_id=99,
                )
                claimed_moderation = cog._case_store.claim_operation(
                    moderation.operation_id, now
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        claimed_moderation.operation_id,
                        claimed_moderation.claim_token,
                        now,
                        "ban",
                    )
                )
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )
                self._finish_case(
                    honeypot,
                    cog,
                    appended.case.case_id,
                    now + timedelta(seconds=1),
                )
                cog.bot.get_guild = lambda _guild_id: self.fail(
                    "superseded role apply must not access Discord"
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(persisted.result, "superseded_by_moderation")

    async def test_terminal_case_completes_without_accessing_discord(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )
                self._finish_case(
                    honeypot,
                    cog,
                    appended.case.case_id,
                    now + timedelta(seconds=1),
                )
                cog.bot.get_guild = lambda _guild_id: self.fail(
                    "terminal role apply must not access Discord"
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(persisted.result, "case_terminal")

    async def test_new_role_is_owned_and_counted_on_first_attempt(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertIsNone(persisted.result)
                self.assertEqual(member.roles, [role])
                self.assertEqual(
                    member.additions,
                    [(55, "Detection case pending moderator review.")],
                )
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), (55,)
                )
                self.assertEqual(config.stats, {"pending_mutes": 1})

    async def test_current_dry_run_plans_role_apply_created_while_live(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role_apply = import_module("NHCogs.honeypot.operations.role_apply")
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(
                    id=20,
                    roles=[],
                    add_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)
                _, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )
                config.settings["dry_run"] = True

                outcome = await role_apply.role_apply_handler(
                    cog,
                    self._handler_context(honeypot, cog, claimed, now),
                )

                self.assertEqual(outcome.result, "planned_role_apply")
                self.assertFalse(outcome.role_was_added)
                member.add_roles.assert_not_awaited()

    async def test_conflicting_durable_ownership_marks_added_role_ambiguous(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                prior = self._append_case(honeypot, cog, now)
                self._record_role_ownership(
                    honeypot, cog, prior.case.case_id, now
                )
                self._finish_case(
                    honeypot,
                    cog,
                    prior.case.case_id,
                    now + timedelta(seconds=1),
                )
                current = self._append_case(
                    honeypot,
                    cog,
                    now + timedelta(seconds=2),
                    message_id=41,
                )
                operation, claimed = self._claim_role_apply(
                    honeypot,
                    cog,
                    current.case.case_id,
                    now + timedelta(seconds=2),
                )

                await cog._execute_detection_case_operation(
                    claimed, now + timedelta(seconds=2)
                )

                snapshot = cog._case_store.get_case(current.case.case_id)
                persisted = self._persisted_operation(
                    cog, current.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(persisted.result, "ambiguous_role_ownership")
                self.assertTrue(snapshot.case.needs_attention)
                self.assertEqual(member.roles, [role])
                self.assertEqual(
                    cog._case_store.owned_role_ids(current.case.case_id), ()
                )
                self.assertEqual(config.stats, {"pending_mutes": 1})

    async def test_attention_failure_preserves_ambiguous_added_outcome(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                prior = self._append_case(honeypot, cog, now)
                self._record_role_ownership(
                    honeypot, cog, prior.case.case_id, now
                )
                self._finish_case(
                    honeypot,
                    cog,
                    prior.case.case_id,
                    now + timedelta(seconds=1),
                )
                current = self._append_case(
                    honeypot,
                    cog,
                    now + timedelta(seconds=2),
                    message_id=41,
                )
                _, claimed = self._claim_role_apply(
                    honeypot,
                    cog,
                    current.case.case_id,
                    now + timedelta(seconds=2),
                )
                failure = RuntimeError("attention write failed")
                real_mark = cog._case_store.mark_case_needs_attention

                def fail_mark(*args, **kwargs):
                    raise failure

                cog._case_store.mark_case_needs_attention = fail_mark
                try:
                    handler = cog._detection_operation_handlers.resolve(
                        honeypot.OperationType.ROLE_APPLY
                    )
                    outcome = await handler(
                        cog,
                        self._handler_context(
                            honeypot,
                            cog,
                            claimed,
                            now + timedelta(seconds=2),
                        ),
                    )
                finally:
                    cog._case_store.mark_case_needs_attention = real_mark

                self.assertIs(outcome.error, failure)
                self.assertEqual(outcome.result, "ambiguous_role_ownership")
                self.assertTrue(outcome.role_was_added)
                self.assertEqual(member.roles, [role])

    async def test_post_add_store_error_preserves_added_role_in_outcome(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config()
                appended = self._append_case(honeypot, cog, now)
                _, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )
                failure = RuntimeError("ownership write failed")
                real_record = cog._case_store.record_operation_role_ownership

                def fail_record(*args, **kwargs):
                    raise failure

                cog._case_store.record_operation_role_ownership = fail_record
                try:
                    handler = cog._detection_operation_handlers.resolve(
                        honeypot.OperationType.ROLE_APPLY
                    )
                    outcome = await handler(
                        cog, self._handler_context(honeypot, cog, claimed, now)
                    )
                finally:
                    cog._case_store.record_operation_role_ownership = real_record

                self.assertIs(outcome.error, failure)
                self.assertTrue(outcome.role_was_added)
                self.assertIsNone(outcome.result)
                self.assertEqual(member.roles, [role])

    async def test_missing_member_fetch_retries_with_exact_error_and_stat(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: None,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertEqual(
                    persisted.last_error,
                    "RuntimeError: detection case member lookup is unavailable",
                )
                self.assertIsNotNone(persisted.retry_at)
                self.assertEqual(config.stats, {"pending_mute_failures": 1})

    async def test_http_member_fetch_preserves_lookup_failure_as_cause(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                lookup_error = honeypot.discord.HTTPException("lookup failed")

                async def fetch_member(_user_id):
                    raise lookup_error

                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: None,
                    get_role=lambda _role_id: role,
                    fetch_member=fetch_member,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                appended = self._append_case(honeypot, cog, now)
                _, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )
                handler = cog._detection_operation_handlers.resolve(
                    honeypot.OperationType.ROLE_APPLY
                )

                with self.assertRaisesRegex(
                    RuntimeError, "detection case member lookup failed"
                ) as captured:
                    await handler(
                        cog, self._handler_context(honeypot, cog, claimed, now)
                    )

                self.assertIs(captured.exception.__cause__, lookup_error)

    async def test_active_member_without_role_retries_with_exact_error(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                member = _Member(20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: None,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertEqual(
                    persisted.last_error,
                    "RuntimeError: detection case role is unavailable",
                )
                self.assertEqual(config.stats, {"pending_mute_failures": 1})

    async def test_preexisting_unowned_role_completes_without_claiming_it(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                member.roles.append(role)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(persisted.result, "preexisting_role")
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), ()
                )
                self.assertEqual(config.stats, {})

    async def test_concurrent_handoff_to_current_case_reports_already_owned(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                member.roles.append(role)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                prior = self._append_case(honeypot, cog, now)
                self._record_role_ownership(
                    honeypot, cog, prior.case.case_id, now
                )
                self._finish_case(
                    honeypot,
                    cog,
                    prior.case.case_id,
                    now + timedelta(seconds=1),
                )
                current = self._append_case(
                    honeypot,
                    cog,
                    now + timedelta(seconds=2),
                    message_id=41,
                )
                operation, claimed = self._claim_role_apply(
                    honeypot,
                    cog,
                    current.case.case_id,
                    now + timedelta(seconds=2),
                )
                self.assertTrue(
                    cog._case_store.transfer_terminal_role_ownership(
                        claimed.operation_id,
                        claimed.claim_token,
                        current.case.case_id,
                        10,
                        20,
                        role_id=role.id,
                        now=now + timedelta(seconds=2),
                    )
                )

                await cog._execute_detection_case_operation(
                    claimed, now + timedelta(seconds=2)
                )

                persisted = self._persisted_operation(
                    cog, current.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(persisted.result, "role_already_owned")
                self.assertEqual(
                    cog._case_store.owned_role_ids(current.case.case_id), (55,)
                )
                self.assertEqual(config.stats, {})

    async def test_terminal_owner_is_transferred_to_current_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                member.roles.append(role)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                prior = self._append_case(honeypot, cog, now)
                self._record_role_ownership(
                    honeypot, cog, prior.case.case_id, now
                )
                self._finish_case(
                    honeypot,
                    cog,
                    prior.case.case_id,
                    now + timedelta(seconds=1),
                )
                current = self._append_case(
                    honeypot,
                    cog,
                    now + timedelta(seconds=2),
                    message_id=41,
                )
                operation, claimed = self._claim_role_apply(
                    honeypot,
                    cog,
                    current.case.case_id,
                    now + timedelta(seconds=2),
                )

                await cog._execute_detection_case_operation(
                    claimed, now + timedelta(seconds=2)
                )

                persisted = self._persisted_operation(
                    cog, current.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(persisted.result, "transferred_role_ownership")
                self.assertEqual(
                    cog._case_store.owned_role_ids(current.case.case_id), (55,)
                )
                self.assertEqual(
                    cog._case_store.owned_role_ids(prior.case.case_id), ()
                )
                self.assertEqual(config.stats, {})

    async def test_started_previous_release_blocks_role_handoff(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                member.roles.append(role)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                prior = self._append_case(honeypot, cog, now)
                self._record_role_ownership(
                    honeypot, cog, prior.case.case_id, now
                )
                release_key = f"role-release:{prior.case.case_id}:{role.id}"
                self._finish_case(
                    honeypot,
                    cog,
                    prior.case.case_id,
                    now + timedelta(seconds=1),
                    final_operations=((honeypot.OperationType.ROLE_RELEASE, release_key),),
                )
                prior_snapshot = cog._case_store.get_case(prior.case.case_id)
                release = next(
                    item
                    for item in prior_snapshot.operations
                    if item.operation_type is honeypot.OperationType.ROLE_RELEASE
                )
                claimed_release = cog._case_store.claim_operation(
                    release.operation_id, now + timedelta(seconds=1)
                )
                self.assertTrue(
                    cog._case_store.start_role_release_effect(
                        claimed_release.operation_id,
                        claimed_release.claim_token,
                        prior.case.case_id,
                        role.id,
                        now + timedelta(seconds=1),
                    )
                )
                current = self._append_case(
                    honeypot,
                    cog,
                    now + timedelta(seconds=2),
                    message_id=41,
                )
                operation, claimed = self._claim_role_apply(
                    honeypot,
                    cog,
                    current.case.case_id,
                    now + timedelta(seconds=2),
                )

                await cog._execute_detection_case_operation(
                    claimed, now + timedelta(seconds=2)
                )

                persisted = self._persisted_operation(
                    cog, current.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertEqual(
                    persisted.last_error,
                    "RuntimeError: previous detection case role release is still in progress",
                )
                self.assertEqual(
                    cog._case_store.owned_role_ids(prior.case.case_id), (55,)
                )
                self.assertEqual(config.stats, {"pending_mute_failures": 1})

    async def test_retry_add_resolves_failure_before_pending_mute_stat(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)
                operation, first = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )
                retry_at = now + timedelta(seconds=1)
                self.assertTrue(
                    cog._case_store.fail_operation(
                        first.operation_id,
                        first.claim_token,
                        "RuntimeError: temporary failure",
                        now,
                        retry_at,
                    )
                )
                retry = cog._case_store.claim_operation(
                    operation.operation_id, retry_at
                )

                await cog._execute_detection_case_operation(retry, retry_at)

                persisted = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(persisted.attempts, 2)
                self.assertIsNone(persisted.result)
                self.assertEqual(member.roles, [role])
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), (55,)
                )
                self.assertEqual(config.stats, {})

    async def test_add_failure_is_persisted_without_claiming_role(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                failure = RuntimeError("Discord role add failed")

                class FailingMember(_Member):
                    async def add_roles(self, role, *, reason):
                        raise failure

                member = FailingMember(20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.FAILED)
                self.assertEqual(
                    persisted.last_error, "RuntimeError: Discord role add failed"
                )
                self.assertEqual(member.roles, [])
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), ()
                )
                self.assertEqual(config.stats, {"pending_mute_failures": 1})

    async def test_add_cancellation_propagates_without_settlement(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)

                class CancelledMember(_Member):
                    async def add_roles(self, role, *, reason):
                        raise asyncio.CancelledError

                member = CancelledMember(20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda _guild_id: guild
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )

                with self.assertRaises(asyncio.CancelledError):
                    await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                self.assertIs(persisted.status, honeypot.OperationStatus.RUNNING)
                self.assertEqual(member.roles, [])
                self.assertEqual(config.stats, {})

    async def test_nested_release_failure_does_not_fail_parent_apply(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                bot = _Bot()
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)

                async def add_role(added, *, reason):
                    member.roles.append(added)
                    lease = cog._case_store.claim_resolution(
                        appended.case.case_id, now + timedelta(seconds=1)
                    )
                    self.assertTrue(
                        cog._case_store.finish_resolution(
                            lease,
                            honeypot.CaseStatus.EXPIRED,
                            "expired",
                            None,
                            now + timedelta(seconds=1),
                        )
                    )

                async def fail_remove(removed, *, reason):
                    raise RuntimeError("nested release failed")

                member.add_roles = add_role
                member.remove_roles = fail_remove
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                member.guild = guild
                bot.get_guild = lambda _guild_id: guild
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                parent = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                release = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type is honeypot.OperationType.ROLE_RELEASE
                )
                self.assertIs(parent.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertIsNone(parent.result)
                self.assertIs(release.status, honeypot.OperationStatus.FAILED)
                self.assertEqual(
                    release.last_error, "RuntimeError: nested release failed"
                )
                self.assertEqual(member.roles, [role])
                self.assertEqual(config.stats, {"pending_mutes": 1})

    async def test_nested_release_cancellation_propagates_from_parent_apply(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                member = _Member(20)
                bot = _Bot()
                cog = honeypot.Honeypot(bot)
                config = _Config()
                cog.config = config
                appended = self._append_case(honeypot, cog, now)

                async def add_role(added, *, reason):
                    member.roles.append(added)
                    lease = cog._case_store.claim_resolution(
                        appended.case.case_id, now + timedelta(seconds=1)
                    )
                    self.assertTrue(
                        cog._case_store.finish_resolution(
                            lease,
                            honeypot.CaseStatus.EXPIRED,
                            "expired",
                            None,
                            now + timedelta(seconds=1),
                        )
                    )

                async def cancel_remove(removed, *, reason):
                    raise asyncio.CancelledError

                member.add_roles = add_role
                member.remove_roles = cancel_remove
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                member.guild = guild
                bot.get_guild = lambda _guild_id: guild
                operation, claimed = self._claim_role_apply(
                    honeypot, cog, appended.case.case_id, now
                )

                with self.assertRaises(asyncio.CancelledError):
                    await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                parent = self._persisted_operation(
                    cog, appended.case.case_id, operation.operation_id
                )
                release = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type is honeypot.OperationType.ROLE_RELEASE
                )
                self.assertIs(parent.status, honeypot.OperationStatus.RUNNING)
                self.assertIs(release.status, honeypot.OperationStatus.RUNNING)
                self.assertEqual(member.roles, [role])
                self.assertEqual(config.stats, {})
