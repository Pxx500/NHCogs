import unittest
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tests.harness import _Bot, _isolated_honeypot_modules, _operational_support


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
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
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

    async def test_errors_channel_is_not_a_review_publication_fallback(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                handler_module = import_module("NHCogs.honeypot.operations.review_publish")
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended, _operation, _claimed, context = (
                    self._claim_review_publish(honeypot, cog, now)
                )
                self._configure(
                    cog,
                    review_channel=None,
                    extra={"errors_channel": 202},
                )
                publications = []

                async def publish_case(*args, **kwargs):
                    publications.append((args, kwargs))

                cog._publish_detection_case = publish_case

                await handler_module.review_publish_handler(cog, context)

                self.assertEqual(
                    publications,
                    [
                        (
                            (appended.case.case_id, None),
                            {"message_sequence": appended.message.sequence},
                        )
                    ],
                )

    async def test_publication_failure_reaches_shared_retry_settlement(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended, operation, claimed, _context = (
                    self._claim_review_publish(honeypot, cog, now)
                )
                self._configure(cog)
                publication_error = RuntimeError("review publication unavailable")

                async def fail_publication(*args, **kwargs):
                    raise publication_error

                cog._publish_detection_case = fail_publication

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                failed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                failures = cog._case_store.list_operational_failures(
                    appended.case.guild_id
                )
                self.assertIs(failed.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(failed.result)
                self.assertEqual(
                    failed.retry_at - failed.updated_at,
                    timedelta(seconds=10),
                )
                self.assertIn("review publication unavailable", failed.last_error)
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0].operation_id, operation.operation_id)

    async def test_first_attempt_resolves_failure_without_recovery_alert_or_follow_up(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                operations = import_module("NHCogs.honeypot.operations")
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended, operation, claimed, _context = (
                    self._claim_review_publish(honeypot, cog, now)
                )
                self._configure(cog)
                cog._case_store.record_operational_failure(
                    guild_id=appended.case.guild_id,
                    source=honeypot.OperationType.REVIEW_PUBLISH,
                    summary="previous publication failure",
                    occurred_at=now,
                    case_id=appended.case.case_id,
                    operation_id=operation.operation_id,
                )
                publications = []
                recovery_alerts = []

                async def send_recovery_alert(guild_id, message):
                    recovery_alerts.append((guild_id, message))

                cog._publish_detection_case = self._record_publications(publications)
                cog._send_operational_alert = send_recovery_alert

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                unresolved = cog._case_store.list_operational_failures(
                    appended.case.guild_id
                )
                failures = cog._case_store.list_operational_failures(
                    appended.case.guild_id,
                    include_resolved=True,
                )
                policy = operations.executor_operation_policy(
                    honeypot.OperationType.REVIEW_PUBLISH
                )
                self.assertEqual(len(publications), 1)
                self.assertIs(completed.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertIsNone(completed.result)
                self.assertEqual(unresolved, ())
                self.assertEqual(len(failures), 1)
                self.assertIsNotNone(failures[0].resolved_at)
                self.assertEqual(recovery_alerts, [])
                self.assertEqual(policy.follow_ups, ())
