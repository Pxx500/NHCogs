"""Handler-seam tests for the message-process operation.

The active-message path is covered end to end through the cog executor in
`tests/test_detection_pipeline.py`; that is not repeated here. What no other
test does is call this handler directly and pin the contract of its own module
boundary: registry routing and the terminal-case short circuit.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules, _operational_support


class MessageProcessHandlerSeamTests(unittest.IsolatedAsyncioTestCase):

    @staticmethod
    def _append_case_with_attachment(
        honeypot, cog, now, *, with_operation, signals=()
    ):
        cog._case_store.initialize()
        planned = (
            (("message_process", "message-process:{case_id}:{sequence}"),)
            if with_operation
            else ()
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
            signals,
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

    def _claim_attachment(self, cog, case_id, sequence, claimed_at):
        reservation = cog._case_store.reserve_attachment_capture(
            case_id,
            sequence,
            0,
            5,
            claimed_at,
            stale_before=claimed_at - timedelta(microseconds=1),
            max_attachment_bytes=5,
            max_case_bytes=5,
        )
        if reservation.status != "claimed" or reservation.claim_token is None:
            raise AssertionError("attachment claim setup failed")
        return reservation

    def _gone_message_guild(self):
        discord = sys.modules["discord"]
        fetch_message = mock.AsyncMock(side_effect=discord.NotFound())
        channel = SimpleNamespace(fetch_message=fetch_message)
        guild = SimpleNamespace(
            id=10,
            get_channel=lambda channel_id: channel,
            get_thread=lambda channel_id: None,
        )
        return guild, fetch_message

    async def _run_gone_message(self, honeypot, cog, case_id, now):
        guild, fetch_message = self._gone_message_guild()
        cog.bot.get_guild = lambda guild_id: guild if guild_id == 10 else None
        cog._scan_case_message_images = mock.AsyncMock()
        cog._publish_detection_case = mock.AsyncMock()
        operation = next(
            item
            for item in cog._case_store.get_case(case_id).operations
            if item.operation_type is honeypot.OperationType.MESSAGE_PROCESS
        )
        claimed = cog._case_store.claim_operation(operation.operation_id, now)
        await cog._execute_detection_case_operation(claimed, now)
        return fetch_message

    async def test_gone_message_with_stale_claim_fails_capture_without_retry(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case_with_attachment(
                    honeypot,
                    cog,
                    now,
                    with_operation=True,
                    signals=(
                        honeypot.DetectionSignal(
                            "forward_purge",
                            "active",
                            honeypot.ActionIntent.NONE,
                            True,
                            {"containment_required": True},
                        ),
                    ),
                )
                case_id = appended.case.case_id
                sequence = appended.message.sequence
                stale_at = now - timedelta(minutes=5, seconds=1)
                reservation = self._claim_attachment(
                    cog, case_id, sequence, stale_at
                )

                fetch_message = await self._run_gone_message(
                    honeypot, cog, case_id, now
                )

                snapshot = cog._case_store.get_case(case_id)
                attachment = snapshot.attachments[0]
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type is honeypot.OperationType.MESSAGE_PROCESS
                )
                fetch_message.assert_awaited_once()
                self.assertEqual(attachment.capture_status, "capture_failed")
                self.assertEqual(
                    attachment.error,
                    "source message is gone before attachment capture completed",
                )
                self.assertEqual(
                    snapshot.messages[0].delete_status,
                    honeypot.DeleteStatus.ALREADY_GONE,
                )
                self.assertEqual(operation.status.value, "succeeded")
                self.assertEqual(operation.result, "processed")
                self.assertIsNone(operation.retry_at)
                self.assertIsNone(
                    cog._case_store.complete_attachment_capture(
                        case_id,
                        sequence,
                        0,
                        reservation.claim_token,
                        5,
                        evidence_path="gone.png",
                        now=now,
                        max_attachment_bytes=5,
                        max_case_bytes=5,
                    )
                )

    async def test_gone_message_with_fresh_claim_keeps_capture_retryable(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case_with_attachment(
                    honeypot,
                    cog,
                    now,
                    with_operation=True,
                    signals=(
                        honeypot.DetectionSignal(
                            "forward_purge",
                            "active",
                            honeypot.ActionIntent.NONE,
                            True,
                            {"containment_required": True},
                        ),
                    ),
                )
                case_id = appended.case.case_id
                sequence = appended.message.sequence
                reservation = self._claim_attachment(cog, case_id, sequence, now)

                fetch_message = await self._run_gone_message(
                    honeypot, cog, case_id, now
                )

                snapshot = cog._case_store.get_case(case_id)
                attachment = snapshot.attachments[0]
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type is honeypot.OperationType.MESSAGE_PROCESS
                )
                fetch_message.assert_awaited_once()
                self.assertEqual(attachment.capture_status, "pending")
                self.assertIsNone(attachment.error)
                self.assertEqual(operation.status.value, "failed")
                self.assertIsNotNone(operation.retry_at)
                self.assertIn("not terminal", operation.last_error)
                self.assertEqual(
                    cog._case_store.complete_attachment_capture(
                        case_id,
                        sequence,
                        0,
                        reservation.claim_token,
                        5,
                        evidence_path="still-held.png",
                        now=now,
                        max_attachment_bytes=5,
                        max_case_bytes=5,
                    ),
                    "captured",
                )


if __name__ == "__main__":
    unittest.main()
