"""Behavioral tests for the channel-scoped GIF detector."""

import asyncio
import unittest
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


class GifDetectorClassificationTests(unittest.TestCase):
    def test_only_discord_gifv_embeds_are_detected(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")

                self.assertTrue(
                    gif_detector.has_gifv_embed(
                        [SimpleNamespace(type="rich"), SimpleNamespace(type="gifv")]
                    )
                )
                self.assertFalse(
                    gif_detector.has_gifv_embed(
                        [SimpleNamespace(type="image"), SimpleNamespace(type="video")]
                    )
                )

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
    def test_animation_removes_track_above_the_rocket_without_leaving_a_trail(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                gif_detector = import_module("Honeypot.gif_detector")

                header = "ICBM detected targeting @User's GIF!"
                self.assertEqual(
                    gif_detector.render_icbm_frame("@User", track_lines=10),
                    "\n".join((header, *("│" for _ in range(10)), "🚀")),
                )
                self.assertEqual(
                    gif_detector.render_icbm_frame("@User", track_lines=1),
                    f"{header}\n│\n🚀",
                )


class GifDetectorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_static_delivery_deletes_once_before_warning(self):
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
                        new=mock.AsyncMock(side_effect=lambda _: events.append("sleep")),
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
                    ["source-delete", "warning-send", "sleep", "warning-delete"],
                )
                channel.send.assert_awaited_once()
                self.assertEqual(channel.send.await_args.args[0], "@User No gifs!")
                allowed = channel.send.await_args.kwargs["allowed_mentions"]
                self.assertEqual(allowed.users, [author])
                self.assertFalse(allowed.everyone)
                self.assertFalse(allowed.roles)

    async def test_first_gif_uses_nine_edits_then_deletes_both_messages(self):
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
                events = []
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
                        new=mock.AsyncMock(side_effect=lambda _: events.append("sleep")),
                    ),
                    mock.patch.object(
                        honeypot.detection,
                        "on_message",
                        new=mock.AsyncMock(),
                    ),
                ):
                    await cog.on_message(message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                self.assertEqual(channel.send.await_args.args[0],
                    gif_detector.render_icbm_frame("@User", track_lines=10))
                self.assertEqual(warning.edit.await_count, 9)
                self.assertEqual(
                    [call.kwargs["content"] for call in warning.edit.await_args_list],
                    [
                        gif_detector.render_icbm_frame("@User", track_lines=lines)
                        for lines in range(9, 0, -1)
                    ],
                )
                self.assertEqual(events.count("sleep"), 10)
                self.assertEqual(events[-2:], ["source-delete", "warning-delete"])
                self.assertEqual(cog._gif_detector_animated_guilds, set())


class GifDetectorCommandTests(unittest.IsolatedAsyncioTestCase):
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
    async def test_late_raw_gifv_embed_uses_cached_message_without_fetching(self):
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
                    cached_message=message,
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

                async def controlled_sleep(seconds):
                    if seconds == 1 and not gate.is_set():
                        await gate.wait()

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
                self.assertEqual(animated_warning.edit.await_count, 9)
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
                        new=mock.AsyncMock(
                            side_effect=sleep_seconds.append
                        ),
                    ),
                    mock.patch.object(
                        honeypot.detection,
                        "on_message",
                        new=mock.AsyncMock(),
                    ),
                ):
                    await cog.on_message(message)
                    await asyncio.gather(*tuple(cog._gif_detector_tasks))

                self.assertEqual(sum(sleep_seconds), 10)
                message.delete.assert_awaited_once()
                self.assertEqual(cog._gif_detector_animated_guilds, set())


if __name__ == "__main__":
    unittest.main()
