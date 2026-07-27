"""Moderator decisions on an open case: ban, kick and ignore, who they are
attributed to, and how they supersede each other.
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
    _Bot,
    _isolated_honeypot_modules,
    drain_background_work,
)


class CaseModeratorDecisionTests(CaseExpiryTestCase):
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
                await drain_background_work(cog, restarted)

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
