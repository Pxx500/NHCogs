"""Attachment capture and evidence persistence: reservation, deadlines,
hashing, the canonical case root and the initial image scan batches.
"""

import asyncio
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import (
    DetectionPipelineTestCase,
    _Bot,
    _isolated_honeypot_modules,
    active_case,
    drain_background_work,
)


class DetectionCaptureTests(DetectionPipelineTestCase):
    async def test_cancelling_coordinator_cancels_inflight_attachment_reads(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                read_started = asyncio.Event()
                read_stopped = asyncio.Event()
                release_read = asyncio.Event()

                async def blocked_read(*args, **kwargs):
                    read_started.set()
                    try:
                        await release_read.wait()
                    finally:
                        read_stopped.set()
                    return b"evidence"

                message.attachments[0].read = mock.AsyncMock(
                    side_effect=blocked_read
                )
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "review_channel": None,
                    "spam_enabled": False,
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                task = asyncio.create_task(cog.on_message(message))
                await asyncio.wait_for(read_started.wait(), timeout=1)

                try:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    await asyncio.sleep(0)
                    self.assertTrue(read_stopped.is_set())
                finally:
                    release_read.set()
                    await drain_background_work(cog)

    async def test_capture_failures_count_failed_timeout_and_too_large_results(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=3)
                message.attachments[0].read = mock.AsyncMock(
                    side_effect=RuntimeError("read failed")
                )

                async def slow_read(*, use_cached):
                    await asyncio.sleep(0.1)
                    return b"late"

                message.attachments[1].read = mock.AsyncMock(side_effect=slow_read)
                message.attachments[2].size = 4
                message.attachments[2].read = mock.AsyncMock(return_value=b"12345")
                config = {
                    "enabled": True, "dry_run": False,
                    "review_channel": None, "spam_enabled": True,
                    "spam_action": "review", "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                cog._record_operational_failure = mock.AsyncMock(
                    wraps=cog._record_operational_failure
                )
                cog._send_operational_alert = mock.AsyncMock()

                with mock.patch.object(
                    honeypot.detection_runtime,
                    "DETECTION_ATTACHMENT_TIMEOUT_SECONDS",
                    0.01,
                ):
                    await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(
                    [item.capture_status for item in snapshot.attachments],
                    ["capture_failed", "capture_timeout", "too_large"],
                )
                failure_calls = [
                    call
                    for call in cog._increment_stat.await_args_list
                    if call.args[1] == "evidence_capture_failures"
                ]
                self.assertEqual(len(failure_calls), 1)
                self.assertEqual(failure_calls[0].args[2], 3)
                operational_failures = await asyncio.to_thread(
                    cog._case_store.list_operational_failures, message.guild.id
                )
                self.assertEqual(len(operational_failures), 1)
                self.assertEqual(operational_failures[0].source, "evidence_capture")
                self.assertIn("Failed to capture 2 attachment(s)", operational_failures[0].summary)
                evidence_failure = next(
                    call
                    for call in cog._record_operational_failure.await_args_list
                    if call.args[1] == "evidence_capture"
                )
                self.assertEqual(evidence_failure.kwargs.get("attempts"), 3)
                self.assertIs(evidence_failure.kwargs.get("terminal"), True)
                alert = cog._send_operational_alert.await_args.args[1]
                self.assertIn("terminal", alert)
                self.assertNotIn("will retry", alert)

    async def test_two_cogs_do_not_apply_an_aggregate_case_byte_limit(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                first_cog = honeypot.Honeypot(_Bot())
                second_cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(first_cog._case_store.initialize)
                await asyncio.to_thread(second_cog._case_store.initialize)
                first_message = self._message(honeypot, message_id=300, attachment_count=3)
                second_message = self._message(honeypot, message_id=301, attachment_count=3)
                attachment_bytes = 25 * 1024 * 1024
                started = 0
                six_started = asyncio.Event()
                release_reads = asyncio.Event()

                async def blocked_read(*, use_cached):
                    nonlocal started
                    started += 1
                    if started == 6:
                        six_started.set()
                    await release_reads.wait()
                    return b"captured"

                for attachment in first_message.attachments + second_message.attachments:
                    attachment.size = attachment_bytes
                    attachment.read = mock.AsyncMock(side_effect=blocked_read)

                first = await asyncio.to_thread(
                    first_cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(first_message),
                    (),
                )
                second = await asyncio.to_thread(
                    second_cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(second_message),
                    (),
                )
                first_capture = asyncio.create_task(
                    first_cog._capture_case_attachments(
                        first_message, first.case.case_id, first.message.sequence
                    )
                )
                second_capture = asyncio.create_task(
                    second_cog._capture_case_attachments(
                        second_message, second.case.case_id, second.message.sequence
                    )
                )

                await asyncio.wait_for(six_started.wait(), timeout=1)
                self.assertEqual(started, 6)
                release_reads.set()
                results = await asyncio.gather(first_capture, second_capture)

                statuses = [capture.status.value for captures in results for capture in captures]
                self.assertEqual(statuses.count("captured"), 6)
                self.assertEqual(statuses.count("too_large"), 0)
                self.assertEqual(
                    sum(
                        attachment.read.await_count
                        for attachment in first_message.attachments + second_message.attachments
                    ),
                    6,
                )

    async def test_capture_cleans_exact_file_when_actual_bytes_exceed_reservation(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                message.attachments[0].size = 4
                message.attachments[0].read = mock.AsyncMock(return_value=b"12345")
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (),
                )

                captures = await cog._capture_case_attachments(
                    message, appended.case.case_id, appended.message.sequence
                )

                case_root = (
                    cog._detection_case_files_path
                    / str(message.guild.id)
                    / appended.case.case_id
                )
                stored = (
                    await asyncio.to_thread(cog._case_store.get_case, appended.case.case_id)
                ).attachments[0]
                self.assertEqual(captures[0].status.value, "too_large")
                self.assertEqual(stored.capture_status, "too_large")
                self.assertIsNone(stored.evidence_path)
                self.assertEqual(
                    [path for path in case_root.rglob("*") if path.is_file()],
                    [],
                )

    async def test_production_capture_and_resolution_share_canonical_case_root(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                case_root = (
                    cog._detection_case_files_path
                    / str(message.guild.id)
                    / snapshot.case.case_id
                )
                evidence_path = Path(snapshot.attachments[0].evidence_path)
                self.assertTrue(evidence_path.is_relative_to(case_root / "1"))
                self.assertTrue(evidence_path.exists())
                stat_names = [call.args[1] for call in cog._increment_stat.await_args_list]
                self.assertIn("forward_purge_deletes", stat_names)
                cog._case_review_rerender = mock.AsyncMock()

                await cog.resolve_detection_case(snapshot.case.case_id, "expired")

                self.assertFalse(case_root.exists())

    async def test_user_deletion_waits_for_inflight_production_capture(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                capture_started = asyncio.Event()
                release_capture = asyncio.Event()

                async def blocked_read(*args, **kwargs):
                    capture_started.set()
                    await release_capture.wait()
                    return b"proof"

                message = self._message(honeypot, attachment_count=1)
                message.attachments[0].read = mock.AsyncMock(side_effect=blocked_read)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                capture_task = asyncio.create_task(cog.on_message(message))
                await asyncio.wait_for(capture_started.wait(), timeout=1)

                deletion_task = asyncio.create_task(
                    cog.red_delete_data_for_user(
                        requester="discord_deleted_user", user_id=message.author.id
                    )
                )
                completed, _pending = await asyncio.wait(
                    {deletion_task}, timeout=0.2
                )
                try:
                    self.assertEqual(completed, set())
                finally:
                    release_capture.set()
                await asyncio.gather(capture_task, deletion_task)

                self.assertIsNone(
                    active_case(
                        cog._case_store,
                        message.guild.id, message.author.id
                    )
                )
                guild_root = cog._detection_case_files_path / str(message.guild.id)
                self.assertFalse(any(guild_root.glob("**/*")) if guild_root.exists() else False)

    async def test_cross_instance_deletion_rejects_late_capture_without_orphan(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                capture_cog = honeypot.Honeypot(_Bot())
                deletion_cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(capture_cog._case_store.initialize)
                await asyncio.to_thread(deletion_cog._case_store.initialize)
                capture_started = asyncio.Event()
                release_capture = asyncio.Event()
                real_capture = honeypot.detection_runtime.capture_attachment

                async def delayed_capture(*args, **kwargs):
                    capture_started.set()
                    await release_capture.wait()
                    return await real_capture(*args, **kwargs)

                message = self._message(honeypot, attachment_count=1)
                self._configure_public_boundary(
                    capture_cog,
                    {
                        "enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                capture_cog._scan_all_case_message_images = mock.AsyncMock()
                capture_cog._publish_detection_case = mock.AsyncMock()

                with mock.patch.object(
                    honeypot.detection_runtime,
                    "capture_attachment",
                    new=delayed_capture,
                ):
                    capture_task = asyncio.create_task(capture_cog.on_message(message))
                    await asyncio.wait_for(capture_started.wait(), timeout=1)
                    snapshot = await asyncio.to_thread(
                        active_case, deletion_cog._case_store,
                        message.guild.id,
                        message.author.id,
                    )
                    case_root = (
                        capture_cog._detection_case_files_path
                        / str(message.guild.id)
                        / snapshot.case.case_id
                    )
                    self.assertFalse(case_root.exists())
                    try:
                        await deletion_cog.red_delete_data_for_user(
                            requester="discord_deleted_user",
                            user_id=message.author.id,
                        )
                        case_root.mkdir(parents=True)
                        unrelated_evidence = case_root / "other-capture.bin"
                        unrelated_evidence.write_bytes(b"other capture")
                    finally:
                        release_capture.set()
                    await capture_task

                self.assertIsNone(
                    active_case(
                        deletion_cog._case_store,
                        message.guild.id, message.author.id
                    )
                )
                self.assertEqual(deletion_cog._case_store.list_planned_case_deletions(), ())
                self.assertEqual(
                    [path for path in case_root.rglob("*") if path.is_file()],
                    [unrelated_evidence],
                )
                self.assertEqual(unrelated_evidence.read_bytes(), b"other capture")
                capture_cog._scan_all_case_message_images.assert_not_awaited()

    async def test_duplicate_attachment_filenames_keep_ordered_evidence_and_hashes(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                payloads = (b"first-image", b"second-image")
                message.attachments = [
                    SimpleNamespace(
                        filename="proof.png",
                        size=len(payload),
                        content_type="image/png",
                        width=10,
                        height=20,
                        url=f"https://cdn.test/{position}/proof.png",
                        description=None,
                        is_spoiler=lambda: False,
                        read=mock.AsyncMock(return_value=payload),
                    )
                    for position, payload in enumerate(payloads)
                ]
                for attachment in message.attachments:
                    async def read_bounded(max_bytes, *, _attachment=attachment):
                        data = await _attachment.read(use_cached=True)
                        return data[: max_bytes + 1]

                    attachment.read_bounded = read_bounded
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                cog._imagescan_load_samples = mock.AsyncMock(return_value=[])
                cog._imagescan_model_state = mock.AsyncMock(
                    return_value={"effective_threshold": 20}
                )
                cog._publish_detection_case = mock.AsyncMock()
                hashes_by_payload = {
                    b"first-image": {"sha256": "first-sha", "phash": "first-phash"},
                    b"second-image": {"sha256": "second-sha", "phash": "second-phash"},
                }

                with (
                    mock.patch.object(
                        honeypot.imagescan,
                        "image_hashes_from_bytes",
                        side_effect=lambda data: hashes_by_payload[data],
                    ),
                    mock.patch.object(
                        honeypot.imagescan,
                        "match_image",
                        side_effect=lambda hashes, samples, threshold: {
                            "sha256": hashes["sha256"]
                        },
                    ),
                ):
                    await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertIsNotNone(snapshot)
                self.assertEqual([item.position for item in snapshot.attachments], [0, 1])
                evidence_paths = [Path(item.evidence_path) for item in snapshot.attachments]
                self.assertEqual(len(set(evidence_paths)), 2)
                self.assertEqual([path.read_bytes() for path in evidence_paths], list(payloads))
                self.assertEqual(
                    [item.sha256 for item in snapshot.attachments],
                    ["first-sha", "second-sha"],
                )
                self.assertEqual(
                    [item.perceptual_hash for item in snapshot.attachments],
                    ["first-phash", "second-phash"],
                )
                self.assertEqual(
                    [dict(item.match_metadata) for item in snapshot.attachments],
                    [{"sha256": "first-sha"}, {"sha256": "second-sha"}],
                )

    async def test_text_projection_precedes_capture_and_evidence_refresh_follows_it(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                capture_started = asyncio.Event()
                release_capture = asyncio.Event()
                contained = asyncio.Event()
                projection_started = asyncio.Event()

                async def blocked_read(*, use_cached):
                    capture_started.set()
                    await release_capture.wait()
                    return b"image"

                async def delete():
                    contained.set()

                async def publish(*args, **kwargs):
                    projection_started.set()

                message.attachments[0].read = mock.AsyncMock(side_effect=blocked_read)
                message.delete = mock.AsyncMock(side_effect=delete)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "review_enabled": True,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock(side_effect=publish)

                processing = asyncio.create_task(cog.on_message(message))
                await asyncio.wait_for(capture_started.wait(), timeout=1)
                await asyncio.wait_for(contained.wait(), timeout=1)
                await asyncio.wait_for(projection_started.wait(), timeout=1)
                try:
                    self.assertFalse(processing.done())
                    self.assertEqual(cog._publish_detection_case.await_count, 1)
                finally:
                    release_capture.set()
                    await processing

                self.assertEqual(cog._publish_detection_case.await_count, 2)

    async def test_saturated_capture_queue_does_not_delay_delete_or_retry_deleted_source(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                cog._detection_case_capture_slots = asyncio.Semaphore(0)
                message = self._message(honeypot, attachment_count=1)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                message_process = import_module(
                    "NHCogs.honeypot.operations.message_process"
                )

                with mock.patch.object(
                    message_process,
                    "DETECTION_CAPTURE_START_TIMEOUT_SECONDS",
                    0.01,
                ):
                    await cog.on_message(message)

                message.delete.assert_awaited_once()
                message.attachments[0].read.assert_not_awaited()
                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "message_process"
                )
                self.assertEqual(
                    snapshot.attachments[0].capture_status, "capture_failed"
                )
                self.assertEqual(operation.status.value, "succeeded")
                self.assertIsNone(operation.retry_at)

    async def test_unavailable_attachment_reservation_keeps_message_process_retryable(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                cog._case_store.reserve_attachment_capture = mock.Mock(
                    return_value=SimpleNamespace(
                        status="unavailable",
                        claim_token=None,
                        error="evidence capture is already claimed",
                    )
                )

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "message_process"
                )
                self.assertEqual(snapshot.attachments[0].capture_status, "pending")
                self.assertEqual(operation.status.value, "failed")
                self.assertIsNotNone(operation.retry_at)
                self.assertIn("not terminal", operation.last_error)

    async def test_terminal_case_recovery_stops_retrying_pending_attachment_capture(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                try:
                    handler_module = import_module(
                        "NHCogs.honeypot.operations.message_process"
                    )
                except ModuleNotFoundError:
                    self.fail("message_process has no dedicated handler module")
                self.assertIs(
                    cog._detection_operation_handlers.resolve(
                        honeypot.OperationType.MESSAGE_PROCESS
                    ),
                    handler_module.message_process_handler,
                )
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime.now(timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
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
                    lambda signals: (("message_process", "message-process:{case_id}:{sequence}"),),
                )
                operation = next(
                    item
                    for item in cog._case_store.get_case(appended.case.case_id).operations
                    if item.operation_type == "message_process"
                )
                lease = cog._case_store.claim_resolution(appended.case.case_id, now)
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.RESOLVED,
                        "ignore",
                        99,
                        now,
                    )
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(snapshot.attachments[0].capture_status, "capture_failed")
                self.assertEqual(persisted.status.value, "succeeded")
                self.assertEqual(persisted.result, "case_terminal")
                self.assertIsNone(persisted.retry_at)

    async def test_capture_deadline_preserves_fast_result_and_times_out_only_slow_attachment(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                self.assertEqual(honeypot.DETECTION_ATTACHMENT_TIMEOUT_SECONDS, 15.0)
                self.assertEqual(
                    honeypot.review_publication.DETECTION_CAPTURE_DEADLINE_SECONDS, 20.0
                )
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                gate = asyncio.Event()
                fast = SimpleNamespace(
                    filename="fast.png", size=4, content_type="image/png",
                    width=None, height=None, url="https://cdn/fast",
                    description=None, is_spoiler=lambda: False,
                    read=mock.AsyncMock(return_value=b"fast"),
                )
                async def slow_read(*args, **kwargs):
                    await gate.wait()
                    return b"slow"
                slow = SimpleNamespace(
                    filename="slow.png", size=4, content_type="image/png",
                    width=None, height=None, url="https://cdn/slow",
                    description=None, is_spoiler=lambda: False,
                    read=mock.AsyncMock(side_effect=slow_read),
                )
                for attachment in (fast, slow):
                    async def read_bounded(max_bytes, *, _attachment=attachment):
                        data = await _attachment.read(use_cached=True)
                        return data[: max_bytes + 1]

                    attachment.read_bounded = read_bounded
                message = self._message(honeypot, attachment_count=0)
                message.attachments = [fast, slow]
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (honeypot.DetectionSignal(
                        "forward_purge", "active", honeypot.ActionIntent.REVIEW, True, {}
                    ),),
                )
                with mock.patch.object(
                    honeypot.review_publication,
                    "DETECTION_CAPTURE_DEADLINE_SECONDS",
                    0.05,
                ):
                    captures = await cog._capture_case_attachments(
                        message, appended.case.case_id, 1
                    )

                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                self.assertEqual([item.status.value for item in captures], ["captured", "capture_timeout"])
                self.assertTrue(Path(snapshot.attachments[0].evidence_path).exists())
                self.assertEqual(snapshot.attachments[1].capture_status, "capture_timeout")

    async def test_discord_accepted_attachment_is_not_rejected_by_fixed_local_limit(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                message.attachments[0].size = 25 * 1024 * 1024 + 1
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "review_channel": None,
                    "spam_enabled": True,
                    "spam_action": "review",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                message.attachments[0].read.assert_awaited_once()
                self.assertEqual(snapshot.attachments[0].capture_status, "captured")
                self.assertFalse(honeypot.render_case(snapshot).incomplete_evidence)

    async def test_all_discord_accepted_attachments_are_captured_without_case_byte_cap(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=5)
                reads_started = 0
                five_reads_started = asyncio.Event()
                release_reads = asyncio.Event()

                async def blocked_read(*, use_cached):
                    nonlocal reads_started
                    reads_started += 1
                    if reads_started == 5:
                        five_reads_started.set()
                    await release_reads.wait()
                    return b"image"

                for attachment in message.attachments:
                    attachment.size = 25 * 1024 * 1024
                    attachment.read = mock.AsyncMock(side_effect=blocked_read)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (honeypot.DetectionSignal(
                        "forward_purge", "active", honeypot.ActionIntent.REVIEW, True, {}
                    ),),
                )
                capture_task = asyncio.create_task(
                    cog._capture_case_attachments(
                        message, appended.case.case_id, appended.message.sequence
                    )
                )
                await asyncio.wait_for(five_reads_started.wait(), timeout=1)
                try:
                    self.assertEqual(reads_started, 5)
                    for attachment in message.attachments:
                        attachment.read.assert_awaited_once_with(use_cached=True)
                finally:
                    release_reads.set()
                captures = await capture_task

                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                self.assertEqual(
                    [capture.status.value for capture in captures],
                    ["captured"] * 5,
                )
                self.assertEqual(
                    [attachment.capture_status for attachment in snapshot.attachments],
                    ["captured"] * 5,
                )
                self.assertFalse(honeypot.render_case(snapshot).incomplete_evidence)

    async def test_two_different_cases_start_attachment_capture_in_parallel(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                first_message = self._message(
                    honeypot, attachment_count=1, message_id=300
                )
                second_message = self._message(
                    honeypot, attachment_count=1, message_id=301
                )
                second_message.author.id = 201
                first_started = asyncio.Event()
                second_started = asyncio.Event()
                release_reads = asyncio.Event()

                async def first_read(*args, **kwargs):
                    first_started.set()
                    await release_reads.wait()
                    return b"first"

                async def second_read(*args, **kwargs):
                    second_started.set()
                    await release_reads.wait()
                    return b"second"

                first_message.attachments[0].size = len(b"first")
                first_message.attachments[0].read = mock.AsyncMock(
                    side_effect=first_read
                )
                second_message.attachments[0].size = len(b"second")
                second_message.attachments[0].read = mock.AsyncMock(
                    side_effect=second_read
                )
                first = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(first_message),
                    (),
                )
                second = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(second_message),
                    (),
                )
                first_task = asyncio.create_task(
                    cog._capture_case_attachments(
                        first_message, first.case.case_id, first.message.sequence
                    )
                )
                await asyncio.wait_for(first_started.wait(), timeout=1)
                second_task = asyncio.create_task(
                    cog._capture_case_attachments(
                        second_message, second.case.case_id, second.message.sequence
                    )
                )
                try:
                    await asyncio.wait_for(second_started.wait(), timeout=10)
                    captures_overlap = True
                except asyncio.TimeoutError:
                    captures_overlap = False
                finally:
                    release_reads.set()
                results = await asyncio.gather(first_task, second_task)

                self.assertTrue(captures_overlap)
                self.assertEqual(
                    [capture.status.value for captures in results for capture in captures],
                    ["captured", "captured"],
                )

    async def test_scan_setup_failure_does_not_prevent_delete_or_publication(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                self._configure_public_boundary(
                    cog, {"enabled": True, "review_enabled": True,
                          "dry_run": False,
                          "review_channel": None, "spam_enabled": False,
                          "firstpost_enabled": False, "firstpost_collect_enabled": False}
                )
                cog._imagescan_load_samples = mock.AsyncMock(side_effect=RuntimeError("model unavailable"))
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(snapshot.messages[0].delete_status.value, "deleted")
                self.assertIn("model unavailable", snapshot.attachments[0].error)
                self.assertEqual(cog._publish_detection_case.await_count, 2)
                operational_failures = await asyncio.to_thread(
                    cog._case_store.list_operational_failures, message.guild.id
                )
                self.assertEqual(len(operational_failures), 1)
                self.assertEqual(operational_failures[0].source, "image_scan_setup")
                self.assertIn("model unavailable", operational_failures[0].summary)

    async def test_forward_route_hashes_and_persists_every_image_attachment(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=3)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                        "imagescan_detector_threshold": 20,
                    },
                )
                cog._imagescan_load_samples = mock.AsyncMock(return_value=[])
                cog._imagescan_model_state = mock.AsyncMock(
                    return_value={"effective_threshold": 20}
                )
                cog._publish_detection_case = mock.AsyncMock()
                with (
                    mock.patch.object(
                        honeypot.imagescan,
                        "image_hashes_from_bytes",
                        side_effect=lambda data: {
                            "sha256": data.decode(),
                            "phash": f"phash-{data.decode()}",
                            "dhash": "dhash",
                            "ahash": "ahash",
                        },
                    ) as hashes,
                    mock.patch.object(
                        honeypot.imagescan,
                        "match_image",
                        return_value={"matched": False, "score": None},
                    ),
                ):
                    await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(hashes.call_count, 3)
                self.assertEqual(
                    [item.sha256 for item in snapshot.attachments],
                    ["image-1", "image-2", "image-3"],
                )
                self.assertEqual(
                    [item.perceptual_hash for item in snapshot.attachments],
                    ["phash-image-1", "phash-image-2", "phash-image-3"],
                )

    async def test_too_large_capture_is_not_fallback_read_by_scanner(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message(honeypot, attachment_count=1)
                message.attachments[0].read = mock.AsyncMock(
                    side_effect=AssertionError("too-large evidence must not be read")
                )
                capture = honeypot.detection_runtime.CaptureResult(
                    0,
                    honeypot.detection_runtime.CaptureStatus.TOO_LARGE,
                    None,
                    "over budget",
                )

                scans = await cog._scan_image_attachments(
                    message, (), 20, capture_results=(capture,)
                )

                message.attachments[0].read.assert_not_awaited()
                self.assertIn("over budget", scans[0]["error"])

    async def test_failed_or_timed_out_capture_is_not_fallback_read_by_scanner(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                for status in (
                    honeypot.detection_runtime.CaptureStatus.FAILED,
                    honeypot.detection_runtime.CaptureStatus.TIMEOUT,
                ):
                    with self.subTest(status=status.value):
                        message = self._message(honeypot, attachment_count=1)
                        message.attachments[0].read = mock.AsyncMock(
                            side_effect=AssertionError(
                                "failed evidence must not be read again"
                            )
                        )
                        capture = honeypot.detection_runtime.CaptureResult(
                            0, status, None, "capture unavailable"
                        )

                        scans = await cog._scan_image_attachments(
                            message, (), 20, capture_results=(capture,)
                        )

                        message.attachments[0].read.assert_not_awaited()
                        self.assertIn("capture unavailable", scans[0]["error"])

    async def test_image_processing_failure_is_recorded_for_moderators(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message(honeypot, attachment_count=1)
                message.attachments[0].read = mock.AsyncMock(return_value=b"broken image")
                case_id = "case-image-processing-failure"
                cog._imagescan_load_samples = mock.AsyncMock(return_value=())
                cog._imagescan_model_state = mock.AsyncMock(
                    return_value={"effective_threshold": 20}
                )
                cog._record_operational_failure = mock.AsyncMock()
                cog._case_store.update_attachment_scan = mock.Mock()

                with mock.patch.object(
                    honeypot.imagescan,
                    "image_hashes_from_bytes",
                    side_effect=ValueError("invalid image"),
                ):
                    await cog._scan_case_message_images(
                        message.guild.id,
                        tuple(message.attachments),
                        honeypot.GuildSettings.from_mapping(
                            {"imagescan_detector_threshold": 20}
                        ),
                        case_id,
                        1,
                        (),
                    )

                cog._record_operational_failure.assert_awaited_once()
                failure_call = cog._record_operational_failure.await_args
                self.assertEqual(failure_call.args[:2], (message.guild.id, "image_scan"))
                self.assertIn("invalid image", failure_call.args[2])
                self.assertEqual(failure_call.kwargs["case_id"], case_id)

    async def test_image_trigger_scans_and_persists_remaining_images(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                await cog._init_imagescan_store()
                message = self._message(honeypot, attachment_count=6)
                config = {
                    "enabled": True, "dry_run": False,
                    "review_channel": None, "spam_enabled": False,
                    "firstpost_enabled": False, "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": True,
                    "imagescan_detector_action": "review",
                    "imagescan_detector_threshold": 20,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._imagescan_load_samples = mock.AsyncMock(
                    return_value=[SimpleNamespace(decision="true_positive")]
                )
                cog._imagescan_model_state = mock.AsyncMock(
                    return_value={"valid": True, "effective_threshold": 20}
                )
                cog._publish_detection_case = mock.AsyncMock()

                with (
                    mock.patch.object(
                        honeypot.imagescan,
                        "image_hashes_from_bytes",
                        side_effect=lambda data: {
                            "sha256": data.decode(), "phash": f"p-{data.decode()}"
                        },
                    ),
                    mock.patch.object(
                        honeypot.imagescan,
                        "match_image",
                        side_effect=lambda hashes, samples, threshold: {
                            "matched": hashes["sha256"] == "image-1",
                            "score": 1,
                            "exact_decision": "true_positive"
                            if hashes["sha256"] == "image-1" else None,
                        },
                    ),
                ):
                    await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(
                    [item.sha256 for item in snapshot.attachments],
                    [f"image-{index}" for index in range(1, 7)],
                )
                self.assertEqual(
                    [dict(item.match_metadata) for item in snapshot.attachments],
                    [
                        {
                            "matched": index == 1,
                            "score": 1,
                            "exact_decision": "true_positive" if index == 1 else None,
                        }
                        for index in range(1, 7)
                    ],
                )
                for attachment in message.attachments:
                    attachment.read.assert_awaited_once()

    async def test_initial_scan_read_failure_is_retried_for_evidence_and_durable_scan(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                await cog._init_imagescan_store()
                message = self._message(honeypot, attachment_count=2)
                message.attachments[0].read.side_effect = [
                    OSError("temporary CDN failure"),
                    b"image-1",
                ]
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "review_channel": None,
                    "spam_enabled": False,
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": True,
                    "imagescan_detector_action": "review",
                    "imagescan_detector_threshold": 20,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._imagescan_load_samples = mock.AsyncMock(
                    return_value=[SimpleNamespace(decision="true_positive")]
                )
                cog._imagescan_model_state = mock.AsyncMock(
                    return_value={"valid": True, "effective_threshold": 20}
                )
                cog._publish_detection_case = mock.AsyncMock()

                with (
                    mock.patch.object(
                        honeypot.imagescan,
                        "image_hashes_from_bytes",
                        side_effect=lambda data: {
                            "sha256": data.decode(),
                            "phash": f"p-{data.decode()}",
                        },
                    ),
                    mock.patch.object(
                        honeypot.imagescan,
                        "match_image",
                        side_effect=lambda hashes, samples, threshold: {
                            "matched": hashes["sha256"] == "image-2",
                            "score": 1,
                        },
                    ),
                ):
                    await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(
                    [item.sha256 for item in snapshot.attachments],
                    ["image-1", "image-2"],
                )
                self.assertEqual(message.attachments[0].read.await_count, 2)
                self.assertNotIn(
                    (message.guild.id, message.id), cog._initial_image_scan_batches
                )

    async def test_failed_admission_releases_initial_scan_batch(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message(honeypot, attachment_count=1)
                signal = honeypot.DetectionSignal(
                    "image",
                    "matched",
                    honeypot.ActionIntent.REVIEW,
                    True,
                    {},
                )
                cog._collect_detection_signals = mock.AsyncMock(return_value=(signal,))
                cog._process_detected_message = mock.AsyncMock(
                    side_effect=RuntimeError("admission failed")
                )
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                cog._observe_message = mock.AsyncMock()
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog.config.guild = lambda guild: SimpleNamespace(
                    all=mock.AsyncMock(return_value={"enabled": True})
                )
                batch_key = (message.guild.id, message.id)
                completed = asyncio.create_task(asyncio.sleep(0, result={"data": b"image"}))
                cog._initial_image_scan_batches[batch_key] = {0: completed}

                with self.assertRaisesRegex(RuntimeError, "admission failed"):
                    await cog.on_message(message)

                self.assertNotIn(batch_key, cog._initial_image_scan_batches)
                await drain_background_work(cog)
