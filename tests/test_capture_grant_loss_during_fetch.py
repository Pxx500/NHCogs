"""A grant lost during REST fetching must not destroy uncaptured evidence."""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import tests.test_capability_deferred_case_expiry as expiry_fixtures
from tests.harness import DetectionPipelineTestCase, _isolated_honeypot_modules


class CaptureGrantLossDuringFetchTests(DetectionPipelineTestCase):
    async def _fixture_with_fetch_loss(self, module, *, redact):
        cog, message, case_id, now = await expiry_fixtures.CapabilityDeferredExpiryTests()._fixture(module)
        cog.bot.intents.message_content = True
        full_attachments = list(message.attachments)

        async def fetch(_message_id):
            cog.bot.intents.message_content = False
            if redact:
                message.attachments = []
            return message

        channel = SimpleNamespace(fetch_message=mock.AsyncMock(side_effect=fetch))
        cog._fetch_message_channel = mock.AsyncMock(return_value=channel)
        return cog, message, case_id, now, channel, full_attachments

    async def test_lost_grant_and_redacted_fetch_defers_without_deleting_then_recovers(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog, message, case_id, now, channel, attachments = await self._fixture_with_fetch_loss(
                module, redact=True,
            )
            before = cog._case_store.get_case(case_id)
            pending = before.operations[0]
            claimed = cog._case_store.claim_operation(pending.operation_id, now)

            await cog._execute_detection_case_operation(claimed, now)

            message.delete.assert_not_awaited()
            attachments[0].read.assert_not_awaited()
            waiting = cog._case_store.get_case(case_id)
            self.assertEqual(waiting.messages, before.messages)
            self.assertEqual(waiting.attachments, before.attachments)
            queued = next(operation for operation in waiting.operations
                          if operation.operation_id == pending.operation_id)
            self.assertEqual(queued.last_error, "Waiting for Gateway message_content data")
            self.assertEqual(queued.attempts, 0)
            self.assertIsNotNone(queued.retry_at)
            self.assertEqual(cog._case_store.list_due_cases(now), ())
            cog.bot.intents.message_content = True
            message.attachments = attachments
            channel.fetch_message = mock.AsyncMock(return_value=message)
            recovered_at = queued.retry_at + timedelta(seconds=1)
            resumed = cog._case_store.claim_operation(pending.operation_id, recovered_at)
            await cog._execute_detection_case_operation(resumed, recovered_at)
            recovered = cog._case_store.get_case(case_id)
            self.assertEqual(recovered.attachments[0].capture_status, "captured")
            self.assertEqual(recovered.operations[0].status.value, "succeeded")
            self.assertEqual(recovered.messages[0].created_at, message.created_at)
            attachments[0].read.assert_awaited_once()
            message.delete.assert_awaited_once()

    async def test_lost_grant_with_full_fetched_attachments_still_captures_and_deletes(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog, message, case_id, now, _channel, attachments = await self._fixture_with_fetch_loss(
                module, redact=False,
            )
            pending = cog._case_store.get_case(case_id).operations[0]
            claimed = cog._case_store.claim_operation(pending.operation_id, now)

            await cog._execute_detection_case_operation(claimed, now)

            current = cog._case_store.get_case(case_id)
            self.assertEqual(current.attachments[0].capture_status, "captured")
            self.assertEqual(current.operations[0].status.value, "succeeded")
            attachments[0].read.assert_awaited_once()
            message.delete.assert_awaited_once()

    async def test_already_captured_bytes_allow_redacted_fetch_after_grant_loss(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog, message, case_id, now, _channel, attachments = await self._fixture_with_fetch_loss(
                module, redact=True,
            )
            await cog._capture_case_attachments(message, case_id, 1)
            self.assertEqual(cog._case_store.get_case(case_id).attachments[0].capture_status, "captured")
            pending = cog._case_store.get_case(case_id).operations[0]
            claimed = cog._case_store.claim_operation(pending.operation_id, now)

            await cog._execute_detection_case_operation(claimed, now)

            current = cog._case_store.get_case(case_id)
            self.assertEqual(current.operations[0].status.value, "succeeded")
            attachments[0].read.assert_awaited_once()
            message.delete.assert_awaited_once()
