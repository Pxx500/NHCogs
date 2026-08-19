"""Detection signal collection: how honeypot, spam, firstpost and image
signals are gathered from a message before the pipeline acts on them.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import get_ident
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules, drain_background_work


class DetectionSignalCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_detection_stats_cover_detector_hits_intents_and_catches(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._increment_stat = mock.AsyncMock()
                cog._record_daily_stat = mock.AsyncMock()
                guild = SimpleNamespace(id=10)
                occurred_at = datetime(2026, 8, 19, 20, tzinfo=timezone.utc)
                signals = (
                    honeypot.DetectionSignal(
                        "firstpost",
                        "young account",
                        honeypot.ActionIntent.REVIEW,
                        True,
                        {},
                    ),
                    honeypot.DetectionSignal(
                        "spam",
                        "duplicate",
                        honeypot.ActionIntent.BAN,
                        True,
                        {},
                    ),
                )

                await honeypot.detection._record_detection_stats(
                    cog, guild, signals, occurred_at
                )

                keys = [call.args[1] for call in cog._increment_stat.await_args_list]
                self.assertEqual(
                    set(keys),
                    {
                        "detections",
                        "suspicious",
                        "firstpost_hits",
                        "firstpost_reviews",
                        "early_catches",
                        "spam_hits",
                        "spam_bans",
                        "spam_catches",
                    },
                )
                cog._record_daily_stat.assert_awaited_once_with(
                    guild, occurred_at, "detections"
                )

    async def test_detection_daily_stat_precedes_fallible_lifetime_counter(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._increment_stat = mock.AsyncMock(
                    side_effect=RuntimeError("config unavailable")
                )
                cog._record_daily_stat = mock.AsyncMock()
                guild = SimpleNamespace(id=10)
                occurred_at = datetime(2026, 8, 19, 20, tzinfo=timezone.utc)
                signals = (
                    honeypot.DetectionSignal(
                        "honeypot",
                        "trap message",
                        honeypot.ActionIntent.REVIEW,
                        True,
                        {},
                    ),
                )

                with self.assertRaisesRegex(RuntimeError, "config unavailable"):
                    await honeypot.detection._record_detection_stats(
                        cog, guild, signals, occurred_at
                    )

                cog._record_daily_stat.assert_awaited_once_with(
                    guild, occurred_at, "detections"
                )

    async def test_whitelist_bypass_only_increments_whitelisted_stat(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                cog._increment_stat = mock.AsyncMock()
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                guild = SimpleNamespace(id=10)
                message = SimpleNamespace(
                    id=40,
                    guild=guild,
                    author=SimpleNamespace(
                        id=20,
                        display_name="Allowed User",
                        display_avatar=None,
                        created_at=now,
                        joined_at=now,
                    ),
                    channel=SimpleNamespace(id=30),
                    content="allowed message",
                    created_at=now,
                    jump_url="https://discord.com/channels/10/30/40",
                    attachments=[],
                )
                signal = honeypot.DetectionSignal(
                    "honeypot",
                    "Message posted in a configured honeypot channel",
                    honeypot.ActionIntent.NONE,
                    True,
                    {"whitelist_bypass": True},
                )

                await cog._process_detected_message(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {"review_enabled": True}
                    ),
                    (signal,),
                )

                self.assertEqual(
                    [call.args[1] for call in cog._increment_stat.await_args_list],
                    ["whitelisted"],
                )

    @staticmethod
    def _message(*, attachments=None, content="", channel_id=3, roles=()):
        return SimpleNamespace(
            id=42,
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=2, roles=list(roles)),
            channel=SimpleNamespace(id=channel_id),
            content=content,
            attachments=list(attachments or []),
        )

    async def test_active_forward_purge_collects_decisive_containment_signal(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = SimpleNamespace(
                    id=42,
                    guild=SimpleNamespace(id=1),
                    author=SimpleNamespace(id=2),
                    channel=SimpleNamespace(id=3),
                    content="",
                    attachments=[],
                )
                cog._is_forward_purge_active = mock.Mock(return_value=True)
                message.delete = mock.AsyncMock()

                signals = await cog._collect_detection_signals(
                    message, honeypot.GuildSettings.from_mapping({})
                )

                self.assertEqual(len(signals), 1)
                signal = signals[0]
                self.assertEqual(signal.detector, "forward_purge")
                self.assertEqual(signal.action, honeypot.ActionIntent.REVIEW)
                self.assertTrue(signal.decisive)
                self.assertTrue(signal.metadata["containment_required"])
                message.delete.assert_not_awaited()

    async def test_forward_purge_retains_cheap_context_and_skips_image_and_effects(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message(attachments=[object()] * 4)
                message.delete = mock.AsyncMock()
                cog._is_forward_purge_active = mock.Mock(return_value=True)
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._firstpost_loaded_guilds.add(message.guild.id)
                cog._initial_image_signal = mock.AsyncMock()
                cog._send_review = mock.AsyncMock()
                cog._increment_stat = mock.AsyncMock()

                signals = await cog._collect_detection_signals(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {
                            "spam_enabled": True,
                            "spam_action": "review",
                            "firstpost_enabled": True,
                            "firstpost_action": "kick",
                            "imagescan_detector_enabled": True,
                        }
                    ),
                )

                self.assertEqual(
                    [signal.detector for signal in signals],
                    ["forward_purge", "spam", "firstpost"],
                )
                cog._initial_image_signal.assert_not_awaited()
                message.delete.assert_not_awaited()
                cog._send_review.assert_not_awaited()
                cog._increment_stat.assert_not_awaited()

    async def test_spam_and_firstpost_signals_are_both_collected_in_priority_order(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message(attachments=[object()] * 4)
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._firstpost_loaded_guilds.add(message.guild.id)

                signals = await cog._collect_detection_signals(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {
                            "spam_enabled": True,
                            "spam_action": "none",
                            "firstpost_enabled": True,
                            "firstpost_action": "kick",
                        }
                    ),
                )

                self.assertEqual([signal.detector for signal in signals], ["spam", "firstpost"])
                self.assertEqual(signals[0].action, honeypot.ActionIntent.NONE)
                self.assertEqual(signals[1].action, honeypot.ActionIntent.KICK)

    async def test_invalid_spam_action_defaults_to_review(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message()
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])

                signals = await cog._collect_detection_signals(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {"spam_enabled": True, "spam_action": "invalid"}
                    ),
                )

                self.assertEqual(signals[0].action, honeypot.ActionIntent.REVIEW)

    async def test_dry_run_preserves_spam_action_intent(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message()
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])

                signals = await cog._collect_detection_signals(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {
                            "dry_run": True,
                            "spam_enabled": True,
                            "spam_action": "review",
                        }
                    ),
                )

                self.assertEqual(signals[0].action, honeypot.ActionIntent.REVIEW)

    async def test_collector_does_not_filter_protected_members(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message()
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._is_protected_member = mock.AsyncMock(return_value=True)

                signals = await cog._collect_detection_signals(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {"spam_enabled": True, "spam_action": "review"}
                    ),
                )

                self.assertEqual([signal.detector for signal in signals], ["spam"])
                cog._is_protected_member.assert_not_awaited()

    async def test_three_attachments_do_not_produce_a_firstpost_signal(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message(attachments=[object()] * 3)
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._firstpost_loaded_guilds.add(message.guild.id)

                signals = await cog._collect_detection_signals(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {"firstpost_enabled": True}
                    ),
                )

                self.assertEqual(signals, ())

    async def test_collect_only_firstpost_does_not_reserve_before_case_append(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                message = self._message(attachments=[object()] * 4)
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._firstpost_loaded_guilds.add(message.guild.id)

                signals = await cog._collect_detection_signals(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {"firstpost_collect_enabled": True}
                    ),
                )

                self.assertEqual(signals, ())

    async def test_honeypot_channel_collects_signal_and_other_channel_does_not(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._suspicion_reasons = mock.AsyncMock(return_value=["young account"])
                config = {"honeypot_channels": [9], "action": "ban"}

                hit = await cog._collect_detection_signals(
                    self._message(channel_id=9),
                    honeypot.GuildSettings.from_mapping(config),
                )
                miss = await cog._collect_detection_signals(
                    self._message(channel_id=8),
                    honeypot.GuildSettings.from_mapping(config),
                )

                self.assertEqual([signal.detector for signal in hit], ["honeypot"])
                self.assertEqual(hit[0].action, honeypot.ActionIntent.BAN)
                self.assertEqual(miss, ())

    async def test_legacy_honeypot_channel_field_does_not_enable_detection(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._suspicion_reasons = mock.AsyncMock(
                    return_value=["young account"]
                )

                signals = await cog._collect_detection_signals(
                    self._message(channel_id=9),
                    honeypot.GuildSettings.from_mapping(
                        {"honeypot_channel": 9, "action": "ban"}
                    ),
                )

                self.assertEqual(signals, ())

    async def test_explicitly_empty_detection_lists_do_not_restore_defaults(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                settings = honeypot.GuildSettings.from_mapping(
                    {
                        "scam_keywords": [],
                        "attachment_patterns": [],
                    }
                )
                author = SimpleNamespace(
                    created_at=datetime.now(timezone.utc) - timedelta(days=30)
                )
                keyword_message = self._message(content="free nitro")
                keyword_message.author = author
                attachment_message = self._message(
                    attachments=[
                        SimpleNamespace(
                            content_type="application/octet-stream",
                            filename="image.png",
                        ),
                        SimpleNamespace(
                            content_type="application/octet-stream",
                            filename="image (1).png",
                        ),
                    ]
                )
                attachment_message.author = author

                keyword_reasons = await cog._suspicion_reasons(
                    keyword_message, settings
                )
                attachment_reasons = await cog._suspicion_reasons(
                    attachment_message, settings
                )

                self.assertNotIn("Matched keywords: free nitro", keyword_reasons)
                self.assertFalse(
                    any(
                        reason.startswith("Matched attachment rules:")
                        for reason in attachment_reasons
                    )
                )

    async def test_whitelist_bypass_still_collects_non_actionable_honeypot_signal(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                role = SimpleNamespace(id=7)

                signals = await cog._collect_detection_signals(
                    self._message(channel_id=9, roles=[role]),
                    honeypot.GuildSettings.from_mapping(
                        {
                            "honeypot_channels": [9],
                            "whitelisted_roles": [7],
                            "whitelist_mode": "bypass",
                            "action": "ban",
                        }
                    ),
                )

                self.assertEqual(signals[0].action, honeypot.ActionIntent.NONE)
                self.assertTrue(signals[0].metadata["whitelist_bypass"])

    async def test_forward_purge_is_preserved_for_whitelist_bypass_honeypot(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                role = SimpleNamespace(id=7)
                message = self._message(channel_id=9, roles=[role])
                cog._is_forward_purge_active = mock.Mock(return_value=True)
                cog._initial_image_signal = mock.AsyncMock()

                signals = await cog._collect_detection_signals(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {
                            "honeypot_channels": [9],
                            "whitelisted_roles": [7],
                            "whitelist_mode": "bypass",
                        }
                    ),
                )

                self.assertEqual(
                    [signal.detector for signal in signals],
                    ["forward_purge", "honeypot"],
                )
                self.assertEqual(signals[0].action, honeypot.ActionIntent.REVIEW)
                self.assertEqual(signals[1].action, honeypot.ActionIntent.NONE)
                cog._initial_image_signal.assert_not_awaited()

    async def test_image_signal_stops_after_first_match_and_returns_serializable_match(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await cog._init_imagescan_store()
                started = 0
                four_started = asyncio.Event()
                release_non_matches = asyncio.Event()

                def image_read(index):
                    async def read(*args, **kwargs):
                        nonlocal started
                        started += 1
                        if started == 4:
                            four_started.set()
                        if index != 3:
                            await release_non_matches.wait()
                        return f"image-{index}".encode()

                    return read

                attachments = [
                    SimpleNamespace(
                        filename=f"image-{index}.png",
                        content_type="image/png",
                        read=mock.AsyncMock(side_effect=image_read(index)),
                    )
                    for index in range(1, 7)
                ]
                for attachment in attachments:
                    async def read_bounded(max_bytes, *, _attachment=attachment):
                        data = await _attachment.read(use_cached=True)
                        return data[: max_bytes + 1]

                    attachment.read_bounded = read_bounded
                message = self._message(attachments=attachments)
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._imagescan_load_samples = mock.AsyncMock(
                    return_value=[SimpleNamespace(decision="true_positive")]
                )
                cog._imagescan_model_state = mock.AsyncMock(
                    return_value={"valid": True, "effective_threshold": 20}
                )
                cog._increment_stat = mock.AsyncMock()
                cog._send_review = mock.AsyncMock()
                event_loop_thread = get_ident()
                match_threads = []

                def match(hashes, samples, threshold):
                    match_threads.append(get_ident())
                    return {
                        "matched": hashes["sha256"] == "image-3",
                        "exact_decision": "true_positive"
                        if hashes["sha256"] == "image-3"
                        else None,
                        "score": 0 if hashes["sha256"] == "image-3" else 3,
                        "threshold": threshold,
                    }

                with (
                    mock.patch.object(
                        honeypot.imagescan,
                        "image_hashes_from_bytes",
                        side_effect=lambda data: {
                            "sha256": data.decode(),
                            "phash": "00",
                            "dhash": "00",
                            "ahash": "00",
                        },
                    ),
                    mock.patch.object(
                        honeypot.imagescan,
                        "match_image",
                        side_effect=match,
                    ),
                ):
                    signal_task = asyncio.create_task(
                        cog._collect_detection_signals(
                            message,
                            honeypot.GuildSettings.from_mapping(
                                {
                                    "imagescan_detector_enabled": True,
                                    "imagescan_detector_action": "invalid",
                                }
                            ),
                        )
                    )
                    try:
                        await asyncio.sleep(0.05)
                        self.assertEqual(started, 4)
                        signals = await asyncio.wait_for(
                            asyncio.shield(signal_task), timeout=0.2
                        )
                        self.assertFalse(release_non_matches.is_set())
                    finally:
                        release_non_matches.set()
                        await signal_task

                self.assertEqual([signal.detector for signal in signals], ["image"])
                self.assertEqual(signals[0].action, honeypot.ActionIntent.REVIEW)
                self.assertEqual(
                    [match["position"] for match in signals[0].metadata["matches"]], [3]
                )
                self.assertEqual(signals[0].metadata["matches"][0]["exact_decision"], "true_positive")
                self.assertEqual(signals[0].metadata["matches"][0]["threshold"], 20)
                for attachment in attachments[:4]:
                    attachment.read.assert_awaited_once()
                for attachment in attachments[4:]:
                    attachment.read.assert_not_awaited()
                self.assertGreaterEqual(len(match_threads), 1)
                self.assertTrue(
                    all(thread_id != event_loop_thread for thread_id in match_threads)
                )
                profile = await cog._imagescan_profile(message.guild.id)
                self.assertEqual(profile["messages_scanned"], 1)
                self.assertEqual(profile["messages_with_images"], 1)
                self.assertGreaterEqual(profile["images_considered"], 1)
                self.assertEqual(profile["decision_ms_count"], 1)
                self.assertGreaterEqual(profile["download_ms_count"], 1)
                self.assertGreaterEqual(profile["hash_ms_count"], 1)
                self.assertGreaterEqual(profile["compare_ms_count"], 1)
                cog._increment_stat.assert_not_awaited()
                cog._send_review.assert_not_awaited()
                await drain_background_work(cog)

    async def test_four_negative_initial_images_do_not_scan_later_attachments(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                attachments = [
                    SimpleNamespace(
                        filename=f"image-{index}.png",
                        content_type="image/png",
                        read=mock.AsyncMock(return_value=f"image-{index}".encode()),
                    )
                    for index in range(1, 7)
                ]
                message = self._message(attachments=attachments)
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._imagescan_load_samples = mock.AsyncMock(
                    return_value=[SimpleNamespace(decision="true_positive")]
                )
                cog._imagescan_model_state = mock.AsyncMock(
                    return_value={"valid": True, "effective_threshold": 20}
                )
                cog._imagescan_increment_profile = mock.AsyncMock()
                with (
                    mock.patch.object(
                        honeypot.imagescan,
                        "image_hashes_from_bytes",
                        side_effect=lambda data: {"sha256": data.decode()},
                    ),
                    mock.patch.object(
                        honeypot.imagescan,
                        "match_image",
                        return_value={"matched": False, "score": None},
                    ),
                ):
                    signals = await cog._collect_detection_signals(
                        message,
                        honeypot.GuildSettings.from_mapping(
                            {"imagescan_detector_enabled": True}
                        ),
                    )

                self.assertNotIn(
                    (message.guild.id, message.id), cog._initial_image_scan_batches
                )

                self.assertEqual(signals, ())
                for attachment in attachments[:4]:
                    attachment.read.assert_awaited_once()
                for attachment in attachments[4:]:
                    attachment.read.assert_not_awaited()

    async def test_decisive_non_image_signal_skips_initial_image_reads(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                attachment = SimpleNamespace(
                    filename="image.png",
                    content_type="image/png",
                    read=mock.AsyncMock(),
                )
                message = self._message(attachments=[attachment])
                cog._is_forward_purge_active = mock.Mock(return_value=False)
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])

                signals = await cog._collect_detection_signals(
                    message,
                    honeypot.GuildSettings.from_mapping(
                        {
                            "spam_enabled": True,
                            "spam_action": "none",
                            "imagescan_detector_enabled": True,
                        }
                    ),
                )

                self.assertEqual([signal.detector for signal in signals], ["spam"])
                attachment.read.assert_not_awaited()
