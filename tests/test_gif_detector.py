"""Behavioral tests for the channel-scoped GIF detector."""

import asyncio
import unittest
from collections import deque
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules
from tests.test_settings_commands import _OverviewAllowedMentions, _OverviewEmbed


class GifDetectorSettingsTests(unittest.TestCase):
    def test_empty_settings_disable_detection_and_keep_animation_available(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                configured = honeypot.GuildSettings.from_mapping({})

                self.assertFalse(configured.gif_detector_enabled)
                self.assertTrue(configured.gif_detector_animation_enabled)
                self.assertEqual(configured.gif_detector_channels, [])
                self.assertEqual(configured.gif_detector_secondary_message, "No gifs!")
                self.assertEqual(
                    configured.gif_detector_retention_seconds, 5
                )
                self.assertEqual(configured.gif_detector_threshold, 3)
                self.assertEqual(configured.gif_detector_window_seconds, 60)
                self.assertEqual(configured.gif_detector_mute_duration_seconds, 3600)


class GifDetectorClassificationTests(unittest.TestCase):
    def test_malformed_embed_and_attachment_collections_are_ignored(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")

                for field in ("embeds", "attachments"):
                    with self.subTest(field=field):
                        self.assertFalse(gif_detector.has_gif_evidence(**{field: 1}))

    def test_supported_gif_evidence_is_detected_without_matching_ordinary_mp4(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")

                positive_cases = {
                    "gifv embed": {"embeds": [{"type": "gifv"}]},
                    "GIF media type": {
                        "attachments": [{"filename": "upload.bin", "content_type": "image/gif"}]
                    },
                    "original GIF filename": {
                        "attachments": [
                            {"filename": "REACTION.GIF", "content_type": "video/mp4"}
                        ]
                    },
                    "direct GIF URL": {
                        "content": "https://cdn.example.test/reaction.GIF?size=large#preview"
                    },
                    "embed image URL": {
                        "embeds": [
                            {"type": "image", "image": {"url": "https://cdn.example/a.gif?v=1"}}
                        ]
                    },
                    "Tenor video provenance": {
                        "embeds": [
                            {
                                "type": "video",
                                "url": "https://tenor.com/view/reaction-123",
                                "video": {
                                    "url": "https://media.tenor.com/example/tenor.mp4"
                                },
                            }
                        ]
                    },
                    "Giphy provider object": {
                        "embeds": [
                            SimpleNamespace(
                                type="video",
                                provider=SimpleNamespace(name="GIPHY", url="https://giphy.com"),
                                video=SimpleNamespace(url="https://media.giphy.com/media/example/giphy.mp4"),
                            )
                        ]
                    },
                }
                negative_cases = {
                    "ordinary MP4 attachment": {
                        "attachments": [
                            {"filename": "clip.mp4", "content_type": "video/mp4"}
                        ]
                    },
                    "ordinary MP4 link": {"content": "https://cdn.example.test/clip.mp4"},
                    "generic video embed": {
                        "embeds": [
                            {
                                "type": "video",
                                "url": "https://youtube.com/watch?v=gif",
                                "video": {"url": "https://cdn.example.test/clip.mp4"},
                            }
                        ]
                    },
                    "GIF only in query": {
                        "content": "https://cdn.example.test/image.png?format=gif"
                    },
                    "GIF hostname fragment": {
                        "content": "https://notgiphy.example.test/clip.mp4"
                    },
                }

                for label, message in positive_cases.items():
                    with self.subTest(label=label):
                        self.assertTrue(gif_detector.has_gif_evidence(**message))
                for label, message in negative_cases.items():
                    with self.subTest(label=label):
                        self.assertFalse(gif_detector.has_gif_evidence(**message))

    def test_thread_scope_uses_its_parent_channel(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")

                self.assertEqual(
                    gif_detector.channel_scope_id(
                        SimpleNamespace(id=200, parent_id=100)
                    ),
                    100,
                )
                self.assertEqual(
                    gif_detector.channel_scope_id(
                        SimpleNamespace(id=300, parent_id=None)
                    ),
                    300,
                )


class GifDetectorAnimationTests(unittest.TestCase):
    def test_animation_uses_a_fixed_horizontal_track(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")

                expected_frames = [
                    "🚀──────────🎯 @User's GIF",
                    "──🚀────────🎯 @User's GIF",
                    "────🚀──────🎯 @User's GIF",
                    "──────🚀────🎯 @User's GIF",
                    "────────🚀──🎯 @User's GIF",
                ]

                self.assertEqual(
                    [
                        gif_detector.render_icbm_frame("@User", rocket_position=position)
                        for position in range(0, 10, 2)
                    ],
                    expected_frames,
                )


class GifDetectorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_webp_fallback_is_scheduled_after_main_detection(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                message = SimpleNamespace(
                    embeds=[],
                    attachments=[],
                    content="https://media.example.test/reaction.webp",
                )
                events = []

                async def detect(*args, **kwargs):
                    events.append("detection")

                async def schedule(*args, **kwargs):
                    events.append("webp-fallback")

                with (
                    mock.patch.object(
                        honeypot.detection,
                        "on_message",
                        new=detect,
                    ),
                    mock.patch.object(
                        gif_detector,
                        "schedule_remote_media_fallback",
                        new=schedule,
                        create=True,
                    ),
                ):
                    await cog.on_message(message)

                self.assertEqual(events, ["detection", "webp-fallback"])

    async def test_local_gif_evidence_skips_webp_fallback(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                message = SimpleNamespace(
                    embeds=[SimpleNamespace(type="gifv")],
                    attachments=[],
                    content="",
                )

                with (
                    mock.patch.object(
                        gif_detector,
                        "_admit_message",
                        new=mock.AsyncMock(),
                    ),
                    mock.patch.object(
                        honeypot.detection,
                        "on_message",
                        new=mock.AsyncMock(),
                    ),
                    mock.patch.object(
                        gif_detector,
                        "schedule_remote_media_fallback",
                        new=mock.AsyncMock(),
                        create=True,
                    ) as fallback,
                ):
                    await cog.on_message(message)

                fallback.assert_not_awaited()

    async def test_animated_avif_url_uses_remote_admission_path(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    return_value=True
                )
                url = "https://cdn.discordapp.com/attachments/1/2/TEST.avif?hm=old"
                guild = SimpleNamespace(id=1)
                current_message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=None,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[],
                    content=url,
                )
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    fetch_message=mock.AsyncMock(return_value=current_message),
                )
                current_message.channel = channel
                message = SimpleNamespace(**vars(current_message))

                with mock.patch.object(
                    gif_detector,
                    "_admit_message",
                    new=mock.AsyncMock(),
                ) as admit:
                    await gif_detector.schedule_remote_media_fallback(cog, message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                cog._gif_detector_remote_inspector.inspect.assert_awaited_once_with(url)
                admit.assert_awaited_once_with(cog, current_message)

    async def test_animated_webp_fallback_reuses_existing_admission_path(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    return_value=True
                )
                guild = SimpleNamespace(id=1)
                current_message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=None,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[],
                    content="https://media.example.test/reaction.webp",
                )
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    fetch_message=mock.AsyncMock(return_value=current_message),
                )
                current_message.channel = channel
                message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=channel,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[],
                    content="https://media.example.test/reaction.webp",
                )

                with mock.patch.object(
                    gif_detector,
                    "_admit_message",
                    new=mock.AsyncMock(),
                ) as admit:
                    await gif_detector.schedule_remote_media_fallback(cog, message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                cog._gif_detector_remote_inspector.inspect.assert_awaited_once_with(
                    "https://media.example.test/reaction.webp"
                )
                admit.assert_awaited_once_with(cog, current_message)

    async def test_webp_fallback_prioritizes_signed_discord_candidate(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                unsigned_url = "https://cdn.discordapp.com/a/anim.webp"
                signed_url = (
                    "https://cdn.discordapp.com/a/anim.webp"
                    "?ex=future&is=issued&hm=signature"
                )

                async def inspect(url):
                    return True if url == signed_url else None

                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    side_effect=inspect
                )
                guild = SimpleNamespace(id=1)
                embed = SimpleNamespace(
                    url=unsigned_url,
                    image=None,
                    thumbnail=SimpleNamespace(url=signed_url, proxy_url=None),
                    video=None,
                )
                current_message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=None,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[embed],
                    attachments=[],
                    content=unsigned_url,
                )
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    fetch_message=mock.AsyncMock(return_value=current_message),
                )
                current_message.channel = channel
                message = SimpleNamespace(**vars(current_message))
                message.channel = channel

                with mock.patch.object(
                    gif_detector,
                    "_admit_message",
                    new=mock.AsyncMock(),
                ) as admit:
                    await gif_detector.schedule_remote_media_fallback(cog, message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                self.assertEqual(
                    [
                        call.args[0]
                        for call in cog._gif_detector_remote_inspector.inspect.await_args_list
                    ],
                    [signed_url],
                )
                admit.assert_awaited_once_with(cog, current_message)

    async def test_webp_fallback_accepts_refreshed_signature_for_same_attachment(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    return_value=True
                )
                original_url = "https://cdn.discordapp.com/a/anim.webp?ex=old&hm=old"
                refreshed_url = "https://cdn.discordapp.com/a/anim.webp?ex=new&hm=new"
                guild = SimpleNamespace(id=1)
                current_message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=None,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[
                        SimpleNamespace(
                            filename="anim.webp",
                            content_type="image/webp",
                            url=refreshed_url,
                            proxy_url=None,
                        )
                    ],
                    content="",
                )
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    fetch_message=mock.AsyncMock(return_value=current_message),
                )
                current_message.channel = channel
                message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=channel,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[
                        SimpleNamespace(
                            filename="anim.webp",
                            content_type="image/webp",
                            url=original_url,
                            proxy_url=None,
                        )
                    ],
                    content="",
                )

                with mock.patch.object(
                    gif_detector,
                    "_admit_message",
                    new=mock.AsyncMock(),
                ) as admit:
                    await gif_detector.schedule_remote_media_fallback(cog, message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                admit.assert_awaited_once_with(cog, current_message)

    async def test_webp_fallback_rejects_different_discord_attachment_path(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    return_value=True
                )
                guild = SimpleNamespace(id=1)
                current_message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=None,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[
                        SimpleNamespace(
                            filename="other.webp",
                            content_type="image/webp",
                            url="https://cdn.discordapp.com/a/other.webp?hm=new",
                            proxy_url=None,
                        )
                    ],
                    content="",
                )
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    fetch_message=mock.AsyncMock(return_value=current_message),
                )
                current_message.channel = channel
                message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=channel,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[
                        SimpleNamespace(
                            filename="anim.webp",
                            content_type="image/webp",
                            url="https://cdn.discordapp.com/a/anim.webp?hm=old",
                            proxy_url=None,
                        )
                    ],
                    content="",
                )

                with mock.patch.object(
                    gif_detector,
                    "_admit_message",
                    new=mock.AsyncMock(),
                ) as admit:
                    await gif_detector.schedule_remote_media_fallback(cog, message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                admit.assert_not_awaited()

    def test_discord_webp_identity_only_ignores_signature_query_fields(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")
                original = (
                    "https://media.discordapp.net/a/anim.webp"
                    "?format=webp&width=320&ex=old&is=old&hm=old"
                )

                self.assertTrue(
                    gif_detector._same_remote_media_candidate(
                        original,
                        "https://media.discordapp.net/a/anim.webp"
                        "?format=webp&width=320&ex=new&is=new&hm=new",
                    )
                )
                for changed in (
                    "https://media.discordapp.net/a/anim.webp?format=png&width=320",
                    "https://media.discordapp.net/a/anim.webp?format=webp&width=640",
                    "https://media.discordapp.net/a/anim.webp;other?format=webp&width=320",
                    "https://media.discordapp.net/a/anim.webp"
                    "?format=webp&width=320&EX=resource-a",
                ):
                    with self.subTest(changed=changed):
                        self.assertFalse(
                            gif_detector._same_remote_media_candidate(original, changed)
                        )

    async def test_static_webp_does_not_enter_admission_path(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    return_value=False
                )
                message = SimpleNamespace(
                    id=30,
                    guild=SimpleNamespace(id=1),
                    channel=SimpleNamespace(id=10, parent_id=None),
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[
                        SimpleNamespace(
                            type="image",
                            image=SimpleNamespace(
                                url="https://media.example.test/still.webp"
                            ),
                        )
                    ],
                    attachments=[],
                    content="",
                )

                with mock.patch.object(
                    gif_detector,
                    "_admit_message",
                    new=mock.AsyncMock(),
                ) as admit:
                    await gif_detector.schedule_remote_media_fallback(cog, message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                admit.assert_not_awaited()

    async def test_webp_removed_during_inspection_is_not_admitted(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    return_value=True
                )
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    fetch_message=mock.AsyncMock(),
                )
                message = SimpleNamespace(
                    id=30,
                    guild=SimpleNamespace(id=1),
                    channel=channel,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[],
                    content="https://media.example.test/reaction.webp",
                )
                channel.fetch_message.return_value = SimpleNamespace(
                    id=30,
                    guild=message.guild,
                    channel=channel,
                    author=message.author,
                    webhook_id=None,
                    embeds=[],
                    attachments=[],
                    content="edited away",
                )

                with mock.patch.object(
                    gif_detector,
                    "_admit_message",
                    new=mock.AsyncMock(),
                ) as admit:
                    await gif_detector.schedule_remote_media_fallback(cog, message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                admit.assert_not_awaited()

    async def test_webp_deleted_during_inspection_is_not_admitted_or_reported(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    return_value=True
                )

                class SourceGone(Exception):
                    pass

                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    fetch_message=mock.AsyncMock(side_effect=SourceGone()),
                )
                message = SimpleNamespace(
                    id=30,
                    guild=SimpleNamespace(id=1),
                    channel=channel,
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[],
                    content="https://media.example.test/reaction.webp",
                )

                with (
                    mock.patch.object(gif_detector.discord, "NotFound", SourceGone),
                    mock.patch.object(
                        gif_detector,
                        "_admit_message",
                        new=mock.AsyncMock(),
                    ) as admit,
                    mock.patch.object(
                        cog,
                        "_record_operational_failure",
                        new=mock.AsyncMock(),
                    ) as report,
                ):
                    await gif_detector.schedule_remote_media_fallback(cog, message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                admit.assert_not_awaited()
                report.assert_not_awaited()

    async def test_ineligible_webp_message_is_rejected_before_remote_inspection(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=True)
                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    return_value=True
                )
                message = SimpleNamespace(
                    id=30,
                    guild=SimpleNamespace(id=1),
                    channel=SimpleNamespace(id=10, parent_id=None),
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[],
                    content="https://media.example.test/reaction.webp",
                )

                await gif_detector.schedule_remote_media_fallback(cog, message)
                await asyncio.gather(*tuple(cog._gif_detector_tasks))

                cog._gif_detector_remote_inspector.inspect.assert_not_awaited()

    async def test_concurrent_webp_events_share_one_remote_inspection(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                started = asyncio.Event()
                release = asyncio.Event()

                async def inspect(url):
                    started.set()
                    await release.wait()
                    return False

                cog._gif_detector_remote_inspector.inspect = mock.AsyncMock(
                    side_effect=inspect
                )
                message = SimpleNamespace(
                    id=30,
                    guild=SimpleNamespace(id=1),
                    channel=SimpleNamespace(id=10, parent_id=None),
                    author=SimpleNamespace(id=20, bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[],
                    content="https://media.example.test/reaction.webp",
                )

                await gif_detector.schedule_remote_media_fallback(cog, message)
                await started.wait()
                await gif_detector.schedule_remote_media_fallback(cog, message)
                release.set()
                await asyncio.gather(*tuple(cog._gif_detector_tasks))

                self.assertEqual(
                    cog._gif_detector_remote_inspector.inspect.await_count,
                    1,
                )

    async def test_new_hit_prunes_expired_rate_state_for_its_guild(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                configured = honeypot.GuildSettings.from_mapping({})
                cog._gif_detector_hits[(1, 90)] = deque([0.0])
                cog._gif_detector_hits[(2, 91)] = deque([0.0])
                cog._gif_detector_active_mutes[(1, 92)] = 10.0
                message = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    author=SimpleNamespace(id=20),
                )

                with mock.patch.object(gif_detector.time, "monotonic", return_value=100.0):
                    await gif_detector._record_gif_hit(cog, message, configured)

                self.assertNotIn((1, 90), cog._gif_detector_hits)
                self.assertIn((2, 91), cog._gif_detector_hits)
                self.assertNotIn((1, 92), cog._gif_detector_active_mutes)

    async def test_third_gif_in_default_window_requests_one_hour_role_mute(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_animation_enabled=False,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                mute_role = SimpleNamespace(id=99)
                guild = SimpleNamespace(
                    id=1,
                    me=SimpleNamespace(id=999),
                    get_role=mock.Mock(return_value=mute_role),
                )
                author = SimpleNamespace(id=20, mention="@User", bot=False)
                warning = SimpleNamespace(delete=mock.AsyncMock())
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    send=mock.AsyncMock(return_value=warning),
                )
                mute_role_value = mock.AsyncMock(return_value=99)
                mutes = SimpleNamespace(
                    config=SimpleNamespace(
                        guild=mock.Mock(
                            return_value=SimpleNamespace(mute_role=mute_role_value)
                        )
                    ),
                    mute_user=mock.AsyncMock(
                        return_value=SimpleNamespace(success=True, reason=None)
                    ),
                )
                cog.bot.get_cog = mock.Mock(
                    side_effect=lambda name: mutes if name == "Mutes" else None
                )

                def source(message_id):
                    return SimpleNamespace(
                        id=message_id,
                        guild=guild,
                        channel=channel,
                        author=author,
                        webhook_id=None,
                        embeds=[SimpleNamespace(type="gifv")],
                        attachments=[],
                        content="",
                        delete=mock.AsyncMock(),
                    )

                with (
                    mock.patch.object(
                        gif_detector.asyncio,
                        "sleep",
                        new=mock.AsyncMock(),
                    ),
                    mock.patch.object(
                        honeypot.detection,
                        "on_message",
                        new=mock.AsyncMock(),
                    ),
                ):
                    before = datetime.now(timezone.utc)
                    await cog.on_message(source(30))
                    await cog.on_message(source(31))
                    self.assertEqual(mutes.mute_user.await_count, 0)
                    await cog.on_message(source(32))
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))
                    for message_id in (33, 34, 35):
                        await cog.on_message(source(message_id))
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))
                    self.assertEqual(mutes.mute_user.await_count, 1)

                    cog._gif_detector_active_mutes[(guild.id, author.id)] = 0
                    await asyncio.gather(
                        *(cog.on_message(source(message_id)) for message_id in (36, 37, 38))
                    )
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                self.assertEqual(mutes.mute_user.await_count, 2)
                first_call = mutes.mute_user.await_args_list[0]
                args = first_call.args
                self.assertEqual(args[:3], (guild, guild.me, author))
                until = first_call.kwargs["until"]
                self.assertIsNotNone(until.tzinfo)
                self.assertGreaterEqual((until - before).total_seconds(), 3599)
                self.assertLessEqual((until - before).total_seconds(), 3601)
                self.assertEqual(
                    first_call.kwargs["reason"],
                    "GIF defense system activated, ICBM launch privileges revoked",
                )

    async def test_missing_core_mute_role_records_failure_without_calling_mute_service(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog._record_operational_failure = mock.AsyncMock()
                guild = SimpleNamespace(
                    id=1,
                    me=SimpleNamespace(id=999),
                    get_role=mock.Mock(return_value=None),
                )
                member = SimpleNamespace(id=20)
                message = SimpleNamespace(guild=guild, author=member)
                mutes = SimpleNamespace(
                    config=SimpleNamespace(
                        guild=mock.Mock(
                            return_value=SimpleNamespace(
                                mute_role=mock.AsyncMock(return_value=None)
                            )
                        )
                    ),
                    mute_user=mock.AsyncMock(),
                )
                cog.bot.get_cog = mock.Mock(return_value=mutes)
                key = (guild.id, member.id)
                cog._gif_detector_mutes_in_flight.add(key)

                await gif_detector._apply_gif_mute(cog, message, key, 3600)

                mutes.mute_user.assert_not_awaited()
                cog._record_operational_failure.assert_awaited_once()
                self.assertNotIn(key, cog._gif_detector_mutes_in_flight)

    async def test_successful_core_mute_resolves_member_and_creates_modlog_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog._record_operational_failure = mock.AsyncMock()
                user = SimpleNamespace(id=20)
                member = SimpleNamespace(
                    id=20,
                    guild_permissions=object(),
                    top_role=object(),
                )
                role = SimpleNamespace(id=99)
                guild = SimpleNamespace(
                    id=1,
                    me=SimpleNamespace(id=999),
                    get_role=mock.Mock(return_value=role),
                    get_member=mock.Mock(return_value=None),
                    fetch_member=mock.AsyncMock(return_value=member),
                )
                created_at = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
                message = SimpleNamespace(
                    guild=guild,
                    author=user,
                    created_at=created_at,
                )
                mutes = SimpleNamespace(
                    config=SimpleNamespace(
                        guild=mock.Mock(
                            return_value=SimpleNamespace(
                                mute_role=mock.AsyncMock(return_value=role.id)
                            )
                        )
                    ),
                    mute_user=mock.AsyncMock(
                        return_value=SimpleNamespace(success=True, reason=None)
                    ),
                )
                gif_detector.modlog.create_case = mock.AsyncMock()
                cog.bot.get_cog = mock.Mock(return_value=mutes)
                key = (guild.id, user.id)
                cog._gif_detector_mutes_in_flight.add(key)

                await gif_detector._apply_gif_mute(cog, message, key, 3600)

                guild.fetch_member.assert_awaited_once_with(user.id)
                self.assertIs(mutes.mute_user.await_args.args[2], member)
                reason = "GIF defense system activated, ICBM launch privileges revoked"
                until = mutes.mute_user.await_args.kwargs["until"]
                gif_detector.modlog.create_case.assert_awaited_once_with(
                    cog.bot,
                    guild,
                    created_at,
                    "smute",
                    member,
                    guild.me,
                    reason,
                    until=until,
                    channel=None,
                )
                self.assertIn(key, cog._gif_detector_active_mutes)
                self.assertNotIn(key, cog._gif_detector_mutes_in_flight)

    async def test_modlog_failure_keeps_successful_mute_tracked_and_reports_failure(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog._record_operational_failure = mock.AsyncMock()
                user = SimpleNamespace(id=20)
                member = SimpleNamespace(
                    id=20,
                    guild_permissions=object(),
                    top_role=object(),
                )
                role = SimpleNamespace(id=99)
                guild = SimpleNamespace(
                    id=1,
                    me=SimpleNamespace(id=999),
                    get_role=mock.Mock(return_value=role),
                    get_member=mock.Mock(return_value=member),
                )
                message = SimpleNamespace(
                    guild=guild,
                    author=user,
                    created_at=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
                )
                mutes = SimpleNamespace(
                    config=SimpleNamespace(
                        guild=mock.Mock(
                            return_value=SimpleNamespace(
                                mute_role=mock.AsyncMock(return_value=role.id)
                            )
                        )
                    ),
                    mute_user=mock.AsyncMock(
                        return_value=SimpleNamespace(success=True, reason=None)
                    ),
                )
                gif_detector.modlog.create_case = mock.AsyncMock(
                    side_effect=RuntimeError("modlog unavailable")
                )
                cog.bot.get_cog = mock.Mock(return_value=mutes)
                key = (guild.id, user.id)
                cog._gif_detector_mutes_in_flight.add(key)

                await gif_detector._apply_gif_mute(cog, message, key, 3600)

                self.assertIn(key, cog._gif_detector_active_mutes)
                self.assertNotIn(key, cog._gif_detector_mutes_in_flight)
                cog._record_operational_failure.assert_awaited_once()
                self.assertIn(
                    "modlog case could not be created",
                    cog._record_operational_failure.await_args.args[2],
                )

    async def test_zero_retention_deletes_static_gif_and_keeps_warning_for_five_seconds(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_animation_enabled=False,
                    gif_detector_channels=[10],
                    gif_detector_retention_seconds=0,
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                events = []
                warning = SimpleNamespace(
                    delete=mock.AsyncMock(side_effect=lambda: events.append("warning-delete"))
                )
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    send=mock.AsyncMock(
                        side_effect=lambda *args, **kwargs: (
                            events.append("warning-send") or warning
                        )
                    ),
                )
                guild = SimpleNamespace(id=1)
                author = SimpleNamespace(id=20, mention="@User", bot=False)
                message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=channel,
                    author=author,
                    webhook_id=None,
                    embeds=[SimpleNamespace(type="gifv")],
                    delete=mock.AsyncMock(
                        side_effect=lambda: events.append("source-delete")
                    ),
                )

                with (
                    mock.patch.object(
                        gif_detector.asyncio,
                        "sleep",
                        new=mock.AsyncMock(
                            side_effect=lambda seconds: events.append(
                                f"sleep:{seconds}"
                            )
                        ),
                    ),
                    mock.patch.object(
                        honeypot.detection,
                        "on_message",
                        new=mock.AsyncMock(),
                    ),
                ):
                    await cog.on_message(message)
                    await cog.on_message(message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                self.assertEqual(
                    events,
                    [
                        "warning-send",
                        "source-delete",
                        "sleep:5",
                        "warning-delete",
                    ],
                )
                channel.send.assert_awaited_once()
                self.assertEqual(channel.send.await_args.args[0], "@User No gifs!")
                allowed = channel.send.await_args.kwargs["allowed_mentions"]
                self.assertEqual(allowed.users, [author])
                self.assertFalse(allowed.everyone)
                self.assertFalse(allowed.roles)

    async def test_first_gif_ends_with_three_second_impact_frame(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_animation_enabled=True,
                    gif_detector_channels=[10],
                    gif_detector_retention_seconds=10,
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                events = []
                sleep_seconds = []
                clock = 100.0

                async def advance_clock(seconds):
                    nonlocal clock
                    events.append("sleep")
                    sleep_seconds.append(seconds)
                    clock += seconds
                warning = SimpleNamespace(
                    edit=mock.AsyncMock(
                        side_effect=lambda *args, **kwargs: events.append("edit")
                    ),
                    delete=mock.AsyncMock(
                        side_effect=lambda: events.append("warning-delete")
                    ),
                )
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    send=mock.AsyncMock(
                        side_effect=lambda *args, **kwargs: (
                            events.append("warning-send") or warning
                        )
                    ),
                )
                guild = SimpleNamespace(id=1)
                author = SimpleNamespace(id=20, mention="@User", bot=False)
                message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=channel,
                    author=author,
                    webhook_id=None,
                    embeds=[SimpleNamespace(type="gifv")],
                    delete=mock.AsyncMock(
                        side_effect=lambda: events.append("source-delete")
                    ),
                )

                with (
                    mock.patch.object(
                        gif_detector.asyncio,
                        "sleep",
                        new=mock.AsyncMock(side_effect=advance_clock),
                    ),
                    mock.patch.object(
                        gif_detector.time,
                        "monotonic",
                        side_effect=lambda: clock,
                    ),
                    mock.patch.object(
                        honeypot.detection,
                        "on_message",
                        new=mock.AsyncMock(),
                    ),
                ):
                    await cog.on_message(message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                self.assertEqual(
                    channel.send.await_args.args[0],
                    gif_detector.render_icbm_frame("@User", rocket_position=0),
                )
                self.assertEqual(warning.edit.await_count, 5)
                self.assertEqual(
                    [call.kwargs["content"] for call in warning.edit.await_args_list],
                    [
                        gif_detector.render_icbm_frame(
                            "@User", rocket_position=position
                        )
                        for position in range(2, 10, 2)
                    ]
                    + ["──────────💥"],
                )
                self.assertEqual(sleep_seconds, [1, 1, 1, 1, 6, 3])
                self.assertLess(
                    events.index("source-delete"),
                    events.index("warning-delete"),
                )
                self.assertEqual(events[-2:], ["sleep", "warning-delete"])
                self.assertEqual(cog._gif_detector_animated_guilds, set())

    async def test_short_animated_retention_impacts_at_source_deadline(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                events = []
                clock = 100.0

                async def advance_clock(seconds):
                    nonlocal clock
                    events.append(f"sleep:{seconds}")
                    clock += seconds
                warning = SimpleNamespace(
                    edit=mock.AsyncMock(side_effect=lambda **kwargs: events.append("edit")),
                    delete=mock.AsyncMock(
                        side_effect=lambda: events.append("warning-delete")
                    ),
                )
                channel = SimpleNamespace(
                    send=mock.AsyncMock(
                        side_effect=lambda *args, **kwargs: (
                            events.append("warning-send") or warning
                        )
                    )
                )
                message = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    channel=channel,
                    author=SimpleNamespace(mention="@User"),
                    delete=mock.AsyncMock(
                        side_effect=lambda: events.append("source-delete")
                    ),
                )

                with (
                    mock.patch.object(
                        gif_detector.asyncio,
                        "sleep",
                        new=mock.AsyncMock(side_effect=advance_clock),
                    ),
                    mock.patch.object(
                        gif_detector.time,
                        "monotonic",
                        side_effect=lambda: clock,
                    ),
                ):
                    await gif_detector._run_animated(cog, message, 2)

                self.assertEqual(warning.edit.await_count, 2)
                self.assertEqual(warning.edit.await_args.kwargs["content"], "──────────💥")
                self.assertEqual(
                    events,
                    [
                        "warning-send",
                        "sleep:1.0",
                        "edit",
                        "sleep:1.0",
                        "source-delete",
                        "edit",
                        "sleep:3",
                        "warning-delete",
                    ],
                )


class GifDetectorCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_retention_command_stores_a_bounded_source_deadline(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")
                retention = mock.AsyncMock(return_value=5)
                retention.set = mock.AsyncMock()
                guild_config = SimpleNamespace(
                    gif_detector_retention_seconds=retention,
                )
                cog = SimpleNamespace(
                    config=SimpleNamespace(guild=lambda guild: guild_config)
                )
                ctx = SimpleNamespace(guild=SimpleNamespace(id=1), send=mock.AsyncMock())
                command = getattr(gif_detector, "gif_detector_retention", None)

                self.assertTrue(callable(command), "retention command is not registered")
                await command(cog, ctx)
                await command(cog, ctx, 12)
                try:
                    await command(cog, ctx, 0)
                except gif_detector.commands.UserFeedbackCheckFailure:
                    self.fail("zero retention should delete the GIF immediately")

                self.assertIn("5", ctx.send.await_args_list[0].args[0])
                self.assertEqual(
                    [call.args[0] for call in retention.set.await_args_list],
                    [12, 0],
                )
                for invalid in (-1, 61):
                    with self.subTest(invalid=invalid):
                        with self.assertRaises(
                            gif_detector.commands.UserFeedbackCheckFailure
                        ):
                            await command(cog, ctx, invalid)

    async def test_rate_commands_store_bounded_threshold_window_and_mute_duration(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")
                threshold = SimpleNamespace(set=mock.AsyncMock())
                window = SimpleNamespace(set=mock.AsyncMock())
                duration = SimpleNamespace(set=mock.AsyncMock())
                guild_config = SimpleNamespace(
                    gif_detector_threshold=threshold,
                    gif_detector_window_seconds=window,
                    gif_detector_mute_duration_seconds=duration,
                )
                cog = SimpleNamespace(
                    config=SimpleNamespace(guild=lambda guild: guild_config)
                )
                ctx = SimpleNamespace(guild=SimpleNamespace(id=1), send=mock.AsyncMock())

                await gif_detector.gif_detector_threshold(cog, ctx, 4)
                await gif_detector.gif_detector_window(cog, ctx, 90)
                await gif_detector.gif_detector_mute_duration(cog, ctx, 7200)

                threshold.set.assert_awaited_once_with(4)
                window.set.assert_awaited_once_with(90)
                duration.set.assert_awaited_once_with(7200)

                for command, invalid in (
                    (gif_detector.gif_detector_threshold, 1),
                    (gif_detector.gif_detector_window, 4),
                    (gif_detector.gif_detector_mute_duration, 59),
                ):
                    with self.subTest(command=command.__name__):
                        with self.assertRaises(
                            gif_detector.commands.UserFeedbackCheckFailure
                        ):
                            await command(cog, ctx, invalid)

    async def test_root_group_shows_leaf_commands_without_section_rows(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                configure = mock.AsyncMock()
                gif_detector = import_module("Honeypot.gif_detector")
                gif_detector.config_gif_detector = configure
                channel_group = SimpleNamespace(
                    qualified_name="honeypot gifdetector channel",
                    signature="",
                    short_doc="Configure tracked channels.",
                    commands=[
                        SimpleNamespace(
                            qualified_name="honeypot gifdetector channel add",
                            signature="[channel]",
                            short_doc="Add a channel.",
                            commands=[],
                        ),
                        SimpleNamespace(
                            qualified_name="honeypot gifdetector channel remove",
                            signature="[channel]",
                            short_doc="Remove a channel.",
                            commands=[],
                        ),
                    ],
                )
                message_group = SimpleNamespace(
                    qualified_name="honeypot gifdetector message",
                    signature="",
                    short_doc="Configure static warning text.",
                    commands=[
                        SimpleNamespace(
                            qualified_name="honeypot gifdetector message set",
                            signature="<text>",
                            short_doc="Set the message.",
                            commands=[],
                        ),
                        SimpleNamespace(
                            qualified_name="honeypot gifdetector message reset",
                            signature="",
                            short_doc="Reset the message.",
                            commands=[],
                        ),
                    ],
                )
                ctx = SimpleNamespace(
                    clean_prefix="?",
                    command=SimpleNamespace(
                        name="gifdetector",
                        short_doc="Configure GIF interception.",
                        commands=[channel_group, message_group],
                    ),
                    guild=SimpleNamespace(default_role=object()),
                    channel=SimpleNamespace(
                        permissions_for=lambda role: SimpleNamespace(
                            view_channel=False
                        )
                    ),
                    send=mock.AsyncMock(),
                )

                with (
                    mock.patch.object(honeypot.discord, "Embed", _OverviewEmbed),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        _OverviewAllowedMentions,
                    ),
                ):
                    await honeypot.Honeypot.gif_detector.callback(cog, ctx)

                configure.assert_awaited_once_with(cog, ctx)
                embed = ctx.send.await_args.kwargs["embed"]
                rendered = "\n".join(field.value for field in embed.fields)
                self.assertIn("?honeypot gifdetector channel add [channel]", rendered)
                self.assertIn("?honeypot gifdetector channel remove [channel]", rendered)
                self.assertIn("?honeypot gifdetector message set <text>", rendered)
                self.assertIn("?honeypot gifdetector message reset", rendered)
                self.assertNotIn("`?honeypot gifdetector channel` —", rendered)
                self.assertNotIn("`?honeypot gifdetector message` —", rendered)

    async def test_configuration_summary_shows_current_values_and_channels(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_animation_enabled=False,
                    gif_detector_channels=[10],
                    gif_detector_secondary_message="Stop that.",
                    gif_detector_retention_seconds=12,
                )
                cog._send_config_dump = mock.AsyncMock()
                configured_channel = SimpleNamespace(id=10, mention="#gifs")
                guild = SimpleNamespace(
                    id=1,
                    get_channel=lambda channel_id: (
                        configured_channel if channel_id == 10 else None
                    ),
                )
                ctx = SimpleNamespace(guild=guild)

                await gif_detector.config_gif_detector(cog, ctx)

                rows = dict(cog._send_config_dump.await_args.args[2])
                self.assertEqual(rows["Enabled"], "enabled")
                self.assertEqual(rows["Animation"], "disabled")
                self.assertEqual(rows["Channels"], "#gifs")
                self.assertEqual(rows["Secondary message"], "Stop that.")
                self.assertEqual(rows["GIF retention"], "12 seconds")
                self.assertEqual(rows["Mute threshold"], "3")
                self.assertEqual(rows["Rolling window"], "60 seconds")
                self.assertEqual(rows["Mute duration"], "3600 seconds")

                cog.config.defaults["gif_detector_channels"] = []
                await gif_detector.config_gif_detector(cog, ctx)
                empty_rows = dict(cog._send_config_dump.await_args.args[2])
                self.assertEqual(empty_rows["Channels"], "Not configured")

    async def test_doctor_reports_missing_send_and_manage_message_permissions(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    action="none",
                    fallback_action="none",
                    whitelist_mode="bypass",
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                permissions = SimpleNamespace(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=False,
                    send_messages_in_threads=False,
                    manage_messages=False,
                )
                configured_channel = SimpleNamespace(
                    id=10,
                    mention="#gifs",
                    permissions_for=lambda member: permissions,
                    purge=lambda: None,
                )
                top_role = mock.MagicMock()
                top_role.__gt__.return_value = True
                me = SimpleNamespace(
                    guild_permissions=SimpleNamespace(
                        kick_members=True,
                        ban_members=True,
                        manage_roles=True,
                    ),
                    top_role=top_role,
                )
                guild = SimpleNamespace(
                    id=1,
                    me=me,
                    channels=[configured_channel],
                    threads=[],
                    get_channel=lambda channel_id: (
                        configured_channel if channel_id == 10 else None
                    ),
                    get_thread=lambda channel_id: None,
                    get_role=lambda role_id: None,
                    default_role=object(),
                )
                ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())

                await cog.honeypot_doctor(ctx)

                report = "\n".join(call.args[0] for call in ctx.send.await_args_list)
                self.assertIn("GIF detector cannot send messages in #gifs", report)
                self.assertIn(
                    "GIF detector cannot send messages in threads under #gifs",
                    report,
                )
                self.assertIn("GIF detector cannot manage messages in #gifs", report)

    async def test_channel_and_message_commands_update_normalized_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")
                channel_ids = []

                class ChannelList:
                    async def __aenter__(self):
                        return channel_ids

                    async def __aexit__(self, exc_type, exc, traceback):
                        return False

                message_value = SimpleNamespace(set=mock.AsyncMock())
                guild_config = SimpleNamespace(
                    gif_detector_channels=ChannelList,
                    gif_detector_secondary_message=message_value,
                )
                cog = SimpleNamespace(
                    config=SimpleNamespace(guild=lambda guild: guild_config)
                )
                parent = SimpleNamespace(id=10, mention="#gifs")
                thread = SimpleNamespace(
                    id=11,
                    parent_id=10,
                    parent=parent,
                    mention="#gifs-thread",
                )
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    channel=thread,
                    send=mock.AsyncMock(),
                )

                await gif_detector.gif_detector_channel_add(cog, ctx)
                self.assertEqual(channel_ids, [10])
                await gif_detector.gif_detector_channel_remove(cog, ctx)
                self.assertEqual(channel_ids, [])
                await gif_detector.gif_detector_message_set(
                    cog, ctx, text="  Be still.  "
                )
                await gif_detector.gif_detector_message_reset(cog, ctx)

                self.assertEqual(
                    [call.args[0] for call in message_value.set.await_args_list],
                    ["Be still.", "No gifs!"],
                )
                self.assertIn("#gifs", ctx.send.await_args_list[0].args[0])
                with self.assertRaises(gif_detector.commands.UserFeedbackCheckFailure):
                    await gif_detector.gif_detector_message_set(cog, ctx, text="   ")

class GifDetectorRuntimeEdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_raw_webp_embed_schedules_remote_fallback(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                message = SimpleNamespace(
                    id=30,
                    embeds=[],
                    attachments=[],
                    content="",
                )
                payload = SimpleNamespace(
                    message_id=30,
                    data={
                        "embeds": [
                            {
                                "type": "image",
                                "image": {
                                    "url": "https://media.example.test/reaction.webp"
                                },
                            }
                        ]
                    },
                    message=message,
                )

                with mock.patch.object(
                    gif_detector,
                    "schedule_remote_media_fallback",
                    new=mock.AsyncMock(),
                ) as fallback:
                    await cog.on_raw_message_edit(payload)

                fallback.assert_awaited_once_with(
                    cog,
                    message,
                    candidate="https://media.example.test/reaction.webp",
                )

    async def test_late_raw_webp_attachment_uses_its_cdn_url_as_candidate(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                message = SimpleNamespace(
                    id=30,
                    embeds=[],
                    attachments=[],
                    content="",
                )
                payload = SimpleNamespace(
                    message_id=30,
                    data={
                        "attachments": [
                            {
                                "filename": "reaction.webp",
                                "content_type": "image/webp",
                                "url": "https://cdn.discordapp.com/attachments/1/2/reaction",
                            }
                        ]
                    },
                    message=message,
                )

                with mock.patch.object(
                    gif_detector,
                    "schedule_remote_media_fallback",
                    new=mock.AsyncMock(),
                ) as fallback:
                    await cog.on_raw_message_edit(payload)

                fallback.assert_awaited_once_with(
                    cog,
                    message,
                    candidate="https://cdn.discordapp.com/attachments/1/2/reaction",
                )

    async def test_late_raw_animated_image_attachment_uses_extensionless_cdn_candidate(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                message = SimpleNamespace(
                    id=30,
                    embeds=[],
                    attachments=[],
                    content="",
                )
                cases = (
                    ("reaction.avifs", "image/avif-sequence"),
                    ("reaction.apng", "image/apng"),
                )

                for filename, content_type in cases:
                    with self.subTest(filename=filename):
                        candidate = (
                            "https://cdn.discordapp.com/attachments/1/2/"
                            f"{filename}.bin"
                        )
                        payload = SimpleNamespace(
                            message_id=30,
                            data={
                                "attachments": [
                                    {
                                        "filename": filename,
                                        "content_type": content_type,
                                        "url": candidate,
                                    }
                                ]
                            },
                            message=message,
                        )

                        with mock.patch.object(
                            gif_detector,
                            "schedule_remote_media_fallback",
                            new=mock.AsyncMock(),
                        ) as fallback:
                            await cog.on_raw_message_edit(payload)

                        fallback.assert_awaited_once_with(
                            cog,
                            message,
                            candidate=candidate,
                        )

    async def test_late_raw_gifv_embed_uses_updated_message_without_fetching(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_animation_enabled=False,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                warning = SimpleNamespace(delete=mock.AsyncMock())
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    send=mock.AsyncMock(return_value=warning),
                    fetch_message=mock.AsyncMock(),
                )
                guild = SimpleNamespace(id=1)
                message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=channel,
                    author=SimpleNamespace(id=20, mention="@User", bot=False),
                    webhook_id=None,
                    embeds=[],
                    delete=mock.AsyncMock(),
                )
                payload = SimpleNamespace(
                    message_id=30,
                    data={"embeds": [{"type": "gifv"}]},
                    message=message,
                )

                with mock.patch.object(
                    gif_detector.asyncio,
                    "sleep",
                    new=mock.AsyncMock(),
                ):
                    await cog.on_raw_message_edit(payload)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                message.delete.assert_awaited_once()
                channel.send.assert_awaited_once()
                channel.fetch_message.assert_not_awaited()

    async def test_positive_uncached_raw_update_fetches_and_handles_source_message(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_animation_enabled=False,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                warning = SimpleNamespace(delete=mock.AsyncMock())
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    send=mock.AsyncMock(return_value=warning),
                    fetch_message=mock.AsyncMock(),
                )
                message = SimpleNamespace(
                    id=30,
                    guild=SimpleNamespace(id=1),
                    channel=channel,
                    author=SimpleNamespace(id=20, mention="@User", bot=False),
                    webhook_id=None,
                    embeds=[],
                    attachments=[],
                    content="",
                    delete=mock.AsyncMock(),
                )
                channel.fetch_message.return_value = message
                cog.bot.get_channel = mock.Mock(return_value=channel)
                payload = SimpleNamespace(
                    guild_id=1,
                    channel_id=10,
                    message_id=30,
                    data={
                        "embeds": [
                            {
                                "type": "video",
                                "url": "https://tenor.com/view/reaction-123",
                                "video": {"url": "https://media.tenor.com/example/tenor.mp4"},
                            }
                        ]
                    },
                    cached_message=None,
                )

                with mock.patch.object(
                    gif_detector.asyncio,
                    "sleep",
                    new=mock.AsyncMock(),
                ):
                    await cog.on_raw_message_edit(payload)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                channel.fetch_message.assert_awaited_once_with(30)
                message.delete.assert_awaited_once()
                channel.send.assert_awaited_once()

    async def test_negative_uncached_raw_update_does_not_fetch_source_message(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.bot.get_channel = mock.Mock()
                payload = SimpleNamespace(
                    guild_id=1,
                    channel_id=10,
                    message_id=30,
                    data={
                        "embeds": [
                            {
                                "type": "video",
                                "url": "https://example.test/watch/gif",
                                "video": {"url": "https://cdn.example.test/clip.mp4"},
                            }
                        ]
                    },
                    cached_message=None,
                )

                await cog.on_raw_message_edit(payload)

                cog.bot.get_channel.assert_not_called()

    async def test_cog_unload_cancels_and_awaits_gif_tasks(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                started = asyncio.Event()
                cleaned = asyncio.Event()

                async def wait_until_cancelled():
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaned.set()

                task = asyncio.create_task(wait_until_cancelled())
                cog._gif_detector_tasks.add(task)
                cog._gif_detector_animated_guilds.add(1)
                cog._flush_firstpost_seen_authors = mock.AsyncMock()
                await started.wait()

                try:
                    await cog.cog_unload()

                    self.assertTrue(task.cancelled())
                    self.assertTrue(cleaned.is_set())
                    self.assertEqual(cog._gif_detector_tasks, set())
                    self.assertEqual(cog._gif_detector_animated_guilds, set())
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_static_retention_cleans_up_source_and_warning(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                started = asyncio.Event()

                async def wait_until_cancelled(seconds):
                    started.set()
                    await asyncio.Event().wait()

                warning = SimpleNamespace(delete=mock.AsyncMock())
                message = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    channel=SimpleNamespace(
                        send=mock.AsyncMock(return_value=warning)
                    ),
                    author=SimpleNamespace(mention="@User"),
                    delete=mock.AsyncMock(),
                )

                with mock.patch.object(
                    gif_detector.asyncio,
                    "sleep",
                    new=mock.AsyncMock(side_effect=wait_until_cancelled),
                ):
                    task = asyncio.create_task(
                        gif_detector._run_secondary(cog, message, "No gifs!", 60)
                    )
                    await started.wait()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

                self.assertTrue(task.cancelled())
                message.delete.assert_awaited_once()
                warning.delete.assert_awaited_once()

    async def test_second_gif_uses_static_path_while_guild_animation_is_active(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_animation_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                gate = asyncio.Event()
                real_sleep = asyncio.sleep
                clock = 100.0

                async def controlled_sleep(seconds):
                    nonlocal clock
                    if seconds <= 1 and not gate.is_set():
                        await gate.wait()
                    clock += seconds

                animated_warning = SimpleNamespace(
                    edit=mock.AsyncMock(),
                    delete=mock.AsyncMock(),
                )
                static_warning = SimpleNamespace(delete=mock.AsyncMock())
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    send=mock.AsyncMock(
                        side_effect=[animated_warning, static_warning]
                    ),
                )
                guild = SimpleNamespace(id=1)
                author = SimpleNamespace(id=20, mention="@User", bot=False)

                def source(message_id):
                    return SimpleNamespace(
                        id=message_id,
                        guild=guild,
                        channel=channel,
                        author=author,
                        webhook_id=None,
                        embeds=[SimpleNamespace(type="gifv")],
                        delete=mock.AsyncMock(),
                    )

                first = source(30)
                second = source(31)

                with (
                    mock.patch.object(
                        gif_detector.asyncio,
                        "sleep",
                        new=mock.AsyncMock(side_effect=controlled_sleep),
                    ),
                    mock.patch.object(
                        gif_detector.time,
                        "monotonic",
                        side_effect=lambda: clock,
                    ),
                    mock.patch.object(
                        honeypot.detection,
                        "on_message",
                        new=mock.AsyncMock(),
                    ),
                ):
                    await cog.on_message(first)
                    await cog.on_message(second)
                    await real_sleep(0)
                    await real_sleep(0)

                    first.delete.assert_not_awaited()
                    second.delete.assert_awaited_once()
                    self.assertEqual(channel.send.await_count, 2)
                    self.assertEqual(channel.send.await_args_list[1].args[0], "@User No gifs!")

                    gate.set()
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                first.delete.assert_awaited_once()
                self.assertGreaterEqual(animated_warning.edit.await_count, 1)
                self.assertEqual(
                    animated_warning.edit.await_args.kwargs["content"],
                    "──────────💥",
                )
                self.assertEqual(cog._gif_detector_animated_guilds, set())

    async def test_deleted_animation_warning_does_not_shorten_source_deadline(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                gif_detector = import_module("Honeypot.gif_detector")
                cog = honeypot.Honeypot(_Bot())
                cog.config.defaults.update(
                    gif_detector_enabled=True,
                    gif_detector_channels=[10],
                )
                cog.bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                cog._is_protected_member = mock.AsyncMock(return_value=False)
                sleep_seconds = []
                clock = 100.0

                async def advance_clock(seconds):
                    nonlocal clock
                    sleep_seconds.append(seconds)
                    clock += seconds
                warning = SimpleNamespace(
                    edit=mock.AsyncMock(side_effect=honeypot.discord.NotFound()),
                    delete=mock.AsyncMock(),
                )
                channel = SimpleNamespace(
                    id=10,
                    parent_id=None,
                    send=mock.AsyncMock(return_value=warning),
                )
                guild = SimpleNamespace(id=1)
                message = SimpleNamespace(
                    id=30,
                    guild=guild,
                    channel=channel,
                    author=SimpleNamespace(id=20, mention="@User", bot=False),
                    webhook_id=None,
                    embeds=[SimpleNamespace(type="gifv")],
                    delete=mock.AsyncMock(),
                )

                with (
                    mock.patch.object(
                        gif_detector.asyncio,
                        "sleep",
                        new=mock.AsyncMock(side_effect=advance_clock),
                    ),
                    mock.patch.object(
                        gif_detector.time,
                        "monotonic",
                        side_effect=lambda: clock,
                    ),
                    mock.patch.object(
                        honeypot.detection,
                        "on_message",
                        new=mock.AsyncMock(),
                    ),
                ):
                    await cog.on_message(message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                self.assertEqual(sum(sleep_seconds), 5)
                message.delete.assert_awaited_once()
                self.assertEqual(cog._gif_detector_animated_guilds, set())


if __name__ == "__main__":
    unittest.main()
