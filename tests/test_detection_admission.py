"""Message admission into the detection pipeline: the settings gate, signal
admission order, containment, redelivery and restart recovery.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import (
    DetectionPipelineTestCase,
    _Bot,
    _Config,
    _isolated_honeypot_modules,
    active_case,
)


class DetectionAdmissionTests(DetectionPipelineTestCase):
    async def test_bot_messages_are_registered_before_detection_filters(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                await cog._message_registry.initialize()
                message = self._message(honeypot, attachment_count=0)
                message.author.bot = True
                message.pinned = False

                await cog.on_message(message)

                self.assertEqual(
                    await cog._message_registry.recent_by_author(
                        message.guild.id,
                        message.author.id,
                    ),
                    (
                        honeypot.MessageRecord(
                            message_id=message.id,
                            guild_id=message.guild.id,
                            channel_id=message.channel.id,
                            author_id=message.author.id,
                            created_at=message.created_at,
                            pinned=False,
                            author_kind="bot",
                            fingerprint=None,
                        ),
                    ),
                )

    async def test_registry_observation_failure_does_not_stop_detection(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message(honeypot, attachment_count=0)
                self._configure_public_boundary(
                    cog,
                    {"enabled": True, "logs_channel": None},
                )
                cog._observe_message = mock.AsyncMock(
                    side_effect=RuntimeError("registry unavailable")
                )
                cog._record_operational_failure = mock.AsyncMock()
                cog._collect_detection_signals = mock.AsyncMock(return_value=())

                await cog.on_message(message)

                cog._collect_detection_signals.assert_awaited_once()
                cog._record_operational_failure.assert_awaited_once()

    async def test_malformed_enabled_setting_does_not_enter_detection_pipeline(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                bot.owner_ids = set()
                bot.is_mod = mock.AsyncMock(return_value=True)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                cog._observe_message = mock.AsyncMock()
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={"enabled": "true", "logs_channel": None}
                        )
                    )
                )
                message = self._message(honeypot, attachment_count=0)
                message.guild.me = SimpleNamespace(top_role=10)
                message.guild.get_member = lambda user_id: None
                message.guild.fetch_member = mock.AsyncMock(
                    return_value=SimpleNamespace(
                        id=message.author.id,
                        guild=message.guild,
                        guild_permissions=SimpleNamespace(manage_guild=False),
                        top_role=1,
                    )
                )

                with self.assertLogs("red.Honeypot", level=logging.WARNING):
                    await cog.on_message(message)

                message.guild.fetch_member.assert_not_awaited()

    async def test_guild_message_from_departed_user_reaches_detection_pipeline(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                bot.owner_ids = set()
                bot.is_mod = mock.AsyncMock(return_value=False)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                message = self._message(honeypot, attachment_count=0)
                message.guild.get_member = lambda user_id: None
                message.guild.fetch_member = mock.AsyncMock(
                    side_effect=honeypot.discord.NotFound("member was banned")
                )
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "logs_channel": None,
                    },
                )
                del cog._is_protected_member
                cog._collect_detection_signals = mock.AsyncMock(return_value=())

                await cog.on_message(message)

                message.guild.fetch_member.assert_awaited_once_with(message.author.id)
                cog._collect_detection_signals.assert_awaited_once()

    async def test_uncached_protected_member_is_still_excluded(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                bot.owner_ids = set()
                bot.is_mod = mock.AsyncMock(return_value=False)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                message = self._message(honeypot, attachment_count=0)
                message.guild.me = SimpleNamespace(top_role=10)
                resolved_member = SimpleNamespace(
                    id=message.author.id,
                    guild=message.guild,
                    guild_permissions=SimpleNamespace(manage_guild=True),
                    top_role=1,
                )
                message.guild.get_member = lambda user_id: None
                message.guild.fetch_member = mock.AsyncMock(
                    return_value=resolved_member
                )
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "logs_channel": None,
                    },
                )
                del cog._is_protected_member
                cog._collect_detection_signals = mock.AsyncMock(return_value=())

                await cog.on_message(message)

                message.guild.fetch_member.assert_awaited_once_with(message.author.id)
                cog._collect_detection_signals.assert_not_awaited()

    async def test_whitelist_bypass_records_case_without_deleting_honeypot_message(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(
                    honeypot, attachment_count=0, channel_id=9
                )
                message.author.roles = [SimpleNamespace(id=7)]
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "honeypot_channels": [9],
                    "whitelisted_roles": [7],
                    "whitelist_mode": "bypass",
                    "action": "ban",
                    "fallback_action": "none",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                message.delete.assert_not_awaited()
                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                self.assertEqual(snapshot.messages[0].delete_status.value, "pending")
                self.assertNotIn(
                    "review_publish",
                    {operation.operation_type for operation in snapshot.operations},
                )
                cog._publish_detection_case.assert_not_awaited()
                cog._increment_stat.assert_any_await(message.guild, "whitelisted")

    async def test_admission_preserves_discord_attachment_description_and_spoiler(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1, channel_id=9)
                message.attachments[0].description = "suspicious payment form"
                message.attachments[0].is_spoiler = lambda: True
                message.author.roles = [SimpleNamespace(id=7)]
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "honeypot_channels": [9],
                    "whitelisted_roles": [7],
                    "whitelist_mode": "bypass",
                    "action": "ban",
                    "fallback_action": "none",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                self.assertEqual(
                    snapshot.attachments[0].description,
                    "suspicious payment form",
                )
                self.assertTrue(snapshot.attachments[0].spoiler)

    async def test_concurrent_detection_preserves_message_arrival_order(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                first = self._message(
                    honeypot, attachment_count=0, message_id=300
                )
                second = self._message(
                    honeypot, attachment_count=0, message_id=301
                )
                second.guild = first.guild
                second.author = first.author
                second.created_at = first.created_at + timedelta(seconds=1)
                first_detection_started = asyncio.Event()
                release_first_detection = asyncio.Event()
                second_processed = asyncio.Event()
                signal = honeypot.DetectionSignal(
                    "spam", "duplicate", honeypot.ActionIntent.REVIEW, True, {}
                )

                async def collect(message, config):
                    if message.id == first.id:
                        first_detection_started.set()
                        await release_first_detection.wait()
                    return (signal,)

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
                cog._collect_detection_signals = collect
                cog._scan_all_case_message_images = mock.AsyncMock()

                async def publish(*args, **kwargs):
                    second_processed.set()

                cog._publish_detection_case = mock.AsyncMock(side_effect=publish)
                first_task = asyncio.create_task(cog.on_message(first))
                await asyncio.wait_for(first_detection_started.wait(), timeout=1)
                second_task = asyncio.create_task(cog.on_message(second))
                try:
                    await asyncio.wait_for(second_processed.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
                release_first_detection.set()
                await asyncio.gather(first_task, second_task)

                snapshot = active_case(
                    cog._case_store,
                    first.guild.id, first.author.id
                )
                self.assertEqual(
                    [message.message_id for message in snapshot.messages],
                    [first.id, second.id],
                )
                self.assertEqual(
                    snapshot.case.expires_at,
                    first.created_at + timedelta(hours=24),
                )

    async def test_next_message_is_admitted_while_previous_message_finishes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                first = self._message(honeypot, attachment_count=0, message_id=300)
                second = self._message(honeypot, attachment_count=0, message_id=301)
                second.guild = first.guild
                second.author = first.author
                second.created_at = first.created_at + timedelta(seconds=1)
                first_scan_started = asyncio.Event()
                release_first_scan = asyncio.Event()
                signal = honeypot.DetectionSignal(
                    "honeypot", "bait", honeypot.ActionIntent.REVIEW, True, {}
                )

                config = {
                    "enabled": True,
                    "dry_run": True,
                    "logs_channel": None,
                    "review_channel": None,
                    "review_enabled": True,
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._collect_detection_signals = mock.AsyncMock(return_value=(signal,))
                cog._publish_detection_case = mock.AsyncMock()

                async def scan(message, *args, **kwargs):
                    if message.id == first.id:
                        first_scan_started.set()
                        await release_first_scan.wait()

                cog._scan_all_case_message_images = scan
                first_task = asyncio.create_task(cog.on_message(first))
                await asyncio.wait_for(first_scan_started.wait(), timeout=1)
                second_task = asyncio.create_task(cog.on_message(second))
                async def wait_for_second_admission():
                    while True:
                        snapshot = await asyncio.to_thread(
                            active_case, cog._case_store,
                            first.guild.id,
                            first.author.id,
                        )
                        if snapshot is not None and len(snapshot.messages) == 2:
                            return snapshot
                        await asyncio.sleep(0)

                try:
                    snapshot = await asyncio.wait_for(
                        wait_for_second_admission(), timeout=0.5
                    )
                    self.assertEqual(
                        [message.message_id for message in snapshot.messages],
                        [first.id, second.id],
                    )
                finally:
                    release_first_scan.set()
                    await asyncio.gather(first_task, second_task)

    async def test_firstpost_claim_is_persisted_with_containment(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                signal = honeypot.DetectionSignal(
                    "firstpost",
                    "suspicious first message",
                    honeypot.ActionIntent.REVIEW,
                    True,
                    {},
                )
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": False,
                    "firstpost_enabled": True,
                    "firstpost_collect_enabled": False,
                    "firstpost_action": "review",
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._firstpost_loaded_guilds.add(message.guild.id)
                cog._collect_detection_signals = mock.AsyncMock(
                    return_value=(signal,)
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                await cog.on_message(message)

                message.delete.assert_awaited_once()
                snapshot = active_case(
                    cog._case_store,
                    message.guild.id, message.author.id
                )
                self.assertFalse(snapshot.case.needs_attention)
                self.assertEqual(
                    [item.signal.detector for item in snapshot.signals],
                    ["firstpost"],
                )

    async def test_restart_recovers_voice_channel_work_committed_with_message_admission(self):
        class SimulatedCrash(BaseException):
            pass

        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                crashed = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(crashed._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                message.guild.get_member = lambda user_id: message.author
                honeypot.discord.TextChannel = type("TextChannel", (), {})
                honeypot.discord.Thread = type("Thread", (), {})

                class VoiceChannel:
                    def __init__(self):
                        self.id = message.channel.id
                        self.fetch_message = mock.AsyncMock(return_value=message)

                channel = VoiceChannel()
                message.channel = channel
                message.guild.get_channel = lambda channel_id: channel
                config = {
                    "enabled": True,
                    "review_enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": True,
                    "spam_action": "ban",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(crashed, config)
                crashed._is_forward_purge_active.return_value = False
                crashed._spam_suspicion_reasons = mock.AsyncMock(
                    return_value=["duplicate"]
                )
                with mock.patch.object(
                    crashed._case_store,
                    "claim_operation",
                    side_effect=SimulatedCrash(),
                ):
                    with self.assertRaises(SimulatedCrash):
                        await crashed.on_message(message)

                admitted = active_case(
                    crashed._case_store,
                    message.guild.id, message.author.id
                )
                self.assertEqual(
                    {item.operation_type for item in admitted.operations},
                    {"message_process", "moderation_action", "review_publish"},
                )

                restarted = honeypot.Honeypot(_Bot())
                await restarted._message_registry.initialize()
                restarted.config = _Config()
                restarted.config.register_guild(**config)
                restarted.bot.get_guild = lambda guild_id: message.guild
                restarted._execute_action = mock.AsyncMock(
                    return_value=honeypot.ModerationEffectResult(
                        "banned",
                        None,
                        honeypot.detection.EffectStatus.SUCCEEDED,
                    )
                )
                restarted._publish_detection_case = mock.AsyncMock()
                restarted._imagescan_load_samples = mock.AsyncMock(return_value=())
                restarted._imagescan_model_state = mock.AsyncMock(
                    return_value={"effective_threshold": 20}
                )
                with mock.patch.object(
                    honeypot.imagescan,
                    "image_hashes_from_bytes",
                    return_value={"sha256": "recovered-image", "phash": "recovered-phash"},
                ), mock.patch.object(
                    honeypot.imagescan,
                    "match_image",
                    return_value={"matched": False, "score": None},
                ):
                    await restarted._run_detection_reconciliation(
                        now=datetime.now(timezone.utc) + timedelta(minutes=10)
                    )

                message.delete.assert_awaited_once()
                restarted._execute_action.assert_awaited_once()
                restarted._publish_detection_case.assert_awaited()
                recovered = restarted._case_store.get_case(admitted.case.case_id)
                self.assertIn(recovered.case.status.value, {"resolved", "expired"})
                self.assertEqual(recovered.messages, ())
                self.assertEqual(recovered.attachments, ())
                self.assertEqual(recovered.signals, ())
                self.assertEqual(recovered.operations, ())

    async def test_firstpost_only_admission_persists_signal_before_pipeline_claim(self):
        class SimulatedCrash(BaseException):
            pass

        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                signal = honeypot.DetectionSignal(
                    "firstpost",
                    "suspicious first message",
                    honeypot.ActionIntent.REVIEW,
                    True,
                    {},
                )
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": False,
                    "firstpost_enabled": True,
                    "firstpost_collect_enabled": False,
                    "firstpost_action": "review",
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._firstpost_loaded_guilds.add(message.guild.id)
                cog._collect_detection_signals = mock.AsyncMock(return_value=(signal,))

                with mock.patch.object(
                    cog._case_store,
                    "claim_operation",
                    side_effect=SimulatedCrash(),
                ):
                    with self.assertRaises(SimulatedCrash):
                        await cog.on_message(message)

                snapshot = active_case(
                    cog._case_store,
                    message.guild.id, message.author.id
                )
                self.assertEqual(
                    [record.signal.detector for record in snapshot.signals],
                    ["firstpost"],
                )

    async def test_successive_forward_messages_share_case_and_keep_ordered_channels(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                config = {
                    "enabled": True,
                    "review_enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": False,
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                first = self._message(honeypot, attachment_count=0)
                second = self._message(
                    honeypot,
                    attachment_count=0,
                    message_id=301,
                    channel_id=401,
                )

                await cog.on_message(first)
                await cog.on_message(second)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, first.guild.id, first.author.id
                )
                self.assertEqual(
                    [(item.sequence, item.channel_id, item.delete_status.value) for item in snapshot.messages],
                    [(1, 400, "deleted"), (2, 401, "deleted")],
                )
                self.assertEqual(cog._publish_detection_case.await_count, 4)
                self.assertEqual(
                    [
                        call.kwargs["exclude_message_id"]
                        for call in cog._purge_detection_case_cached_messages.await_args_list
                    ],
                    [300, 301],
                )

    async def test_reconciliation_checks_due_retries_every_ten_seconds(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                self.assertEqual(
                    honeypot.Honeypot.detection_reconciliation_loop.options,
                    {"seconds": 10},
                )

    async def test_duplicate_discord_delivery_does_not_repeat_containment(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
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
                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(len(snapshot.messages), 1)
                message.delete.assert_awaited_once()

    async def test_duplicate_delivery_does_not_repeat_cached_purge_or_stats(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                config = {
                    "enabled": True, "review_enabled": True,
                    "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": False,
                    "firstpost_enabled": False, "firstpost_collect_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._purge_detection_case_cached_messages = mock.AsyncMock(return_value=2)
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)
                await cog.on_message(message)

                cog._purge_detection_case_cached_messages.assert_awaited_once()
                purged_stats = [
                    call for call in cog._increment_stat.await_args_list
                    if call.args == (message.guild, "purged_messages", 2)
                ]
                cached_stats = [
                    call for call in cog._increment_stat.await_args_list
                    if call.args == (message.guild, "cached_purge_deletes", 2)
                ]
                self.assertEqual(len(purged_stats), 1)
                self.assertEqual(len(cached_stats), 1)
                self.assertEqual(cog._scan_all_case_message_images.await_count, 1)
                self.assertEqual(cog._publish_detection_case.await_count, 3)

    async def test_redelivery_resumes_a_preappended_pending_message(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True, "review_enabled": True,
                        "dry_run": False, "logs_channel": None,
                        "review_channel": None, "spam_enabled": False,
                        "firstpost_enabled": False, "firstpost_collect_enabled": False,
                    },
                )
                await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (honeypot.detection._forward_purge_signal(cog, message),),
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(snapshot.messages[0].delete_status.value, "deleted")
                self.assertEqual(snapshot.attachments[0].capture_status, "captured")
                self.assertIsNotNone(snapshot.attachments[0].evidence_path)
                message.delete.assert_awaited_once()
                message.attachments[0].read.assert_awaited_once()
                self.assertEqual(cog._publish_detection_case.await_count, 2)

    async def test_forward_firstpost_state_is_consumed_only_after_case_append(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                cog._firstpost_loaded_guilds.add(100)
                message = self._message(honeypot, attachment_count=4)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": True,
                        "logs_channel": None,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": True,
                        "firstpost_collect_enabled": False,
                    },
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(
                    [signal.signal.detector for signal in snapshot.signals],
                    ["forward_purge", "firstpost"],
                )

    async def test_firstpost_review_delete_failure_is_visible(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                cog._firstpost_loaded_guilds.add(100)
                message = self._message(
                    honeypot,
                    attachment_count=4,
                    delete_error=honeypot.discord.Forbidden("manage messages denied"),
                )
                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": False,
                    "firstpost_enabled": True, "firstpost_action": "review",
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(
                    [item.signal.detector for item in snapshot.signals], ["firstpost"]
                )
                self.assertTrue(snapshot.case.needs_attention)
                self.assertEqual(snapshot.messages[0].delete_status.value, "forbidden")
                stat_names = [call.args[1] for call in cog._increment_stat.await_args_list]
                self.assertNotIn("forward_purge_deletes", stat_names)
                self.assertNotIn("forward_purge_delete_failures", stat_names)

    async def test_none_signal_does_not_delete_without_stronger_signal(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                prior = self._message(
                    honeypot, attachment_count=1, message_id=299, channel_id=399
                )
                message = self._message(honeypot, attachment_count=1)
                prior.created_at = datetime.now(timezone.utc)
                message.created_at = prior.created_at
                prior_delete = mock.AsyncMock()
                cached_channel = SimpleNamespace(
                    get_partial_message=lambda message_id: SimpleNamespace(delete=prior_delete)
                )
                message.guild.get_channel = lambda channel_id: (
                    cached_channel if channel_id == prior.channel.id else None
                )
                cog.bot.get_guild = lambda guild_id: message.guild
                prior.guild = message.guild
                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": True,
                    "spam_action": "none", "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(
                    side_effect=[[], ["duplicate"]]
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(prior)
                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(snapshot.signals[0].signal.action, honeypot.ActionIntent.NONE)
                self.assertEqual(snapshot.messages[0].delete_status.value, "pending")
                message.delete.assert_not_awaited()
                prior_delete.assert_not_awaited()

    async def test_honeypot_none_still_deletes_outside_dry_run(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0, channel_id=999)
                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "honeypot_channels": [999],
                    "whitelisted_roles": [], "fallback_action": "none",
                    "action": "none", "spam_enabled": False,
                    "firstpost_enabled": False, "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._suspicion_reasons = mock.AsyncMock(return_value=[])
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                message.delete.assert_awaited_once()

    async def test_redelivery_resumes_durable_moderation_action_once(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                message.guild.get_member = lambda user_id: message.author
                message.guild.fetch_ban = mock.AsyncMock(
                    side_effect=honeypot.discord.NotFound("not banned")
                )
                cog.bot.get_guild = lambda guild_id: message.guild
                enforced = mock.AsyncMock()
                attempts = 0

                async def execute(*args, **kwargs):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise RuntimeError("crash after append")
                    await enforced()
                    return honeypot.ModerationEffectResult(
                        "banned",
                        None,
                        honeypot.detection.EffectStatus.SUCCEEDED,
                    )

                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": True,
                    "spam_action": "ban", "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._execute_action = mock.AsyncMock(side_effect=execute)
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)
                failed = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                operation = next(
                    item for item in failed.operations
                    if item.operation_type == "moderation_action"
                )
                self.assertEqual(operation.status.value, "failed")

                await cog.on_message(message)

                completed = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                operation = next(
                    item for item in completed.operations
                    if item.operation_type == "moderation_action"
                )
                self.assertEqual(operation.status.value, "succeeded")
                self.assertEqual(operation.attempts, 2)
                self.assertEqual(operation.result, "ban")
                enforced.assert_awaited_once()
                message.attachments[0].read.assert_awaited_once()
                cog._scan_all_case_message_images.assert_awaited_once()

    async def test_concurrent_firstpost_messages_have_one_action_owner(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                other = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                await asyncio.to_thread(other._case_store.initialize)
                cog._firstpost_loaded_guilds.add(100)
                other._firstpost_loaded_guilds.add(100)
                first = self._message(
                    honeypot, attachment_count=4, message_id=300, channel_id=400
                )
                second = self._message(
                    honeypot, attachment_count=4, message_id=301, channel_id=401
                )
                second.guild = first.guild
                second.author = first.author
                first.guild.get_member = lambda user_id: first.author
                cog.bot.get_guild = lambda guild_id: first.guild
                other.bot.get_guild = lambda guild_id: first.guild
                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": False,
                    "firstpost_enabled": True, "firstpost_action": "ban",
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                self._configure_public_boundary(other, config)
                cog._is_forward_purge_active.return_value = False
                other._is_forward_purge_active.return_value = False
                cog._execute_action = mock.AsyncMock(return_value=("banned", None))
                other._execute_action = mock.AsyncMock(return_value=("banned", None))
                cog._scan_all_case_message_images = mock.AsyncMock()
                other._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                other._publish_detection_case = mock.AsyncMock()

                await asyncio.gather(cog.on_message(first), other.on_message(second))

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, first.guild.id, first.author.id
                )
                firstpost_signals = [
                    item for item in snapshot.signals
                    if item.signal.detector == "firstpost"
                ]
                self.assertEqual(len(firstpost_signals), 1)
                self.assertEqual(
                    cog._execute_action.await_count + other._execute_action.await_count, 1
                )
