"""Case lifecycle: durable operations, operation leases, reconciliation,
expiry and resolution, and evidence cleanup.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.detection_case_fixtures import capture_attachment, publish_primary
from tests.harness import (
    CaseExpiryTestCase,
    _async_noop,
    _Bot,
    _isolated_honeypot_modules,
    _operational_support,
)


class CaseLifecycleTests(CaseExpiryTestCase):
    async def test_resolution_failure_releases_the_case_lease(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                with mock.patch.object(
                    cog._case_store,
                    "finish_resolution",
                    side_effect=ValueError("conflicting decision"),
                ):
                    with self.assertRaisesRegex(ValueError, "conflicting decision"):
                        await cog.resolve_detection_case(
                            appended.case.case_id, "images:fp", moderator_id=99
                        )

                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                self.assertEqual(snapshot.case.status.value, "pending")

    async def test_evidence_cleanup_waits_for_terminal_review_projection(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                await asyncio.to_thread(cog._case_store.initialize)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10,
                        20,
                        30,
                        40,
                        "evidence",
                        now,
                        None,
                        (
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
                )
                evidence = (
                    honeypot.case_evidence_root(
                        cog._detection_case_files_path,
                        appended.case.guild_id,
                        appended.case.case_id,
                    )
                    / "1"
                    / "proof.png"
                )
                evidence.parent.mkdir(parents=True, exist_ok=True)
                evidence.write_bytes(b"image")
                await asyncio.to_thread(
                    capture_attachment,
                    cog._case_store,
                    appended.case.case_id,
                    1,
                    0,
                    evidence,
                )
                self.assertTrue(
                    await asyncio.to_thread(
                        publish_primary,
                        cog._case_store,
                        appended.case.case_id,
                        50,
                        60,
                    )
                )
                lease = await asyncio.to_thread(
                    cog._case_store.claim_resolution,
                    appended.case.case_id,
                    now,
                )
                self.assertTrue(
                    await asyncio.to_thread(
                        cog._case_store.finish_resolution,
                        lease,
                        honeypot.CaseStatus.RESOLVED,
                        "ignore",
                        99,
                        now,
                        decisions=None,
                        final_operations=(
                            ("review_update", f"review-update:{appended.case.case_id}"),
                            (
                                "evidence_cleanup",
                                f"evidence-cleanup:{appended.case.case_id}",
                            ),
                        ),
                    )
                )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                cleanup = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "evidence_cleanup"
                )
                running = await asyncio.to_thread(
                    cog._case_store.claim_operation, cleanup.operation_id, now
                )

                self.assertTrue(evidence.exists())
                self.assertIsNone(running)
                stored = next(
                    item
                    for item in cog._case_store.get_case(
                        appended.case.case_id
                    ).operations
                    if item.operation_id == cleanup.operation_id
                )
                self.assertEqual(stored.status.value, "pending")
                self.assertIsNone(stored.retry_at)

    async def test_cancelled_operation_worker_stops_its_lease_heartbeat(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config = self._config({})
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "review_publish",
                    f"review-publish:{appended.case.case_id}",
                )
                running = cog._case_store.claim_operation(
                    operation.operation_id, datetime.now(timezone.utc)
                )
                heartbeat_started = asyncio.Event()
                heartbeat_stopped = asyncio.Event()
                publication_started = asyncio.Event()

                async def heartbeat(_operation):
                    heartbeat_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        heartbeat_stopped.set()

                async def blocked_publication(*args, **kwargs):
                    publication_started.set()
                    await asyncio.Event().wait()

                cog._renew_detection_operation = heartbeat
                cog._publish_detection_case = blocked_publication
                task = asyncio.create_task(
                    cog._execute_detection_case_operation(
                        running, datetime.now(timezone.utc)
                    )
                )
                await asyncio.wait_for(heartbeat_started.wait(), timeout=1)
                await asyncio.wait_for(publication_started.wait(), timeout=1)

                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0)

                self.assertTrue(heartbeat_stopped.is_set())

    async def test_unsupported_operation_error_uses_persisted_value(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.MODERATOR_IGNORE,
                    f"unsupported:{appended.case.case_id}",
                )
                claimed = cog._case_store.claim_operation(
                    operation.operation_id,
                    now,
                )

                await cog._execute_detection_case_operation(claimed, now)

                failed = next(
                    item
                    for item in cog._case_store.get_case(
                        appended.case.case_id
                    ).operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(
                    failed.last_error,
                    "RuntimeError: unsupported detection case operation: moderator_ignore",
                )

    async def test_unknown_persisted_operation_fails_without_escaping_worker(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, now)

                with self.assertLogs(level="WARNING") as store_logs:
                    operation = cog._case_store.ensure_operation(
                        appended.case.case_id,
                        "future_operation",
                        f"future:{appended.case.case_id}",
                    )
                    reopened = honeypot.DetectionCaseStore(
                        cog._case_store.database_path
                    )
                    reopened.initialize()
                    claimed = reopened.claim_operation(operation.operation_id, now)
                cog._case_store = reopened

                self.assertEqual(claimed.operation_type, "future_operation")
                self.assertIn("future_operation", "\n".join(store_logs.output))
                try:
                    with self.assertLogs(level="WARNING") as operation_logs:
                        await cog._execute_detection_case_operation(claimed, now)
                except Exception as error:
                    self.fail(f"operation worker raised {error!r}")

                failed = next(
                    item
                    for item in reopened.get_case(appended.case.case_id).operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(failed.status, honeypot.OperationStatus.FAILED)
                self.assertEqual(failed.operation_type, "future_operation")
                self.assertEqual(
                    failed.last_error,
                    "RuntimeError: unsupported detection case operation: future_operation",
                )
                self.assertIn(
                    "kind=future_operation",
                    "\n".join(operation_logs.output),
                )

    async def test_recovered_operation_does_not_send_error_alert(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                channel = SimpleNamespace(send=mock.AsyncMock())
                guild = SimpleNamespace(
                    id=10,
                    get_channel=lambda channel_id: channel,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot, _operational_support())
                cog.config = self._config({"errors_channel": 30})
                honeypot.discord.AllowedMentions = SimpleNamespace(none=lambda: None)
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.EVIDENCE_CLEANUP,
                    f"evidence-cleanup:{appended.case.case_id}",
                )
                first = cog._case_store.claim_operation(operation.operation_id, now)
                self.assertTrue(
                    cog._case_store.fail_operation(
                        first.operation_id,
                        first.claim_token,
                        "temporary",
                        now,
                        now,
                    )
                )
                cog._case_store.record_operational_failure(
                    guild_id=10,
                    source=honeypot.OperationType.EVIDENCE_CLEANUP,
                    summary="temporary",
                    occurred_at=now,
                    case_id=appended.case.case_id,
                    operation_id=operation.operation_id,
                )
                retried = cog._case_store.claim_operation(
                    operation.operation_id,
                    now + timedelta(seconds=1),
                )

                await cog._execute_detection_case_operation(
                    retried,
                    now + timedelta(seconds=1),
                )

                channel.send.assert_not_awaited()
                cog._support.send_technical_alert.assert_not_awaited()

    async def test_terminal_capture_failure_is_a_current_case_note(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._case_store.initialize()
                now = datetime.now(timezone.utc)
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        10,
                        20,
                        30,
                        40,
                        "evidence",
                        now,
                        None,
                        (
                            honeypot.NewAttachment(
                                0,
                                "proof.png",
                                4,
                                "image/png",
                                None,
                                None,
                                "https://cdn.test/proof.png",
                            ),
                        ),
                    ),
                    (),
                )
                self.assertEqual(
                    cog._case_store.fail_pending_attachment_captures(
                        appended.case.case_id,
                        appended.message.sequence,
                        "NotFound after 3 attempts",
                    ),
                    1,
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)

                self.assertTrue(
                    any(
                        "evidence" in note.lower()
                        and "unavailable" in note.lower()
                        for note in honeypot.render_timeline(snapshot).case_notes
                    )
                )

    async def test_case_projection_warns_about_failed_required_operations(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_release",
                    f"role-release:{appended.case.case_id}:77",
                )
                running = cog._case_store.claim_operation(operation.operation_id, now)
                self.assertTrue(
                    cog._case_store.fail_operation(
                        running.operation_id,
                        running.claim_token,
                        "missing permission",
                        now,
                        now + timedelta(minutes=5),
                    )
                )

                projection = honeypot.render_case(
                    cog._case_store.get_case(appended.case.case_id)
                )

                warnings = "\n".join(
                    field.value for page in projection.pages for field in page
                )
                self.assertIn("mute", warnings.lower())
                self.assertIn("bot logs", warnings.lower())
                self.assertNotIn("role_release", warnings)

    async def test_startup_expires_overdue_case_before_restoring_views(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                created_at = datetime.now(timezone.utc) - timedelta(hours=25)
                first_cog = honeypot.Honeypot(_Bot(), _operational_support())
                first_cog._case_store.initialize()
                appended = first_cog._case_store.append_message(
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

                restarted = honeypot.Honeypot(_Bot(), _operational_support())
                restarted._init_firstpost_seen_store = _async_noop
                restarted._init_imagescan_store = _async_noop
                restarted._restore_pending_reviews = _async_noop
                restored_statuses = []

                async def observe_restore():
                    snapshot = restarted._case_store.get_case(appended.case.case_id)
                    restored_statuses.append(snapshot.case.status.value)

                restarted._restore_detection_case_views = observe_restore
                await restarted.cog_load()
                try:
                    await asyncio.wait_for(restarted._case_restore_task, timeout=2)
                    snapshot = restarted._case_store.get_case(appended.case.case_id)

                    self.assertEqual(restored_statuses, ["expired"])
                    self.assertEqual(snapshot.case.status.value, "expired")
                    self.assertEqual(snapshot.case.resolution, "expired")
                finally:
                    await restarted.cog_unload()

    async def test_scheduler_and_moderator_cannot_resolve_same_case_twice(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc) - timedelta(hours=25)
                )

                outcomes = await asyncio.gather(
                    cog.resolve_detection_case(appended.case.case_id, "expired"),
                    cog.resolve_detection_case(
                        appended.case.case_id, "images:ignore", moderator_id=99
                    ),
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)

                self.assertEqual(sum(outcomes), 1)
                self.assertIn(snapshot.case.status.value, {"expired", "resolved"})

    async def test_stale_resolving_case_is_reclaimed(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                now = datetime.now(timezone.utc)
                appended = self._append_case(honeypot, cog, now - timedelta(hours=25))
                cog._case_store.claim_resolution(
                    appended.case.case_id, now - timedelta(minutes=6)
                )

                resolved = await cog.resolve_detection_case(
                    appended.case.case_id, "expired", now=now
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)

                self.assertTrue(resolved)
                self.assertEqual(snapshot.case.status.value, "expired")

    async def test_reconciliation_reclaims_stale_resolving_but_not_fresh_lease(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                now = datetime.now(timezone.utc)
                stale = self._append_case(
                    honeypot, cog, now - timedelta(hours=25), message_id=41
                )
                fresh = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=21,
                        channel_id=30,
                        message_id=42,
                        content="fresh",
                        created_at=now - timedelta(hours=25),
                        jump_url="https://discord.test/messages/42",
                        attachments=(),
                    ),
                    (),
                )
                cog._case_store.claim_resolution(
                    stale.case.case_id, now - timedelta(minutes=7)
                )
                cog._case_store.claim_resolution(
                    fresh.case.case_id, now - timedelta(minutes=1)
                )

                await cog._run_detection_reconciliation()

                stale_snapshot = cog._case_store.get_case(stale.case.case_id)
                fresh_snapshot = cog._case_store.get_case(fresh.case.case_id)
                self.assertEqual(stale_snapshot.case.status.value, "expired")
                self.assertEqual(fresh_snapshot.case.status.value, "resolving")

    async def test_startup_restores_pending_case_view(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                first = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, first, datetime.now(timezone.utc))
                publish_primary(first._case_store, appended.case.case_id, 30, 77)

                bot = _Bot()
                restarted = honeypot.Honeypot(bot, _operational_support())
                restarted._case_review_rerender = mock.AsyncMock()
                await restarted._restore_detection_case_views()

                self.assertEqual(len(bot.restored_views), 1)
                view, message_id = bot.restored_views[0]
                self.assertEqual(view.case_id, appended.case.case_id)
                self.assertEqual(message_id, 77)
                restarted._case_review_rerender.assert_awaited_once_with(
                    appended.case.case_id
                )

    async def test_missing_discord_review_does_not_block_expiry(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config = self._config({"review_channel": None})
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc) - timedelta(hours=25)
                )
                publish_primary(cog._case_store, appended.case.case_id, 30, 77)

                await honeypot.detection._run_detection_case_expiry(cog)
                snapshot = cog._case_store.get_case(appended.case.case_id)

                self.assertEqual(snapshot.case.status.value, "expired")
                operation = next(
                    item for item in snapshot.operations if item.operation_type == "review_update"
                )
                self.assertEqual(operation.status.value, "failed")
                self.assertIsNotNone(operation.retry_at)

    async def test_live_operation_heartbeat_prevents_stale_reclaim(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._detection_heartbeat_interval_seconds = 0.05
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id, "review_update", "heartbeat-review"
                )
                claimed = cog._case_store.claim_operation(
                    operation.operation_id, datetime.now(timezone.utc)
                )
                started = asyncio.Event()
                release = asyncio.Event()
                renewed = asyncio.Event()
                renewal_times = []
                event_loop = asyncio.get_running_loop()

                renew_operation_claim = cog._case_store.renew_operation_claim

                def record_renewal(operation_id, token, now):
                    did_renew = renew_operation_claim(operation_id, token, now)
                    if did_renew:
                        renewal_times.append(now)
                        event_loop.call_soon_threadsafe(renewed.set)
                    return did_renew

                async def blocked_review(case_id):
                    started.set()
                    await release.wait()

                cog._case_store.renew_operation_claim = record_renewal
                cog._case_review_rerender = blocked_review
                worker = asyncio.create_task(
                    cog._execute_detection_case_operation(claimed, datetime.now(timezone.utc))
                )
                await started.wait()
                try:
                    await asyncio.wait_for(renewed.wait(), timeout=1)
                    stale_before = claimed.claimed_at + (
                        renewal_times[0] - claimed.claimed_at
                    ) / 2
                    contenders = cog._case_store.claim_due_operations(
                        datetime.now(timezone.utc),
                        stale_before=stale_before,
                    )
                    self.assertEqual(contenders, ())
                finally:
                    release.set()
                    await worker

    async def test_failed_review_edit_does_not_revert_expired_state(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config = self._config({})
                cog._case_review_rerender = mock.AsyncMock(
                    side_effect=honeypot.discord.HTTPException()
                )
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                publish_primary(cog._case_store, appended.case.case_id, 30, 77)

                resolved = await cog.resolve_detection_case(
                    appended.case.case_id, "expired"
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)

                self.assertTrue(resolved)
                self.assertEqual(snapshot.case.status.value, "expired")
                operation = next(
                    item for item in snapshot.operations if item.operation_type == "review_update"
                )
                self.assertEqual(operation.status.value, "failed")

    async def test_reconciliation_retries_due_operation(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config = self._config({})
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "review_update",
                    f"review-update:{appended.case.case_id}",
                )
                cog._case_review_rerender = mock.AsyncMock()

                await cog._run_detection_reconciliation()
                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )

                self.assertEqual(completed.status.value, "succeeded")

    async def test_terminal_case_atomically_contains_required_operations(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config = self._config({"mute_role": 55})
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                ownership = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                owned_at = datetime.now(timezone.utc)
                ownership = cog._case_store.claim_operation(ownership.operation_id, owned_at)
                cog._case_store.start_operation_effect(
                    ownership.operation_id, ownership.claim_token, owned_at
                )
                cog._case_store.record_operation_role_ownership(
                    ownership.operation_id, ownership.claim_token,
                    appended.case.case_id, 10, 20, role_id=55, now=owned_at,
                )
                cog._case_store.complete_operation(
                    ownership.operation_id, ownership.claim_token, owned_at
                )

                await cog.resolve_detection_case(appended.case.case_id, "expired")
                snapshot = cog._case_store.get_case(appended.case.case_id)

                self.assertEqual(snapshot.case.status.value, "expired")
                self.assertEqual(
                    {item.operation_type for item in snapshot.operations},
                    {"role_apply", "review_update", "role_release", "evidence_cleanup"},
                )

    async def test_evidence_cleanup_retries_then_removes_case_files(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config = self._config({"mute_role": None})
                cog._case_review_rerender = mock.AsyncMock()
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                case_directory = (
                    cog._detection_case_files_path
                    / str(appended.case.guild_id)
                    / appended.case.case_id
                )
                case_directory.mkdir(parents=True)
                evidence = case_directory / "proof.png"
                evidence.write_bytes(b"proof")

                real_unlink = Path.unlink
                attempts = 0

                def fail_once(path, *args, **kwargs):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise OSError("busy")
                    return real_unlink(path, *args, **kwargs)

                with mock.patch.object(Path, "unlink", fail_once):
                    await cog.resolve_detection_case(appended.case.case_id, "expired")
                failed = cog._case_store.get_case(appended.case.case_id)
                cleanup = next(
                    item for item in failed.operations
                    if item.operation_type == "evidence_cleanup"
                )
                self.assertEqual(failed.case.status.value, "expired")
                self.assertEqual(cleanup.status.value, "failed")
                self.assertTrue(evidence.exists())

                cleanup_now = cleanup.retry_at + timedelta(seconds=1)
                claimed = cog._case_store.claim_due_operations(cleanup_now)
                cleanup_claim = next(
                    item for item in claimed if item.operation_id == cleanup.operation_id
                )
                await cog._execute_detection_case_operation(cleanup_claim, cleanup_now)
                completed = cog._case_store.get_case(appended.case.case_id)
                self.assertFalse(evidence.exists())
                self.assertEqual(completed.operations, ())

    async def test_evidence_cleanup_treats_already_missing_sample_as_removed(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config = self._config({"mute_role": None})
                cog._case_review_rerender = mock.AsyncMock()
                cog._case_store.initialize()
                now = datetime.now(timezone.utc)
                attachment = honeypot.NewAttachment(
                    0, "proof.png", 4, "image/png", None, None, "https://cdn/proof"
                )
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=now,
                        jump_url="https://discord.test/messages/40",
                        attachments=(attachment,),
                    ),
                    (),
                )
                missing = (
                    cog._detection_case_files_path
                    / str(appended.case.guild_id)
                    / appended.case.case_id
                    / str(appended.message.sequence)
                    / "proof.png"
                )
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        missing,
                    )
                )
                cog._case_review_rerender = mock.AsyncMock()
                cog._imagescan_add_file_sample = mock.AsyncMock(
                    return_value=("error", None)
                )

                await cog.resolve_detection_case(
                    appended.case.case_id, "images:tp", moderator_id=99
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                self.assertEqual(snapshot.case.status.value, "resolved")
                self.assertEqual(snapshot.messages, ())
                self.assertEqual(snapshot.attachments, ())
                self.assertEqual(snapshot.operations, ())
                cog._imagescan_add_file_sample.assert_not_awaited()

    async def test_evidence_cleanup_refuses_path_outside_case_root(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config = self._config({"mute_role": None})
                cog._case_review_rerender = mock.AsyncMock()
                cog._case_store.initialize()
                outside = Path(directory) / "do-not-delete.png"
                outside.write_bytes(b"safe")
                attachment = honeypot.NewAttachment(
                    0, "proof.png", 4, "image/png", None, None, "https://cdn/proof"
                )
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=44,
                        content="evidence",
                        created_at=datetime.now(timezone.utc),
                        jump_url="https://discord.test/messages/44",
                        attachments=(attachment,),
                    ),
                    (),
                )
                capture_attachment(
                    cog._case_store,
                    appended.case.case_id,
                    appended.message.sequence,
                    0,
                    outside,
                )

                await cog.resolve_detection_case(appended.case.case_id, "expired")
                snapshot = cog._case_store.get_case(appended.case.case_id)
                cleanup = next(
                    item for item in snapshot.operations
                    if item.operation_type == "evidence_cleanup"
                )

                self.assertTrue(outside.exists())
                self.assertEqual(snapshot.case.status.value, "expired")
                self.assertEqual(cleanup.status.value, "failed")
                self.assertIn("escapes case root", cleanup.last_error)
