"""Detection signals admitted through on_message: the case that is stored
and the counters a moderator can read back.
"""

import asyncio
import sqlite3
import unittest
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import (
    _Bot,
    _isolated_honeypot_modules,
    _operational_support,
    drain_background_work,
)


class DetectionSignalOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._now = datetime.now(timezone.utc).replace(microsecond=0)

    async def test_spam_records_case_signals_and_stat_counters(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                attachments = self._attachments(4)
                message = self._message(
                    guild,
                    author,
                    content="free nitro",
                    attachments=attachments,
                    channel_id=30,
                    message_id=300,
                )
                await self._settings(
                    cog,
                    guild,
                    spam_enabled=True,
                    spam_action="ban",
                    dry_run=True,
                )
                await self._observe_duplicate(cog, honeypot, message)

                await cog.on_message(message)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                signals = self._signals(snapshot, snapshot.messages[-1].sequence)
                self.assertEqual(
                    [signal.detector for signal in signals],
                    ["spam"],
                )
                self.assertEqual(signals[0].action, honeypot.ActionIntent.BAN)
                moderation = [
                    operation
                    for operation in snapshot.operations
                    if operation.operation_type
                    is honeypot.OperationType.MODERATION_ACTION
                ]
                self.assertEqual(
                    [operation.result for operation in moderation],
                    ["planned_ban"],
                )
                stats = self._stats(cog)
                for key in (
                    "detections",
                    "suspicious",
                    "spam_hits",
                    "spam_bans",
                    "spam_catches",
                ):
                    self.assertGreaterEqual(stats.get(key, 0), 1, key)
                self.assertEqual(stats.get("image_hits", 0), 0)
                daily = await asyncio.to_thread(
                    cog._case_store.get_daily_stats,
                    guild.id,
                    message.created_at.astimezone(timezone.utc).date(),
                )
                self.assertGreaterEqual(daily.detections, 1)
                message.delete.assert_not_awaited()
                await self._finish(cog)

    async def test_daily_detection_count_survives_lifetime_counter_failure(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                message = self._message(
                    guild, author, content="free nitro", channel_id=30
                )
                await self._settings(
                    cog,
                    guild,
                    spam_enabled=True,
                    spam_action="review",
                )
                await self._observe_duplicate(cog, honeypot, message)
                cog.config = _FailingStatsConfig(cog.config)

                with self.assertRaisesRegex(RuntimeError, "config unavailable"):
                    await cog.on_message(message)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                self.assertEqual(
                    [signal.detector for signal in self._signals(snapshot)],
                    ["spam"],
                )
                self.assertEqual(self._stats(cog).get("detections", 0), 0)
                daily = await asyncio.to_thread(
                    cog._case_store.get_daily_stats,
                    guild.id,
                    message.created_at.astimezone(timezone.utc).date(),
                )
                self.assertEqual(daily.detections, 1)
                await self._finish(cog)

    async def test_whitelist_bypass_records_only_the_whitelisted_counter(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                author.roles = [SimpleNamespace(id=7)]
                message = self._message(
                    guild, author, content="hello", channel_id=9
                )
                await self._settings(
                    cog,
                    guild,
                    honeypot_channels=[9],
                    whitelisted_roles=[7],
                    whitelist_mode="bypass",
                    action="ban",
                    fallback_action="none",
                )

                await cog.on_message(message)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                signal = self._signals(snapshot)[0]
                self.assertEqual(signal.detector, "honeypot")
                self.assertEqual(signal.action, honeypot.ActionIntent.NONE)
                self.assertTrue(signal.metadata["whitelist_bypass"])
                self.assertNotIn(
                    honeypot.OperationType.MODERATION_ACTION,
                    {item.operation_type for item in snapshot.operations},
                )
                self.assertEqual(snapshot.messages[0].delete_status.value, "pending")
                self.assertEqual(self._stats(cog), {"whitelisted": 1})
                daily = await asyncio.to_thread(
                    cog._case_store.get_daily_stats,
                    guild.id,
                    message.created_at.astimezone(timezone.utc).date(),
                )
                self.assertEqual(daily.detections, 0)
                message.delete.assert_not_awaited()
                await self._finish(cog)

    async def test_invalid_spam_action_is_stored_as_review(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                message = self._message(
                    guild, author, content="free nitro", channel_id=30
                )
                await self._settings(
                    cog,
                    guild,
                    spam_enabled=True,
                    spam_action="invalid",
                )
                await self._observe_duplicate(cog, honeypot, message)

                await cog.on_message(message)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                signal = self._signals(snapshot)[0]
                self.assertEqual(signal.detector, "spam")
                self.assertEqual(signal.action, honeypot.ActionIntent.REVIEW)
                self.assertGreaterEqual(self._stats(cog).get("spam_reviews", 0), 1)
                self.assertEqual(self._stats(cog).get("spam_bans", 0), 0)
                await self._finish(cog)

    async def test_dry_run_keeps_the_spam_review_on_the_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                message = self._message(
                    guild, author, content="free nitro", channel_id=30
                )
                await self._settings(
                    cog,
                    guild,
                    dry_run=True,
                    spam_enabled=True,
                    spam_action="review",
                )
                await self._observe_duplicate(cog, honeypot, message)

                await cog.on_message(message)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                signal = self._signals(snapshot)[0]
                self.assertEqual(signal.action, honeypot.ActionIntent.REVIEW)
                self.assertEqual(snapshot.messages[0].delete_status.value, "planned")
                self.assertGreaterEqual(self._stats(cog).get("spam_reviews", 0), 1)
                message.delete.assert_not_awaited()
                await self._finish(cog)

    async def test_protected_member_spam_does_not_open_a_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot, protected=True)
                message = self._message(
                    guild, author, content="free nitro", channel_id=30
                )
                await self._settings(cog, guild, spam_enabled=True, spam_action="ban")
                await self._observe_duplicate(cog, honeypot, message)

                await cog.on_message(message)

                self.assertIsNone(await self._snapshot(cog, guild.id, author.id))
                self.assertEqual(self._stats(cog).get("spam_hits", 0), 0)
                self.assertEqual(self._stats(cog).get("detections", 0), 0)
                message.delete.assert_not_awaited()
                author.ban.assert_not_awaited()
                await self._finish(cog)

    async def test_retired_firstpost_settings_do_not_create_cases_or_block_image_detection(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                message = self._message(
                    guild,
                    author,
                    content="hello",
                    attachments=self._attachments(4),
                )
                await self._settings(cog, guild, firstpost_enabled=True)

                await cog.on_message(message)

                self.assertIsNone(await self._snapshot(cog, guild.id, author.id))
                self.assertEqual(self._stats(cog).get("firstpost_hits", 0), 0)
                await cog._init_imagescan_store()
                self._insert_sample(cog, guild.id, sha256="known-bad")
                await self._settings(cog, guild, imagescan_detector_enabled=True)
                image_message = self._message(
                    guild, author, content="hello", message_id=301,
                    attachments=self._attachments(4, payloads=[b"known-bad", b"nope", b"nope", b"nope"]),
                )
                with mock.patch.object(honeypot.imagescan, "image_hashes_from_bytes", side_effect=self._hashes_from_bytes):
                    await cog.on_message(image_message)
                snapshot = await self._snapshot(cog, guild.id, author.id)
                self.assertEqual([signal.detector for signal in self._signals(snapshot)], ["image"])
                await self._finish(cog)


    async def test_honeypot_channel_opens_a_ban_case_and_another_channel_does_not(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot, young=True)
                await self._settings(
                    cog,
                    guild,
                    honeypot_channels=[9],
                    action="ban",
                    fallback_action="none",
                )
                missed = self._message(
                    guild, author, content="hello", channel_id=8, message_id=300
                )
                caught = self._message(
                    guild, author, content="hello", channel_id=9, message_id=301
                )

                await cog.on_message(missed)
                self.assertIsNone(await self._snapshot(cog, guild.id, author.id))

                await cog.on_message(caught)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                signal = self._signals(snapshot)[0]
                self.assertEqual(signal.detector, "honeypot")
                self.assertEqual(signal.action, honeypot.ActionIntent.BAN)
                self.assertEqual(snapshot.messages[0].channel_id, 9)
                self.assertGreaterEqual(self._stats(cog).get("honeypot_bans", 0), 1)
                self.assertGreaterEqual(self._stats(cog).get("honeypot_hits", 0), 1)
                author.ban.assert_awaited()
                missed.delete.assert_not_awaited()
                await self._finish(cog)

    async def test_legacy_honeypot_channel_field_does_not_open_a_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot, young=True)
                await self._settings(
                    cog,
                    guild,
                    honeypot_channel=9,
                    action="ban",
                )
                message = self._message(
                    guild, author, content="hello", channel_id=9
                )

                await cog.on_message(message)

                self.assertIsNone(await self._snapshot(cog, guild.id, author.id))
                self.assertEqual(self._stats(cog).get("honeypot_hits", 0), 0)
                author.ban.assert_not_awaited()
                await self._finish(cog)

    async def test_empty_keyword_and_attachment_lists_do_not_restore_defaults(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                author.created_at = self._now - timedelta(days=30)
                await self._settings(
                    cog,
                    guild,
                    honeypot_channels=[9],
                    action="ban",
                    fallback_action="none",
                    scam_keywords=[],
                    attachment_patterns=[],
                )
                keyword_message = self._message(
                    guild, author, content="free nitro", channel_id=9, message_id=300
                )

                await cog.on_message(keyword_message)

                keyword_case = await self._snapshot(cog, guild.id, author.id)
                keyword_signal = self._signals(keyword_case)[0]
                self.assertEqual(keyword_signal.action, honeypot.ActionIntent.NONE)
                self.assertNotIn("free nitro", keyword_signal.reason.lower())
                self.assertEqual(self._stats(cog).get("honeypot_bans", 0), 0)

                other = SimpleNamespace(**author.__dict__)
                other.id = 201
                other.ban = mock.AsyncMock()
                other.kick = mock.AsyncMock()
                attachment_message = self._message(
                    guild,
                    other,
                    content="",
                    attachments=[
                        self._attachment("image.png", b"one"),
                        self._attachment("image (1).png", b"two"),
                    ],
                    channel_id=9,
                    message_id=301,
                )

                await cog.on_message(attachment_message)

                attachment_case = await self._snapshot(cog, guild.id, other.id)
                attachment_signal = self._signals(attachment_case)[0]
                reasons = attachment_signal.metadata.get("reasons") or ()
                self.assertFalse(
                    any(str(reason).startswith("Matched attachment rules:") for reason in reasons)
                )
                await self._finish(cog)

    async def test_forward_purge_keeps_later_signals_and_skips_image_match(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                await cog._init_imagescan_store()
                self._insert_sample(cog, guild.id, sha256="known-bad")
                await self._settings(
                    cog,
                    guild,
                    spam_enabled=True,
                    spam_action="review",
                    purge_forward_seconds=60,
                    imagescan_detector_enabled=True,
                )
                primer = self._message(
                    guild, author, content="free nitro", channel_id=30, message_id=300
                )
                await self._observe_duplicate(cog, honeypot, primer, message_id=299)
                await cog.on_message(primer)

                await self._settings(
                    cog,
                    guild,
                    spam_action="ban",
                )
                attachments = self._attachments(4, payloads=[b"known-bad", b"nope", b"nope", b"nope"])
                attachments[0].read = mock.AsyncMock(wraps=attachments[0].read)
                follow_up = self._message(
                    guild,
                    author,
                    content="different text",
                    attachments=attachments,
                    channel_id=40,
                    message_id=301,
                    created_at=self._now + timedelta(seconds=1),
                )
                await self._observe_duplicate(
                    cog, honeypot, follow_up, channel_id=41, message_id=298
                )
                with mock.patch.object(
                    honeypot.imagescan,
                    "image_hashes_from_bytes",
                    side_effect=self._hashes_from_bytes,
                ):
                    await cog.on_message(follow_up)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                follow_signals = self._signals(snapshot, snapshot.messages[-1].sequence)
                self.assertEqual(
                    [signal.detector for signal in follow_signals],
                    ["forward_purge", "spam"],
                )
                self.assertEqual(follow_signals[0].action, honeypot.ActionIntent.REVIEW)
                self.assertTrue(follow_signals[0].metadata["containment_required"])
                self.assertEqual(follow_signals[1].action, honeypot.ActionIntent.BAN)
                self.assertEqual(self._stats(cog).get("image_hits", 0), 0)
                stored = next(
                    item
                    for item in snapshot.attachments
                    if item.message_sequence == snapshot.messages[-1].sequence
                    and item.filename == "file-0.png"
                )
                self.assertEqual(stored.capture_status, "captured")
                self.assertIsNotNone(stored.evidence_path)
                self.assertEqual(stored.sha256, "known-bad")
                attachments[0].read.assert_awaited()
                await self._finish(cog)

    async def test_review_only_admission_stores_no_moderation_action(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                message = self._message(
                    guild, author, content="free nitro", channel_id=30
                )
                await self._settings(
                    cog,
                    guild,
                    spam_enabled=True,
                    spam_action="review",
                    dry_run=True,
                )
                await self._observe_duplicate(cog, honeypot, message)

                await cog.on_message(message)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                self.assertEqual(
                    [signal.action for signal in self._signals(snapshot)],
                    [honeypot.ActionIntent.REVIEW],
                )
                self.assertFalse(
                    any(
                        operation.operation_type
                        is honeypot.OperationType.MODERATION_ACTION
                        for operation in snapshot.operations
                    )
                )
                await self._finish(cog)

    async def test_whitelist_bypass_during_forward_purge_stays_non_actionable(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                await cog._init_imagescan_store()
                self._insert_sample(cog, guild.id, sha256="known-bad")
                await self._settings(
                    cog,
                    guild,
                    spam_enabled=True,
                    spam_action="review",
                    purge_forward_seconds=60,
                    imagescan_detector_enabled=True,
                )
                primer = self._message(
                    guild, author, content="free nitro", channel_id=30, message_id=300
                )
                await self._observe_duplicate(cog, honeypot, primer, message_id=299)
                await cog.on_message(primer)
                author.roles = [SimpleNamespace(id=7)]
                await self._settings(
                    cog,
                    guild,
                    honeypot_channels=[9],
                    whitelisted_roles=[7],
                    whitelist_mode="bypass",
                    action="ban",
                )
                attachment = self._attachment("proof.png", b"known-bad")
                follow_up = self._message(
                    guild,
                    author,
                    content="allowed",
                    attachments=[attachment],
                    channel_id=9,
                    message_id=301,
                )
                with mock.patch.object(
                    honeypot.imagescan,
                    "image_hashes_from_bytes",
                    side_effect=self._hashes_from_bytes,
                ):
                    await cog.on_message(follow_up)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                signals = self._signals(snapshot, snapshot.messages[-1].sequence)
                self.assertEqual(
                    [signal.detector for signal in signals],
                    ["forward_purge", "honeypot"],
                )
                self.assertEqual(signals[1].action, honeypot.ActionIntent.NONE)
                self.assertTrue(signals[1].metadata["whitelist_bypass"])
                self.assertGreaterEqual(self._stats(cog).get("whitelisted", 0), 1)
                self.assertEqual(self._stats(cog).get("honeypot_bans", 0), 0)
                self.assertEqual(self._stats(cog).get("image_hits", 0), 0)
                author.ban.assert_not_awaited()
                await self._finish(cog)

    async def test_image_match_is_on_the_case_before_slower_images_finish(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                await cog._init_imagescan_store()
                self._insert_sample(cog, guild.id, sha256="image-3")
                release = asyncio.Event()
                started = []

                def image_read(index):
                    async def read(*args, **kwargs):
                        started.append(index)
                        if index != 3:
                            await release.wait()
                        return f"image-{index}".encode()

                    return read

                attachments = []
                for index in range(1, 7):
                    attachment = self._attachment(f"image-{index}.png", b"pending")
                    attachment.read = image_read(index)
                    attachments.append(attachment)
                message = self._message(
                    guild,
                    author,
                    content="hello",
                    attachments=attachments,
                    channel_id=30,
                )
                await self._settings(
                    cog,
                    guild,
                    imagescan_detector_enabled=True,
                    imagescan_detector_action="invalid",
                    imagescan_detector_threshold=20,
                )
                with mock.patch.object(
                    honeypot.imagescan,
                    "image_hashes_from_bytes",
                    side_effect=self._hashes_from_bytes,
                ):
                    processing = asyncio.create_task(cog.on_message(message))
                    try:
                        snapshot = await self._wait_for_stat(
                            cog, guild.id, author.id, "image_hits", processing
                        )
                        self.assertFalse(release.is_set())
                        self.assertFalse(processing.done())
                        signal = self._signals(snapshot)[0]
                        self.assertEqual(signal.detector, "image")
                        self.assertEqual(signal.action, honeypot.ActionIntent.REVIEW)
                        match = signal.metadata["matches"][0]
                        self.assertEqual(match["position"], 3)
                        self.assertEqual(match["exact_decision"], "true_positive")
                        self.assertEqual(match["threshold"], 20)
                        self.assertGreaterEqual(self._stats(cog).get("image_reviews", 0), 1)
                        self.assertGreaterEqual(self._stats(cog).get("image_catches", 0), 1)
                        daily = await asyncio.to_thread(
                            cog._case_store.get_daily_stats,
                            guild.id,
                            message.created_at.astimezone(timezone.utc).date(),
                        )
                        self.assertGreaterEqual(daily.detections, 1)
                        profile = await cog._imagescan_profile(guild.id)
                        self.assertEqual(profile["messages_scanned"], 1)
                        self.assertEqual(profile["messages_with_images"], 1)
                        self.assertGreaterEqual(profile["images_considered"], 1)
                        self.assertEqual(profile["decision_ms_count"], 1)
                        self.assertGreaterEqual(profile["download_ms_count"], 1)
                        self.assertGreaterEqual(profile["hash_ms_count"], 1)
                        self.assertGreaterEqual(profile["compare_ms_count"], 1)
                        self.assertFalse(any(index > 4 for index in started))
                    finally:
                        release.set()
                        await processing
                self.assertGreaterEqual(len(started), 4)
                await self._finish(cog)

    async def test_unmatched_images_past_the_initial_limit_are_not_downloaded(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                await cog._init_imagescan_store()
                self._insert_sample(cog, guild.id, sha256="known-bad")
                attachments = [
                    self._attachment(f"image-{index}.png", f"image-{index}".encode())
                    for index in range(1, 7)
                ]
                for attachment in attachments:
                    attachment.read = mock.AsyncMock(wraps=attachment.read)
                message = self._message(
                    guild, author, content="hello", attachments=attachments
                )
                await self._settings(
                    cog,
                    guild,
                    imagescan_detector_enabled=True,
                    imagescan_detector_threshold=20,
                )
                with mock.patch.object(
                    honeypot.imagescan,
                    "image_hashes_from_bytes",
                    side_effect=self._hashes_from_bytes,
                ):
                    await cog.on_message(message)

                self.assertIsNone(await self._snapshot(cog, guild.id, author.id))
                self.assertEqual(self._stats(cog).get("image_hits", 0), 0)
                for attachment in attachments[:4]:
                    attachment.read.assert_awaited()
                for attachment in attachments[4:]:
                    attachment.read.assert_not_awaited()
                await self._finish(cog)

    async def test_spam_hit_does_not_record_an_image_signal(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog, guild, author = await self._open_cog(honeypot)
                await cog._init_imagescan_store()
                self._insert_sample(cog, guild.id, sha256="known-bad")
                attachment = self._attachment("proof.png", b"known-bad")
                attachment.read = mock.AsyncMock(return_value=b"known-bad")
                message = self._message(
                    guild,
                    author,
                    content="free nitro",
                    attachments=[attachment],
                    channel_id=30,
                )
                await self._settings(
                    cog,
                    guild,
                    spam_enabled=True,
                    spam_action="review",
                    imagescan_detector_enabled=True,
                )
                await self._observe_duplicate(cog, honeypot, message)
                with mock.patch.object(
                    honeypot.imagescan,
                    "image_hashes_from_bytes",
                    side_effect=self._hashes_from_bytes,
                ):
                    await cog.on_message(message)

                snapshot = await self._snapshot(cog, guild.id, author.id)
                self.assertEqual(
                    [signal.detector for signal in self._signals(snapshot)],
                    ["spam"],
                )
                self.assertEqual(self._stats(cog).get("image_hits", 0), 0)
                self.assertGreaterEqual(self._stats(cog).get("spam_hits", 0), 1)
                stored = snapshot.attachments[0]
                self.assertEqual(stored.filename, "proof.png")
                self.assertEqual(stored.capture_status, "captured")
                self.assertIsNotNone(stored.evidence_path)
                self.assertEqual(stored.sha256, "known-bad")
                attachment.read.assert_awaited()
                await self._finish(cog)

    async def _open_cog(self, honeypot, *, protected=False, young=False):
        guild = SimpleNamespace(id=100, name="Guild", icon=None)
        guild.get_channel = lambda channel_id: None
        guild.get_thread = lambda channel_id: None
        guild.get_member = lambda user_id: None
        guild.me = SimpleNamespace(
            id=1,
            top_role=10,
            guild_permissions=SimpleNamespace(
                kick_members=True,
                ban_members=True,
                manage_roles=True,
                manage_messages=True,
                view_channel=True,
            ),
        )
        created_at = self._now - timedelta(days=1 if young else 40)
        author = SimpleNamespace(
            id=200,
            bot=False,
            guild=guild,
            roles=[],
            display_name="User",
            display_avatar=None,
            created_at=created_at,
            joined_at=created_at,
            guild_permissions=SimpleNamespace(manage_guild=protected),
            top_role=10 if protected else 1,
            ban=mock.AsyncMock(),
            kick=mock.AsyncMock(),
        )
        bot = _Bot()
        bot.owner_ids = set()
        bot.is_mod = mock.AsyncMock(return_value=False)
        bot.is_admin = mock.AsyncMock(return_value=False)
        bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
        bot.get_guild = lambda guild_id: guild if guild.id == guild_id else None
        bot.loop = asyncio.get_running_loop()

        async def fetch_user(user_id):
            if user_id == author.id:
                return author
            return SimpleNamespace(
                id=user_id,
                ban=mock.AsyncMock(),
                kick=mock.AsyncMock(),
            )

        bot.fetch_user = fetch_user
        cog = honeypot.Honeypot(bot, _operational_support())
        honeypot.modlog.create_case = mock.AsyncMock()
        await asyncio.to_thread(cog._case_store.initialize)
        await cog._message_registry.initialize()
        await self._settings(cog, guild, enabled=True)
        return cog, guild, author

    async def _settings(self, cog, guild, **values):
        guild_config = cog.config.guild(guild)
        for key, value in values.items():
            await guild_config.set_raw(key, value=value)

    def _message(
        self,
        guild,
        author,
        *,
        content,
        attachments=(),
        channel_id=30,
        message_id=300,
        created_at=None,
    ):
        message = SimpleNamespace(
            id=message_id,
            guild=guild,
            author=author,
            channel=SimpleNamespace(id=channel_id),
            content=content,
            attachments=list(attachments),
            created_at=created_at or self._now,
            jump_url=f"https://discord.test/channels/{guild.id}/{channel_id}/{message_id}",
            webhook_id=None,
            embeds=[],
            pinned=False,
        )
        message.delete = mock.AsyncMock()
        return message

    def _attachments(self, count, *, payloads=None):
        payloads = list(payloads or [f"file-{index}".encode() for index in range(count)])
        return [
            self._attachment(f"file-{index}.png", payloads[index])
            for index in range(count)
        ]

    def _attachment(self, filename, payload):
        attachment = SimpleNamespace(
            filename=filename,
            size=len(payload),
            content_type="image/png",
            width=8,
            height=8,
            description=None,
            url=f"https://cdn.test/{filename}",
        )

        async def read(*args, **kwargs):
            return payload

        async def read_bounded(max_bytes):
            return payload[: max_bytes + 1]

        attachment.read = read
        attachment.read_bounded = read_bounded
        attachment.is_spoiler = lambda: False
        return attachment

    async def _observe_duplicate(
        self, cog, honeypot, message, *, channel_id=31, message_id=299
    ):
        await cog._message_registry.observe(
            honeypot.MessageRecord(
                message_id=message_id,
                guild_id=message.guild.id,
                channel_id=channel_id,
                author_id=message.author.id,
                created_at=message.created_at,
                pinned=False,
                author_kind="member",
                fingerprint=honeypot.detection.message_spam_fingerprint(message),
            )
        )

    def _insert_sample(self, cog, guild_id, *, sha256):
        cog._imagescan_store.insert(
            {
                "sample_id": f"sample-{sha256}",
                "guild_id": str(guild_id),
                "decision": "true_positive",
                "sha256": sha256,
                "phash": "ffffffffffffffff",
                "dhash": "ffffffffffffffff",
                "ahash": "ffffffffffffffff",
                "created_at": int(self._now.timestamp()),
            }
        )

    @staticmethod
    def _hashes_from_bytes(data):
        return {
            "sha256": data.decode(),
            "phash": "0000000000000000",
            "dhash": "0000000000000000",
            "ahash": "0000000000000000",
        }

    def _stats(self, cog):
        config = cog.config
        stats = getattr(config, "_stats", None)
        if stats is None:
            stats = config._inner._stats
        return dict(stats)

    async def _snapshot(self, cog, guild_id, user_id):
        def load():
            with closing(sqlite3.connect(cog._detection_case_db_path)) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """SELECT case_id FROM detection_cases
                       WHERE guild_id = ? AND user_id = ?
                       ORDER BY created_at DESC
                       LIMIT 1""",
                    (guild_id, user_id),
                ).fetchone()
            if row is None:
                return None
            return cog._case_store.get_case(row["case_id"])

        return await asyncio.to_thread(load)

    def _signals(self, snapshot, sequence=None):
        if sequence is None:
            sequence = snapshot.messages[-1].sequence
        return tuple(
            item.signal
            for item in snapshot.signals
            if item.message_sequence == sequence
        )

    async def _wait_for_stat(self, cog, guild_id, user_id, key, processing):
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if processing.done():
                await processing
            snapshot = await self._snapshot(cog, guild_id, user_id)
            if snapshot is not None and self._stats(cog).get(key, 0) >= 1:
                return snapshot
            await asyncio.sleep(0.01)
        if processing.done():
            await processing
        raise AssertionError(f"case did not record {key} while other images were downloading")

    async def _finish(self, cog):
        for task in list(getattr(cog, "_post_ban_sweep_tasks", ())):
            task.cancel()
        for task in list(getattr(cog, "_gif_detector_tasks", ())):
            task.cancel()
        await drain_background_work(cog)


class _FailingStatsConfig:
    def __init__(self, inner):
        self._inner = inner
        self._stats = inner._stats

    def guild(self, guild):
        return _FailingGuildConfig(self._inner.guild(guild))

    def guild_from_id(self, guild_id):
        return _FailingGuildConfig(self._inner.guild_from_id(guild_id))


class _FailingGuildConfig:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @asynccontextmanager
    async def stats(self):
        raise RuntimeError("config unavailable")
        yield None  # pragma: no cover
