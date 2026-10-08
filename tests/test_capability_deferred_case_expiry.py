import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import (
    DetectionPipelineTestCase,
    _Bot,
    _isolated_honeypot_modules,
    _operational_support,
)


class CapabilityDeferredExpiryTests(DetectionPipelineTestCase):
    async def _fixture(self, module):
        now = datetime.now(timezone.utc)
        cog = module.Honeypot(_Bot(), _operational_support())
        await asyncio.to_thread(cog._case_store.initialize)
        message = self._message(module, attachment_count=1)
        message.created_at = now - timedelta(hours=25)
        self._configure_public_boundary(cog, {
            "enabled": True, "dry_run": False, "review_channel": None,
            "spam_enabled": False, "imagescan_detector_enabled": False,
        })
        cog.bot.get_guild = lambda _guild_id: message.guild
        cog._fetch_message_channel = mock.AsyncMock(return_value=SimpleNamespace(
            fetch_message=mock.AsyncMock(return_value=message),
        ))
        cog._scan_all_case_message_images = mock.AsyncMock()
        cog._publish_detection_case = mock.AsyncMock()
        cog._case_review_rerender = mock.AsyncMock()
        attachment = message.attachments[0]
        appended = cog._case_store.append_message(
            module.NewMessage(
                message.guild.id, message.author.id, message.channel.id, message.id,
                message.content, message.created_at, message.jump_url,
                (module.NewAttachment(
                    0, attachment.filename, attachment.size, attachment.content_type,
                    attachment.width, attachment.height, attachment.url,
                ),),
            ),
            (module.DetectionSignal("spam", "review needed", module.ActionIntent.REVIEW, True, {}),),
            ((module.OperationType.MESSAGE_PROCESS, "message-process:{case_id}:{sequence}"),),
        )
        cog.bot.intents.message_content = False
        return cog, message, appended.case.case_id, now

    async def _expire(self, module, cog, now):
        with mock.patch.object(module.detection, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            await module.detection._run_detection_case_expiry(cog)

    async def test_deferred_capture_survives_expiry_and_reconciliation_then_gets_full_review_window(self):
        for path in ("expiry", "reconciliation", "expiry_before_first_operation"):
            with self.subTest(path=path), TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)) as module:
                    cog, message, case_id, now = await self._fixture(module)
                    if path != "expiry_before_first_operation":
                        pending = cog._case_store.get_case(case_id).operations[0]
                        claimed = cog._case_store.claim_operation(pending.operation_id, now)
                        await cog._execute_detection_case_operation(claimed, now)
                    # Reopen the durable store before testing automatic expiry.
                    cog._case_store = module.DetectionCaseStore(cog._detection_case_db_path)
                    await asyncio.to_thread(cog._case_store.initialize)
                    if path == "reconciliation":
                        await module.detection._run_detection_reconciliation(cog, now=now)
                    else:
                        await self._expire(module, cog, now)
                    retained = cog._case_store.get_case(case_id)
                    self.assertEqual(retained.case.status.value, "pending")
                    self.assertEqual(retained.attachments[0].capture_status, "pending")
                    self.assertTrue(any(
                        operation.last_error == "Waiting for Gateway message_content data"
                        for operation in retained.operations
                    ))
                    message.attachments[0].read.assert_not_awaited()
                    message.delete.assert_not_awaited()
                    recovered_at = now + timedelta(minutes=2)
                    cog.bot.intents.message_content = True
                    await module.detection._run_detection_reconciliation(cog, now=recovered_at)
                    recovered = cog._case_store.get_case(case_id)
                    self.assertEqual(recovered.case.status.value, "pending")
                    self.assertEqual(recovered.attachments[0].capture_status, "captured")
                    self.assertGreaterEqual(recovered.case.expires_at,
                                            recovered_at + timedelta(hours=24) - timedelta(seconds=1))
                    self.assertEqual(recovered.messages[0].created_at, message.created_at)
                    message.attachments[0].read.assert_awaited_once()
                    await self._expire(module, cog, recovered.case.expires_at + timedelta(seconds=1))
                    self.assertEqual(cog._case_store.get_case(case_id).case.status.value, "expired")

    async def test_manual_close_and_explicit_deletion_remain_available_during_outage(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog, _message, case_id, now = await self._fixture(module)
            pending = cog._case_store.get_case(case_id).operations[0]
            claimed = cog._case_store.claim_operation(pending.operation_id, now)
            await cog._execute_detection_case_operation(claimed, now)
            self.assertTrue(await cog.resolve_detection_case(case_id, "fp", moderator_id=999, now=now))
            self.assertEqual(cog._case_store.get_case(case_id).case.status.value, "resolved")
            self.assertEqual(cog._case_store.plan_user_case_deletion(200), ((100, case_id),))

    async def test_wait_after_claim_blocks_expiry_and_refreshes_review_using_durable_marker(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog, _message, case_id, now = await self._fixture(module)
            pending = cog._case_store.get_case(case_id).operations[0]
            claimed = cog._case_store.claim_operation(pending.operation_id, now)
            self.assertIsNone(claimed.last_error)
            self.assertTrue(cog._case_store.pause_case_for_unavailable_capture(case_id, now))
            self.assertIsNone(cog._case_store.claim_resolution(case_id, now, automatic_expiry=True))
            self.assertFalse(cog._case_store.resume_capability_deferred_case(
                claimed.operation_id, "stale-token", now,
            ))
            restored_at = now + timedelta(minutes=2)
            cog.bot.intents.message_content = True
            # The operation object predates the wait. The database is authoritative.
            await cog._execute_detection_case_operation(claimed, restored_at)
            recovered = cog._case_store.get_case(case_id)
            self.assertEqual(recovered.attachments[0].capture_status, "captured")
            self.assertGreaterEqual(recovered.case.expires_at,
                                    restored_at + timedelta(hours=24) - timedelta(seconds=1))

    async def test_stale_expiry_selection_cannot_close_a_recovered_review_window(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog, _message, case_id, now = await self._fixture(module)
            selected = cog._case_store.list_due_cases(now)
            self.assertEqual([case.case_id for case in selected], [case_id])
            pending = cog._case_store.get_case(case_id).operations[0]
            claimed = cog._case_store.claim_operation(pending.operation_id, now)
            await cog._execute_detection_case_operation(claimed, now)
            restored_at = now + timedelta(minutes=2)
            cog.bot.intents.message_content = True
            await module.detection._run_detection_reconciliation(cog, now=restored_at)
            self.assertFalse(await cog.resolve_detection_case(
                selected[0].case_id, "expired", now=restored_at,
            ))
            self.assertEqual(cog._case_store.get_case(case_id).case.status.value, "pending")
            self.assertIsNone(cog._case_store.claim_resolution(
                case_id, restored_at, restored_at - timedelta(minutes=5), automatic_expiry=True,
            ))

    async def test_outage_with_pending_capture_cannot_get_expiry_lease_or_terminal_cleanup(self):
        for run_operation in (False, True):
            with self.subTest(run_operation=run_operation), TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)) as module:
                    cog, message, case_id, now = await self._fixture(module)
                    pending = cog._case_store.get_case(case_id).operations[0]
                    claimed = cog._case_store.claim_operation(pending.operation_id, now)
                    cog.bot.intents.message_content = True
                    entered, release = threading.Event(), threading.Event()
                    real_claim = cog._case_store.claim_resolution

                    def claim(*args, real_claim=real_claim, entered=entered, release=release, **kwargs):
                        lease = real_claim(*args, **kwargs)
                        entered.set()
                        if not release.wait(timeout=10):
                            raise TimeoutError("Expiry test claim was not released")
                        return lease

                    cog._case_store.claim_resolution = claim
                    task = asyncio.create_task(cog.resolve_detection_case(case_id, "expired", now=now))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                        cog.bot.intents.message_content = False
                        if run_operation:
                            await cog._execute_detection_case_operation(claimed, now)
                    finally:
                        release.set()
                    self.assertFalse(await task)
                    retained = cog._case_store.get_case(case_id)
                    self.assertEqual(retained.case.status.value, "pending")
                    self.assertEqual(retained.attachments[0].capture_status, "pending")
                    self.assertFalse(any(
                        operation.operation_type in {module.OperationType.EVIDENCE_CLEANUP,
                                                     module.OperationType.REVIEW_UPDATE}
                        for operation in retained.operations
                    ))
                    message.delete.assert_not_awaited()
                    cog._case_store.claim_resolution = real_claim

    async def test_final_expiry_commit_rechecks_wait_and_releases_only_its_lease(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog, _message, case_id, now = await self._fixture(module)
            cog._case_store.fail_pending_attachment_captures(case_id, 1, "Fixture terminal failure")
            lease = cog._case_store.claim_resolution(case_id, now, automatic_expiry=True)
            self.assertIsNotNone(lease)
            operation = cog._case_store.get_case(case_id).operations[0]
            claimed = cog._case_store.claim_operation(operation.operation_id, now)
            self.assertTrue(cog._case_store.defer_operation_for_capability(
                claimed.operation_id, now, claimed.claim_token, "message_content",
            ))
            self.assertFalse(cog._case_store.finish_resolution(
                lease, module.CaseStatus.EXPIRED, "expired", None, now, automatic_expiry=True,
            ))
            self.assertEqual(cog._case_store.get_case(case_id).case.status.value, "pending")

    async def test_automatic_expiry_cannot_claim_unfinished_capture_even_with_full_grants(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog, _message, case_id, now = await self._fixture(module)
            cog.bot.intents.message_content = True
            self.assertIsNone(cog._case_store.claim_resolution(case_id, now, automatic_expiry=True))
            self.assertIsNone(cog._case_store.claim_resolution(
                case_id, now, now - timedelta(minutes=5), automatic_expiry=True,
            ))
            self.assertIsNotNone(cog._case_store.claim_resolution(case_id, now))
