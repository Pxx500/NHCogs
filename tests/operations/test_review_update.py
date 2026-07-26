import unittest
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_detection_pipeline import _Bot, _isolated_honeypot_modules


class ReviewUpdateHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _append_case(honeypot, cog, created_at, *, user_id=20, message_id=40):
        cog._case_store.initialize()
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=10,
                user_id=user_id,
                channel_id=30,
                message_id=message_id,
                content="evidence",
                created_at=created_at,
                jump_url=f"https://discord.test/messages/{message_id}",
                attachments=(),
            ),
            (),
        )

    @classmethod
    def _claim_terminal_review_update(
        cls, honeypot, cog, now, *, user_id, message_id
    ):
        appended = cls._append_case(
            honeypot, cog, now, user_id=user_id, message_id=message_id
        )
        resolution = cog._case_store.claim_resolution(appended.case.case_id, now)
        idempotency_key = f"review-update:{appended.case.case_id}"
        if not cog._case_store.finish_resolution(
            resolution,
            honeypot.CaseStatus.EXPIRED,
            "expired",
            None,
            now,
            final_operations=(("review_update", idempotency_key),),
        ):
            raise AssertionError("terminal case setup failed")
        snapshot = cog._case_store.get_case(appended.case.case_id)
        operation = next(iter(snapshot.operations))
        claimed = cog._case_store.claim_operation(operation.operation_id, now)
        return appended, claimed

    async def test_handler_propagates_review_publication_failure(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                try:
                    handler_module = import_module("Honeypot.operations.review_update")
                except ModuleNotFoundError:
                    self.fail("review_update has no dedicated handler module")
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.REVIEW_UPDATE,
                    f"review-update:{appended.case.case_id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                snapshot = cog._case_store.get_case(appended.case.case_id)
                publication_error = RuntimeError("review publication unavailable")
                attempted_case_ids = []

                async def fail_publication(case_id):
                    attempted_case_ids.append(case_id)
                    raise publication_error

                cog._case_review_rerender = fail_publication
                context = honeypot.OperationContext(
                    operation=claimed,
                    snapshot=snapshot,
                    lease=honeypot.OperationLease(
                        operation_id=claimed.operation_id,
                        claim_token=claimed.claim_token,
                    ),
                    now=now,
                )

                with self.assertRaises(RuntimeError) as captured:
                    await handler_module.review_update_handler(cog, context)

                self.assertIs(captured.exception, publication_error)
                self.assertEqual(attempted_case_ids, [appended.case.case_id])

    async def test_registry_routes_review_update_to_concrete_handler(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                handler_module = import_module("Honeypot.operations.review_update")
                cog = honeypot.Honeypot(_Bot())

                registered = cog._detection_operation_handlers.resolve(
                    honeypot.OperationType.REVIEW_UPDATE
                )
                self.assertIs(registered, handler_module.review_update_handler)
                self.assertIsNone(
                    cog._detection_operation_handlers.resolve("moderator_ignore")
                )
                self.assertIsNone(
                    cog._detection_operation_handlers.resolve("unknown_operation")
                )

    async def test_registered_handler_rerenders_and_completes_with_none(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.REVIEW_UPDATE,
                    f"review-update:{appended.case.case_id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                published_case_ids = []

                async def publish_review(case_id):
                    published_case_ids.append(case_id)

                cog._case_review_rerender = publish_review

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(published_case_ids, [appended.case.case_id])
                self.assertIs(completed.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertIsNone(completed.result)

    async def test_terminal_review_update_compacts_case_after_completion(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                terminal, terminal_claim = self._claim_terminal_review_update(
                    honeypot, cog, now, user_id=21, message_id=41
                )

                published_case_ids = []

                async def publish_review(case_id):
                    published_case_ids.append(case_id)

                cog._case_review_rerender = publish_review

                await cog._execute_detection_case_operation(terminal_claim, now)

                compacted = cog._case_store.get_case(terminal.case.case_id)
                self.assertEqual(published_case_ids, [terminal.case.case_id])
                self.assertIs(compacted.case.status, honeypot.CaseStatus.EXPIRED)
                self.assertEqual(compacted.messages, ())
                self.assertEqual(compacted.operations, ())

    async def test_terminal_compaction_runs_when_completion_is_fenced_out(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                deleted, deleted_claim = self._claim_terminal_review_update(
                    honeypot, cog, now, user_id=22, message_id=42
                )
                compact_attempts = []
                compact_terminal_case = cog._case_store.compact_terminal_case

                def observe_compaction(case_id):
                    compact_attempts.append(case_id)
                    return compact_terminal_case(case_id)

                cog._case_store.compact_terminal_case = observe_compaction
                deletion_results = []

                async def delete_during_publication(case_id):
                    cog._case_store.plan_user_case_deletion(22)
                    deletion_results.append(
                        cog._case_store.finalize_case_deletion(10, case_id)
                    )

                cog._case_review_rerender = delete_during_publication

                await cog._execute_detection_case_operation(deleted_claim, now)

                self.assertEqual(deletion_results, [True])
                self.assertIsNone(cog._case_store.get_case(deleted.case.case_id))
                self.assertIn(deleted.case.case_id, compact_attempts)
