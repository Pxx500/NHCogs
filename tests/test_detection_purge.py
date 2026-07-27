"""Cached-message purge and forward purge: their durable operations, their
terminal states and the statistics they increment.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import DetectionPipelineTestCase, _Bot, _isolated_honeypot_modules, active_case


class DetectionPurgeTests(DetectionPipelineTestCase):
    async def test_background_message_recovery_purges_cached_messages_once(self):
        class SimulatedCrash(BaseException):
            pass

        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                prior = self._message(
                    honeypot, attachment_count=0, message_id=299
                )
                message = self._message(honeypot, attachment_count=0)
                message.guild = prior.guild
                message.author = prior.author
                prior.created_at = datetime.now(timezone.utc)
                message.created_at = prior.created_at
                cached_delete = mock.AsyncMock()
                channel = SimpleNamespace(
                    id=message.channel.id,
                    fetch_message=mock.AsyncMock(return_value=message),
                    get_partial_message=lambda message_id: SimpleNamespace(
                        delete=cached_delete
                    ),
                )
                prior.channel = channel
                message.channel = channel
                message.guild.get_channel = lambda channel_id: channel
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": True,
                    "spam_action": "review",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                del cog._purge_detection_case_cached_messages
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.Mock(
                    side_effect=[[], ["duplicate"]]
                )
                cog._publish_detection_case = mock.AsyncMock()
                cog.bot.get_guild = lambda guild_id: message.guild

                await cog.on_message(prior)
                with mock.patch.object(
                    cog._case_store,
                    "claim_operation",
                    side_effect=SimulatedCrash(),
                ):
                    with self.assertRaises(SimulatedCrash):
                        await cog.on_message(message)

                await cog._run_detection_reconciliation(
                    now=datetime.now(timezone.utc) + timedelta(minutes=10)
                )
                await cog._run_detection_reconciliation(
                    now=datetime.now(timezone.utc) + timedelta(minutes=11)
                )

                cached_delete.assert_awaited_once()
                cached_stats = [
                    call
                    for call in cog._increment_stat.await_args_list
                    if call.args == (message.guild, "cached_purge_deletes", 1)
                ]
                self.assertEqual(len(cached_stats), 1)

    async def test_forbidden_forward_delete_is_persisted_and_published_after_all_images_scan(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                forbidden = honeypot.discord.Forbidden("manage messages denied")
                message = self._message(honeypot, delete_error=forbidden)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "review_enabled": True,
                        "dry_run": False,
                        "logs_channel": None,
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
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertIsNotNone(snapshot)
                self.assertEqual(
                    [item.signal.detector for item in snapshot.signals],
                    ["forward_purge"],
                )
                self.assertTrue(snapshot.case.needs_attention)
                self.assertEqual(len(snapshot.messages), 1)
                self.assertEqual(snapshot.messages[0].delete_status.value, "forbidden")
                self.assertIn("Forbidden", snapshot.messages[0].error)
                self.assertEqual(len(snapshot.attachments), 3)
                self.assertTrue(all(item.evidence_path for item in snapshot.attachments))
                self.assertTrue(all(item.capture_status == "captured" for item in snapshot.attachments))
                source_delete = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "source_delete"
                )
                self.assertEqual(source_delete.status.value, "failed")
                self.assertEqual(source_delete.attempts, 1)
                self.assertEqual(
                    source_delete.retry_at - source_delete.updated_at,
                    timedelta(seconds=10),
                )
                failures = await asyncio.to_thread(
                    cog._case_store.list_operational_failures, message.guild.id
                )
                self.assertEqual([item.source for item in failures], ["source_delete"])
                scan_args = cog._scan_all_case_message_images.await_args.args
                self.assertEqual(scan_args[0], message)
                self.assertEqual((scan_args[2], scan_args[3]), (snapshot.case.case_id, 1))
                self.assertEqual(len(scan_args[4]), 3)
                self.assertEqual(cog._publish_detection_case.await_count, 2)
                message.delete.assert_awaited_once()
                cog._handle_spam_message.assert_not_awaited()
                cog._handle_firstpost_message.assert_not_awaited()
                cog._handle_imagescan_detector_message.assert_not_awaited()
                stat_names = [call.args[1] for call in cog._increment_stat.await_args_list]
                self.assertIn("forward_purge_delete_failures", stat_names)
                self.assertIn("delete_forbidden", stat_names)
                self.assertEqual(
                    cog._purge_detection_case_cached_messages.await_args.kwargs[
                        "exclude_message_id"
                    ],
                    message.id,
                )

                retried_message = SimpleNamespace(delete=mock.AsyncMock())
                source_channel = SimpleNamespace(
                    id=message.channel.id,
                    fetch_message=mock.AsyncMock(return_value=retried_message),
                )
                message.guild.get_channel = lambda channel_id: source_channel
                cog.bot.get_guild = lambda guild_id: message.guild
                await cog._run_detection_reconciliation(now=source_delete.retry_at)

                retried_message.delete.assert_awaited_once()
                failures = await asyncio.to_thread(
                    cog._case_store.list_operational_failures,
                    message.guild.id,
                    include_resolved=True,
                )
                self.assertIsNotNone(failures[0].resolved_at)

    async def test_spam_only_delete_does_not_increment_forward_purge_stats(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": True,
                    "spam_action": "review", "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.Mock(return_value=["duplicate"])
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(
                    [item.signal.detector for item in snapshot.signals], ["spam"]
                )
                stat_names = [call.args[1] for call in cog._increment_stat.await_args_list]
                self.assertIn("purged_messages", stat_names)
                self.assertNotIn("forward_purge_deletes", stat_names)
                self.assertNotIn("forward_purge_delete_failures", stat_names)

    async def test_image_only_delete_does_not_increment_forward_purge_stats(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=6)
                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": False,
                    "firstpost_enabled": False, "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": True,
                    "imagescan_detector_action": "review",
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._initial_image_signal = mock.AsyncMock(
                    return_value=honeypot.DetectionSignal(
                        "image", "known image", honeypot.ActionIntent.REVIEW, True, {}
                    )
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case_serial = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(
                    [item.signal.detector for item in snapshot.signals], ["image"]
                )
                scan_args = cog._scan_all_case_message_images.await_args.args
                self.assertEqual(scan_args[0], message)
                self.assertEqual(len(scan_args[4]), 6)
                stat_names = [call.args[1] for call in cog._increment_stat.await_args_list]
                self.assertIn("purged_messages", stat_names)
                self.assertNotIn("forward_purge_deletes", stat_names)
                self.assertNotIn("forward_purge_delete_failures", stat_names)

    async def test_dry_run_has_no_cached_or_forward_purge_side_effects(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                case_cached_purge = cog._purge_detection_case_cached_messages
                is_forward_purge_active = cog._is_forward_purge_active
                message = self._message(honeypot, attachment_count=0)
                message.created_at = datetime.now(timezone.utc)
                message.author.kick = mock.AsyncMock()
                message.author.ban = mock.AsyncMock()
                previous = self._message(
                    honeypot,
                    attachment_count=0,
                    message_id=299,
                    channel_id=401,
                )
                previous.created_at = datetime.now(timezone.utc)
                follow_up = self._message(
                    honeypot,
                    attachment_count=0,
                    message_id=301,
                    channel_id=401,
                )
                follow_up.created_at = datetime.now(timezone.utc)
                follow_up.author = message.author
                config = {
                    "enabled": True,
                    "dry_run": True,
                    "logs_channel": None,
                    "review_channel": None,
                    "honeypot_channels": [message.channel.id],
                    "action": "ban",
                    "fallback_action": "ban",
                    "spam_enabled": False,
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "purge_backward_seconds": 300,
                    "purge_forward_seconds": 300,
                }
                self._configure_public_boundary(
                    cog,
                    config,
                )
                cog._purge_detection_case_cached_messages = case_cached_purge
                cog._is_forward_purge_active = is_forward_purge_active
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                cog._record_recent_user_message(
                    previous, honeypot.GuildSettings.from_mapping(config)
                )

                await cog.on_message(message)
                await cog.on_message(follow_up)

                message.delete.assert_not_awaited()
                follow_up.delete.assert_not_awaited()
                message.author.kick.assert_not_awaited()
                message.author.ban.assert_not_awaited()
                self.assertNotIn(
                    message.author.id,
                    cog._hot_purge_users.get(message.guild.id, {}),
                )

    async def test_cached_purge_not_found_is_persisted_as_already_gone(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                prior = self._message(
                    honeypot, attachment_count=0, message_id=299, channel_id=399
                )
                message = self._message(honeypot, attachment_count=0)
                prior.created_at = datetime.now(timezone.utc)
                message.created_at = prior.created_at
                prior.guild = message.guild
                prior.author = message.author
                cached_delete = mock.AsyncMock(
                    side_effect=honeypot.discord.NotFound("already deleted")
                )
                cached_channel = SimpleNamespace(
                    get_partial_message=lambda message_id: SimpleNamespace(
                        delete=cached_delete
                    )
                )
                message.guild.get_channel = lambda channel_id: (
                    cached_channel if channel_id == prior.channel.id else None
                )
                cog.bot.get_guild = lambda guild_id: message.guild
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": True,
                    "spam_action": "review",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                del cog._purge_detection_case_cached_messages
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.Mock(
                    side_effect=[[], ["duplicate"]]
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(prior)
                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                operations = [
                    item
                    for item in snapshot.operations
                    if item.operation_type == "cached_purge"
                ]
                self.assertEqual(len(operations), 1)
                operation = operations[0]
                self.assertEqual(operation.status.value, "succeeded")
                self.assertEqual(operation.result, "already_gone")
                self.assertEqual(operation.attempts, 1)
                self.assertIn(f":{prior.channel.id}:{prior.id}", operation.idempotency_key)
                cached_delete.assert_awaited_once()

    async def test_cached_purge_missing_channel_is_terminal_and_moderator_visible(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (),
                )
                operation = await asyncio.to_thread(
                    cog._case_store.ensure_operation,
                    appended.case.case_id,
                    "cached_purge",
                    f"cached_purge:{appended.case.case_id}:399:299",
                    appended.message.sequence,
                )
                now = datetime.now(timezone.utc)
                operation = await asyncio.to_thread(
                    cog._case_store.claim_operation, operation.operation_id, now
                )
                message.guild.get_channel = lambda channel_id: None
                message.guild.get_thread = lambda channel_id: None
                cog.bot.get_guild = lambda guild_id: message.guild

                await cog._execute_detection_case_operation(operation, now)

                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                stored = next(
                    item for item in snapshot.operations if item.operation_id == operation.operation_id
                )
                projection = honeypot.render_case(snapshot)
                self.assertEqual(stored.status.value, "abandoned")
                self.assertEqual(stored.result, "channel_unavailable")
                self.assertIsNone(stored.retry_at)
                self.assertTrue(snapshot.case.needs_attention)
                self.assertTrue(
                    any(
                        "<#399>" in line
                        and "Could not delete: channel unavailable" in line
                        for line in projection.cached_purge_lines
                    )
                )

    async def test_cached_purge_unsupported_channel_is_terminal_and_moderator_visible(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (),
                )
                operation = await asyncio.to_thread(
                    cog._case_store.ensure_operation,
                    appended.case.case_id,
                    "cached_purge",
                    f"cached_purge:{appended.case.case_id}:399:299",
                    appended.message.sequence,
                )
                now = datetime.now(timezone.utc)
                operation = await asyncio.to_thread(
                    cog._case_store.claim_operation, operation.operation_id, now
                )
                unsupported_channel = SimpleNamespace(id=399)
                message.guild.get_channel = lambda channel_id: unsupported_channel
                cog.bot.get_guild = lambda guild_id: message.guild

                await cog._execute_detection_case_operation(operation, now)

                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                stored = next(
                    item for item in snapshot.operations if item.operation_id == operation.operation_id
                )
                projection = honeypot.render_case(snapshot)
                self.assertEqual(stored.status.value, "abandoned")
                self.assertEqual(stored.result, "unsupported_channel")
                self.assertIsNone(stored.retry_at)
                self.assertTrue(snapshot.case.needs_attention)
                self.assertTrue(
                    any(
                        "<#399>" in line
                        and "Could not delete: unsupported channel" in line
                        for line in projection.cached_purge_lines
                    )
                )

    async def test_cached_purge_forbidden_requires_staff_attention(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                prior = self._message(
                    honeypot, attachment_count=0, message_id=299, channel_id=399
                )
                message = self._message(honeypot, attachment_count=0)
                prior.created_at = datetime.now(timezone.utc)
                message.created_at = prior.created_at
                prior.guild = message.guild
                prior.author = message.author
                cached_delete = mock.AsyncMock(
                    side_effect=honeypot.discord.Forbidden("missing permissions")
                )
                cached_channel = SimpleNamespace(
                    get_partial_message=lambda message_id: SimpleNamespace(
                        delete=cached_delete
                    )
                )
                message.guild.get_channel = lambda channel_id: (
                    cached_channel if channel_id == prior.channel.id else None
                )
                cog.bot.get_guild = lambda guild_id: message.guild
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": True,
                    "spam_action": "review",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                del cog._purge_detection_case_cached_messages
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.Mock(
                    side_effect=[[], ["duplicate"]]
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(prior)
                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "cached_purge"
                )
                projection = honeypot.render_case(snapshot)
                self.assertEqual(operation.status.value, "abandoned")
                self.assertEqual(operation.result, "forbidden")
                self.assertEqual(operation.attempts, 1)
                self.assertIn("Forbidden", operation.last_error)
                self.assertTrue(snapshot.case.needs_attention)
                self.assertTrue(projection.needs_attention)
                self.assertIn(
                    "Warnings:",
                    [field.name for field in projection.fields],
                )
                cached_delete.assert_awaited_once()

    async def test_cached_purge_exhausted_transient_retries_require_attention(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                prior = self._message(
                    honeypot, attachment_count=0, message_id=299, channel_id=399
                )
                message = self._message(honeypot, attachment_count=0)
                prior.created_at = datetime.now(timezone.utc)
                message.created_at = prior.created_at
                prior.guild = message.guild
                prior.author = message.author
                cached_delete = mock.AsyncMock(
                    side_effect=honeypot.discord.HTTPException()
                )
                cached_channel = SimpleNamespace(
                    get_partial_message=lambda message_id: SimpleNamespace(
                        delete=cached_delete
                    )
                )
                message.guild.get_channel = lambda channel_id: (
                    cached_channel if channel_id == prior.channel.id else None
                )
                cog.bot.get_guild = lambda guild_id: message.guild
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": True,
                    "spam_action": "review",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                del cog._purge_detection_case_cached_messages
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.Mock(
                    side_effect=[[], ["duplicate"]]
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(prior)
                await cog.on_message(message)

                first_snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                first_operation = next(
                    item
                    for item in first_snapshot.operations
                    if item.operation_type == "cached_purge"
                )
                first_operation_id = first_operation.operation_id
                first_projection = honeypot.render_case(first_snapshot)
                self.assertEqual(first_operation.status.value, "failed")
                self.assertEqual(first_operation.result, "transient_failure")
                self.assertIsNotNone(first_operation.retry_at)
                self.assertFalse(first_snapshot.case.needs_attention)
                self.assertTrue(
                    any(
                        "Could not delete: temporary Discord error" in line
                        for line in first_projection.cached_purge_lines
                    )
                )

                for expected_attempt in (2, 3):
                    snapshot = await asyncio.to_thread(
                        active_case, cog._case_store,
                        message.guild.id,
                        message.author.id,
                    )
                    operation = next(
                        item
                        for item in snapshot.operations
                        if item.operation_type == "cached_purge"
                    )
                    claimed = await asyncio.to_thread(
                        cog._case_store.claim_operation,
                        operation.operation_id,
                        operation.retry_at,
                    )
                    await cog._execute_detection_case_operation(
                        claimed, operation.retry_at
                    )
                    snapshot = await asyncio.to_thread(
                        active_case, cog._case_store,
                        message.guild.id,
                        message.author.id,
                    )
                    operation = next(
                        item
                        for item in snapshot.operations
                        if item.operation_type == "cached_purge"
                    )
                    self.assertEqual(operation.attempts, expected_attempt)
                    self.assertEqual(operation.operation_id, first_operation_id)

                self.assertEqual(
                    sum(
                        item.operation_type == "cached_purge"
                        for item in snapshot.operations
                    ),
                    1,
                )
                self.assertEqual(operation.status.value, "abandoned")
                self.assertEqual(operation.result, "transient_failure")
                self.assertIsNone(operation.retry_at)
                self.assertIn("HTTPException", operation.last_error)
                self.assertTrue(snapshot.case.needs_attention)
                self.assertEqual(cached_delete.await_count, 9)
