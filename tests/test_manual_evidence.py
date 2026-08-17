from __future__ import annotations

import asyncio
import unittest
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules


class ManualEvidenceSelectionTests(unittest.TestCase):
    def test_selection_enforces_punishment_compatibility(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                selection_type = getattr(module, "EvidenceSelection", None)
                self.assertIsNotNone(selection_type)

                selection = selection_type()
                selection.toggle_mement()
                selection.toggle_mute()
                self.assertTrue(selection.mement)
                self.assertTrue(selection.mute)
                self.assertEqual(selection.member_action.value, "none")

                selection.select_member_action("kick")
                self.assertFalse(selection.mement)
                self.assertFalse(selection.mute)
                self.assertEqual(selection.member_action.value, "kick")

                selection.toggle_mute()
                self.assertTrue(selection.mute)
                self.assertEqual(selection.member_action.value, "none")

                selection.select_member_action("ban")
                self.assertFalse(selection.mement)
                self.assertFalse(selection.mute)
                self.assertEqual(selection.member_action.value, "ban")

    def test_mute_duration_accepts_compact_units_within_range(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")

                self.assertEqual(module.parse_mute_duration("30m"), 30 * 60)
                self.assertEqual(module.parse_mute_duration("2h"), 2 * 60 * 60)
                self.assertEqual(module.parse_mute_duration("3d"), 3 * 24 * 60 * 60)
                self.assertEqual(module.parse_mute_duration("1w"), 7 * 24 * 60 * 60)

                for invalid in ("", "60", "0m", "29d", "1.5h", "1y"):
                    with self.subTest(invalid=invalid):
                        with self.assertRaises(ValueError):
                            module.parse_mute_duration(invalid)


class ManualEvidenceSettingsTests(unittest.TestCase):
    def test_manual_evidence_channels_are_typed_guild_settings(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                settings = import_module("Honeypot.settings")

                defaults = settings.GuildSettings.from_mapping({})
                self.assertIsNone(defaults.manual_evidence_memes_channel)
                self.assertIsNone(defaults.manual_evidence_mement_notification_channel)

                configured = settings.GuildSettings.from_mapping(
                    {
                        "manual_evidence_memes_channel": 123,
                        "manual_evidence_mement_notification_channel": 456,
                    }
                )
                self.assertEqual(configured.manual_evidence_memes_channel, 123)
                self.assertEqual(
                    configured.manual_evidence_mement_notification_channel,
                    456,
                )


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
        if self.command is not None and self.command.name == name:
            self.command = None
        self.remove_count += 1


class ManualEvidenceLifecycleTests(unittest.TestCase):
    def test_context_action_registration_and_removal_are_idempotent(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                tree = _CommandTree()
                cog = SimpleNamespace(bot=SimpleNamespace(tree=tree))

                controller_type = getattr(module, "ManualEvidenceController", None)
                self.assertIsNotNone(controller_type)
                controller = controller_type(cog)

                self.assertEqual(controller.context_menu.name, "Add evidence")
                self.assertTrue(controller.context_menu.guild_only)
                self.assertTrue(controller.context_menu.default_permissions.manage_messages)

                controller.register()
                controller.register()
                controller.unregister()
                controller.unregister()

                self.assertEqual(tree.add_count, 1)
                self.assertEqual(tree.remove_count, 1)
                self.assertTrue(tree.override)


class ManualEvidenceViewTests(unittest.TestCase):
    def test_initial_view_exposes_only_contextually_available_actions(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                view_type = getattr(module, "EvidenceActionView", None)
                self.assertIsNotNone(view_type)
                controller = SimpleNamespace()
                message = SimpleNamespace(id=50)

                ordinary = view_type(
                    controller,
                    message,
                    moderator_id=10,
                    allow_mement=False,
                )
                ordinary_labels = {
                    child.label for child in ordinary.children if hasattr(child, "label")
                }
                self.assertNotIn("Memen't: Off", ordinary_labels)
                self.assertIn("Mute: Off", ordinary_labels)
                self.assertIn("Confirm", ordinary_labels)
                self.assertIn("Cancel", ordinary_labels)
                self.assertEqual(
                    [option.value for option in ordinary.member_action.options],
                    ["none", "kick", "ban"],
                )

                memes = view_type(
                    controller,
                    message,
                    moderator_id=10,
                    allow_mement=True,
                )
                meme_labels = {child.label for child in memes.children if hasattr(child, "label")}
                self.assertIn("Memen't: Off", meme_labels)

    def test_punishment_modal_contains_only_fields_for_selected_actions(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                selection = module.EvidenceSelection(mement=True, mute=True)
                modal = module.PunishmentDetailsModal(
                    SimpleNamespace(),
                    SimpleNamespace(),
                    SimpleNamespace(),
                    selection,
                )

                self.assertEqual(
                    [child.label for child in modal.children],
                    ["Memen't reason", "Mute duration", "Mute reason"],
                )

                kick_modal = module.PunishmentDetailsModal(
                    SimpleNamespace(),
                    SimpleNamespace(),
                    SimpleNamespace(),
                    module.EvidenceSelection(member_action=module.MemberAction.KICK),
                )
                self.assertEqual(
                    [child.label for child in kick_modal.children],
                    ["Kick reason"],
                )

    def test_initial_evidence_records_selected_actions_as_pending(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                source = SimpleNamespace(
                    id=50,
                    channel=SimpleNamespace(id=123, mention="#memes"),
                    author=SimpleNamespace(id=20, display_name="target"),
                    content="bad meme",
                    created_at=SimpleNamespace(timestamp=lambda: 1_700_000_000),
                )
                rendered = module._render_evidence(
                    source,
                    SimpleNamespace(id=10, display_name="moderator"),
                    module.EvidenceSelection(mement=True, mute=True),
                    source_deletion="Pending",
                )

                self.assertIn("Memen't: Pending", rendered)
                self.assertIn("Mute: Waiting for details", rendered)


class ManualEvidenceContextActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_memes_channel_opens_private_mement_view(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                logs_channel = SimpleNamespace(
                    id=900,
                    permissions_for=lambda role: SimpleNamespace(view_channel=False),
                )
                source_channel = SimpleNamespace(id=123, mention="#memes")
                role = SimpleNamespace(id=module.MEMENT_ROLE_ID)
                guild = SimpleNamespace(
                    id=1,
                    default_role=SimpleNamespace(id=0),
                    get_channel=lambda channel_id: (
                        logs_channel if channel_id == logs_channel.id else None
                    ),
                    get_role=lambda role_id: role if role_id == role.id else None,
                )
                config_values = {
                    "logs_channel": logs_channel.id,
                    "manual_evidence_memes_channel": source_channel.id,
                    "manual_evidence_mement_notification_channel": 901,
                }
                guild_config = SimpleNamespace(all=mock.AsyncMock(return_value=config_values))
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    config=SimpleNamespace(guild=lambda _guild: guild_config),
                    _missing_channel_permissions=mock.Mock(return_value=None),
                    _channel_is_private=mock.Mock(return_value=True),
                )
                response = SimpleNamespace(send_message=mock.AsyncMock())
                interaction = SimpleNamespace(
                    guild=guild,
                    permissions=SimpleNamespace(manage_messages=True),
                    user=SimpleNamespace(id=10),
                    response=response,
                )
                source = SimpleNamespace(
                    id=50,
                    guild=guild,
                    channel=source_channel,
                    author=SimpleNamespace(id=20, display_name="target"),
                    content="bad meme",
                )
                controller = module.ManualEvidenceController(cog)

                await controller.open(interaction, source)

                response.send_message.assert_awaited_once()
                sent = response.send_message.await_args.kwargs
                self.assertTrue(sent["ephemeral"])
                self.assertIn("bad meme", response.send_message.await_args.args[0])
                self.assertIsInstance(sent["view"], module.EvidenceActionView)
                self.assertIsNotNone(sent["view"].mement_button)

    async def test_public_status_does_not_disclose_evidence_configuration(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                cog = SimpleNamespace(
                    _group_overview_is_private=mock.Mock(return_value=False),
                    config=SimpleNamespace(
                        guild=mock.Mock(
                            side_effect=AssertionError("public status must not read configuration")
                        )
                    ),
                )
                ctx = SimpleNamespace(send=mock.AsyncMock())

                await module.show_status(cog, ctx)

                ctx.send.assert_awaited_once_with("Run this command in a private staff channel.")

    async def test_deleted_configured_channel_is_cleared_without_fallback(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")

                class ConfigValue:
                    def __init__(self, value):
                        self.value = value
                        self.clear = mock.AsyncMock()

                    async def __call__(self):
                        return self.value

                memes = ConfigValue(123)
                notifications = ConfigValue(456)
                guild_config = SimpleNamespace(
                    manual_evidence_memes_channel=memes,
                    manual_evidence_mement_notification_channel=notifications,
                )
                cog = SimpleNamespace(config=SimpleNamespace(guild=lambda _guild: guild_config))
                channel = SimpleNamespace(
                    id=123,
                    guild=SimpleNamespace(id=1),
                )

                await module.clear_deleted_channel(cog, channel)

                memes.clear.assert_awaited_once_with()
                notifications.clear.assert_not_awaited()


class ManualEvidencePreliminaryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_modal_response_does_not_start_preliminary_effects(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    _observe_background_task=mock.Mock(),
                )
                controller = module.ManualEvidenceController(cog)
                source = SimpleNamespace(id=50)
                view = module.EvidenceActionView(
                    controller,
                    source,
                    moderator_id=10,
                    allow_mement=False,
                )
                view.selection.toggle_mute()
                interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        send_modal=mock.AsyncMock(side_effect=module.discord.HTTPException())
                    )
                )

                with self.assertRaises(module.discord.HTTPException):
                    await controller.confirm(interaction, view)

                self.assertFalse(controller._tasks)

    async def test_selected_punishment_requires_a_current_guild_member(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                guild = SimpleNamespace(
                    id=1,
                    get_member=lambda _member_id: None,
                )
                source = SimpleNamespace(
                    id=50,
                    guild=guild,
                    author=SimpleNamespace(id=20, display_name="target"),
                    delete=mock.AsyncMock(),
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    config=SimpleNamespace(
                        guild=mock.Mock(
                            side_effect=AssertionError(
                                "configuration must not be read after resolution failure"
                            )
                        )
                    ),
                    _observe_background_task=mock.Mock(),
                )
                controller = module.ManualEvidenceController(cog)
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=10, display_name="moderator"),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                result = await controller._run_preliminary(
                    interaction,
                    source,
                    module.EvidenceSelection(mute=True),
                )

                self.assertIsNone(result)
                source.delete.assert_not_awaited()
                self.assertIn(
                    "no longer available",
                    interaction.followup.send.await_args.args[0],
                )

    async def test_confirm_opens_details_modal_without_waiting_for_evidence_upload(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                upload_started = asyncio.Event()
                finish_upload = asyncio.Event()
                evidence_message = SimpleNamespace(
                    jump_url="https://discord/evidence/700",
                    edit=mock.AsyncMock(),
                    delete=mock.AsyncMock(),
                )

                async def publish(*args, **kwargs):
                    upload_started.set()
                    await finish_upload.wait()
                    return evidence_message

                logs_channel = SimpleNamespace(
                    id=900,
                    send=mock.AsyncMock(side_effect=publish),
                )
                source = SimpleNamespace(
                    id=50,
                    guild=SimpleNamespace(
                        id=1,
                        get_channel=lambda _channel_id: logs_channel,
                        get_member=lambda _member_id: source.author,
                    ),
                    channel=SimpleNamespace(id=123, mention="#general"),
                    author=SimpleNamespace(id=20, display_name="target"),
                    content="offending content",
                    attachments=(),
                    created_at=SimpleNamespace(timestamp=lambda: 1_700_000_000),
                    delete=mock.AsyncMock(),
                )
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"logs_channel": 900})
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    config=SimpleNamespace(guild=lambda _guild: guild_config),
                    _observe_background_task=mock.Mock(),
                )
                controller = module.ManualEvidenceController(cog)
                view = module.EvidenceActionView(
                    controller,
                    source,
                    moderator_id=10,
                    allow_mement=False,
                )
                view.selection.toggle_mute()
                response = SimpleNamespace(send_modal=mock.AsyncMock())
                interaction = SimpleNamespace(
                    guild=source.guild,
                    user=SimpleNamespace(id=10, display_name="moderator"),
                    response=response,
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await controller.confirm(interaction, view)

                response.send_modal.assert_awaited_once()
                self.assertIsInstance(
                    response.send_modal.await_args.args[0],
                    module.PunishmentDetailsModal,
                )
                self.assertFalse(finish_upload.is_set())
                await upload_started.wait()
                finish_upload.set()
                await asyncio.gather(*controller._tasks)

    async def test_confirm_publishes_evidence_before_deleting_source(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                events = []
                evidence_message = SimpleNamespace(
                    id=700,
                    jump_url="https://discord/evidence/700",
                    edit=mock.AsyncMock(),
                    delete=mock.AsyncMock(),
                )

                async def publish(*args, **kwargs):
                    events.append("publish")
                    return evidence_message

                logs_channel = SimpleNamespace(id=900, send=mock.AsyncMock(side_effect=publish))
                source_channel = SimpleNamespace(id=123, mention="#general")
                guild = SimpleNamespace(
                    id=1,
                    get_channel=lambda channel_id: (
                        logs_channel if channel_id == logs_channel.id else None
                    ),
                )
                source = SimpleNamespace(
                    id=50,
                    guild=guild,
                    channel=source_channel,
                    author=SimpleNamespace(id=20, display_name="target"),
                    content="offending content",
                    attachments=(),
                    created_at=SimpleNamespace(timestamp=lambda: 1_700_000_000),
                    jump_url="https://discord/source/50",
                )

                async def delete_source():
                    events.append("delete")

                source.delete = mock.AsyncMock(side_effect=delete_source)
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(
                        return_value={
                            "logs_channel": logs_channel.id,
                            "manual_evidence_memes_channel": None,
                            "manual_evidence_mement_notification_channel": None,
                        }
                    )
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    config=SimpleNamespace(guild=lambda _guild: guild_config),
                    _observe_background_task=mock.Mock(),
                )
                controller = module.ManualEvidenceController(cog)
                view = module.EvidenceActionView(
                    controller,
                    source,
                    moderator_id=10,
                    allow_mement=False,
                )
                interaction = SimpleNamespace(
                    guild=guild,
                    user=SimpleNamespace(id=10, display_name="moderator"),
                    response=SimpleNamespace(defer=mock.AsyncMock()),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await controller.confirm(interaction, view)
                for _ in range(20):
                    if source.delete.await_count:
                        break
                    await asyncio.sleep(0)

                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                self.assertEqual(events[:2], ["publish", "delete"])
                self.assertIn(
                    "offending content",
                    logs_channel.send.await_args_list[0].kwargs["content"],
                )
                interaction.followup.send.assert_awaited()

    async def test_attachment_failure_leaves_source_and_cleans_partial_evidence(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                evidence_message = SimpleNamespace(
                    id=700,
                    jump_url="https://discord/evidence/700",
                    edit=mock.AsyncMock(),
                    delete=mock.AsyncMock(),
                )
                logs_channel = SimpleNamespace(
                    id=900,
                    send=mock.AsyncMock(return_value=evidence_message),
                )
                source_channel = SimpleNamespace(id=123, mention="#general")
                guild = SimpleNamespace(
                    id=1,
                    get_channel=lambda channel_id: (
                        logs_channel if channel_id == logs_channel.id else None
                    ),
                )
                attachment = SimpleNamespace(
                    to_file=mock.AsyncMock(side_effect=module.discord.HTTPException())
                )
                source = SimpleNamespace(
                    id=50,
                    guild=guild,
                    channel=source_channel,
                    author=SimpleNamespace(id=20, display_name="target"),
                    content="offending attachment",
                    attachments=(attachment,),
                    created_at=SimpleNamespace(timestamp=lambda: 1_700_000_000),
                    jump_url="https://discord/source/50",
                    delete=mock.AsyncMock(),
                )
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"logs_channel": logs_channel.id})
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    config=SimpleNamespace(guild=lambda _guild: guild_config),
                    _observe_background_task=mock.Mock(),
                )
                controller = module.ManualEvidenceController(cog)
                view = module.EvidenceActionView(
                    controller,
                    source,
                    moderator_id=10,
                    allow_mement=False,
                )
                interaction = SimpleNamespace(
                    guild=guild,
                    user=SimpleNamespace(id=10, display_name="moderator"),
                    response=SimpleNamespace(defer=mock.AsyncMock()),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await controller.confirm(interaction, view)
                for _ in range(20):
                    if interaction.followup.send.await_count:
                        break
                    await asyncio.sleep(0)

                attachment.to_file.assert_awaited_once_with(use_cached=True)
                source.delete.assert_not_awaited()
                evidence_message.delete.assert_awaited_once_with()
                self.assertIn(
                    "source message was not deleted",
                    interaction.followup.send.await_args.args[0],
                )

    async def test_source_deletion_failure_prevents_selected_punishments(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                evidence_message = SimpleNamespace(
                    jump_url="https://discord/evidence/700",
                    edit=mock.AsyncMock(),
                    delete=mock.AsyncMock(),
                )
                logs_channel = SimpleNamespace(
                    id=900,
                    send=mock.AsyncMock(return_value=evidence_message),
                )
                role = SimpleNamespace(id=module.MEMENT_ROLE_ID)
                author = SimpleNamespace(
                    id=20,
                    display_name="target",
                    add_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=1,
                    get_member=lambda _member_id: author,
                    get_channel=lambda _channel_id: logs_channel,
                    get_role=lambda _role_id: role,
                )
                source = SimpleNamespace(
                    id=50,
                    guild=guild,
                    channel=SimpleNamespace(id=123, mention="#memes"),
                    author=author,
                    content="bad meme",
                    attachments=(),
                    created_at=SimpleNamespace(timestamp=lambda: 1_700_000_000),
                    delete=mock.AsyncMock(side_effect=module.discord.HTTPException()),
                )
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"logs_channel": 900})
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    config=SimpleNamespace(guild=lambda _guild: guild_config),
                    _observe_background_task=mock.Mock(),
                )
                controller = module.ManualEvidenceController(cog)
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=10, display_name="moderator"),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                result = await controller._run_preliminary(
                    interaction,
                    source,
                    module.EvidenceSelection(mement=True),
                )

                self.assertIsNone(result)
                author.add_roles.assert_not_awaited()
                self.assertIn(
                    "could not be deleted",
                    interaction.followup.send.await_args.args[0],
                )

    async def test_long_message_content_is_preserved_as_a_text_attachment(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                published = [
                    SimpleNamespace(delete=mock.AsyncMock()),
                    SimpleNamespace(delete=mock.AsyncMock()),
                ]
                logs_channel = SimpleNamespace(send=mock.AsyncMock(side_effect=published))
                content = "x" * 4_000
                source = SimpleNamespace(
                    id=50,
                    channel=SimpleNamespace(id=123, mention="#general"),
                    author=SimpleNamespace(id=20, display_name="target"),
                    content=content,
                    attachments=(),
                    created_at=SimpleNamespace(timestamp=lambda: 1_700_000_000),
                )

                await module._publish_evidence(
                    logs_channel,
                    source,
                    SimpleNamespace(id=10, display_name="moderator"),
                    module.EvidenceSelection(),
                )

                primary_call, files_call = logs_channel.send.await_args_list
                self.assertLessEqual(len(primary_call.kwargs["content"]), 2_000)
                self.assertNotIn(content, primary_call.kwargs["content"])
                text_file = files_call.kwargs["files"][0]
                self.assertEqual(text_file.filename, "message.txt")
                self.assertEqual(text_file.fp.getvalue().decode("utf-8"), content)

    async def test_mement_role_is_applied_after_source_deletion(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                events = []
                evidence_message = SimpleNamespace(
                    jump_url="https://discord/evidence/700",
                    edit=mock.AsyncMock(),
                    delete=mock.AsyncMock(),
                )

                async def publish(*args, **kwargs):
                    events.append("publish")
                    return evidence_message

                async def delete_source():
                    events.append("delete")

                async def add_roles(*args, **kwargs):
                    events.append("mement")

                logs_channel = SimpleNamespace(
                    id=900,
                    send=mock.AsyncMock(side_effect=publish),
                )
                role = SimpleNamespace(id=module.MEMENT_ROLE_ID)
                guild = SimpleNamespace(
                    id=1,
                    get_channel=lambda channel_id: (
                        logs_channel if channel_id == logs_channel.id else None
                    ),
                    get_role=lambda role_id: role if role_id == role.id else None,
                    get_member=lambda _member_id: author,
                )
                author = SimpleNamespace(
                    id=20,
                    display_name="target",
                    add_roles=mock.AsyncMock(side_effect=add_roles),
                )
                source = SimpleNamespace(
                    id=50,
                    guild=guild,
                    channel=SimpleNamespace(id=123, mention="#memes"),
                    author=author,
                    content="bad meme",
                    attachments=(),
                    created_at=SimpleNamespace(timestamp=lambda: 1_700_000_000),
                    delete=mock.AsyncMock(side_effect=delete_source),
                )
                guild_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"logs_channel": 900})
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    config=SimpleNamespace(guild=lambda _guild: guild_config),
                    _observe_background_task=mock.Mock(),
                )
                controller = module.ManualEvidenceController(cog)
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=10, display_name="moderator"),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                result = await controller._run_preliminary(
                    interaction,
                    source,
                    module.EvidenceSelection(mement=True),
                )

                self.assertEqual(events, ["publish", "delete", "mement"])
                author.add_roles.assert_awaited_once()
                self.assertIsNotNone(result)


class ManualEvidencePunishmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_mute_duration_reopens_the_modal_without_losing_reason(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                preliminary = asyncio.get_running_loop().create_future()
                controller = module.ManualEvidenceController(
                    SimpleNamespace(bot=SimpleNamespace(tree=_CommandTree()))
                )
                modal = module.PunishmentDetailsModal(
                    controller,
                    preliminary,
                    SimpleNamespace(),
                    module.EvidenceSelection(mute=True),
                )
                modal.inputs["mute_duration"].value = "forever"
                modal.inputs["mute_reason"].value = "NSFW content"
                response = SimpleNamespace(send_modal=mock.AsyncMock())
                interaction = SimpleNamespace(response=response)

                await controller.submit_details(interaction, modal)

                response.send_modal.assert_awaited_once()
                retry = response.send_modal.await_args.args[0]
                self.assertEqual(retry.title, "Invalid mute duration. Try again")
                self.assertEqual(retry.inputs["mute_reason"].value, "NSFW content")
                self.assertIs(retry.preliminary_task, preliminary)

    async def test_mement_and_mute_are_applied_and_notified_independently(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                module.modlog.create_case = mock.AsyncMock()
                moderator = SimpleNamespace(id=10, display_name="moderator")
                target = SimpleNamespace(id=20, display_name="target")
                offense_channel = SimpleNamespace(
                    id=123,
                    mention="#memes",
                    send=mock.AsyncMock(),
                )
                notification_channel = SimpleNamespace(id=901, send=mock.AsyncMock())
                mute_role = SimpleNamespace(id=777)
                guild = SimpleNamespace(
                    id=1,
                    get_channel=lambda channel_id: (
                        notification_channel if channel_id == 901 else None
                    ),
                    get_role=lambda role_id: mute_role if role_id == mute_role.id else None,
                )
                source = SimpleNamespace(
                    id=50,
                    guild=guild,
                    channel=offense_channel,
                    author=target,
                    content="bad meme",
                    created_at=SimpleNamespace(timestamp=lambda: 1_700_000_000),
                )
                evidence_message = SimpleNamespace(
                    jump_url="https://discord/evidence/700",
                    edit=mock.AsyncMock(),
                )
                selection = module.EvidenceSelection(mement=True, mute=True)
                settings = module.GuildSettings.from_mapping(
                    {"manual_evidence_mement_notification_channel": 901}
                )
                result = module.PreliminaryResult(
                    source_message=source,
                    moderator=moderator,
                    selection=selection,
                    settings=settings,
                    evidence=module.PublishedEvidence(
                        primary=evidence_message,
                        parts=(),
                        content_external=False,
                    ),
                    outcomes=["Memen't: Applied"],
                )
                preliminary = asyncio.get_running_loop().create_future()
                preliminary.set_result(result)
                mutes = SimpleNamespace(
                    config=SimpleNamespace(
                        guild=lambda _guild: SimpleNamespace(
                            mute_role=mock.AsyncMock(return_value=mute_role.id)
                        )
                    ),
                    mute_user=mock.AsyncMock(
                        return_value=SimpleNamespace(success=True, reason=None)
                    ),
                )
                cog = SimpleNamespace(
                    bot=SimpleNamespace(
                        tree=_CommandTree(),
                        get_cog=lambda name: mutes if name == "Mutes" else None,
                    ),
                )
                controller = module.ManualEvidenceController(cog)
                modal = module.PunishmentDetailsModal(
                    controller,
                    preliminary,
                    source,
                    selection,
                )
                modal.inputs["mement_reason"].value = "inappropriate meme"
                modal.inputs["mute_duration"].value = "30m"
                modal.inputs["mute_reason"].value = "NSFW content"
                interaction = SimpleNamespace(
                    user=moderator,
                    response=SimpleNamespace(defer=mock.AsyncMock()),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await controller.submit_details(interaction, modal)

                notification_channel.send.assert_awaited_once()
                self.assertEqual(
                    notification_channel.send.await_args.args[0],
                    "<@20> received memen't from <@10>.\nReason: inappropriate meme",
                )
                self.assertEqual(
                    notification_channel.send.await_args.kwargs["allowed_mentions"].users,
                    [target, moderator],
                )
                mutes.mute_user.assert_awaited_once()
                mute_call = mutes.mute_user.await_args
                self.assertEqual(mute_call.args[:3], (guild, moderator, target))
                self.assertEqual(mute_call.kwargs["reason"], "NSFW content")
                offense_channel.send.assert_awaited_once()
                self.assertEqual(
                    offense_channel.send.await_args.args[0],
                    "<@20> was muted by <@10> for 30m.\nReason: NSFW content",
                )
                evidence_content = evidence_message.edit.await_args.kwargs["content"]
                self.assertIn("Memen't reason: inappropriate meme", evidence_content)
                self.assertIn("Mute reason: NSFW content", evidence_content)

    async def test_kick_uses_honeypot_action_executor_with_moderator_reason(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                module = import_module("Honeypot.manual_evidence")
                moderator = SimpleNamespace(id=10, display_name="moderator")
                target = SimpleNamespace(id=20, display_name="target")
                guild = SimpleNamespace(id=1)
                source = SimpleNamespace(
                    id=50,
                    guild=guild,
                    channel=SimpleNamespace(id=123, mention="#general"),
                    author=target,
                    content="offense",
                    created_at=SimpleNamespace(timestamp=lambda: 1_700_000_000),
                )
                selection = module.EvidenceSelection(member_action=module.MemberAction.KICK)
                evidence_message = SimpleNamespace(
                    jump_url="https://discord/evidence/700",
                    edit=mock.AsyncMock(),
                )
                result = module.PreliminaryResult(
                    source_message=source,
                    moderator=moderator,
                    selection=selection,
                    settings=module.GuildSettings.from_mapping({}),
                    evidence=module.PublishedEvidence(
                        primary=evidence_message,
                        parts=(),
                        content_external=False,
                    ),
                    outcomes=[],
                )
                preliminary = asyncio.get_running_loop().create_future()
                preliminary.set_result(result)
                cog = SimpleNamespace(
                    bot=SimpleNamespace(tree=_CommandTree()),
                    _execute_action=mock.AsyncMock(
                        return_value=SimpleNamespace(
                            label="The member has been kicked.",
                            failed_message=None,
                        )
                    ),
                )
                controller = module.ManualEvidenceController(cog)
                modal = module.PunishmentDetailsModal(
                    controller,
                    preliminary,
                    source,
                    selection,
                )
                modal.inputs["member_action_reason"].value = "repeated abuse"
                interaction = SimpleNamespace(
                    user=moderator,
                    response=SimpleNamespace(defer=mock.AsyncMock()),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await controller.submit_details(interaction, modal)

                cog._execute_action.assert_awaited_once_with(
                    guild,
                    target,
                    source.created_at,
                    result.settings,
                    reason="repeated abuse",
                    action="kick",
                    moderator=moderator,
                )
                self.assertIn(
                    "Kick reason: repeated abuse",
                    evidence_message.edit.await_args.kwargs["content"],
                )


if __name__ == "__main__":
    unittest.main()
