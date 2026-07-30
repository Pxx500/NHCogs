"""Automatic moderation dispatched by the detection pipeline: ban, kick,
the review role and the dry-run variants of each.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.detection_case_fixtures import publish_primary
from tests.harness import DetectionPipelineTestCase, _Bot, _isolated_honeypot_modules, active_case


def _effect_result(honeypot, label, failed_message=None):
    status = (
        honeypot.detection.EffectStatus.FAILED
        if failed_message is not None
        else honeypot.detection.EffectStatus.SUCCEEDED
    )
    return honeypot.ModerationEffectResult(label, failed_message, status)


class DetectionActionTests(DetectionPipelineTestCase):
    async def test_malformed_dry_run_setting_does_not_suppress_source_deletion(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                bot.owner_ids = set()
                bot.is_mod = mock.AsyncMock(return_value=False)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                await asyncio.to_thread(cog._case_store.initialize)
                await cog._message_registry.initialize()
                config = {
                    "enabled": True,
                    "dry_run": "false",
                    "honeypot_channels": [400],
                    "logs_channel": None,
                    "review_enabled": False,
                    "review_channel": None,
                }
                stats_context = mock.MagicMock()
                stats_context.__aenter__ = mock.AsyncMock(return_value={})
                stats_context.__aexit__ = mock.AsyncMock(return_value=False)
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value=config),
                    stats=mock.Mock(return_value=stats_context),
                )
                cog.config = SimpleNamespace(
                    guild=lambda guild: guild_config,
                    guild_from_id=lambda guild_id: SimpleNamespace(
                        all=mock.AsyncMock(return_value=config)
                    ),
                )
                message = self._message(honeypot, attachment_count=0)
                message.guild.me = SimpleNamespace(top_role=10)
                message.author.guild = message.guild
                message.author.guild_permissions = SimpleNamespace(
                    manage_guild=False
                )
                message.author.top_role = 1
                message.guild.get_member = lambda user_id: message.author

                await cog.on_message(message)

                message.delete.assert_awaited_once()

    async def test_malformed_dry_run_setting_does_not_suppress_kick(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                bot.cog_disabled_in_guild = mock.AsyncMock(return_value=False)
                bot.owner_ids = set()
                bot.is_mod = mock.AsyncMock(return_value=False)
                bot.is_admin = mock.AsyncMock(return_value=False)
                cog = honeypot.Honeypot(bot)
                await asyncio.to_thread(cog._case_store.initialize)
                await cog._message_registry.initialize()
                config = {
                    "enabled": True,
                    "dry_run": "false",
                    "honeypot_channels": [400],
                    "logs_channel": None,
                    "review_enabled": False,
                    "review_channel": None,
                    "fallback_action": "kick",
                }
                stats_context = mock.MagicMock()
                stats_context.__aenter__ = mock.AsyncMock(return_value={})
                stats_context.__aexit__ = mock.AsyncMock(return_value=False)
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value=config),
                    stats=mock.Mock(return_value=stats_context),
                )
                cog.config = SimpleNamespace(
                    guild=lambda guild: guild_config,
                    guild_from_id=lambda guild_id: SimpleNamespace(
                        all=mock.AsyncMock(return_value=config)
                    ),
                )
                message = self._message(honeypot, attachment_count=0)
                message.guild.me = SimpleNamespace(
                    top_role=10,
                    guild_permissions=SimpleNamespace(
                        kick_members=True,
                        ban_members=True,
                    ),
                )
                message.author.guild = message.guild
                message.author.guild_permissions = SimpleNamespace(
                    manage_guild=False
                )
                message.author.top_role = 1
                message.author.kick = mock.AsyncMock()
                message.guild.get_member = lambda user_id: message.author
                bot.get_guild = lambda guild_id: message.guild
                honeypot.modlog.create_case = mock.AsyncMock()

                await cog.on_message(message)

                message.author.kick.assert_awaited_once()

    async def test_dry_run_does_not_apply_review_role(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                role = SimpleNamespace(id=55)
                message.author.add_roles = mock.AsyncMock()
                message.guild.get_member = lambda user_id: message.author
                message.guild.get_role = lambda role_id: role
                cog.bot.get_guild = lambda guild_id: message.guild
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": True,
                        "logs_channel": None,
                        "review_channel": None,
                        "honeypot_channels": [message.channel.id],
                        "action": "review",
                        "fallback_action": "review",
                        "mute_role": role.id,
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
                message.author.add_roles.assert_not_awaited()
                self.assertFalse(
                    any(
                        operation.operation_type == "role_apply"
                        for operation in snapshot.operations
                    )
                )

    async def test_message_is_contained_while_old_moderator_effect_is_in_flight(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                first = self._message(
                    honeypot, attachment_count=0, message_id=299
                )
                second = self._message(honeypot, attachment_count=0)
                second.guild = first.guild
                second.author = first.author
                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": False,
                    "firstpost_enabled": False, "firstpost_collect_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(first),
                    (honeypot.detection._forward_purge_signal(cog, first),),
                )
                now = datetime.now(timezone.utc)
                moderator = await asyncio.to_thread(
                    cog._case_store.claim_moderator_action,
                    appended.case.case_id,
                    "ban",
                    99,
                    now - timedelta(seconds=7),
                )
                moderator = await asyncio.to_thread(
                    cog._case_store.claim_operation,
                    moderator.operation_id,
                    now - timedelta(seconds=7),
                )
                self.assertTrue(
                    await asyncio.to_thread(
                        cog._case_store.start_operation_effect,
                        moderator.operation_id,
                        moderator.claim_token,
                        now - timedelta(seconds=6),
                    )
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await asyncio.wait_for(cog.on_message(second), timeout=1)

                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                self.assertEqual(
                    [message.message_id for message in snapshot.messages],
                    [first.id, second.id],
                )
                self.assertEqual(snapshot.messages[-1].delete_status.value, "deleted")
                second.delete.assert_awaited_once()

    async def test_multiple_signals_execute_only_one_ban(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                cog._firstpost_loaded_guilds.add(100)
                message = self._message(honeypot, attachment_count=4)
                message.author.ban = mock.AsyncMock()
                message.guild.get_member = lambda user_id: message.author
                message.guild.me = SimpleNamespace(id=1)
                cog.bot.get_guild = lambda guild_id: message.guild
                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": True,
                    "spam_action": "ban", "firstpost_enabled": True,
                    "firstpost_action": "ban", "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._ban_delete_message_seconds = mock.Mock(return_value=0)
                cog._schedule_post_ban_sweep = mock.Mock()
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                honeypot.modlog.create_case = mock.AsyncMock()

                await cog.on_message(message)

                message.author.ban.assert_awaited_once()
                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(
                    [item.signal.detector for item in snapshot.signals],
                    ["spam", "firstpost"],
                )

    async def test_automatic_ban_case_projection_catches_up_after_early_publication(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(
                    honeypot,
                    attachment_count=4,
                    channel_id=400,
                )
                message.created_at = datetime.now(timezone.utc)
                message.guild.get_member = lambda user_id: message.author
                message.guild.me = SimpleNamespace(id=1)
                cog.bot.get_guild = lambda guild_id: message.guild
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "review_enabled": True,
                    "honeypot_channels": [400],
                    "whitelisted_roles": [],
                    "fallback_action": "review",
                    "action": "ban",
                    "spam_enabled": False,
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._suspicion_reasons = mock.AsyncMock(
                    return_value=["Matched suspicious content rule"]
                )
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._ban_delete_message_seconds = mock.Mock(return_value=0)
                cog._schedule_post_ban_sweep = mock.Mock()
                honeypot.modlog.create_case = mock.AsyncMock()

                action_started = asyncio.Event()
                release_action = asyncio.Event()

                async def execute_action(*args, **kwargs):
                    action_started.set()
                    await release_action.wait()
                    return _effect_result(honeypot, "banned")

                async def persist_image_matches(
                    _message,
                    _config,
                    case_id,
                    sequence,
                    capture_results,
                ):
                    for result in capture_results:
                        await asyncio.to_thread(
                            cog._case_store.update_attachment_scan,
                            case_id,
                            sequence,
                            result.position,
                            f"sha-{result.position}",
                            f"phash-{result.position}",
                            match_metadata={"matched": True, "score": 0},
                            error=None,
                        )

                published = []

                async def publish_case(case_id, _config, _channel, **kwargs):
                    skip_if_done = kwargs.get("skip_if_done")
                    if skip_if_done is not None and skip_if_done.done():
                        return False
                    snapshot = await asyncio.to_thread(
                        cog._case_store.get_case, case_id
                    )
                    projection = honeypot.render_case(snapshot)
                    published.append(
                        (
                            projection,
                            projection.moderation_actions,
                            tuple(
                                item
                                for item in honeypot.case_feedback_items(snapshot)
                                if item.decision is None
                            ),
                        )
                    )
                    return True

                cog._execute_action = execute_action
                cog._scan_all_case_message_images = persist_image_matches
                cog._publish_detection_case = publish_case

                processing = asyncio.create_task(cog.on_message(message))
                await asyncio.wait_for(action_started.wait(), timeout=1)
                snapshot = await asyncio.to_thread(
                    active_case,
                    cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                review = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "review_publish"
                )
                claimed_review = await asyncio.to_thread(
                    cog._case_store.claim_operation,
                    review.operation_id,
                    datetime.now(timezone.utc),
                )
                self.assertIsNotNone(claimed_review)
                await cog._execute_detection_case_operation(
                    claimed_review, datetime.now(timezone.utc)
                )
                release_action.set()
                await processing

                final_projection, moderation_actions, pending_images = published[-1]
                self.assertEqual(final_projection.moderation_status, "Ban")
                self.assertEqual(moderation_actions, ())
                self.assertEqual(len(pending_images), 4)
                self.assertTrue(
                    any(
                        "Matched suspicious content rule" in line
                        for line in final_projection.signal_lines
                    )
                )

                await cog._case_review_service.apply_bulk(
                    snapshot.case.case_id,
                    "ignore",
                    999,
                )
                self.assertTrue(
                    await cog._finish_case_review_if_ready(
                        snapshot.case.case_id,
                        999,
                    )
                )
                resolved = await asyncio.to_thread(
                    cog._case_store.get_case,
                    snapshot.case.case_id,
                )
                resolved_projection, resolved_actions, pending_images = published[-1]
                self.assertEqual(resolved.case.status.value, "resolved")
                self.assertEqual(resolved_projection.resolution, "ban")
                self.assertEqual(resolved_actions, ())
                self.assertEqual(pending_images, ())

    async def test_late_automatic_moderation_completion_refreshes_open_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                member = SimpleNamespace(id=20, roles=[])
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    fetch_ban=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("not banned")
                    ),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = SimpleNamespace(
                    guild_from_id=lambda guild_id: SimpleNamespace(
                        all=mock.AsyncMock(return_value={"dry_run": False})
                    )
                )
                await asyncio.to_thread(cog._case_store.initialize)
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
                                10,
                                "image/png",
                                None,
                                None,
                                "https://cdn.test/proof.png",
                            ),
                        ),
                    ),
                    (
                        honeypot.DetectionSignal(
                            "honeypot",
                            "Matched suspicious content rule",
                            honeypot.ActionIntent.BAN,
                            True,
                            {},
                        ),
                    ),
                )
                moderation = await asyncio.to_thread(
                    cog._case_store.ensure_operation,
                    appended.case.case_id,
                    "moderation_action",
                    f"moderation:{appended.case.case_id}:1:ban",
                    appended.message.sequence,
                )
                self.assertTrue(
                    publish_primary(
                        cog._case_store,
                        appended.case.case_id,
                        50,
                        60,
                    )
                )
                cog._execute_action = mock.AsyncMock(
                    return_value=_effect_result(honeypot, "ban")
                )
                published = []

                async def publish_case(case_id, _config, _channel, **kwargs):
                    snapshot = await asyncio.to_thread(
                        cog._case_store.get_case, case_id
                    )
                    published.append(honeypot.render_case(snapshot))
                    return True

                cog._publish_detection_case = publish_case
                claimed = await asyncio.to_thread(
                    cog._case_store.claim_operation,
                    moderation.operation_id,
                    now,
                )

                await cog._execute_detection_case_operation(claimed, now)

                self.assertTrue(published)
                self.assertEqual(published[-1].moderation_status, "Ban")
                self.assertEqual(published[-1].moderation_actions, ())

    async def test_automatic_ban_uses_persisted_id_when_member_cache_misses(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                target = SimpleNamespace(id=20)
                guild = SimpleNamespace(
                    id=100,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: None,
                    ban=mock.AsyncMock(),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                bot.fetch_user = mock.AsyncMock(return_value=target)
                cog = honeypot.Honeypot(bot)
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                message.guild = guild
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
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._ban_delete_message_seconds = mock.Mock(return_value=0)
                cog._schedule_post_ban_sweep = mock.Mock()
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()
                honeypot.modlog.create_case = mock.AsyncMock()

                await cog.on_message(message)

                guild.ban.assert_awaited_once()
                self.assertIs(guild.ban.await_args.args[0], target)
                case_id = cog._publish_detection_case.await_args_list[0].args[0]
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, case_id
                )
                self.assertEqual(snapshot.case.status.value, "resolved")

    async def test_automatic_kick_missing_member_is_terminal_and_classified(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild = SimpleNamespace(
                    id=100,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: None,
                    fetch_member=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("member left")
                    ),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                message.guild = guild
                config = {
                    "enabled": True, "dry_run": False, "logs_channel": None,
                    "review_channel": None, "spam_enabled": True,
                    "spam_action": "kick", "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._execute_action = mock.AsyncMock(
                    side_effect=AssertionError("missing member cannot be kicked")
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                case_id = cog._publish_detection_case.await_args_list[0].args[0]
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, case_id
                )
                guild.fetch_member.assert_awaited_once_with(message.author.id)
                cog._execute_action.assert_not_awaited()
                self.assertEqual(snapshot.case.status.value, "resolved")

    async def test_automatic_dry_run_preserves_planned_action_kind(self):
        for action in ("ban", "kick"):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    cog = honeypot.Honeypot(_Bot())
                    await asyncio.to_thread(cog._case_store.initialize)
                    message = self._message(honeypot, attachment_count=0)
                    message.guild.get_member = lambda user_id: message.author
                    cog.bot.get_guild = lambda guild_id: message.guild
                    config = {
                        "enabled": True,
                        "dry_run": True,
                        "logs_channel": None,
                        "review_channel": None,
                        "spam_enabled": True,
                        "spam_action": action,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                        "imagescan_detector_enabled": False,
                    }
                    self._configure_public_boundary(cog, config)
                    cog._is_forward_purge_active.return_value = False
                    cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                    cog._execute_action = mock.AsyncMock(
                        side_effect=AssertionError("dry-run cannot execute moderation")
                    )
                    cog._scan_all_case_message_images = mock.AsyncMock()
                    cog._publish_detection_case = mock.AsyncMock()
                    admitted_case_ids = []
                    append_message = cog._case_store.append_message

                    def record_admission(*args, **kwargs):
                        admitted = append_message(*args, **kwargs)
                        if admitted is not None:
                            admitted_case_ids.append(admitted.case.case_id)
                        return admitted

                    with mock.patch.object(
                        cog._case_store,
                        "append_message",
                        side_effect=record_admission,
                    ):
                        await cog.on_message(message)

                    snapshot = await asyncio.to_thread(
                        cog._case_store.get_case,
                        admitted_case_ids[0],
                    )
                    moderation = next(
                        operation
                        for operation in snapshot.operations
                        if operation.operation_type
                        is honeypot.OperationType.MODERATION_ACTION
                    )
                    self.assertEqual(moderation.result, f"planned_{action}")
                    self.assertEqual(snapshot.case.status.value, "resolved")
                    cog._execute_action.assert_not_awaited()

    async def test_automatic_ban_reclaim_observes_started_effect_without_repeating_it(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                banned = False

                async def execute_action(*args, **kwargs):
                    nonlocal banned
                    banned = True
                    return _effect_result(honeypot, "banned")

                async def fetch_ban(target):
                    if not banned:
                        raise honeypot.discord.NotFound("not banned")
                    return SimpleNamespace(user=target)

                member = SimpleNamespace(id=20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    fetch_ban=mock.AsyncMock(side_effect=fetch_ban),
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                execute = mock.AsyncMock(side_effect=execute_action)
                async def config_values():
                    return {"dry_run": False}

                config = SimpleNamespace(
                    guild_from_id=lambda guild_id: SimpleNamespace(all=config_values)
                )
                first = honeypot.Honeypot(bot)
                first.config = config
                first._execute_action = execute
                first._case_store.initialize()
                appended = first._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=now,
                        jump_url="https://discord.test/messages/40",
                        attachments=(),
                    ),
                    (
                        honeypot.DetectionSignal(
                            "spam", "duplicate", honeypot.ActionIntent.BAN, True, {}
                        ),
                    ),
                )
                operation = first._case_store.ensure_operation(
                    appended.case.case_id,
                    "moderation_action",
                    f"moderation_action:{appended.case.case_id}:1:ban",
                    appended.message.sequence,
                )
                claimed = first._case_store.claim_operation(operation.operation_id, now)
                first._case_store.complete_operation = mock.Mock(
                    side_effect=RuntimeError("crash after Discord effect")
                )

                with self.assertRaisesRegex(RuntimeError, "crash after Discord effect"):
                    await first._execute_detection_case_operation(claimed, now)

                restarted = honeypot.Honeypot(bot)
                restarted.config = config
                restarted._execute_action = execute
                reclaimed = restarted._case_store.claim_due_operations(
                    now + timedelta(minutes=6),
                    stale_before=now + timedelta(minutes=5),
                )
                self.assertEqual(len(reclaimed), 1)

                await restarted._execute_detection_case_operation(
                    reclaimed[0], now + timedelta(minutes=6)
                )

                persisted = restarted._case_store.get_case(appended.case.case_id)
                completed = next(
                    item for item in persisted.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(execute.await_count, 1)
                guild.fetch_ban.assert_awaited_once()
                self.assertEqual(completed.status.value, "succeeded")
                self.assertEqual(completed.result, "ban")

    async def test_moderation_starts_after_containment_without_waiting_for_capture(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                message.guild.get_member = lambda user_id: message.author
                cog.bot.get_guild = lambda guild_id: message.guild
                capture_started = asyncio.Event()
                release_capture = asyncio.Event()
                delete_called = asyncio.Event()
                action_started = asyncio.Event()
                release_action = asyncio.Event()

                async def read_attachment(*args, **kwargs):
                    capture_started.set()
                    await release_capture.wait()
                    return b"evidence"

                async def delete_message(*args, **kwargs):
                    delete_called.set()

                async def fail_action(*args, **kwargs):
                    action_started.set()
                    await release_action.wait()
                    raise RuntimeError("moderation failed")

                message.attachments[0].read = mock.AsyncMock(side_effect=read_attachment)
                message.attachments[0].size = len(b"evidence")
                message.delete = mock.AsyncMock(side_effect=delete_message)
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": True,
                    "spam_action": "ban",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._execute_action = mock.AsyncMock(side_effect=fail_action)
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                task = asyncio.create_task(cog.on_message(message))
                try:
                    await asyncio.wait_for(capture_started.wait(), timeout=1)
                    await asyncio.wait_for(action_started.wait(), timeout=1)
                    containment_preceded_action = delete_called.is_set()
                finally:
                    release_capture.set()
                    release_action.set()
                outcome = (await asyncio.gather(task, return_exceptions=True))[0]

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "moderation_action"
                )
                self.assertTrue(containment_preceded_action)
                self.assertIsNone(outcome)
                self.assertEqual(snapshot.messages[0].delete_status.value, "deleted")
                self.assertEqual(snapshot.attachments[0].capture_status, "captured")
                self.assertEqual(operation.status.value, "failed")
                self.assertIn("moderation failed", operation.last_error)

    async def test_blocked_review_role_starts_after_containment_before_capture_finishes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                role = SimpleNamespace(id=55)
                message.author.roles = []
                message.guild.get_member = lambda user_id: message.author
                message.guild.get_role = lambda role_id: role
                cog.bot.get_guild = lambda guild_id: message.guild
                capture_started = asyncio.Event()
                source_deleted = asyncio.Event()
                cached_contained = asyncio.Event()
                role_started = asyncio.Event()
                capture_finished = asyncio.Event()
                release_capture = asyncio.Event()
                release_role = asyncio.Event()

                async def read_attachment(*args, **kwargs):
                    capture_started.set()
                    await release_capture.wait()
                    capture_finished.set()
                    return b"evidence"

                async def delete_message(*args, **kwargs):
                    source_deleted.set()

                async def purge_cached(*args, **kwargs):
                    cached_contained.set()
                    return 1

                async def block_role(*args, **kwargs):
                    role_started.set()
                    await release_role.wait()

                message.attachments[0].read = mock.AsyncMock(side_effect=read_attachment)
                message.attachments[0].size = len(b"evidence")
                message.delete = mock.AsyncMock(side_effect=delete_message)
                message.author.add_roles = mock.AsyncMock(side_effect=block_role)
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "mute_role": role.id,
                    "spam_enabled": True,
                    "spam_action": "review",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._purge_detection_case_cached_messages = mock.AsyncMock(
                    side_effect=purge_cached
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                task = asyncio.create_task(cog.on_message(message))
                await asyncio.wait_for(capture_started.wait(), timeout=1)
                await asyncio.wait_for(source_deleted.wait(), timeout=1)
                await asyncio.wait_for(cached_contained.wait(), timeout=1)
                try:
                    await asyncio.wait_for(role_started.wait(), timeout=0.05)
                    role_started_before_capture_finished = True
                except TimeoutError:
                    role_started_before_capture_finished = False

                release_capture.set()
                await asyncio.wait_for(role_started.wait(), timeout=1)
                release_role.set()
                await task
                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )

                self.assertTrue(role_started_before_capture_finished)
                self.assertTrue(capture_finished.is_set())
                self.assertEqual(snapshot.attachments[0].capture_status, "captured")

    async def test_moderation_retry_does_not_treat_missing_source_as_success(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                message.guild.get_member = lambda user_id: message.author
                message.guild.fetch_ban = mock.AsyncMock(
                    side_effect=honeypot.discord.NotFound("not banned")
                )
                cog.bot.get_guild = lambda guild_id: message.guild
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "spam_enabled": True,
                    "spam_action": "ban",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                cog._execute_action = mock.AsyncMock(
                    return_value=_effect_result(
                        honeypot, None, "initial moderation failure"
                    )
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "moderation_action"
                )
                self.assertEqual(operation.status.value, "failed")
                channel = SimpleNamespace(
                    fetch_message=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("source deleted")
                    )
                )
                message.guild.get_channel = lambda channel_id: channel
                message.guild.get_thread = lambda channel_id: None
                cog.config.guild_from_id = lambda guild_id: SimpleNamespace(
                    all=mock.AsyncMock(return_value=config)
                )
                cog._execute_action.reset_mock()
                cog._execute_action.return_value = _effect_result(
                    honeypot, None, "retry moderation failure"
                )
                now = operation.retry_at
                claimed = await asyncio.to_thread(
                    cog._case_store.claim_operation, operation.operation_id, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                retried = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                operation = next(
                    item
                    for item in retried.operations
                    if item.operation_type == "moderation_action"
                )
                cog._execute_action.assert_awaited_once()
                self.assertEqual(operation.status.value, "failed")
                self.assertEqual(operation.attempts, 2)
                self.assertIn("retry moderation failure", operation.last_error)

    async def test_honeypot_image_match_uses_suspicious_action(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                await cog._init_imagescan_store()
                message = self._message(
                    honeypot,
                    attachment_count=1,
                    channel_id=400,
                )
                message.author.ban = mock.AsyncMock()
                message.guild.get_member = lambda user_id: message.author
                message.guild.me = SimpleNamespace(id=1)
                cog.bot.get_guild = lambda guild_id: message.guild
                config = {
                    "enabled": True,
                    "dry_run": False,
                    "logs_channel": None,
                    "review_channel": None,
                    "honeypot_channels": [400],
                    "whitelisted_roles": [],
                    "fallback_action": "review",
                    "action": "ban",
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
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._ban_delete_message_seconds = mock.Mock(return_value=0)
                cog._schedule_post_ban_sweep = mock.Mock()
                cog._publish_detection_case = mock.AsyncMock()
                honeypot.modlog.create_case = mock.AsyncMock()

                with (
                    mock.patch.object(
                        honeypot.imagescan,
                        "image_hashes_from_bytes",
                        return_value={"sha256": "new", "phash": "near-known"},
                    ),
                    mock.patch.object(
                        honeypot.imagescan,
                        "match_image",
                        return_value={
                            "matched": True,
                            "score": 7,
                            "threshold": 20,
                            "exact_decision": None,
                        },
                    ),
                ):
                    await cog.on_message(message)

                message.author.ban.assert_awaited_once()
                snapshot = await asyncio.to_thread(
                    active_case,
                    cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                signals = {item.signal.detector: item.signal for item in snapshot.signals}
                self.assertEqual(signals["honeypot"].action, honeypot.ActionIntent.BAN)
                self.assertEqual(signals["image"].action, honeypot.ActionIntent.NONE)
                match = signals["image"].metadata["matches"][0]
                self.assertEqual(match["hash_diff"], 7)
                self.assertEqual(match["threshold"], 20)
