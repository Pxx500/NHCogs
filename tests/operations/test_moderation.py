import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.test_detection_pipeline import _Bot, _isolated_honeypot_modules


class _GuildConfig:
    def __init__(self, values):
        self._values = values

    async def all(self):
        return dict(self._values)


class _Config:
    def __init__(self, values):
        self._values = values

    def guild_from_id(self, guild_id):
        return _GuildConfig(self._values)


class ModerationActionHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _append_case(
        honeypot, cog, created_at, action, *, pending_attachment=False
    ):
        cog._case_store.initialize()
        attachments = (
            (
                honeypot.NewAttachment(
                    0,
                    "proof.png",
                    5,
                    "image/png",
                    None,
                    None,
                    "https://cdn.test/proof.png",
                    spoiler=False,
                ),
            )
            if pending_attachment
            else ()
        )
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=10,
                user_id=20,
                channel_id=30,
                message_id=40,
                content="evidence",
                created_at=created_at,
                jump_url="https://discord.test/messages/40",
                attachments=attachments,
            ),
            (
                honeypot.DetectionSignal(
                    "spam", "duplicate", action, True, {}
                ),
            ),
        )

    @staticmethod
    def _claim_moderation(
        honeypot, cog, appended, now, *, message_sequence=None
    ):
        if message_sequence is None:
            message_sequence = appended.message.sequence
        operation = cog._case_store.ensure_operation(
            appended.case.case_id,
            honeypot.OperationType.MODERATION_ACTION,
            f"moderation:{appended.case.case_id}:{appended.message.sequence}",
            message_sequence,
        )
        claimed = cog._case_store.claim_operation(operation.operation_id, now)
        if claimed is None:
            raise AssertionError("moderation operation was not claimable")
        return operation, claimed

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

    @staticmethod
    def _handler(honeypot):
        operations = import_module("Honeypot.operations")
        return operations.OperationHandlerRegistry().resolve(
            honeypot.OperationType.MODERATION_ACTION
        )

    @staticmethod
    def _persisted_operation(cog, case_id, operation_id):
        snapshot = cog._case_store.get_case(case_id)
        return next(
            operation
            for operation in snapshot.operations
            if operation.operation_id == operation_id
        )

    async def test_dry_run_ban_returns_exact_planned_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog.config = _Config({"dry_run": True})
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.BAN
                )
                _, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )
                handler = self._handler(honeypot)

                self.assertIsNotNone(handler)
                outcome = await handler(
                    cog,
                    self._handler_context(honeypot, cog, claimed, now),
                )

                self.assertEqual(outcome.result, "planned_ban")

    async def test_dry_run_kick_returns_exact_planned_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog.config = _Config({"dry_run": True})
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.KICK
                )
                _, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )
                handler = self._handler(honeypot)

                outcome = await handler(
                    cog,
                    self._handler_context(honeypot, cog, claimed, now),
                )

                self.assertEqual(outcome.result, "planned_kick")

    async def test_missing_source_raises_exact_error(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog.config = _Config({"dry_run": True})
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.BAN
                )
                _, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )
                handler = self._handler(honeypot)
                context = self._handler_context(honeypot, cog, claimed, now)
                context = replace(
                    context,
                    snapshot=replace(context.snapshot, messages=()),
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "^detection case source message is unavailable$",
                ):
                    await handler(
                        cog,
                        context,
                    )

    async def test_non_moderating_action_raises_exact_error(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog.config = _Config({"dry_run": True})
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.REVIEW
                )
                _, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )
                handler = self._handler(honeypot)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "^detection case moderation action is no longer applicable$",
                ):
                    await handler(
                        cog,
                        self._handler_context(honeypot, cog, claimed, now),
                    )

    async def test_missing_guild_raises_exact_error(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog.config = _Config({"dry_run": False})
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.BAN
                )
                _, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )
                handler = self._handler(honeypot)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "^detection case guild is unavailable$",
                ):
                    await handler(
                        cog,
                        self._handler_context(honeypot, cog, claimed, now),
                    )

    async def test_departed_kick_target_returns_exact_missing_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                guild = SimpleNamespace(
                    get_member=lambda user_id: None,
                    fetch_member=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("member left")
                    ),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config({"dry_run": False})
                appended = self._append_case(
                    honeypot,
                    cog,
                    now,
                    honeypot.ActionIntent.KICK,
                    pending_attachment=True,
                )
                operation, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog,
                    appended.case.case_id,
                    operation.operation_id,
                )

                self.assertEqual(
                    (persisted.status, persisted.result),
                    (honeypot.OperationStatus.SUCCEEDED, "kick_missing"),
                )

    async def test_reclaimed_ban_confirms_effect_without_repeating_it(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                member = SimpleNamespace(
                    id=20,
                    ban=mock.AsyncMock(
                        side_effect=AssertionError("ban must not repeat")
                    ),
                )
                guild = SimpleNamespace(
                    get_member=lambda user_id: member,
                    fetch_ban=mock.AsyncMock(
                        return_value=SimpleNamespace(user=member)
                    ),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config({"dry_run": False})
                appended = self._append_case(
                    honeypot,
                    cog,
                    now,
                    honeypot.ActionIntent.BAN,
                    pending_attachment=True,
                )
                operation, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )
                self.assertTrue(
                    cog._case_store.start_operation_effect(
                        claimed.operation_id,
                        claimed.claim_token,
                        now,
                    )
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog,
                    appended.case.case_id,
                    operation.operation_id,
                )

                self.assertEqual(
                    (persisted.status, persisted.result),
                    (honeypot.OperationStatus.SUCCEEDED, "ban"),
                )
                member.ban.assert_not_awaited()

    async def test_unresolvable_ban_target_raises_exact_member_error(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                guild = SimpleNamespace(get_member=lambda user_id: None)
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config({"dry_run": False})
                cog._get_user_or_object = mock.AsyncMock(return_value=None)
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.BAN
                )
                _, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )
                handler = self._handler(honeypot)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "^detection case member is unavailable$",
                ):
                    await handler(
                        cog,
                        self._handler_context(honeypot, cog, claimed, now),
                    )

    async def test_live_kick_uses_public_reason_and_persists_exact_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                member = SimpleNamespace(id=20, kick=mock.AsyncMock())
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(
                        guild_permissions=SimpleNamespace(
                            kick_members=True,
                            ban_members=True,
                        )
                    ),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config({"dry_run": False})
                appended = self._append_case(
                    honeypot,
                    cog,
                    now,
                    honeypot.ActionIntent.KICK,
                    pending_attachment=True,
                )
                operation, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )

                with mock.patch.object(
                    honeypot.modlog,
                    "create_case",
                    new=mock.AsyncMock(),
                    create=True,
                ):
                    await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog,
                    appended.case.case_id,
                    operation.operation_id,
                )
                self.assertEqual(
                    (persisted.status, persisted.result),
                    (honeypot.OperationStatus.SUCCEEDED, "kick"),
                )
                member.kick.assert_awaited_once_with(
                    reason="Same message in multiple channels"
                )

    async def test_lost_effect_fence_raises_exact_error(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                member = SimpleNamespace(id=20, kick=mock.AsyncMock())
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(
                        guild_permissions=SimpleNamespace(
                            kick_members=True,
                            ban_members=True,
                        )
                    ),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config({"dry_run": False})
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.KICK
                )
                _, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )
                context = self._handler_context(
                    honeypot, cog, claimed, now
                )
                context = replace(
                    context,
                    operation=replace(claimed, claim_token="lost-fence"),
                )
                handler = self._handler(honeypot)

                with (
                    mock.patch.object(
                        honeypot.modlog,
                        "create_case",
                        new=mock.AsyncMock(),
                        create=True,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "^moderation action operation lease was lost$",
                    ),
                ):
                    await handler(cog, context)

    async def test_action_failure_is_persisted_without_success_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                member = SimpleNamespace(
                    id=20,
                    kick=mock.AsyncMock(
                        side_effect=honeypot.discord.HTTPException(
                            "temporary kick failure"
                        )
                    ),
                )
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(
                        guild_permissions=SimpleNamespace(
                            kick_members=True,
                            ban_members=True,
                        )
                    ),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config({"dry_run": False})
                appended = self._append_case(
                    honeypot,
                    cog,
                    now,
                    honeypot.ActionIntent.KICK,
                    pending_attachment=True,
                )
                operation, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog,
                    appended.case.case_id,
                    operation.operation_id,
                )
                self.assertEqual(
                    (persisted.status, persisted.result),
                    (honeypot.OperationStatus.FAILED, None),
                )
                self.assertIn("temporary kick failure", persisted.last_error)

    async def test_action_cancellation_propagates_without_settlement(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                member = SimpleNamespace(
                    id=20,
                    kick=mock.AsyncMock(side_effect=asyncio.CancelledError),
                )
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(
                        guild_permissions=SimpleNamespace(
                            kick_members=True,
                            ban_members=True,
                        )
                    ),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config({"dry_run": False})
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.KICK
                )
                operation, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )

                with self.assertRaises(asyncio.CancelledError):
                    await cog._execute_detection_case_operation(claimed, now)

                persisted = self._persisted_operation(
                    cog,
                    appended.case.case_id,
                    operation.operation_id,
                )
                self.assertEqual(
                    (persisted.status, persisted.result),
                    (honeypot.OperationStatus.RUNNING, None),
                )

    async def test_completed_dry_run_moderation_finishes_case_review(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog.config = _Config({"dry_run": True})
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.BAN
                )
                operation, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(
                    (
                        persisted.status,
                        persisted.result,
                        snapshot.case.status,
                    ),
                    (
                        honeypot.OperationStatus.SUCCEEDED,
                        "planned_ban",
                        honeypot.CaseStatus.RESOLVED,
                    ),
                )

    async def test_lost_completion_fence_skips_moderation_follow_up(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                member = SimpleNamespace(id=20)
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(
                        guild_permissions=SimpleNamespace(
                            kick_members=True,
                            ban_members=True,
                        )
                    ),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config({"dry_run": False})
                appended = self._append_case(
                    honeypot, cog, now, honeypot.ActionIntent.KICK
                )
                operation, claimed = self._claim_moderation(
                    honeypot, cog, appended, now
                )

                async def win_completion_fence(*, reason):
                    self.assertTrue(
                        cog._case_store.complete_operation(
                            claimed.operation_id,
                            claimed.claim_token,
                            now,
                            "kick",
                        )
                    )

                member.kick = win_completion_fence

                with (
                    mock.patch.object(
                        honeypot.modlog,
                        "create_case",
                        new=mock.AsyncMock(),
                        create=True,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "^detection case operation lease was lost before completion$",
                    ),
                ):
                    await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(
                    (
                        persisted.status,
                        persisted.result,
                        snapshot.case.status,
                    ),
                    (
                        honeypot.OperationStatus.SUCCEEDED,
                        "kick",
                        honeypot.CaseStatus.PENDING,
                    ),
                )
