"""Durable moderation retains work when privileged Gateway data disappears."""

import unittest
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import tests.operations.test_moderation as moderation_fixtures
import tests.operations.test_role_apply as role_fixtures
from tests.harness import _Bot, _isolated_honeypot_modules, _operational_support


class HoneypotIntentSafetyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _bot_with_missing_intents():
        bot = _Bot()
        bot.intents = SimpleNamespace(members=False, presences=False, message_content=False)
        return bot

    async def test_missing_content_defers_capture_without_deleting_source_and_recovers(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            now = datetime.now(timezone.utc)
            bot = self._bot_with_missing_intents()
            cog = honeypot.Honeypot(bot, _operational_support())
            appended = moderation_fixtures.ModerationActionHandlerTests._append_case(
                honeypot, cog, now, honeypot.ActionIntent.BAN, pending_attachment=True,
            )
            operation = cog._case_store.ensure_operation(
                appended.case.case_id, honeypot.OperationType.MESSAGE_PROCESS,
                f"message-process:{appended.case.case_id}:{appended.message.sequence}",
                appended.message.sequence,
            )
            claimed = cog._case_store.claim_operation(operation.operation_id, now)
            source = SimpleNamespace(delete=mock.AsyncMock())

            async def process_message(_cog, _context):
                await source.delete()
                return honeypot.OperationOutcome(result="processed")

            handler = mock.AsyncMock(side_effect=process_message)
            cog._detection_operation_handlers.register(honeypot.OperationType.MESSAGE_PROCESS, handler)
            cog._finish_case_review_if_ready = mock.AsyncMock()
            before = cog._case_store.get_case(appended.case.case_id)

            await cog._execute_detection_case_operation(claimed, now)

            handler.assert_not_awaited()
            source.delete.assert_not_awaited()
            waiting = cog._case_store.get_case(appended.case.case_id)
            self.assertEqual(waiting.case.status, honeypot.CaseStatus.PENDING)
            self.assertEqual(waiting.messages, before.messages)
            self.assertEqual(waiting.attachments, before.attachments)
            queued = next(item for item in waiting.operations if item.operation_id == operation.operation_id)
            self.assertNotIn(queued.status, (honeypot.OperationStatus.SUCCEEDED,
                                            honeypot.OperationStatus.ABANDONED))
            self.assertIsNotNone(queued.retry_at)
            retry_at = queued.retry_at + timedelta(seconds=1)
            bot.intents.message_content = True
            resumed = cog._case_store.claim_operation(operation.operation_id, retry_at)
            self.assertIsNotNone(resumed)
            await cog._execute_detection_case_operation(resumed, retry_at)
            handler.assert_awaited_once()
            source.delete.assert_awaited_once()
            completed = next(item for item in cog._case_store.get_case(appended.case.case_id).operations
                             if item.operation_id == operation.operation_id)
            self.assertEqual(completed.status, honeypot.OperationStatus.SUCCEEDED)

    async def test_known_ban_remains_runnable_without_privileged_intents(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            now = datetime.now(timezone.utc)
            bot = self._bot_with_missing_intents()
            guild = SimpleNamespace(id=10, get_member=lambda _user: None, ban=mock.AsyncMock())
            bot.get_guild = lambda _guild: guild
            cog = honeypot.Honeypot(bot, _operational_support())
            cog.config = moderation_fixtures._Config({"dry_run": False})
            cog._get_user_or_object = mock.AsyncMock(return_value=SimpleNamespace(id=20))
            cog._finish_case_review_if_ready = mock.AsyncMock()
            effects = import_module(f"{honeypot.__package__}.effects")

            async def execute_ban(_guild, target, *_args, **_kwargs):
                await guild.ban(target)
                return honeypot.ModerationEffectResult("Banned", None, effects.EffectStatus.SUCCEEDED)

            cog._execute_action = mock.AsyncMock(side_effect=execute_ban)
            appended = moderation_fixtures.ModerationActionHandlerTests._append_case(
                honeypot, cog, now, honeypot.ActionIntent.BAN,
            )
            operation, claimed = moderation_fixtures.ModerationActionHandlerTests._claim_moderation(
                honeypot, cog, appended, now,
            )

            await cog._execute_detection_case_operation(claimed, now)

            guild.ban.assert_awaited_once()
            current = moderation_fixtures.ModerationActionHandlerTests._persisted_operation(
                cog, appended.case.case_id, operation.operation_id,
            )
            self.assertEqual(current.status, honeypot.OperationStatus.SUCCEEDED)
            self.assertEqual(current.result, "ban")

    async def test_role_apply_uses_rest_member_when_cache_and_members_intent_are_missing(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            now = datetime.now(timezone.utc)
            bot = self._bot_with_missing_intents()
            role = SimpleNamespace(id=55)
            member = role_fixtures._Member(20)
            guild = SimpleNamespace(id=10, get_member=lambda _user: None,
                                    get_role=lambda _role: role,
                                    fetch_member=mock.AsyncMock(return_value=member))
            bot.get_guild = lambda _guild: guild
            cog = honeypot.Honeypot(bot, _operational_support())
            cog.config = role_fixtures._Config()
            cog._punitive_effect_allowed = mock.AsyncMock(return_value=True)
            appended = role_fixtures.RoleApplyHandlerTests._append_case(honeypot, cog, now)
            operation, claimed = role_fixtures.RoleApplyHandlerTests._claim_role_apply(
                honeypot, cog, appended.case.case_id, now,
            )

            await cog._execute_detection_case_operation(claimed, now)

            guild.fetch_member.assert_awaited_once_with(20)
            self.assertEqual(member.additions, [(55, "Detection case pending moderator review.")])
            self.assertEqual(cog._case_store.owned_role_ids(appended.case.case_id), (55,))
            current = role_fixtures.RoleApplyHandlerTests._persisted_operation(
                cog, appended.case.case_id, operation.operation_id,
            )
            self.assertEqual(current.status, honeypot.OperationStatus.SUCCEEDED)
