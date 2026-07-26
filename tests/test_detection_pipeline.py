import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.detection_case_fixtures import capture_attachment, publish_primary
from tests.harness import CaseExpiryTestCase, _Bot, _async_noop, _isolated_honeypot_modules


class DetectionExpiryTests(CaseExpiryTestCase):
    async def test_completed_moderation_waits_for_pending_attachment_capture(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                appended = cog._case_store.append_message(
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
                )
                self._complete_case_operation(
                    cog, appended.case.case_id, "ban", now
                )
                cog.resolve_detection_case = mock.AsyncMock(return_value=True)

                finished = await cog._finish_case_review_if_ready(
                    appended.case.case_id, 99
                )

                self.assertFalse(finished)
                cog.resolve_detection_case.assert_not_awaited()

    async def test_planned_moderation_is_a_completed_case_decision(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                self._complete_case_operation(
                    cog, appended.case.case_id, "planned_ban", now
                )
                cog.resolve_detection_case = mock.AsyncMock(return_value=True)

                finished = await cog._finish_case_review_if_ready(
                    appended.case.case_id, 99
                )

                self.assertTrue(finished)
                cog.resolve_detection_case.assert_awaited_once_with(
                    appended.case.case_id,
                    "planned_ban",
                    None,
                )

    async def test_resolution_failure_releases_the_case_lease(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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

    async def test_reclaimed_effect_fences_the_stale_role_worker(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(
                    roles=[], add_roles=mock.AsyncMock(), id=appended.case.user_id
                )
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                cog.bot.get_guild = lambda guild_id: guild
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                stale = cog._case_store.claim_operation(operation.operation_id, now)
                self.assertTrue(
                    cog._case_store.start_operation_effect(
                        stale.operation_id, stale.claim_token, now
                    )
                )
                reclaimed = cog._case_store.claim_due_operations(
                    now + timedelta(minutes=10),
                    stale_before=now + timedelta(minutes=5),
                )[0]
                self.assertNotEqual(stale.claim_token, reclaimed.claim_token)

                await cog._execute_detection_case_operation(stale, now)

                member.add_roles.assert_not_awaited()

    async def test_role_apply_is_superseded_by_completed_moderation(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(
                    id=appended.case.user_id,
                    roles=[],
                    add_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                cog.bot.get_guild = lambda _guild_id: guild
                cog._increment_stat = mock.AsyncMock()
                moderation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "moderator_ban",
                    f"moderator-ban:{appended.case.case_id}",
                    actor_id=99,
                )
                claimed_moderation = cog._case_store.claim_operation(
                    moderation.operation_id,
                    now,
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        claimed_moderation.operation_id,
                        claimed_moderation.claim_token,
                        now,
                        "ban",
                    )
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(completed.status.value, "succeeded")
                self.assertEqual(completed.result, "superseded_by_moderation")
                member.add_roles.assert_not_awaited()
                self.assertFalse(
                    any(
                        call.args[1] == "pending_mute_failures"
                        for call in cog._increment_stat.await_args_list
                    )
                )

    async def test_unsupported_operation_error_uses_persisted_value(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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

    async def test_recovered_operation_alert_uses_persisted_value(self):
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
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"logs_channel": 30})
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

                channel.send.assert_awaited_once()
                self.assertEqual(
                    channel.send.await_args.args[0],
                    "✅ Recovered: evidence_cleanup succeeded after 2 attempts.",
                )

    async def test_role_apply_fetches_cache_miss_and_terminalizes_not_found(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                fetch_member = mock.AsyncMock(
                    side_effect=honeypot.discord.NotFound()
                )
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda _user_id: None,
                    fetch_member=fetch_member,
                    get_role=lambda _role_id: role,
                )
                cog.bot.get_guild = lambda _guild_id: guild
                cog._increment_stat = mock.AsyncMock()
                cog._record_operational_failure = mock.AsyncMock()
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(completed.status.value, "succeeded")
                self.assertEqual(completed.result, "member_unavailable")
                fetch_member.assert_awaited_once_with(appended.case.user_id)
                cog._record_operational_failure.assert_not_awaited()
                self.assertFalse(
                    any(
                        call.args[1] == "pending_mute_failures"
                        for call in cog._increment_stat.await_args_list
                    )
                )

    async def test_current_role_apply_warning_disappears_after_recovery(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:77",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                retry_at = now + timedelta(seconds=10)
                self.assertTrue(
                    cog._case_store.fail_operation(
                        claimed.operation_id,
                        claimed.claim_token,
                        "RuntimeError: temporary failure",
                        now,
                        retry_at,
                    )
                )

                failed = cog._case_store.get_case(appended.case.case_id)
                self.assertTrue(
                    any(
                        "temporary mute" in note.lower() and "retry" in note.lower()
                        for note in honeypot.render_timeline(failed).case_notes
                    )
                )

                retry = cog._case_store.claim_operation(
                    operation.operation_id,
                    retry_at,
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        retry.operation_id,
                        retry.claim_token,
                        retry_at,
                        "already_present",
                    )
                )
                recovered = cog._case_store.get_case(appended.case.case_id)
                self.assertFalse(
                    any(
                        "temporary mute" in note.lower()
                        for note in honeypot.render_timeline(recovered).case_notes
                    )
                )

    async def test_terminal_capture_failure_is_a_current_case_note(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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

    async def test_terminal_case_fences_late_role_apply(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(
                    roles=[], add_roles=mock.AsyncMock(), id=appended.case.user_id
                )
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                cog.bot.get_guild = lambda guild_id: guild
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                running = cog._case_store.claim_operation(operation.operation_id, now)
                lease = cog._case_store.claim_resolution(
                    appended.case.case_id, now + timedelta(seconds=1)
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.EXPIRED,
                        "expired",
                        None,
                        now + timedelta(seconds=1),
                    )
                )

                await cog._execute_detection_case_operation(running, now)

                member.add_roles.assert_not_awaited()
                stored = next(
                    item
                    for item in cog._case_store.get_case(appended.case.case_id).operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(stored.result, "case_terminal")

    async def test_role_apply_compensates_when_case_resolves_during_discord_add(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(id=appended.case.user_id, roles=[])

                async def add_role(added, **kwargs):
                    member.roles.append(added)
                    lease = cog._case_store.claim_resolution(
                        appended.case.case_id, now + timedelta(seconds=1)
                    )
                    self.assertTrue(
                        cog._case_store.finish_resolution(
                            lease,
                            honeypot.CaseStatus.EXPIRED,
                            "expired",
                            None,
                            now + timedelta(seconds=1),
                        )
                    )

                async def remove_role(removed, **kwargs):
                    member.roles.remove(removed)

                member.add_roles = mock.AsyncMock(side_effect=add_role)
                member.remove_roles = mock.AsyncMock(side_effect=remove_role)
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                cog.bot.get_guild = lambda guild_id: guild
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )

                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(operation.operation_id, now), now
                )

                self.assertNotIn(role, member.roles)
                member.remove_roles.assert_awaited_once()
                snapshot = cog._case_store.get_case(appended.case.case_id)
                release = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "role_release"
                )
                self.assertEqual(release.status.value, "succeeded")

    async def test_review_role_ownership_hands_off_to_the_next_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                first = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(id=first.case.user_id, roles=[])

                async def add_role(*args, **kwargs):
                    member.roles.append(role)

                async def remove_role(*args, **kwargs):
                    member.roles.remove(role)

                member.add_roles = mock.AsyncMock(side_effect=add_role)
                member.remove_roles = mock.AsyncMock(side_effect=remove_role)
                guild = SimpleNamespace(
                    id=first.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                cog.bot.get_guild = lambda guild_id: guild
                first_apply = cog._case_store.ensure_operation(
                    first.case.case_id,
                    "role_apply",
                    f"role-apply:{first.case.case_id}:{role.id}",
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(first_apply.operation_id, now), now
                )
                lease = cog._case_store.claim_resolution(
                    first.case.case_id, now + timedelta(seconds=1)
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.EXPIRED,
                        "expired",
                        None,
                        now + timedelta(seconds=1),
                        final_operations=(
                            (
                                "role_release",
                                f"role-release:{first.case.case_id}:{role.id}",
                            ),
                        ),
                    )
                )
                second = self._append_case(
                    honeypot, cog, now + timedelta(seconds=2), message_id=41
                )
                second_apply = cog._case_store.ensure_operation(
                    second.case.case_id,
                    "role_apply",
                    f"role-apply:{second.case.case_id}:{role.id}",
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(
                        second_apply.operation_id, now + timedelta(seconds=2)
                    ),
                    now + timedelta(seconds=2),
                )

                self.assertEqual(
                    cog._case_store.owned_role_ids(second.case.case_id), (role.id,)
                )
                release = next(
                    item
                    for item in cog._case_store.get_case(first.case.case_id).operations
                    if item.operation_type == "role_release"
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(
                        release.operation_id, now + timedelta(seconds=3)
                    ),
                    now + timedelta(seconds=3),
                )
                member.remove_roles.assert_not_awaited()
                self.assertIn(role, member.roles)

    async def test_role_release_cannot_remove_role_after_handoff_race(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                first = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(id=first.case.user_id, roles=[])

                async def add_role(added, **kwargs):
                    if added not in member.roles:
                        member.roles.append(added)

                member.add_roles = mock.AsyncMock(side_effect=add_role)
                member.remove_roles = mock.AsyncMock()
                guild = SimpleNamespace(
                    id=first.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                cog.bot.get_guild = lambda guild_id: guild
                first_apply = cog._case_store.ensure_operation(
                    first.case.case_id,
                    "role_apply",
                    f"role-apply:{first.case.case_id}:{role.id}",
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(first_apply.operation_id, now), now
                )
                lease = cog._case_store.claim_resolution(
                    first.case.case_id, now + timedelta(seconds=1)
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.EXPIRED,
                        "expired",
                        None,
                        now + timedelta(seconds=1),
                        final_operations=((
                            "role_release",
                            f"role-release:{first.case.case_id}:{role.id}",
                        ),),
                    )
                )
                release = next(
                    item
                    for item in cog._case_store.get_case(first.case.case_id).operations
                    if item.operation_type == "role_release"
                )
                release_started = asyncio.Event()
                allow_release = asyncio.Event()

                async def blocked_remove(*args, **kwargs):
                    release_started.set()
                    await allow_release.wait()
                    if role in member.roles:
                        member.roles.remove(role)
                    return True

                cog._remove_review_mute_role = blocked_remove
                release_worker = asyncio.create_task(
                    cog._execute_detection_case_operation(
                        cog._case_store.claim_operation(
                            release.operation_id, now + timedelta(seconds=2)
                        ),
                        now + timedelta(seconds=2),
                    )
                )
                await asyncio.wait_for(release_started.wait(), timeout=1)

                second = self._append_case(
                    honeypot, cog, now + timedelta(seconds=3), message_id=41
                )
                second_apply = cog._case_store.ensure_operation(
                    second.case.case_id,
                    "role_apply",
                    f"role-apply:{second.case.case_id}:{role.id}",
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(
                        second_apply.operation_id, now + timedelta(seconds=3)
                    ),
                    now + timedelta(seconds=3),
                )
                allow_release.set()
                await release_worker

                pending = next(
                    item
                    for item in cog._case_store.get_case(second.case.case_id).operations
                    if item.operation_id == second_apply.operation_id
                )
                self.assertEqual(pending.status.value, "failed")
                retry = cog._case_store.claim_operation(
                    second_apply.operation_id,
                    pending.retry_at + timedelta(seconds=1),
                )
                await cog._execute_detection_case_operation(retry, pending.retry_at)

                self.assertIn(role, member.roles)
                self.assertEqual(
                    cog._case_store.owned_role_ids(second.case.case_id), (role.id,)
                )

    async def test_case_projection_warns_about_failed_required_operations(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
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

    async def test_case_ignore_control_resolves_without_image_decisions(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = self._config({})
                guild = SimpleNamespace(id=10)
                cog.bot.get_guild = lambda guild_id: guild
                cog._increment_stat = mock.AsyncMock()
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                honeypot.detection._activate_forward_purge(
                    cog,
                    appended.case.guild_id,
                    appended.case.user_id,
                    60,
                )
                self.assertTrue(
                    cog._is_forward_purge_active(
                        appended.case.guild_id, appended.case.user_id
                    )
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(
                            manage_messages=True, ban_members=False
                        ),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        is_done=lambda: True,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ignore"
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "moderator_ignore"
                )
                interaction.response.defer.assert_not_awaited()
                self.assertEqual(snapshot.case.status.value, "resolved")
                self.assertEqual(snapshot.case.resolution, "ignore")
                self.assertEqual(operation.actor_id, interaction.user.id)
                self.assertEqual(snapshot.case.moderator_id, interaction.user.id)
                self.assertEqual(
                    [item.learning_decision for item in snapshot.attachments], []
                )
                self.assertNotIn(
                    "moderator_action",
                    {item.operation_type for item in snapshot.operations},
                )
                self.assertFalse(
                    cog._is_forward_purge_active(
                        appended.case.guild_id, appended.case.user_id
                    )
                )
                cog._increment_stat.assert_awaited_once_with(guild, "ignored")

    async def test_case_ignore_keeps_captured_image_review_open(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = self._config({})
                cog._case_store.initialize()
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=datetime.now(timezone.utc),
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
                )
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        Path(directory) / "proof.png",
                    )
                )
                cog._execute_detection_case_operation = mock.AsyncMock()
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        is_done=lambda: True,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ignore"
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                projection = honeypot.render_case(snapshot)
                self.assertEqual(snapshot.case.status.value, "resolving")
                self.assertIsNone(snapshot.case.resolution)
                self.assertIsNone(snapshot.attachments[0].learning_decision)
                self.assertEqual(projection.moderation_actions, ())
                honeypot.DetectionCaseView.add_item = lambda view, item: setattr(
                    view, "children", getattr(view, "children", []) + [item]
                )
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                view = honeypot.DetectionCaseView(
                    cog,
                    appended.case.case_id,
                    has_image_feedback=True,
                    moderation_actions=projection.moderation_actions,
                    feedback_items=honeypot.case_feedback_items(snapshot),
                )
                custom_ids = {item.custom_id for item in view.children}
                self.assertFalse(any(":moderate:" in item for item in custom_ids))
                self.assertTrue(any(":resolve:" in item for item in custom_ids))
                self.assertTrue(any(":images:" in item for item in custom_ids))

                restarted = honeypot.Honeypot(_Bot())
                restarted.config = self._config({})
                restarted._execute_detection_case_operation = mock.AsyncMock()

                await restarted._case_review_attachment_interaction(
                    interaction,
                    honeypot.AttachmentKey(
                        appended.case.case_id, appended.message.sequence, 0
                    ),
                    "tp",
                )

                snapshot = restarted._case_store.get_case(appended.case.case_id)
                self.assertEqual(snapshot.case.status.value, "resolved")
                self.assertEqual(snapshot.case.resolution, "ignore")
                self.assertEqual(
                    snapshot.attachments[0].learning_decision, "true_positive"
                )

    async def test_case_ignore_releases_owned_role_before_image_review_finishes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(
                    id=20,
                    roles=[role],
                    remove_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"mute_role": role.id})
                cog._increment_stat = mock.AsyncMock()
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                cog._case_store.initialize()
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=datetime.now(timezone.utc),
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
                )
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        Path(directory) / "proof.png",
                    )
                )
                owned_at = datetime.now(timezone.utc)
                ownership = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                ownership = cog._case_store.claim_operation(
                    ownership.operation_id, owned_at
                )
                cog._case_store.start_operation_effect(
                    ownership.operation_id, ownership.claim_token, owned_at
                )
                self.assertEqual(
                    cog._case_store.record_operation_role_ownership(
                        ownership.operation_id,
                        ownership.claim_token,
                        appended.case.case_id,
                        guild.id,
                        member.id,
                        role_id=role.id,
                        now=owned_at,
                    ),
                    "owned",
                )
                cog._case_store.complete_operation(
                    ownership.operation_id,
                    ownership.claim_token,
                    owned_at,
                    "applied",
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        is_done=lambda: True,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ignore"
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                member.remove_roles.assert_awaited_once()
                self.assertEqual(snapshot.case.status.value, "resolving")
                self.assertIsNone(snapshot.attachments[0].learning_decision)
                release = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "role_release"
                )
                self.assertEqual(release.status.value, "succeeded")
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), ()
                )

    async def test_role_release_treats_departed_member_as_completed(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: None,
                    fetch_member=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("departed")
                    ),
                    get_role=lambda role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({})
                appended = self._append_case(honeypot, cog, now)
                ownership = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                ownership = cog._case_store.claim_operation(
                    ownership.operation_id,
                    now,
                )
                cog._case_store.start_operation_effect(
                    ownership.operation_id,
                    ownership.claim_token,
                    now,
                )
                self.assertEqual(
                    cog._case_store.record_operation_role_ownership(
                        ownership.operation_id,
                        ownership.claim_token,
                        appended.case.case_id,
                        guild.id,
                        appended.case.user_id,
                        role_id=role.id,
                        now=now,
                    ),
                    "owned",
                )
                cog._case_store.complete_operation(
                    ownership.operation_id,
                    ownership.claim_token,
                    now,
                    "applied",
                )
                release = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_release",
                    f"role-release:{appended.case.case_id}:{role.id}",
                )
                release = cog._case_store.claim_operation(
                    release.operation_id,
                    now,
                )

                await cog._execute_detection_case_operation(release, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == release.operation_id
                )
                self.assertEqual(persisted.status.value, "succeeded")
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id),
                    (),
                )

    async def test_ignore_records_moderation_while_attachment_capture_finishes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime.now(timezone.utc)
                appended = cog._case_store.append_message(
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
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        is_done=lambda: True,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ignore"
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                self.assertEqual(snapshot.case.status.value, "resolving")
                self.assertIsNone(snapshot.case.resolution)
                self.assertIn(
                    "moderator_ignore",
                    {item.operation_type for item in snapshot.operations},
                )

    async def test_bulk_tp_interaction_ignores_captured_pdf_evidence(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=datetime.now(timezone.utc),
                        jump_url="https://discord.test/messages/40",
                        attachments=(
                            honeypot.NewAttachment(
                                0, "proof.png", 10, "image/png", None, None, "png-url"
                            ),
                            honeypot.NewAttachment(
                                1, "invoice.pdf", 10, "application/pdf", None, None, "pdf-url"
                            ),
                        ),
                    ),
                    (),
                )
                for position, filename in enumerate(("proof.png", "invoice.pdf")):
                    self.assertTrue(
                        capture_attachment(
                            cog._case_store,
                            appended.case.case_id,
                            appended.message.sequence,
                            position,
                            Path(directory) / filename,
                        )
                    )
                cog._execute_detection_case_operation = mock.AsyncMock()
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        send_message=mock.AsyncMock(),
                        is_done=lambda: False,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_bulk_interaction(
                    interaction, appended.case.case_id, "tp"
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                self.assertTrue(
                    all(
                        attachment.learning_decision is None
                        for attachment in snapshot.attachments
                    )
                )
                interaction.response.send_message.assert_awaited_once()

                completed = await cog._case_review_bulk_interaction(
                    interaction,
                    appended.case.case_id,
                    "tp",
                    confirmed=True,
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                decisions = {
                    attachment.filename: attachment.learning_decision
                    for attachment in snapshot.attachments
                }
                self.assertTrue(completed)
                self.assertEqual(decisions["proof.png"], "true_positive")
                self.assertIsNone(decisions["invoice.pdf"])

    async def test_case_ban_control_executes_one_durable_action(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                member = SimpleNamespace(id=20, ban=mock.AsyncMock(), roles=[])
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                bot.loop = asyncio.get_running_loop()
                cog = honeypot.Honeypot(bot)
                config = {"dry_run": False}
                cog.config = self._config(config)
                cog.config.guild = lambda target_guild: SimpleNamespace(
                    all=mock.AsyncMock(return_value=config)
                )
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._ban_delete_message_seconds = mock.Mock(return_value=0)
                cog._increment_stat = mock.AsyncMock()
                cog._cached_purge_user_messages = mock.AsyncMock(return_value=0)
                honeypot.modlog.create_case = mock.AsyncMock()
                honeypot.detection.POST_BAN_SWEEP_DELAY_SECONDS = 0
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(
                            manage_messages=True, ban_members=True
                        ),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(), is_done=lambda: True
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ban"
                )
                await asyncio.gather(*cog._post_ban_sweep_tasks)
                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ban"
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "moderator_ban"
                )
                member.ban.assert_awaited_once()
                cog._cached_purge_user_messages.assert_awaited_once_with(
                    guild,
                    member.id,
                    honeypot.GuildSettings.from_mapping(config),
                )
                cog._increment_stat.assert_any_await(guild, "banned")
                honeypot.modlog.create_case.assert_awaited_once()
                self.assertEqual(operation.status.value, "succeeded")
                self.assertEqual(operation.result, "ban")
                self.assertEqual(operation.attempts, 1)
                self.assertEqual(snapshot.case.status.value, "resolved")
                self.assertEqual(snapshot.case.resolution, "ban")
                self.assertEqual(snapshot.case.moderator_id, 99)

    async def test_moderator_ban_requires_confirmation_for_unreviewed_attachment(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                member = SimpleNamespace(id=20, roles=[], ban=mock.AsyncMock())
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog._case_store.initialize()
                cog.config = self._config({"dry_run": True})
                honeypot.DetectionModerationConfirmationView.add_item = (
                    lambda view, item: setattr(
                        view, "children", getattr(view, "children", []) + [item]
                    )
                )
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                now = datetime.now(timezone.utc)
                appended = cog._case_store.append_message(
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
                )
                response = SimpleNamespace(
                    defer=mock.AsyncMock(),
                    send_message=mock.AsyncMock(),
                    is_done=lambda: False,
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(ban_members=True),
                    ),
                    response=response,
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ban"
                )

                response.defer.assert_not_awaited()
                response.send_message.assert_awaited_once()
                self.assertTrue(response.send_message.await_args.kwargs["ephemeral"])
                confirmation = response.send_message.await_args.kwargs["view"]
                self.assertEqual(
                    [item.label for item in confirmation.children],
                    ["Confirm Ban"],
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                self.assertEqual(snapshot.case.status.value, "pending")
                self.assertEqual(snapshot.operations, ())

                confirmation_response_done = False

                async def defer_confirmation():
                    nonlocal confirmation_response_done
                    confirmation_response_done = True

                confirmation_interaction = SimpleNamespace(
                    user=interaction.user,
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer_confirmation),
                        is_done=lambda: confirmation_response_done,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(),
                )
                await confirmation.children[0].callback(confirmation_interaction)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "moderator_ban"
                )
                confirmation_interaction.response.defer.assert_awaited_once()
                confirmation_interaction.delete_original_response.assert_awaited_once_with()
                self.assertEqual(operation.status.value, "succeeded")
                self.assertEqual(operation.result, "planned_ban")

    async def test_bulk_confirmation_dismisses_prompt_before_action_finishes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                honeypot.DetectionBulkConfirmationView.add_item = (
                    lambda view, item: setattr(
                        view, "children", getattr(view, "children", []) + [item]
                    )
                )
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                cog = honeypot.Honeypot(_Bot())
                action_started = asyncio.Event()
                release_action = asyncio.Event()
                order = []

                async def run_action(*args, **kwargs):
                    order.append("action")
                    action_started.set()
                    await release_action.wait()
                    return False

                cog._case_review_bulk_interaction = mock.AsyncMock(
                    side_effect=run_action
                )
                view = honeypot.DetectionBulkConfirmationView(
                    cog,
                    "case-1",
                    "tp",
                )
                response_done = False

                async def defer():
                    nonlocal response_done
                    order.append("defer")
                    response_done = True

                async def delete_original_response():
                    order.append("delete")

                interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(
                        side_effect=delete_original_response
                    ),
                )

                callback = asyncio.create_task(
                    view.children[0].callback(interaction)
                )
                await action_started.wait()

                interaction.delete_original_response.assert_awaited_once_with()
                self.assertEqual(order, ["defer", "delete", "action"])
                release_action.set()
                await callback

    async def test_classification_returns_before_final_operations_finish(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        Path(directory) / "proof.png",
                    )
                )
                moderation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "moderator_ban",
                    f"moderator-ban:{appended.case.case_id}",
                    actor_id=99,
                )
                claimed_moderation = cog._case_store.claim_operation(
                    moderation.operation_id,
                    now,
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        claimed_moderation.operation_id,
                        claimed_moderation.claim_token,
                        now,
                        "ban",
                    )
                )
                final_operation_started = asyncio.Event()
                release_final_operation = asyncio.Event()

                async def block_final_operation(*_args, **_kwargs):
                    final_operation_started.set()
                    await release_final_operation.wait()

                cog._execute_detection_case_operation = mock.AsyncMock(
                    side_effect=block_final_operation
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        is_done=lambda: False,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                interaction_task = asyncio.create_task(
                    cog._case_review_bulk_interaction(
                        interaction,
                        appended.case.case_id,
                        "tp",
                        confirmed=True,
                        expected_keys=(
                            honeypot.AttachmentKey(
                                appended.case.case_id,
                                appended.message.sequence,
                                0,
                            ),
                        ),
                    )
                )
                await final_operation_started.wait()
                try:
                    try:
                        completed = await asyncio.wait_for(
                            asyncio.shield(interaction_task),
                            timeout=0.05,
                        )
                    except TimeoutError:
                        self.fail(
                            "classification interaction waited for final operations"
                        )
                    self.assertTrue(completed)
                    snapshot = cog._case_store.get_case(appended.case.case_id)
                    self.assertEqual(snapshot.case.status.value, "resolved")
                    self.assertEqual(
                        snapshot.attachments[0].learning_decision,
                        "true_positive",
                    )
                    self.assertTrue(
                        any(
                            operation.operation_type == "review_update"
                            for operation in snapshot.operations
                        )
                    )
                finally:
                    release_final_operation.set()
                    await interaction_task
                    pending = tuple(getattr(cog, "_case_review_tasks", ()))
                    if pending:
                        await asyncio.gather(*pending)

    async def test_automatic_ban_does_not_inherit_image_reviewer_attribution(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        Path(directory) / "proof.png",
                    )
                )
                automatic_ban = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "moderation_action",
                    f"moderation-action:{appended.case.case_id}:1",
                    message_sequence=appended.message.sequence,
                )
                claimed_ban = cog._case_store.claim_operation(
                    automatic_ban.operation_id,
                    now,
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        claimed_ban.operation_id,
                        claimed_ban.claim_token,
                        now,
                        "ban",
                    )
                )
                cog._schedule_case_review_followup = mock.Mock()
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=999,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        is_done=lambda: False,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                completed = await cog._case_review_bulk_interaction(
                    interaction,
                    appended.case.case_id,
                    "tp",
                    confirmed=True,
                    expected_keys=(
                        honeypot.AttachmentKey(
                            appended.case.case_id,
                            appended.message.sequence,
                            0,
                        ),
                    ),
                )

                self.assertTrue(completed)
                snapshot = cog._case_store.get_case(appended.case.case_id)
                self.assertEqual(snapshot.case.status.value, "resolved")
                self.assertEqual(snapshot.case.resolution, "ban")
                resolved = cog._case_store.get_case(appended.case.case_id)
                self.assertIsNone(resolved.case.moderator_id)
                self.assertEqual(
                    snapshot.attachments[0].learning_decision,
                    "true_positive",
                )
                self.assertEqual(
                    snapshot.attachments[0].learning_metadata["moderator_id"],
                    999,
                )

    async def test_dismissed_confirmation_reports_failure_in_new_ephemeral_message(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                honeypot.DetectionBulkConfirmationView.add_item = (
                    lambda view, item: setattr(
                        view, "children", getattr(view, "children", []) + [item]
                    )
                )
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                cog = honeypot.Honeypot(_Bot())
                view = honeypot.DetectionBulkConfirmationView(
                    cog,
                    "case-1",
                    "tp",
                )
                response_done = False

                async def defer():
                    nonlocal response_done
                    response_done = True

                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(
                            manage_messages=False,
                            moderate_members=False,
                            ban_members=False,
                            kick_members=False,
                        ),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(),
                )

                await view.children[0].callback(interaction)

                interaction.delete_original_response.assert_awaited_once_with()
                interaction.followup.send.assert_awaited_once_with(
                    "You do not have permission to review this case.",
                    ephemeral=True,
                )

    async def test_moderation_confirmation_dismisses_prompt_before_action_finishes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                honeypot.DetectionModerationConfirmationView.add_item = (
                    lambda view, item: setattr(
                        view, "children", getattr(view, "children", []) + [item]
                    )
                )
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                cog = honeypot.Honeypot(_Bot())
                action_started = asyncio.Event()
                release_action = asyncio.Event()
                order = []

                async def run_action(*args, **kwargs):
                    order.append("action")
                    action_started.set()
                    await release_action.wait()
                    return True

                cog._case_review_moderation_interaction = mock.AsyncMock(
                    side_effect=run_action
                )
                view = honeypot.DetectionModerationConfirmationView(
                    cog,
                    "case-1",
                    "ban",
                )
                response_done = False

                async def defer():
                    nonlocal response_done
                    order.append("defer")
                    response_done = True

                async def delete_original_response():
                    order.append("delete")

                interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(
                        side_effect=delete_original_response
                    ),
                )

                callback = asyncio.create_task(
                    view.children[0].callback(interaction)
                )
                await action_started.wait()

                interaction.delete_original_response.assert_awaited_once_with()
                self.assertEqual(order, ["defer", "delete", "action"])
                release_action.set()
                await callback

    async def test_dry_run_moderator_ban_is_persisted_as_planned(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                member = SimpleNamespace(id=20, ban=mock.AsyncMock(), roles=[])
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"dry_run": True})
                cog._execute_action = mock.AsyncMock(
                    side_effect=AssertionError("dry-run must not execute punishment")
                )
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(
                            manage_messages=True, ban_members=True
                        ),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(), is_done=lambda: True
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ban"
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                operation = next(
                    item for item in snapshot.operations
                    if item.operation_type == "moderator_ban"
                )
                member.ban.assert_not_awaited()
                cog._execute_action.assert_not_awaited()
                self.assertEqual(operation.status.value, "succeeded")
                self.assertEqual(operation.result, "planned_ban")
                self.assertEqual(snapshot.case.resolution, "planned_ban")
                self.assertEqual(
                    honeypot.render_case(snapshot).moderation_status,
                    "Ban planned (dry run)",
                )

    async def test_moderator_ban_uses_persisted_ids_when_member_cache_misses(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                target = SimpleNamespace(id=20)
                actor = SimpleNamespace(id=99)
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: None,
                    ban=mock.AsyncMock(),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                bot.fetch_user = mock.AsyncMock(
                    side_effect=lambda user_id: target if user_id == 20 else actor
                )
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"dry_run": False})
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._ban_delete_message_seconds = mock.Mock(return_value=0)
                cog._schedule_post_ban_sweep = mock.Mock()
                cog._increment_stat = mock.AsyncMock()
                honeypot.modlog.create_case = mock.AsyncMock()
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=actor.id,
                        guild_permissions=SimpleNamespace(
                            manage_messages=False,
                            ban_members=True,
                            kick_members=False,
                        ),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(), is_done=lambda: True
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ban"
                )

                guild.ban.assert_awaited_once()
                self.assertIs(guild.ban.await_args.args[0], target)
                self.assertEqual(
                    honeypot.modlog.create_case.await_args.kwargs["moderator"].id,
                    actor.id,
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "moderator_ban"
                )
                self.assertEqual(operation.status.value, "succeeded")
                self.assertEqual(operation.result, "ban")
                self.assertEqual(snapshot.case.status.value, "resolved")
                self.assertEqual(snapshot.case.resolution, "ban")
                self.assertEqual(snapshot.case.moderator_id, actor.id)

    async def test_moderator_kick_missing_member_finishes_with_explicit_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: None,
                    fetch_member=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("member left")
                    ),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"dry_run": False})
                cog._execute_action = mock.AsyncMock(
                    side_effect=AssertionError("missing member cannot be kicked")
                )
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(
                            manage_messages=False,
                            ban_members=False,
                            kick_members=True,
                        ),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(), is_done=lambda: True
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "kick"
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "moderator_kick"
                )
                guild.fetch_member.assert_awaited_once_with(snapshot.case.user_id)
                cog._execute_action.assert_not_awaited()
                self.assertEqual(operation.status.value, "succeeded")
                self.assertEqual(operation.result, "kick_missing")
                self.assertEqual(snapshot.case.status.value, "resolved")
                self.assertEqual(snapshot.case.resolution, "kick")
                self.assertEqual(snapshot.case.moderator_id, 99)

    async def test_moderator_actor_survives_failed_action_retry_and_resolution(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                target = SimpleNamespace(id=20, roles=[])
                actor = SimpleNamespace(
                    id=99,
                    guild_permissions=SimpleNamespace(
                        manage_messages=False,
                        ban_members=True,
                        kick_members=False,
                    ),
                )
                members = {target.id: target, actor.id: actor}
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=members.get,
                    fetch_ban=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("not banned")
                    ),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"dry_run": False})
                cog._execute_action = mock.AsyncMock(
                    side_effect=[("ban", "temporary failure"), ("ban", None)]
                )
                cog._case_review_rerender = mock.AsyncMock()
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                interaction = SimpleNamespace(
                    user=actor,
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(), is_done=lambda: True
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ban"
                )

                failed_snapshot = cog._case_store.get_case(appended.case.case_id)
                failed = next(
                    item
                    for item in failed_snapshot.operations
                    if item.operation_type == "moderator_ban"
                )
                self.assertEqual(failed.status.value, "failed")
                self.assertEqual(failed.actor_id, 99)
                self.assertEqual(failed_snapshot.case.status.value, "resolving")
                claimed = cog._case_store.claim_operation(
                    failed.operation_id, failed.retry_at
                )
                await cog._execute_detection_case_operation(claimed, failed.retry_at)

                resolved = cog._case_store.get_case(appended.case.case_id)
                retried = next(
                    item
                    for item in resolved.operations
                    if item.operation_type == "moderator_ban"
                )
                self.assertEqual(retried.status.value, "succeeded")
                self.assertEqual(retried.attempts, 2)
                self.assertEqual(retried.actor_id, 99)
                self.assertEqual(resolved.case.status.value, "resolved")
                self.assertEqual(resolved.case.resolution, "ban")
                self.assertEqual(resolved.case.moderator_id, 99)
                self.assertEqual(cog._execute_action.await_count, 2)
                self.assertTrue(
                    all(
                        call.kwargs["moderator"] is actor
                        for call in cog._execute_action.await_args_list
                    )
                )

    async def test_moderator_ban_intent_fences_concurrent_ignore(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                ban_started = asyncio.Event()
                release_ban = asyncio.Event()

                async def blocked_ban(**kwargs):
                    ban_started.set()
                    await release_ban.wait()

                member = SimpleNamespace(
                    id=20,
                    ban=mock.AsyncMock(side_effect=blocked_ban),
                    roles=[],
                )
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"dry_run": False})
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._ban_delete_message_seconds = mock.Mock(return_value=0)
                cog._schedule_post_ban_sweep = mock.Mock()
                cog._increment_stat = mock.AsyncMock()
                cog._case_review_rerender = mock.AsyncMock()
                honeypot.modlog.create_case = mock.AsyncMock()
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(
                            manage_messages=False,
                            ban_members=True,
                            kick_members=False,
                        ),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(), is_done=lambda: True
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                action_task = asyncio.create_task(
                    cog._case_review_moderation_interaction(
                        interaction, appended.case.case_id, "ban"
                    )
                )
                await asyncio.wait_for(ban_started.wait(), timeout=1)
                ignored = await cog.resolve_detection_case(
                    appended.case.case_id, "ignore", moderator_id=100
                )
                release_ban.set()
                await action_task

                snapshot = cog._case_store.get_case(appended.case.case_id)
                self.assertFalse(ignored)
                member.ban.assert_awaited_once()
                self.assertEqual(snapshot.case.status.value, "resolved")
                self.assertEqual(snapshot.case.resolution, "ban")
                self.assertEqual(snapshot.case.moderator_id, 99)

    async def test_moderator_action_publishes_owned_state_before_discord_finishes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                action_started = asyncio.Event()
                release_action = asyncio.Event()

                async def blocked_action(*args, **kwargs):
                    action_started.set()
                    await release_action.wait()
                    return "ban", None

                member = SimpleNamespace(id=20, roles=[])
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: member,
                    fetch_ban=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("not banned")
                    ),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"dry_run": False})
                cog._execute_action = blocked_action
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                cog._case_store.update_message_delete(
                    appended.case.case_id,
                    appended.message.sequence,
                    honeypot.DeleteStatus.DELETED,
                    None,
                    False,
                )
                self.assertTrue(
                    publish_primary(
                        cog._case_store,
                        appended.case.case_id,
                        50,
                        60,
                    )
                )
                published = []

                async def publish_case(case_id, _config, _channel, **kwargs):
                    snapshot = await asyncio.to_thread(
                        cog._case_store.get_case, case_id
                    )
                    published.append(honeypot.render_case(snapshot))
                    return True

                cog._publish_detection_case = publish_case
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        is_done=lambda: True,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                task = asyncio.create_task(
                    cog._case_review_moderation_interaction(
                        interaction,
                        appended.case.case_id,
                        "ban",
                    )
                )
                await asyncio.wait_for(action_started.wait(), timeout=1)
                try:
                    self.assertTrue(published)
                    self.assertEqual(published[-1].moderation_status, "Action pending")
                    self.assertEqual(published[-1].moderation_actions, ())
                finally:
                    release_action.set()
                    await task
                final = cog._case_store.get_case(appended.case.case_id)
                self.assertEqual(final.case.status.value, "resolved")
                self.assertTrue(
                    any(projection.resolution == "ban" for projection in published)
                )

    async def test_reconciliation_completes_started_moderator_ban_without_repeating_it(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                old = datetime.now(timezone.utc) - timedelta(minutes=10)
                member = SimpleNamespace(id=20, ban=mock.AsyncMock(), roles=[])
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: member,
                    fetch_ban=mock.AsyncMock(
                        return_value=SimpleNamespace(user=member)
                    ),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                first = honeypot.Honeypot(bot)
                appended = self._append_case(honeypot, first, old)
                operation = first._case_store.claim_moderator_action(
                    appended.case.case_id, "ban", 99, old
                )
                claimed = first._case_store.claim_operation(
                    operation.operation_id, old
                )
                self.assertTrue(
                    first._case_store.start_operation_effect(
                        claimed.operation_id, claimed.claim_token, old
                    )
                )

                restarted = honeypot.Honeypot(bot)
                restarted.config = self._config({"dry_run": False})
                restarted._execute_action = mock.AsyncMock(
                    side_effect=AssertionError("ban effect must not repeat")
                )

                await restarted._run_detection_reconciliation()

                resolved = restarted._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in resolved.operations
                    if item.operation_id == operation.operation_id
                )
                guild.fetch_ban.assert_awaited_once()
                self.assertEqual(guild.fetch_ban.await_args.args[0].id, member.id)
                restarted._execute_action.assert_not_awaited()
                member.ban.assert_not_awaited()
                self.assertEqual(persisted.status.value, "succeeded")
                self.assertEqual(persisted.result, "ban")
                self.assertEqual(persisted.attempts, 2)
                self.assertEqual(resolved.case.status.value, "resolved")
                self.assertEqual(resolved.case.resolution, "ban")
                self.assertEqual(resolved.case.moderator_id, 99)

    async def test_started_moderator_effect_waits_for_late_evidence_and_containment(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                first = honeypot.Honeypot(_Bot())
                second = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, first, now)
                operation = first._case_store.claim_moderator_action(
                    appended.case.case_id, "ban", 99, now
                )
                claimed = first._case_store.claim_operation(operation.operation_id, now)
                self.assertTrue(
                    first._case_store.start_operation_effect(
                        claimed.operation_id, claimed.claim_token, now
                    )
                )

                late = second._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=31,
                        message_id=41,
                        content="late evidence",
                        created_at=now + timedelta(seconds=1),
                        jump_url="https://discord.test/messages/41",
                        attachments=(
                            honeypot.NewAttachment(
                                0, "late.png", 8, "image/png", None, None, "late-url"
                            ),
                        ),
                    ),
                    (
                        honeypot.DetectionSignal(
                            "spam", "late signal", honeypot.ActionIntent.REVIEW, True, {}
                        ),
                    ),
                )
                cached = second._case_store.ensure_operation(
                    late.case.case_id,
                    "cached_purge",
                    f"cached-purge:{late.case.case_id}:31:39",
                    late.message.sequence,
                )
                reservation = second._case_store.reserve_attachment_capture(
                    late.case.case_id,
                    late.message.sequence,
                    0,
                    8,
                    now + timedelta(seconds=1),
                    stale_before=now - timedelta(minutes=5),
                    max_attachment_bytes=1024,
                    max_case_bytes=2048,
                )
                self.assertEqual(reservation.status, "claimed")

                self.assertTrue(
                    first._case_store.complete_moderator_action(
                        claimed.operation_id, claimed.claim_token, now + timedelta(seconds=2), "ban"
                    )
                )
                waiting = first._case_store.get_case(appended.case.case_id)
                self.assertEqual(waiting.case.status.value, "resolving")
                self.assertEqual(
                    next(
                        item for item in waiting.operations
                        if item.operation_id == claimed.operation_id
                    ).status.value,
                    "succeeded",
                )

                evidence_path = str(Path(directory) / "late.png")
                self.assertEqual(
                    second._case_store.complete_attachment_capture(
                        late.case.case_id,
                        late.message.sequence,
                        0,
                        reservation.claim_token,
                        8,
                        evidence_path=evidence_path,
                        now=now + timedelta(seconds=3),
                        max_attachment_bytes=1024,
                        max_case_bytes=2048,
                    ),
                    "captured",
                )
                self.assertTrue(
                    second._case_store.update_message_delete(
                        late.case.case_id,
                        late.message.sequence,
                        honeypot.DeleteStatus.DELETED,
                        None,
                        False,
                    )
                )
                cached_claim = second._case_store.claim_operation(
                    cached.operation_id, now + timedelta(seconds=3)
                )
                self.assertTrue(
                    second._case_store.complete_operation(
                        cached_claim.operation_id,
                        cached_claim.claim_token,
                        now + timedelta(seconds=3),
                        "deleted",
                    )
                )

                self.assertEqual(
                    second._case_store.reconcile_moderator_actions(
                        now + timedelta(seconds=4)
                    ),
                    (),
                )
                waiting = second._case_store.get_case(appended.case.case_id)
                self.assertEqual(waiting.case.status.value, "resolving")
                self.assertIsNone(waiting.attachments[0].learning_decision)

                self.assertTrue(
                    second._case_store.apply_attachment_decisions(
                        appended.case.case_id,
                        {waiting.attachments[0].key: "true_positive"},
                        99,
                        now + timedelta(seconds=4),
                    )
                )
                self.assertEqual(
                    second._case_store.reconcile_moderator_actions(
                        now + timedelta(seconds=5)
                    ),
                    (appended.case.case_id,),
                )

                resolved = second._case_store.get_case(appended.case.case_id)
                self.assertEqual(resolved.case.status.value, "resolved")
                self.assertEqual(resolved.case.resolution, "ban")
                self.assertEqual(resolved.attachments[0].evidence_path, evidence_path)
                self.assertIn(
                    "evidence_cleanup", {item.operation_type for item in resolved.operations}
                )

    async def test_case_view_keeps_moderation_and_image_controls_separate(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                matched = honeypot.CaseFeedbackItem(
                    honeypot.AttachmentKey("case-1", 1, 0),
                    "proof.png",
                    None,
                    True,
                )

                view = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=True,
                    feedback_items=(matched,),
                )

                self.assertEqual(
                    [item.label for item in view.children],
                    [
                        "Ban",
                        "Kick",
                        "Ignore",
                        "All TP",
                        "All FP",
                        "Ignore",
                        "Individual",
                    ],
                )

                after_ban = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=True,
                    feedback_items=(matched,),
                    moderation_actions=(),
                )
                self.assertEqual(
                    [item.label for item in after_ban.children],
                    ["All TP", "All FP", "Ignore", "Individual"],
                )

                after_classification = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=False,
                )
                self.assertEqual(
                    [item.label for item in after_classification.children],
                    ["Ban", "Kick", "Ignore"],
                )
                self.assertEqual(
                    [item.custom_id for item in view.children[:3]],
                    [
                        "honeypot:case:case-1:moderate:ban",
                        "honeypot:case:case-1:moderate:kick",
                        "honeypot:case:case-1:moderate:ignore",
                    ],
                )
                self.assertEqual(
                    [item.emoji for item in view.children[:3]],
                    ["🔨", "👢", "✅"],
                )
                self.assertEqual(
                    [item.style for item in view.children[:3]],
                    [
                        honeypot.discord.ButtonStyle.danger,
                        honeypot.discord.ButtonStyle.secondary,
                        honeypot.discord.ButtonStyle.success,
                    ],
                )

    async def test_message_view_only_offers_message_feedback(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)

                view = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=True,
                    message_sequence=2,
                    feedback_items=(
                        SimpleNamespace(detector_matched=True, decision=None),
                    ),
                )

                self.assertEqual(
                    [item.label for item in view.children],
                    ["All TP", "All FP", "Ignore", "Individual"],
                )

    async def test_unmatched_and_mixed_views_offer_add_without_fp(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                cog = honeypot.Honeypot(_Bot())
                cog._case_review_bulk_interaction = mock.AsyncMock()
                matched = SimpleNamespace(detector_matched=True, decision=None)
                unmatched = SimpleNamespace(detector_matched=False, decision=None)

                unmatched_view = honeypot.DetectionCaseView(
                    cog,
                    "case-1",
                    has_image_feedback=True,
                    feedback_items=(unmatched,),
                )
                mixed_view = honeypot.DetectionCaseView(
                    cog,
                    "case-2",
                    has_image_feedback=True,
                    feedback_items=(matched, unmatched),
                )

                self.assertEqual(
                    [item.label for item in unmatched_view.children[3:]],
                    ["Add all", "Ignore", "Individual"],
                )
                self.assertEqual(
                    [item.label for item in mixed_view.children[3:]],
                    ["Add all", "Ignore", "Individual"],
                )
                self.assertNotIn(
                    "All FP", [item.label for item in unmatched_view.children]
                )
                await unmatched_view.children[3].callback(SimpleNamespace())
                cog._case_review_bulk_interaction.assert_awaited_once_with(
                    mock.ANY, "case-1", "tp"
                )

    async def test_case_summary_represents_each_source_message_channel(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime(2026, 7, 24, 8, tzinfo=timezone.utc)
                first = cog._case_store.append_message(
                    honeypot.NewMessage(
                        10, 20, 30, 40, "first", now, None, ()
                    ),
                    (
                        honeypot.DetectionSignal(
                            "honeypot",
                            "Multiple image attachments: 4\nKnown suspicious image match",
                            honeypot.ActionIntent.REVIEW,
                            True,
                            {},
                        ),
                        honeypot.DetectionSignal(
                            "image",
                            "Initial image scan matched known suspicious content",
                            honeypot.ActionIntent.REVIEW,
                            True,
                            {},
                        ),
                        honeypot.DetectionSignal(
                            "spam",
                            "Repeated suspicious content",
                            honeypot.ActionIntent.REVIEW,
                            True,
                            {},
                        ),
                    ),
                )
                cog._case_store.append_message(
                    honeypot.NewMessage(
                        10,
                        20,
                        31,
                        41,
                        "second",
                        now + timedelta(seconds=3),
                        None,
                        (),
                    ),
                    (
                        honeypot.DetectionSignal(
                            "spam",
                            "Same message in 2 channels within 3s",
                            honeypot.ActionIntent.REVIEW,
                            True,
                            {},
                        ),
                    ),
                )

                snapshot = cog._case_store.get_case(first.case.case_id)
                projection = honeypot.render_case(snapshot)
                timeline = honeypot.render_timeline(snapshot)

                self.assertIn("Message 1 · <#30>", projection.description)
                self.assertIn("Message 2 · <#31>", projection.description)
                self.assertIn(
                    "Same message in 2 channels within 3s",
                    projection.description,
                )
                rendered_signals = "\n".join(projection.signal_lines)
                self.assertNotIn(
                    "Known suspicious image match",
                    rendered_signals,
                )
                self.assertEqual(
                    rendered_signals.count(
                        "Initial image scan matched known suspicious content"
                    ),
                    1,
                )
                self.assertEqual(
                    timeline.messages[0].signal_reasons,
                    (
                        "Multiple image attachments: 4",
                        "Initial image scan matched known suspicious content",
                        "Repeated suspicious content",
                    ),
                )
                self.assertIn(
                    "Signals:\n"
                    "Message 1 · <#30>:\n"
                    "Multiple image attachments: 4\n"
                    "Initial image scan matched known suspicious content (+1 more)\n"
                    "Message 2 · <#31>:\n"
                    "Same message in 2 channels within 3s",
                    projection.description,
                )

    async def test_identical_reasons_survive_across_source_messages(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime(2026, 7, 24, 8, tzinfo=timezone.utc)
                signal = lambda: honeypot.DetectionSignal(
                    "spam",
                    "Repeated suspicious content",
                    honeypot.ActionIntent.REVIEW,
                    True,
                    {},
                )
                first = cog._case_store.append_message(
                    honeypot.NewMessage(10, 20, 30, 40, "first", now, None, ()),
                    (signal(),),
                )
                cog._case_store.append_message(
                    honeypot.NewMessage(
                        10,
                        20,
                        31,
                        41,
                        "second",
                        now + timedelta(seconds=3),
                        None,
                        (),
                    ),
                    (signal(),),
                )

                projection = honeypot.render_case(
                    cog._case_store.get_case(first.case.case_id)
                )

                self.assertIn("Message 1 · <#30>", projection.description)
                self.assertIn("Message 2 · <#31>", projection.description)
                self.assertEqual(
                    sum(
                        "Repeated suspicious content" in line
                        for line in projection.signal_lines
                    ),
                    2,
                )

    async def test_completed_moderation_with_pending_image_is_awaiting_classification(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime(2026, 7, 24, 8, tzinfo=timezone.utc)
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
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        Path(directory) / "proof.png",
                    )
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "moderator_ban",
                    f"moderator-ban:{appended.case.case_id}",
                    actor_id=99,
                )
                claimed = cog._case_store.claim_operation(
                    operation.operation_id,
                    now,
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        claimed.operation_id,
                        claimed.claim_token,
                        now,
                        "ban",
                    )
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                projection = honeypot.render_case(snapshot)

                self.assertEqual(snapshot.case.status.value, "pending")
                self.assertIn("Status: Awaiting classification", projection.description)

    async def test_case_view_hides_individual_when_case_has_too_many_images(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)

                view = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=True,
                    allow_individual=False,
                )

                self.assertNotIn("Individual", [item.label for item in view.children])

    async def test_case_summary_warns_and_hides_individual_above_25_images(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                bot = _Bot()
                cog = honeypot.Honeypot(bot)
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime.now(timezone.utc)
                attachments = tuple(
                    honeypot.NewAttachment(
                        position,
                        f"proof-{position}.png",
                        5,
                        "image/png",
                        10,
                        10,
                        f"https://cdn.test/proof-{position}.png",
                    )
                    for position in range(26)
                )
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "evidence", now, None, attachments
                    ),
                    (),
                )
                for position in range(26):
                    evidence = data_path / f"proof-{position}.png"
                    evidence.write_bytes(b"image")
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        1,
                        position,
                        evidence,
                    )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                projection = honeypot.render_case(snapshot)
                visible = "\n".join(field.value for field in projection.fields)

                self.assertIn(
                    "Too many images for one menu\nReview them in the thread", visible
                )

                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                view = honeypot.DetectionCaseView(
                    cog,
                    appended.case.case_id,
                    has_image_feedback=True,
                    allow_individual=len(projection.feedback_items) <= 25,
                )
                self.assertNotIn("Individual", [item.label for item in view.children])

    def test_timeline_attachment_humanizes_decision_and_escapes_filename(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                attachment = SimpleNamespace(
                    capture_status="captured",
                    match_metadata={},
                    learning_decision="false_positive",
                    publication_error=None,
                    key=SimpleNamespace(position=0),
                    filename="[proof](https://evil.test).png",
                )

                line = honeypot.Honeypot._case_timeline_attachment_line(attachment)

                self.assertEqual(
                    line,
                    "- 1. `[proof](https://evil.test).png`\n  captured; False positive",
                )
                self.assertNotIn("decision:", line)

    async def test_individual_image_action_opens_dropdown_and_routes_selected_image(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime.now(timezone.utc)
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=now,
                        jump_url="https://discord.test/messages/40",
                        attachments=tuple(
                            honeypot.NewAttachment(
                                position,
                                filename,
                                5,
                                "image/png",
                                10,
                                10,
                                f"https://cdn.test/{filename}",
                            )
                            for position, filename in enumerate(
                                ("proof-one.png", "proof-two.png")
                            )
                        ),
                    ),
                    (),
                )
                for position, filename in enumerate(("proof-one.png", "proof-two.png")):
                    evidence = data_path / filename
                    evidence.write_bytes(b"image")
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        1,
                        position,
                        evidence,
                    )
                self.assertTrue(
                    cog._case_store.update_attachment_scan(
                        appended.case.case_id,
                        1,
                        0,
                        "sha",
                        "phash",
                        match_metadata={"matched": True},
                        error=None,
                    )
                )

                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                def remove_item(view, item):
                    view.children.remove(item)

                honeypot.DetectionIndividualView.add_item = add_item
                honeypot.DetectionIndividualView.remove_item = remove_item
                honeypot.discord.ui.Select = lambda **kwargs: SimpleNamespace(**kwargs)
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                honeypot.discord.SelectOption = lambda **kwargs: SimpleNamespace(**kwargs)
                cog._case_review_attachment_interaction = mock.AsyncMock()
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        send_message=mock.AsyncMock(),
                    ),
                )

                await cog._case_review_individual_prompt(
                    interaction, appended.case.case_id
                )

                interaction.response.defer.assert_not_awaited()
                interaction.response.send_message.assert_awaited_once()
                self.assertTrue(
                    interaction.response.send_message.await_args.kwargs["ephemeral"]
                )
                view = interaction.response.send_message.await_args.kwargs["view"]
                self.assertEqual(len(view.children), 1)
                selector = view.children[0]
                self.assertEqual(
                    [option.label for option in selector.options],
                    ["1.1 proof-one.png", "1.2 proof-two.png"],
                )

                selector.values = [selector.options[0].value]
                selection = SimpleNamespace(
                    response=SimpleNamespace(edit_message=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(),
                )
                await selector.callback(selection)
                selection.delete_original_response.assert_not_awaited()
                self.assertEqual(
                    [item.label for item in view.children[1:]],
                    ["TP", "FP", "Ignore"],
                )
                response_done = False

                async def defer():
                    nonlocal response_done
                    response_done = True

                action_interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    delete_original_response=mock.AsyncMock(),
                )
                await view.children[1].callback(action_interaction)
                action_interaction.delete_original_response.assert_awaited_once_with()
                cog._case_review_attachment_interaction.assert_awaited_with(
                    action_interaction,
                    honeypot.AttachmentKey(appended.case.case_id, 1, 0),
                    "tp",
                )

                selector.values = [selector.options[1].value]
                await selector.callback(selection)
                self.assertEqual(
                    [item.label for item in view.children[1:]],
                    ["Add", "Ignore"],
                )
                response_done = False
                action_interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    delete_original_response=mock.AsyncMock(),
                )
                await view.children[1].callback(action_interaction)
                action_interaction.delete_original_response.assert_awaited_once_with()
                cog._case_review_attachment_interaction.assert_awaited_with(
                    action_interaction,
                    honeypot.AttachmentKey(appended.case.case_id, 1, 1),
                    "tp",
                )
                self.assertEqual(
                    [getattr(option, "default", False) for option in selector.options],
                    [False, True],
                )

    async def test_manage_messages_can_use_case_ban_control(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                response = SimpleNamespace(
                    defer=mock.AsyncMock(),
                    send_message=mock.AsyncMock(),
                    is_done=lambda: False,
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(
                            manage_messages=True,
                            ban_members=False,
                            kick_members=False,
                        ),
                    ),
                    response=response,
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                cog._execute_detection_case_operation = mock.AsyncMock()
                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ban", confirmed=True
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                response.defer.assert_awaited_once()
                self.assertEqual(snapshot.case.status.value, "resolving")
                self.assertEqual(snapshot.operations[0].operation_type, "moderator_ban")

    async def test_moderate_members_can_ignore_and_classify_case_evidence(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        guild_permissions=SimpleNamespace(
                            moderate_members=True,
                            manage_messages=False,
                            ban_members=False,
                            kick_members=False,
                        )
                    )
                )

                self.assertTrue(honeypot.review_publication._case_review_has_permission(interaction))
                self.assertTrue(
                    honeypot.review_publication._case_review_has_action_permission(interaction, "ignore")
                )

    async def test_startup_expires_overdue_case_before_restoring_views(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                created_at = datetime.now(timezone.utc) - timedelta(hours=25)
                first_cog = honeypot.Honeypot(_Bot())
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

                restarted = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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
                first = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, first, datetime.now(timezone.utc))
                publish_primary(first._case_store, appended.case.case_id, 30, 77)

                bot = _Bot()
                restarted = honeypot.Honeypot(bot)
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
                cog = honeypot.Honeypot(_Bot())
                cog.config = self._config({"logs_channel": None, "review_channel": None})
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

    async def test_failed_role_release_creates_retry_operation(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)

                async def remove_roles(*args, **kwargs):
                    raise honeypot.discord.HTTPException()

                member = SimpleNamespace(
                    id=20, roles=[role], remove_roles=remove_roles
                )
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"mute_role": 55})
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
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
                operation = next(
                    item for item in snapshot.operations if item.operation_type == "role_release"
                )
                self.assertEqual(operation.status.value, "failed")
                self.assertIsNotNone(operation.retry_at)

    async def test_expiry_does_not_remove_preexisting_unowned_role(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(
                    id=20,
                    roles=[role],
                    remove_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"mute_role": 55})
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                claimed = cog._case_store.claim_operation(
                    operation.operation_id, datetime.now(timezone.utc)
                )
                await cog._execute_detection_case_operation(
                    claimed, datetime.now(timezone.utc)
                )
                await cog.resolve_detection_case(appended.case.case_id, "expired")
                snapshot = cog._case_store.get_case(appended.case.case_id)

                member.remove_roles.assert_not_awaited()
                self.assertNotIn(
                    "role_release",
                    {item.operation_type for item in snapshot.operations},
                )

    async def test_role_added_by_case_is_removed_once_on_expiry(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(id=20, roles=[])

                async def add_roles(added, **kwargs):
                    member.roles.append(added)

                async def remove_roles(removed, **kwargs):
                    member.roles.remove(removed)

                member.add_roles = mock.AsyncMock(side_effect=add_roles)
                member.remove_roles = mock.AsyncMock(side_effect=remove_roles)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"mute_role": 55})
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                claimed = cog._case_store.claim_operation(
                    operation.operation_id, datetime.now(timezone.utc)
                )

                await cog._execute_detection_case_operation(
                    claimed, datetime.now(timezone.utc)
                )
                await cog.resolve_detection_case(appended.case.case_id, "expired")
                await cog.resolve_detection_case(appended.case.case_id, "expired")

                member.add_roles.assert_awaited_once()
                member.remove_roles.assert_awaited_once()
                self.assertNotIn(role, member.roles)

    async def test_live_operation_heartbeat_prevents_stale_reclaim(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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

                async def blocked_review(case_id):
                    started.set()
                    await release.wait()

                cog._case_review_rerender = blocked_review
                worker = asyncio.create_task(
                    cog._execute_detection_case_operation(claimed, datetime.now(timezone.utc))
                )
                await started.wait()
                try:
                    await asyncio.sleep(0.15)
                    contenders = cog._case_store.claim_due_operations(
                        datetime.now(timezone.utc),
                        stale_before=datetime.now(timezone.utc) - timedelta(milliseconds=75),
                    )
                    self.assertEqual(contenders, ())
                finally:
                    release.set()
                    await worker

    async def test_role_apply_retry_observes_external_state_without_repeating_add(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(id=20, roles=[])

                async def add_roles(added, **kwargs):
                    member.roles.append(added)

                member.add_roles = mock.AsyncMock(side_effect=add_roles)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                now = datetime.now(timezone.utc)
                first = cog._case_store.claim_operation(operation.operation_id, now)
                real_record = cog._case_store.record_operation_role_ownership
                cog._case_store.record_operation_role_ownership = mock.Mock(
                    side_effect=RuntimeError("crash after Discord")
                )

                await cog._execute_detection_case_operation(first, now)
                cog._case_store.record_operation_role_ownership = real_record
                failed = cog._case_store.get_case(appended.case.case_id)
                retry_at = next(
                    item.retry_at for item in failed.operations
                    if item.operation_id == operation.operation_id
                )
                second = cog._case_store.claim_operation(
                    operation.operation_id, retry_at + timedelta(seconds=1)
                )
                await cog._execute_detection_case_operation(second, retry_at)

                member.add_roles.assert_awaited_once()
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), ()
                )

    async def test_role_apply_reclaim_does_not_own_ambiguously_present_role(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(
                    id=20,
                    roles=[],
                    add_roles=mock.AsyncMock(),
                    remove_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = self._config({"mute_role": 55})
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                now = datetime.now(timezone.utc)
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                first = cog._case_store.claim_operation(operation.operation_id, now)
                self.assertTrue(
                    cog._case_store.start_operation_effect(
                        first.operation_id, first.claim_token, now
                    )
                )
                self.assertTrue(
                    cog._case_store.fail_operation(
                        first.operation_id,
                        first.claim_token,
                        "crash before Discord add",
                        now,
                        now + timedelta(seconds=1),
                    )
                )
                member.roles.append(role)
                second = cog._case_store.claim_operation(
                    operation.operation_id, now + timedelta(seconds=1)
                )

                await cog._execute_detection_case_operation(
                    second, now + timedelta(seconds=1)
                )
                await cog.resolve_detection_case(appended.case.case_id, "expired")

                member.add_roles.assert_not_awaited()
                member.remove_roles.assert_not_awaited()
                snapshot = cog._case_store.get_case(appended.case.case_id)
                role_operation = next(
                    item for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                projection = honeypot.render_case(snapshot)
                self.assertEqual(role_operation.result, "ambiguous_role_ownership")
                self.assertTrue(snapshot.case.needs_attention)
                self.assertTrue(projection.needs_attention)
                self.assertTrue(
                    any(
                        "could not confirm that this case applied the temporary mute role"
                        in field.value.lower()
                        for page in projection.pages
                        for field in page
                    )
                )
                self.assertEqual(cog._case_store.owned_role_ids(appended.case.case_id), ())
                self.assertIn(role, member.roles)

    async def test_stale_operation_token_cannot_start_role_effect(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                now = datetime.now(timezone.utc)
                old = cog._case_store.claim_operation(operation.operation_id, now)
                new = cog._case_store.claim_due_operations(
                    now + timedelta(minutes=6),
                    stale_before=now + timedelta(minutes=5),
                )[0]

                started = cog._case_store.start_operation_effect(
                    operation.operation_id, old.claim_token, now
                )

                self.assertFalse(started)
                self.assertFalse(
                    cog._case_store.operation_effect_started(operation.operation_id)
                )
                self.assertNotEqual(old.claim_token, new.claim_token)

    async def test_stale_worker_cannot_record_role_ownership_after_reclaim(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                now = datetime.now(timezone.utc)
                old = cog._case_store.claim_operation(operation.operation_id, now)
                self.assertTrue(
                    cog._case_store.start_operation_effect(
                        operation.operation_id, old.claim_token, now
                    )
                )
                new = cog._case_store.claim_due_operations(
                    now + timedelta(minutes=6),
                    stale_before=now + timedelta(minutes=5),
                )[0]

                recorded = cog._case_store.record_operation_role_ownership(
                    operation.operation_id,
                    old.claim_token,
                    appended.case.case_id,
                    10,
                    20,
                    role_id=55,
                    now=now,
                )

                self.assertFalse(recorded)
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), ()
                )
                self.assertNotEqual(old.claim_token, new.claim_token)

    async def test_failed_review_edit_does_not_revert_expired_state(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(_Bot())
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


if __name__ == "__main__":
    unittest.main()
