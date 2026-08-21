from __future__ import annotations

import unittest
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules


class _CommandTree:
    def __init__(self):
        self.command = None
        self.add_count = 0
        self.remove_count = 0

    def get_command(self, name, *, type):
        if self.command is not None and self.command.name == name:
            return self.command
        return None

    def add_command(self, command, *, override=False):
        self.command = command
        self.add_count += 1
        self.override = override

    def remove_command(self, name, *, type):
        self.command = None
        self.remove_count += 1


def _source_message(*, channel=None, guild=None, attachments=()):
    channel = channel or SimpleNamespace(id=100, name="french", mention="#french")
    guild = guild or SimpleNamespace(id=1)
    author = SimpleNamespace(id=20, display_name="target", roles=())
    return SimpleNamespace(
        id=50,
        guild=guild,
        channel=channel,
        author=author,
        content="offending content",
        attachments=attachments,
        created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        delete=mock.AsyncMock(),
    )


class ManualPunishmentSelectionTests(unittest.TestCase):
    def test_defaults_and_exclusive_actions_match_the_panel_contract(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")

                selection = module.PunishmentSelection()
                self.assertTrue(selection.capture_evidence)
                self.assertEqual(selection.member_action, module.MemberAction.NONE)
                self.assertEqual(selection.role_ids, ())

                selection.select_roles((500, 501))
                selection.select_member_action("mute")
                self.assertEqual(selection.role_ids, (500, 501))
                self.assertEqual(selection.member_action, module.MemberAction.MUTE)

                selection.select_member_action("kick")
                self.assertEqual(selection.role_ids, ())
                self.assertEqual(selection.member_action, module.MemberAction.KICK)

                selection.select_roles((500,))
                self.assertEqual(selection.member_action, module.MemberAction.NONE)
                self.assertEqual(selection.role_ids, (500,))

                selection.toggle_evidence()
                self.assertFalse(selection.capture_evidence)
                self.assertTrue(selection.has_punishment)

    def test_modal_uses_one_reason_and_only_adds_mute_duration_when_needed(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")
                source = _source_message()
                controller = SimpleNamespace()

                combined = module.PunishmentDetailsModal(
                    controller,
                    source,
                    module.PunishmentSelection(
                        member_action=module.MemberAction.MUTE,
                        role_ids=(500,),
                    ),
                )
                self.assertEqual(
                    [child.label for child in combined.children],
                    ["Reason", "Mute duration"],
                )

                ban = module.PunishmentDetailsModal(
                    controller,
                    source,
                    module.PunishmentSelection(member_action=module.MemberAction.BAN),
                )
                self.assertEqual([child.label for child in ban.children], ["Reason"])

    def test_mute_duration_keeps_the_existing_compact_contract(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")

                self.assertEqual(module.parse_mute_duration("30m"), 30 * 60)
                self.assertEqual(module.parse_mute_duration("2h"), 2 * 60 * 60)
                for invalid in ("", "0m", "29d", "1.5h", "1y"):
                    with self.subTest(invalid=invalid):
                        with self.assertRaises(ValueError):
                            module.parse_mute_duration(invalid)


class ManualPunishmentSettingsTests(unittest.TestCase):
    def test_role_nt_settings_are_typed_and_normalized(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                settings = import_module("NHCogs.honeypot.settings")

                configured = settings.GuildSettings.from_mapping(
                    {
                        "manual_punishment_roles": {
                            "101": {
                                "source_channel_ids": [10, 20, 10],
                                "notification_channel_id": 30,
                            },
                            "invalid": {
                                "source_channel_ids": [40],
                                "notification_channel_id": None,
                            },
                        }
                    }
                )

                self.assertEqual(set(configured.manual_punishment_roles), {101})
                role = configured.manual_punishment_roles[101]
                self.assertEqual(role.source_channel_ids, (10, 20))
                self.assertEqual(role.notification_channel_id, 30)


class ManualPunishmentPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_evidence_audit_never_copies_source_content_or_attachments(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")
                publication = import_module("NHCogs.honeypot.manual_punishment_publication")
                attachment = SimpleNamespace(to_file=mock.AsyncMock())
                source = _source_message(attachments=(attachment,))
                primary = SimpleNamespace(edit=mock.AsyncMock(), jump_url="audit-url")
                channel = SimpleNamespace(send=mock.AsyncMock(return_value=primary))
                selection = module.PunishmentSelection(
                    capture_evidence=False,
                    member_action=module.MemberAction.BAN,
                )

                audit = await publication.create_private_audit(
                    channel,
                    source,
                    SimpleNamespace(id=10, display_name="moderator"),
                    selection,
                    reason="scam",
                    mute_duration_label=None,
                )

                content = channel.send.await_args.kwargs["content"]
                self.assertIn("Evidence: Not saved", content)
                self.assertNotIn("offending content", content)
                attachment.to_file.assert_not_awaited()
                self.assertEqual(audit.primary, primary)

    async def test_one_unavailable_attachment_keeps_the_other_evidence(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")
                publication = import_module("NHCogs.honeypot.manual_punishment_publication")
                good_file = object()
                good = SimpleNamespace(
                    filename="good.png",
                    to_file=mock.AsyncMock(return_value=good_file),
                )
                missing = SimpleNamespace(
                    filename="missing.png",
                    to_file=mock.AsyncMock(side_effect=OSError("gone")),
                )
                source = _source_message(attachments=(good, missing))
                primary = SimpleNamespace(edit=mock.AsyncMock(), jump_url="audit-url")
                part = SimpleNamespace()
                channel = SimpleNamespace(
                    send=mock.AsyncMock(side_effect=(primary, part))
                )

                audit = await publication.create_private_audit(
                    channel,
                    source,
                    SimpleNamespace(id=10, display_name="moderator"),
                    module.PunishmentSelection(),
                    reason=None,
                    mute_duration_label=None,
                )

                self.assertEqual(audit.attachment_failures, ("missing.png",))
                self.assertEqual(channel.send.await_count, 2)
                self.assertEqual(channel.send.await_args_list[1].kwargs["files"], [good_file])

    async def test_publication_combines_successes_and_prioritizes_source_channel(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                publication = import_module("NHCogs.honeypot.manual_punishment_publication")
                source_channel = SimpleNamespace(id=100, send=mock.AsyncMock())
                alternate = SimpleNamespace(id=200, send=mock.AsyncMock())
                guild = SimpleNamespace(
                    get_channel=lambda channel_id: alternate if channel_id == 200 else None
                )
                target = SimpleNamespace(id=20)
                moderator = SimpleNamespace(id=10)
                outcomes = (
                    publication.PunishmentOutcome.role_succeeded(
                        500, "Dev-n’t", notification_channel_id=200
                    ),
                    publication.PunishmentOutcome.role_succeeded(
                        501, "French-n’t", notification_channel_id=None
                    ),
                    publication.PunishmentOutcome.mute_succeeded("6 hours"),
                    publication.PunishmentOutcome.failed("ban", "failed"),
                )

                result = await publication.publish_public_result(
                    guild,
                    source_channel,
                    target,
                    moderator,
                    outcomes,
                    reason="repeated disruption",
                )

                source_channel.send.assert_awaited_once()
                alternate.send.assert_not_awaited()
                content = source_channel.send.await_args.args[0]
                self.assertEqual(
                    content,
                    "<@20> received Dev-n’t and French-n’t and was muted by "
                    "<@10> for 6 hours.\nReason: repeated disruption",
                )
                self.assertNotIn("Evidence", content)
                self.assertEqual(result.channel_id, 100)

    async def test_large_role_set_stays_within_discord_message_limits(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")
                publication = import_module("NHCogs.honeypot.manual_punishment_publication")
                role_names = tuple(
                    f"role-{index}-{'x' * 90}" for index in range(25)
                )
                selection = module.PunishmentSelection(
                    capture_evidence=False,
                    role_ids=tuple(range(500, 525)),
                )
                source = _source_message()
                sent_messages = []

                async def send_message(*args, **kwargs):
                    message = SimpleNamespace(
                        edit=mock.AsyncMock(),
                        delete=mock.AsyncMock(),
                        jump_url=f"audit-{len(sent_messages)}",
                    )
                    sent_messages.append(message)
                    return message

                evidence_channel = SimpleNamespace(
                    send=mock.AsyncMock(side_effect=send_message)
                )
                moderator = SimpleNamespace(id=10, display_name="moderator")
                audit = await publication.create_private_audit(
                    evidence_channel,
                    source,
                    moderator,
                    selection,
                    reason="r" * publication.MAX_REASON_LENGTH,
                    mute_duration_label=None,
                    role_names=role_names,
                )
                outcomes = tuple(
                    publication.PunishmentOutcome.role_succeeded(
                        role_id,
                        role_name,
                        notification_channel_id=None,
                    )
                    for role_id, role_name in zip(
                        selection.role_ids, role_names, strict=True
                    )
                )

                await publication.finalize_private_audit(
                    audit,
                    source,
                    moderator,
                    selection,
                    reason="r" * publication.MAX_REASON_LENGTH,
                    mute_duration_label=None,
                    role_names=role_names,
                    source_deletion="Completed",
                    outcomes=outcomes,
                    notification_result=None,
                )

                published_contents = [
                    call.kwargs["content"]
                    for call in evidence_channel.send.await_args_list
                    if "content" in call.kwargs
                ]
                published_contents.extend(
                    call.kwargs["content"]
                    for message in sent_messages
                    for call in message.edit.await_args_list
                )
                self.assertTrue(published_contents)
                self.assertTrue(
                    all(
                        len(content) <= publication.DISCORD_MESSAGE_LIMIT
                        for content in published_contents
                    )
                )
                self.assertIn("role-24-", "\n".join(published_contents))

                public_channel = SimpleNamespace(id=100, send=mock.AsyncMock())
                await publication.publish_public_result(
                    SimpleNamespace(get_channel=lambda channel_id: None),
                    public_channel,
                    SimpleNamespace(id=20),
                    moderator,
                    outcomes,
                    reason="r" * publication.MAX_REASON_LENGTH,
                )
                public_content = public_channel.send.await_args.args[0]
                self.assertLessEqual(
                    len(public_content), publication.DISCORD_MESSAGE_LIMIT
                )
                self.assertIn("25 Role n’t roles", public_content)


class ManualPunishmentControllerTests(unittest.IsolatedAsyncioTestCase):
    def test_context_action_is_registered_only_as_punish(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")
                tree = _CommandTree()
                controller = module.ManualPunishmentController(
                    SimpleNamespace(bot=SimpleNamespace(tree=tree))
                )

                self.assertEqual(controller.context_menu.name, "Punish")
                controller.register()
                controller.register()
                controller.unregister()
                controller.unregister()
                self.assertEqual(tree.add_count, 1)
                self.assertEqual(tree.remove_count, 1)

    async def test_forum_thread_uses_parent_role_nt_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")
                role = SimpleNamespace(id=500, name="Development-n’t", position=10)
                evidence_channel = SimpleNamespace(id=900)
                source_channel = SimpleNamespace(
                    id=101,
                    parent_id=100,
                    name="feature-request",
                    mention="#feature-request",
                )
                guild = SimpleNamespace(
                    id=1,
                    get_role=lambda role_id: role if role_id == role.id else None,
                    get_channel=lambda channel_id: (
                        evidence_channel if channel_id == 900 else None
                    ),
                )
                source = _source_message(channel=source_channel, guild=guild)
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    config=SimpleNamespace(
                        guild=lambda guild: SimpleNamespace(
                            all=mock.AsyncMock(
                                return_value={
                                    "manual_evidence_channel": 900,
                                    "manual_punishment_roles": {
                                        "500": {
                                            "source_channel_ids": [100],
                                            "notification_channel_id": None,
                                        }
                                    },
                                }
                            )
                        )
                    ),
                    _channel_is_private=mock.Mock(return_value=True),
                    _missing_channel_permissions=mock.Mock(return_value=None),
                )
                response = SimpleNamespace(send_message=mock.AsyncMock())
                interaction = SimpleNamespace(
                    guild=guild,
                    permissions=SimpleNamespace(manage_messages=True),
                    user=SimpleNamespace(id=10),
                    response=response,
                )

                await module.ManualPunishmentController(cog).open(
                    interaction,
                    source,
                )

                view = response.send_message.await_args.kwargs["view"]
                self.assertEqual(view.roles, (role,))

    async def test_combined_dry_run_checks_once_and_applies_no_effects(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")
                publication = import_module("NHCogs.honeypot.manual_punishment_publication")
                roles = {
                    500: SimpleNamespace(
                        id=500,
                        name="French-n’t",
                        managed=False,
                        position=20,
                    ),
                    501: SimpleNamespace(
                        id=501,
                        name="Development-n’t",
                        managed=False,
                        position=10,
                    ),
                }
                target = SimpleNamespace(
                    id=20,
                    display_name="target",
                    roles=(),
                    add_roles=mock.AsyncMock(),
                )
                evidence_channel = SimpleNamespace(id=900)
                source_channel = SimpleNamespace(
                    id=101,
                    parent_id=100,
                    name="french",
                )
                guild = SimpleNamespace(
                    id=1,
                    get_member=lambda member_id: target,
                    fetch_member=mock.AsyncMock(return_value=target),
                    get_role=roles.get,
                    get_channel=lambda channel_id: (
                        evidence_channel if channel_id == 900 else source_channel
                    ),
                )
                source = _source_message(channel=source_channel, guild=guild)
                settings = {
                    "dry_run": True,
                    "manual_evidence_channel": 900,
                    "manual_punishment_roles": {
                        "500": {
                            "source_channel_ids": [100],
                            "notification_channel_id": None,
                        },
                        "501": {
                            "source_channel_ids": [100],
                            "notification_channel_id": 200,
                        },
                    },
                }
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree(), get_cog=mock.Mock()),
                    config=SimpleNamespace(
                        guild=lambda guild: SimpleNamespace(
                            all=mock.AsyncMock(return_value=settings)
                        )
                    ),
                    _is_protected_member=mock.AsyncMock(return_value=False),
                    _channel_is_private=mock.Mock(return_value=True),
                    _missing_channel_permissions=mock.Mock(return_value=None),
                    _missing_role_assignment_permission=mock.Mock(return_value=None),
                    _punitive_effect_allowed=mock.AsyncMock(return_value=False),
                    _record_operational_failure=mock.AsyncMock(),
                    _observe_background_task=mock.Mock(),
                )
                audit = publication.PrivateAudit(
                    primary=SimpleNamespace(jump_url="audit-url"),
                    parts=(),
                    content_external=False,
                    attachment_failures=(),
                )
                module.publication.create_private_audit = mock.AsyncMock(
                    return_value=audit
                )
                module.publication.finalize_private_audit = mock.AsyncMock()
                module.publication.publish_public_result = mock.AsyncMock()
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=10, display_name="moderator"),
                    permissions=SimpleNamespace(manage_messages=True),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )
                selection = module.PunishmentSelection(
                    capture_evidence=False,
                    member_action=module.MemberAction.MUTE,
                    role_ids=(501, 500),
                )

                await module.ManualPunishmentController(cog).execute(
                    interaction,
                    source,
                    selection,
                    reason="spam",
                    mute_duration_label="30m",
                    mute_duration_seconds=1800,
                )

                cog._punitive_effect_allowed.assert_awaited_once_with(guild)
                target.add_roles.assert_not_awaited()
                cog.bot.get_cog.assert_not_called()
                module.publication.publish_public_result.assert_not_awaited()
                source.delete.assert_awaited_once()
                outcomes = module.publication.finalize_private_audit.await_args.kwargs[
                    "outcomes"
                ]
                self.assertTrue(outcomes)
                self.assertTrue(all(outcome.status == "planned" for outcome in outcomes))
                self.assertEqual(
                    [outcome.role_id for outcome in outcomes if outcome.kind == "role"],
                    [500, 501],
                )

    async def test_kick_delegates_to_the_central_manual_action_path(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")
                publication = import_module("NHCogs.honeypot.manual_punishment_publication")
                target = SimpleNamespace(id=20, display_name="target", roles=())
                evidence_channel = SimpleNamespace(id=900)
                source_channel = SimpleNamespace(id=100, name="general")
                guild = SimpleNamespace(
                    id=1,
                    get_member=lambda member_id: target,
                    fetch_member=mock.AsyncMock(return_value=target),
                    get_role=lambda role_id: None,
                    get_channel=lambda channel_id: (
                        evidence_channel if channel_id == 900 else source_channel
                    ),
                )
                source = _source_message(channel=source_channel, guild=guild)
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    config=SimpleNamespace(
                        guild=lambda guild: SimpleNamespace(
                            all=mock.AsyncMock(
                                return_value={"manual_evidence_channel": 900}
                            )
                        )
                    ),
                    _is_protected_member=mock.AsyncMock(return_value=False),
                    _channel_is_private=mock.Mock(return_value=True),
                    _missing_channel_permissions=mock.Mock(return_value=None),
                    _execute_action=mock.AsyncMock(
                        return_value=SimpleNamespace(
                            status=module.EffectStatus.SUCCEEDED,
                            label="The member has been kicked.",
                            failed_message=None,
                            modlog_failed=False,
                        )
                    ),
                    _record_operational_failure=mock.AsyncMock(),
                    _observe_background_task=mock.Mock(),
                )
                audit = publication.PrivateAudit(
                    primary=SimpleNamespace(jump_url="audit-url"),
                    parts=(),
                    content_external=False,
                    attachment_failures=(),
                )
                module.publication.create_private_audit = mock.AsyncMock(
                    return_value=audit
                )
                module.publication.finalize_private_audit = mock.AsyncMock()
                module.publication.publish_public_result = mock.AsyncMock(
                    return_value=publication.PublicNotificationResult(100, "sent")
                )
                moderator = SimpleNamespace(id=10, display_name="moderator")
                interaction = SimpleNamespace(
                    user=moderator,
                    permissions=SimpleNamespace(manage_messages=True),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await module.ManualPunishmentController(cog).execute(
                    interaction,
                    source,
                    module.PunishmentSelection(
                        capture_evidence=False,
                        member_action=module.MemberAction.KICK,
                    ),
                    reason="repeated abuse",
                    mute_duration_label=None,
                    mute_duration_seconds=None,
                )

                action_call = cog._execute_action.await_args
                self.assertEqual(action_call.kwargs["action"], "kick")
                self.assertEqual(action_call.kwargs["reason"], "repeated abuse")
                self.assertEqual(action_call.kwargs["moderator"], moderator)
                module.publication.publish_public_result.assert_awaited_once()

    async def test_execute_rechecks_moderator_permission_and_private_audit_channel(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("NHCogs.honeypot.manual_punishment")
                source = _source_message()
                followup = SimpleNamespace(send=mock.AsyncMock())
                controller = module.ManualPunishmentController(
                    SimpleNamespace(bot=SimpleNamespace(tree=_CommandTree()))
                )
                module.publication.create_private_audit = mock.AsyncMock()

                await controller.execute(
                    SimpleNamespace(
                        user=SimpleNamespace(id=10),
                        permissions=SimpleNamespace(manage_messages=False),
                        followup=followup,
                    ),
                    source,
                    module.PunishmentSelection(),
                    reason=None,
                    mute_duration_label=None,
                    mute_duration_seconds=None,
                )

                module.publication.create_private_audit.assert_not_awaited()
                source.delete.assert_not_awaited()
                self.assertIn(
                    "Manage Messages",
                    followup.send.await_args.args[0],
                )

                evidence_channel = SimpleNamespace(id=900)
                source.guild.get_channel = lambda channel_id: evidence_channel
                controller.cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={"manual_evidence_channel": 900}
                        )
                    )
                )
                controller.cog._channel_is_private = mock.Mock(return_value=False)
                controller.cog._missing_channel_permissions = mock.Mock(
                    return_value=None
                )
                followup.send.reset_mock()

                await controller.execute(
                    SimpleNamespace(
                        user=SimpleNamespace(id=10),
                        permissions=SimpleNamespace(manage_messages=True),
                        followup=followup,
                    ),
                    source,
                    module.PunishmentSelection(),
                    reason=None,
                    mute_duration_label=None,
                    mute_duration_seconds=None,
                )

                module.publication.create_private_audit.assert_not_awaited()
                source.delete.assert_not_awaited()
                self.assertIn("must be private", followup.send.await_args.args[0])
