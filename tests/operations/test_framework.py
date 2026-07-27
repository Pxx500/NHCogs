import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.harness import _Bot, _isolated_honeypot_modules


class OperationFrameworkTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_registered_operation_handler_completes_through_shared_settlement(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.REVIEW_UPDATE,
                    f"framework:{appended.case.case_id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                registry = cog._detection_operation_handlers
                observed_contexts = []

                async def handler(handler_cog, context):
                    self.assertIs(handler_cog, cog)
                    observed_contexts.append(context)
                    return honeypot.OperationOutcome(result="framework_result")

                registry.register(honeypot.OperationType.REVIEW_UPDATE, handler)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(completed.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(completed.result, "framework_result")
                self.assertEqual(len(observed_contexts), 1)
                context = observed_contexts[0]
                self.assertEqual(context.operation, claimed)
                self.assertEqual(context.snapshot.case.case_id, appended.case.case_id)
                self.assertEqual(context.lease.operation_id, claimed.operation_id)
                self.assertEqual(context.lease.claim_token, claimed.claim_token)
