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
