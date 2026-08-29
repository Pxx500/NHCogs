import unittest
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules


class ReviewPublishHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _append_case(honeypot, cog, created_at):
        cog._case_store.initialize()
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=10,
                user_id=20,
                channel_id=30,
                message_id=40,
                content="evidence",
                created_at=created_at,
                jump_url="https://discord.test/messages/40",
                attachments=(),
            ),
            (),
        )

    @classmethod
    def _claim_review_publish(cls, honeypot, cog, now):
        appended = cls._append_case(honeypot, cog, now)
        operation = cog._case_store.ensure_operation(
            appended.case.case_id,
            honeypot.OperationType.REVIEW_PUBLISH,
            f"review-publish:{appended.case.case_id}",
            message_sequence=appended.message.sequence,
        )
        claimed = cog._case_store.claim_operation(operation.operation_id, now)
        snapshot = cog._case_store.get_case(appended.case.case_id)
        context = honeypot.OperationContext(
            operation=claimed,
            snapshot=snapshot,
            lease=honeypot.OperationLease(
                operation_id=claimed.operation_id,
                claim_token=claimed.claim_token,
            ),
            now=now,
        )
        return appended, operation, claimed, context

    @staticmethod
    def _configure(cog, *, review_channel=101, extra=None):
        async def load_config():
            values = {"review_channel": review_channel}
            values.update(extra or {})
            return values

        def guild_config(guild_id):
            if guild_id != 10:
                raise AssertionError(f"unexpected guild config lookup: {guild_id}")
            return SimpleNamespace(all=load_config)

        cog.config.guild_from_id = guild_config

    @staticmethod
    def _record_publications(publications):
        async def publish_case(
            case_id,
            review_channel,
            *,
            message_sequence=None,
        ):
            publications.append(
                (
                    case_id,
                    review_channel,
                    message_sequence,
                )
            )

        return publish_case

    async def test_registered_handler_publishes_and_completes_with_none(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                try:
                    handler_module = import_module(
                        "NHCogs.honeypot.operations.review_publish"
                    )
                except ModuleNotFoundError:
                    self.fail("review_publish has no dedicated handler module")
                operations = import_module("NHCogs.honeypot.operations")
                cached_purge = import_module("NHCogs.honeypot.operations.cached_purge")
                evidence_cleanup = import_module(
                    "NHCogs.honeypot.operations.evidence_cleanup"
                )
                message_process = import_module(
                    "NHCogs.honeypot.operations.message_process"
                )
                moderation = import_module("NHCogs.honeypot.operations.moderation")
                moderator_decision = import_module(
                    "NHCogs.honeypot.operations.moderator_decision"
                )
                review_update = import_module("NHCogs.honeypot.operations.review_update")
                role_apply = import_module("NHCogs.honeypot.operations.role_apply")
                role_release = import_module("NHCogs.honeypot.operations.role_release")
                source_delete = import_module("NHCogs.honeypot.operations.source_delete")
                now = datetime.now(timezone.utc)
                guild = object()
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                appended = self._append_case(honeypot, cog, now)
                message_sequence = appended.message.sequence
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.REVIEW_PUBLISH,
                    f"review-publish:{appended.case.case_id}",
                    message_sequence=message_sequence,
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                publications = []

                self._configure(cog)
                cog._publish_detection_case = self._record_publications(publications)

                self.assertIs(
                    cog._detection_operation_handlers.resolve(
                        honeypot.OperationType.REVIEW_PUBLISH
                    ),
                    handler_module.review_publish_handler,
                )
                self.assertEqual(
                    dict(operations.HANDLERS),
                    {
                        honeypot.OperationType.MESSAGE_PROCESS: (
                            message_process.message_process_handler
                        ),
                        honeypot.OperationType.REVIEW_UPDATE: (
                            review_update.review_update_handler
                        ),
                        honeypot.OperationType.REVIEW_PUBLISH: (
                            handler_module.review_publish_handler
                        ),
                        honeypot.OperationType.CACHED_PURGE: (
                            cached_purge.cached_purge_handler
                        ),
                        honeypot.OperationType.SOURCE_DELETE: (
                            source_delete.source_delete_handler
                        ),
                        honeypot.OperationType.EVIDENCE_CLEANUP: (
                            evidence_cleanup.evidence_cleanup_handler
                        ),
                        honeypot.OperationType.ROLE_RELEASE: (
                            role_release.role_release_handler
                        ),
                        honeypot.OperationType.ROLE_APPLY: (
                            role_apply.role_apply_handler
                        ),
                        honeypot.OperationType.MODERATION_ACTION: (
                            moderation.moderation_action_handler
                        ),
                        honeypot.OperationType.MODERATOR_BAN: (
                            moderator_decision.moderator_decision_handler
                        ),
                        honeypot.OperationType.MODERATOR_KICK: (
                            moderator_decision.moderator_decision_handler
                        ),
                    },
                )
                for operation_type in honeypot.OperationType:
                    if operation_type in {
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
                    }:
                        continue
                    self.assertIsNone(
                        cog._detection_operation_handlers.resolve(operation_type)
                    )

                await cog._execute_detection_case_operation(
                    claimed,
                    now,
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(
                    publications,
                    [
                        (
                            appended.case.case_id,
                            101,
                            message_sequence,
                        )
                    ],
                )
                self.assertIs(completed.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertIsNone(completed.result)

    async def test_configured_review_channel_is_used(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                handler_module = import_module("NHCogs.honeypot.operations.review_publish")
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended, _operation, _claimed, context = (
                    self._claim_review_publish(honeypot, cog, now)
                )
                self._configure(cog)
                publications = []

                cog._publish_detection_case = self._record_publications(publications)

                outcome = await handler_module.review_publish_handler(cog, context)

                self.assertEqual(
                    publications,
                    [
                        (
                            appended.case.case_id,
                            101,
                            appended.message.sequence,
                        )
                    ],
                )
                self.assertIsNone(outcome.result)
                self.assertEqual(outcome.follow_ups, ())

    async def test_guild_unavailable_still_uses_configured_review_channel_id(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                handler_module = import_module("NHCogs.honeypot.operations.review_publish")
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended, _operation, _claimed, context = (
                    self._claim_review_publish(honeypot, cog, now)
                )
                self._configure(cog)
                publications = []

                cog._publish_detection_case = self._record_publications(publications)

                await handler_module.review_publish_handler(cog, context)

                self.assertEqual(
                    publications,
                    [
                        (
                            appended.case.case_id,
                            101,
                            appended.message.sequence,
                        )
                    ],
                )

    async def test_publication_exception_identity_reaches_shared_retry_settlement(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                handler_module = import_module("NHCogs.honeypot.operations.review_publish")
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended, operation, claimed, context = (
                    self._claim_review_publish(honeypot, cog, now)
                )
                self._configure(cog)
                cog._record_operational_failure = mock.AsyncMock()
                publication_error = RuntimeError("review publication unavailable")

                async def fail_publication(*args, **kwargs):
                    raise publication_error

                cog._publish_detection_case = fail_publication

                with self.assertRaises(RuntimeError) as captured:
                    await handler_module.review_publish_handler(cog, context)

                self.assertIs(captured.exception, publication_error)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                failed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(failed.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(failed.result)
                self.assertEqual(
                    failed.retry_at - failed.updated_at,
                    timedelta(seconds=10),
                )
                self.assertIn("review publication unavailable", failed.last_error)
                cog._record_operational_failure.assert_awaited_once()
                self.assertEqual(
                    cog._record_operational_failure.await_args.kwargs["operation_id"],
                    operation.operation_id,
                )

    async def test_first_attempt_marks_matching_shared_failure_recovered_without_follow_up(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                operations = import_module("NHCogs.honeypot.operations")
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended, operation, claimed, _context = (
                    self._claim_review_publish(honeypot, cog, now)
                )
                self._configure(cog)
                publications = []

                cog._publish_detection_case = self._record_publications(publications)
                honeypot.detection.mark_operational_error_recovered = mock.AsyncMock()

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                policy = operations.executor_operation_policy(
                    honeypot.OperationType.REVIEW_PUBLISH
                )
                self.assertEqual(len(publications), 1)
                self.assertIs(completed.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertIsNone(completed.result)
                honeypot.detection.mark_operational_error_recovered.assert_awaited_once_with(
                    cog.bot,
                    guild_id=appended.case.guild_id,
                    source="Honeypot",
                    action="review_publish",
                    correlation_key=operation.operation_id,
                )
                self.assertEqual(policy.follow_ups, ())
