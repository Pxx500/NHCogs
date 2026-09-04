"""Handler-seam tests for the message-process operation.

The active-message path is covered end to end through the cog executor in
`tests/test_detection_pipeline.py`; that is not repeated here. What no other
test does is call this handler directly and pin the contract of its own module
boundary: registry routing and the terminal-case short circuit.
"""

import unittest
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.harness import _Bot, _isolated_honeypot_modules, _operational_support


class MessageProcessHandlerSeamTests(unittest.IsolatedAsyncioTestCase):

    @staticmethod
    def _append_case_with_attachment(honeypot, cog, now, *, with_operation):
        cog._case_store.initialize()
        planned = (
            (lambda signals: (("message_process", "message-process:{case_id}:{sequence}"),))
            if with_operation
            else (lambda signals: ())
        )
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=10,
                user_id=20,
                channel_id=30,
                message_id=40,
                content="evidence",
                created_at=now,
                jump_url="https://discord.test/messages/40",
                attachments=(
                    honeypot.NewAttachment(
                        0,
                        "proof.png",
                        5,
                        "image/png",
                        10,
                        10,
                        "https://cdn.test/proof.png",
                    ),
                ),
            ),
            (),
            planned,
        )

    @staticmethod
    def _resolve_case(honeypot, cog, case_id, now):
        lease = cog._case_store.claim_resolution(case_id, now)
        if not cog._case_store.finish_resolution(
            lease, honeypot.CaseStatus.RESOLVED, "ignore", 99, now
        ):
            raise AssertionError("terminal case setup failed")

    @staticmethod
    def _context(honeypot, cog, claimed, case_id, now):
        return honeypot.OperationContext(
            operation=claimed,
            snapshot=cog._case_store.get_case(case_id),
            lease=honeypot.OperationLease(
                operation_id=claimed.operation_id,
                claim_token=claimed.claim_token,
            ),
            now=now,
        )

    async def test_registry_routes_message_process_to_concrete_handler(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                try:
                    handler_module = import_module("NHCogs.honeypot.operations.message_process")
                except ModuleNotFoundError:
                    self.fail("message_process has no dedicated handler module")
                cog = honeypot.Honeypot(_Bot(), _operational_support())

                registered = cog._detection_operation_handlers.resolve(
                    honeypot.OperationType.MESSAGE_PROCESS
                )

                self.assertIs(registered, handler_module.message_process_handler)

    async def test_terminal_case_short_circuits_and_fails_pending_captures(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                handler_module = import_module("NHCogs.honeypot.operations.message_process")
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case_with_attachment(
                    honeypot, cog, now, with_operation=True
                )
                case_id = appended.case.case_id
                operation = next(
                    item
                    for item in cog._case_store.get_case(case_id).operations
                    if item.operation_type is honeypot.OperationType.MESSAGE_PROCESS
                )
                self.assertEqual(operation.message_sequence, 1)
                self._resolve_case(honeypot, cog, case_id, now)
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                pending = cog._case_store.get_case(case_id).attachments[0]
                self.assertEqual(pending.capture_status, "pending")
                self.assertIsNone(pending.error)

                outcome = await handler_module.message_process_handler(
                    cog, self._context(honeypot, cog, claimed, case_id, now)
                )

                self.assertEqual(outcome.result, "case_terminal")
                self.assertIsNone(outcome.error)
                self.assertFalse(outcome.role_was_added)
                attachment = cog._case_store.get_case(case_id).attachments[0]
                self.assertEqual(attachment.capture_status, "capture_failed")
                self.assertEqual(
                    attachment.error,
                    "case closed before attachment capture completed",
                )

    async def test_terminal_case_without_sequence_leaves_captures_untouched(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                handler_module = import_module("NHCogs.honeypot.operations.message_process")
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case_with_attachment(
                    honeypot, cog, now, with_operation=False
                )
                case_id = appended.case.case_id
                before = cog._case_store.get_case(case_id).attachments[0]
                operation = cog._case_store.ensure_operation(
                    case_id,
                    honeypot.OperationType.MESSAGE_PROCESS,
                    f"message-process:{case_id}",
                )
                self.assertIsNone(operation.message_sequence)
                self._resolve_case(honeypot, cog, case_id, now)
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                outcome = await handler_module.message_process_handler(
                    cog, self._context(honeypot, cog, claimed, case_id, now)
                )

                self.assertEqual(outcome.result, "case_terminal")
                after = cog._case_store.get_case(case_id).attachments[0]
                self.assertEqual(after.capture_status, before.capture_status)
                self.assertEqual(after.error, before.error)


if __name__ == "__main__":
    unittest.main()
