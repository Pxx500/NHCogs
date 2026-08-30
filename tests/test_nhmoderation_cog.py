import asyncio
import importlib
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules


@contextmanager
def loaded_nhmoderation():
    with TemporaryDirectory() as directory:
        with _isolated_honeypot_modules(Path(directory)):
            names = [
                name
                for name in sys.modules
                if name.startswith("NHCogs.nhmoderation")
                or name in {"NHCogs.operational_errors", "NHCogs.ranked_donut_chart"}
            ]
            previous = {name: sys.modules[name] for name in names}
            for name in names:
                sys.modules.pop(name, None)
            try:
                yield importlib.import_module("NHCogs.nhmoderation.nhmoderation")
            finally:
                for name in list(sys.modules):
                    if name.startswith("NHCogs.nhmoderation") or name in {
                        "NHCogs.operational_errors",
                        "NHCogs.ranked_donut_chart",
                    }:
                        sys.modules.pop(name, None)
                sys.modules.update(previous)


class NHModerationCogTests(unittest.IsolatedAsyncioTestCase):
    def test_registered_command_tree_uses_accepted_names(self):
        with loaded_nhmoderation() as module:
            names = {
                value.qualified_name
                for value in vars(module.NHModeration).values()
                if getattr(value, "kind", None) in {"command", "group"}
            }

        self.assertEqual(
            names,
            {
                "banchart",
                "nhmod",
                "nhmod status",
                "nhmod migrate",
                "nhmod migrate plan",
                "nhmod migrate run",
                "nhmod filter",
                "nhmod filter add",
                "nhmod filter remove",
                "nhmod filter list",
                "nhmod sync",
                "nhmod repair",
            },
        )

    async def test_maintenance_commands_inherit_manage_messages_permission(self):
        with loaded_nhmoderation() as module:

            def context(*, red_mod=False, manage_messages=False):
                return SimpleNamespace(
                    is_red_mod=red_mod,
                    is_red_admin=False,
                    author=SimpleNamespace(
                        guild_permissions=SimpleNamespace(
                            manage_messages=manage_messages,
                            administrator=False,
                        )
                    ),
                )

            red_moderator = context(red_mod=True)
            manage_messages = context(manage_messages=True)
            unauthorized = context()
            for command_name in (
                "nhmod_filter_add",
                "nhmod_filter_remove",
                "nhmod_filter_list",
                "nhmod_migrate_run",
                "nhmod_sync",
                "nhmod_repair",
            ):
                command = getattr(module.NHModeration, command_name)
                self.assertFalse(await command.can_run(red_moderator))
                self.assertTrue(await command.can_run(manage_messages))
                self.assertFalse(await command.can_run(unauthorized))

    async def test_bare_nhmod_groups_use_runtime_overviews(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = mock.Mock()
            subject._mark_operational_recovered = mock.AsyncMock()
            ctx = SimpleNamespace(guild=SimpleNamespace(id=10))

            with mock.patch.object(
                module, "send_group_overview", new=mock.AsyncMock()
            ) as overview:
                await module.NHModeration.nhmod.callback(subject, ctx)
                await module.NHModeration.nhmod_migrate.callback(subject, ctx)

            self.assertEqual(
                overview.await_args_list,
                [
                    mock.call(ctx, include_descendants=False),
                    mock.call(ctx),
                ],
            )
            self.assertEqual(
                subject._mark_operational_recovered.await_args_list,
                [
                    mock.call(ctx.guild, "nhmod"),
                    mock.call(ctx.guild, "nhmod migrate"),
                ],
            )
            self.assertEqual(
                subject._require_private_channel.call_args_list,
                [mock.call(ctx), mock.call(ctx)],
            )

    async def test_filter_commands_normalize_persist_list_and_remove_phrases(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace())
            subject._require_private_channel = mock.Mock()
            subject._mark_operational_recovered = mock.AsyncMock()
            guild = SimpleNamespace(id=10)
            ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())

            with mock.patch.object(
                module,
                "overview_embeds",
                return_value=[object()],
            ):
                await module.NHModeration.nhmod_filter_add.callback(
                    subject,
                    ctx,
                    phrase="  Mixed CASE Phrase  ",
                )

            stored = await subject.config.guild(guild).get_raw("message_filter_phrases")
            self.assertEqual(stored, ["mixed case phrase"])
            self.assertEqual(subject._message_filter_phrases[10], ("mixed case phrase",))
            self.assertFalse(ctx.send.await_args.kwargs["allowed_mentions"].everyone)

            with self.assertRaisesRegex(Exception, "already configured"):
                await module.NHModeration.nhmod_filter_add.callback(
                    subject,
                    ctx,
                    phrase="MIXED CASE PHRASE",
                )

            ctx.send.reset_mock()
            embed = object()
            with mock.patch.object(
                module,
                "overview_embeds",
                return_value=[embed],
            ) as renderer:
                await module.NHModeration.nhmod_filter_list.callback(subject, ctx)
            self.assertIn(
                "mixed case phrase",
                renderer.call_args.args[2][0][1],
            )
            self.assertEqual(ctx.send.await_args.args, ())
            self.assertIs(ctx.send.await_args.kwargs["embed"], embed)
            self.assertFalse(ctx.send.await_args.kwargs["allowed_mentions"].everyone)

            with mock.patch.object(
                module,
                "overview_embeds",
                return_value=[object()],
            ):
                await module.NHModeration.nhmod_filter_remove.callback(
                    subject,
                    ctx,
                    phrase="MIXED CASE PHRASE",
                )

            stored = await subject.config.guild(guild).get_raw("message_filter_phrases")
            self.assertEqual(stored, [])
            self.assertEqual(subject._message_filter_phrases[10], ())

            with self.assertRaisesRegex(Exception, "cannot be empty"):
                await module.NHModeration.nhmod_filter_add.callback(
                    subject,
                    ctx,
                    phrase="   ",
                )

    async def test_bare_filter_group_shows_overview_and_current_configuration(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace())
            subject._message_filter_phrases = {10: ("blocked phrase",)}
            subject._require_private_channel = mock.Mock()
            subject._mark_operational_recovered = mock.AsyncMock()
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10),
                send=mock.AsyncMock(),
            )

            async def render_overview(_ctx, config_sender):
                await config_sender()

            embed = object()
            with (
                mock.patch.object(
                    module,
                    "send_group_overview",
                    new=mock.AsyncMock(side_effect=render_overview),
                ) as overview,
                mock.patch.object(
                    module,
                    "overview_embeds",
                    return_value=[embed],
                ) as renderer,
            ):
                await module.NHModeration.nhmod_filter.callback(subject, ctx)

            overview.assert_awaited_once()
            self.assertIn("blocked phrase", renderer.call_args.args[2][0][1])
            self.assertEqual(ctx.send.await_args.args, ())
            self.assertIs(ctx.send.await_args.kwargs["embed"], embed)
            self.assertFalse(ctx.send.await_args.kwargs["allowed_mentions"].everyone)
            subject._require_private_channel.assert_called_once_with(ctx)

    async def test_message_filter_deletes_case_insensitive_substring_matches(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace())
            subject._message_filter_phrases = {10: ("blocked phrase",)}
            subject.report_operational_error = mock.AsyncMock()
            subject._mark_operational_recovered = mock.AsyncMock()
            message = SimpleNamespace(
                id=30,
                guild=SimpleNamespace(id=10),
                channel=SimpleNamespace(id=20),
                content="prefixBLOCKED PHRASEsuffix",
                embeds=(),
                author=SimpleNamespace(bot=True),
                webhook_id=40,
                delete=mock.AsyncMock(),
            )

            await module.NHModeration.on_message(subject, message)

            message.delete.assert_awaited_once_with()
            subject.report_operational_error.assert_not_awaited()
            subject._mark_operational_recovered.assert_awaited_once_with(
                message.guild,
                "delete filtered message",
            )

    async def test_message_filter_checks_every_supported_embed_text_part(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace(user=SimpleNamespace(id=50)))
            subject._message_filter_phrases = {10: ("blocked",)}
            subject.report_operational_error = mock.AsyncMock()
            subject._mark_operational_recovered = mock.AsyncMock()
            empty_embed = {
                "title": None,
                "description": None,
                "fields": (),
                "author": SimpleNamespace(name=None),
                "footer": SimpleNamespace(text=None),
            }
            cases = {
                "title": {"title": "prefix BLOCKED suffix"},
                "description": {"description": "prefix BLOCKED suffix"},
                "field name": {
                    "fields": (SimpleNamespace(name="BLOCKED", value="allowed"),)
                },
                "field value": {
                    "fields": (SimpleNamespace(name="allowed", value="BLOCKED"),)
                },
                "author name": {"author": SimpleNamespace(name="BLOCKED")},
                "footer text": {"footer": SimpleNamespace(text="BLOCKED")},
            }

            for label, overrides in cases.items():
                with self.subTest(part=label):
                    embed = SimpleNamespace(**(empty_embed | overrides))
                    message = SimpleNamespace(
                        id=30,
                        guild=SimpleNamespace(id=10),
                        channel=SimpleNamespace(id=20),
                        content="",
                        embeds=(embed,),
                        author=SimpleNamespace(id=60, bot=True),
                        webhook_id=40,
                        delete=mock.AsyncMock(),
                    )

                    await module.NHModeration.on_message(subject, message)

                    message.delete.assert_awaited_once_with()

    async def test_message_filter_checks_embeds_added_by_message_edit(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace())
            subject._message_filter_phrases = {10: ("blocked",)}
            subject.report_operational_error = mock.AsyncMock()
            subject._mark_operational_recovered = mock.AsyncMock()
            before = SimpleNamespace(content="allowed", embeds=())
            after = SimpleNamespace(
                id=30,
                guild=SimpleNamespace(id=10),
                channel=SimpleNamespace(id=20),
                content="allowed",
                embeds=(
                    SimpleNamespace(
                        title=None,
                        description="BLOCKED",
                        fields=(),
                        author=SimpleNamespace(name=None),
                        footer=SimpleNamespace(text=None),
                    ),
                ),
                delete=mock.AsyncMock(),
            )

            await module.NHModeration.on_message_edit(subject, before, after)

            after.delete.assert_awaited_once_with()

    async def test_message_filter_preserves_its_own_configuration_embed(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace(user=SimpleNamespace(id=50)))
            subject._message_filter_phrases = {10: ("blocked",)}
            subject.report_operational_error = mock.AsyncMock()
            subject._mark_operational_recovered = mock.AsyncMock()
            message = SimpleNamespace(
                id=30,
                guild=SimpleNamespace(id=10),
                channel=SimpleNamespace(id=20),
                content="",
                author=SimpleNamespace(id=50),
                embeds=(
                    SimpleNamespace(
                        title="Message filter",
                        description="1. blocked",
                        fields=(),
                        author=SimpleNamespace(name=None),
                        footer=SimpleNamespace(text=None),
                    ),
                ),
                delete=mock.AsyncMock(),
            )

            await module.NHModeration.on_message(subject, message)

            message.delete.assert_not_awaited()

    async def test_filter_command_confirmation_is_preserved_by_message_filter(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace(user=SimpleNamespace(id=50)))
            subject._require_private_channel = mock.Mock()
            subject._mark_operational_recovered = mock.AsyncMock()
            guild = SimpleNamespace(id=10)
            ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())
            def render_confirmation(title, description, fields):
                return [
                    SimpleNamespace(
                        title=title,
                        description=description,
                        fields=fields,
                        author=SimpleNamespace(name=None),
                        footer=SimpleNamespace(text=None),
                    )
                ]

            with mock.patch.object(
                module,
                "overview_embeds",
                side_effect=render_confirmation,
            ):
                await module.NHModeration.nhmod_filter_add.callback(
                    subject,
                    ctx,
                    phrase="blocked",
                )

            confirmation_embed = ctx.send.await_args.kwargs["embed"]
            sent_message = SimpleNamespace(
                id=30,
                guild=guild,
                channel=SimpleNamespace(id=20),
                content="",
                author=SimpleNamespace(id=50),
                embeds=(confirmation_embed,),
                delete=mock.AsyncMock(),
            )
            await module.NHModeration.on_message(subject, sent_message)

            sent_message.delete.assert_not_awaited()

    async def test_message_filter_reports_discord_delete_failures(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace())
            subject._message_filter_phrases = {10: ("blocked",)}
            subject.report_operational_error = mock.AsyncMock()
            subject._mark_operational_recovered = mock.AsyncMock()
            error = module.discord.Forbidden()
            message = SimpleNamespace(
                id=30,
                guild=SimpleNamespace(id=10),
                channel=SimpleNamespace(id=20),
                content="blocked",
                embeds=(),
                delete=mock.AsyncMock(side_effect=error),
            )

            await module.NHModeration.on_message(subject, message)

            subject.report_operational_error.assert_awaited_once_with(
                guild_id=10,
                action="delete filtered message",
                error=error,
                channel_id=20,
                message_id=30,
            )

    async def test_message_filter_ignores_dms_nonmatches_and_already_gone_messages(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace())
            subject._message_filter_phrases = {10: ("blocked",)}
            subject.report_operational_error = mock.AsyncMock()
            subject._mark_operational_recovered = mock.AsyncMock()
            dm = SimpleNamespace(
                guild=None,
                content="blocked",
                delete=mock.AsyncMock(),
            )
            nonmatch = SimpleNamespace(
                guild=SimpleNamespace(id=10),
                content="allowed",
                embeds=(
                    SimpleNamespace(
                        title="allowed",
                        description="still allowed",
                        fields=(
                            SimpleNamespace(name="allowed", value="also allowed"),
                        ),
                        author=SimpleNamespace(name="allowed"),
                        footer=SimpleNamespace(text="allowed"),
                    ),
                ),
                delete=mock.AsyncMock(),
            )
            already_gone = SimpleNamespace(
                id=30,
                guild=SimpleNamespace(id=10),
                channel=SimpleNamespace(id=20),
                content="blocked",
                embeds=(),
                delete=mock.AsyncMock(side_effect=module.discord.NotFound()),
            )

            await module.NHModeration.on_message(subject, dm)
            await module.NHModeration.on_message(subject, nonmatch)
            await module.NHModeration.on_message(subject, already_gone)

            dm.delete.assert_not_awaited()
            nonmatch.delete.assert_not_awaited()
            already_gone.delete.assert_awaited_once_with()
            subject.report_operational_error.assert_not_awaited()
            subject._mark_operational_recovered.assert_awaited_once_with(
                already_gone.guild,
                "delete filtered message",
            )

    async def test_message_filter_cache_is_restored_during_cog_load(self):
        with loaded_nhmoderation() as module:
            subject = module.NHModeration(SimpleNamespace())
            await subject.config.guild_from_id(10).set_raw(
                "message_filter_phrases",
                value=["first phrase", "second phrase"],
            )
            subject.history.initialize = mock.AsyncMock()
            subject._operational_errors.initialize = mock.AsyncMock()
            subject._weekly_scheduler = mock.AsyncMock()
            subject._startup_catchup = mock.AsyncMock()

            await module.NHModeration.cog_load(subject)
            try:
                self.assertEqual(
                    subject._message_filter_phrases[10],
                    ("first phrase", "second phrase"),
                )
            finally:
                await module.NHModeration.cog_unload(subject)

    async def test_banchart_reads_history_and_sends_shared_chart(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._mark_operational_recovered = mock.AsyncMock()
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(migration_state="complete")
                ),
                get_ban_chart=mock.AsyncMock(
                    return_value=SimpleNamespace(
                        rows=(SimpleNamespace(label=None, moderator_user_id=55, count=3),),
                        other_count=0,
                        total_count=3,
                    )
                )
            )
            guild = SimpleNamespace(
                id=10,
                name="Test guild",
                get_member=lambda user_id: SimpleNamespace(display_name="Moderator"),
            )
            ctx = SimpleNamespace(guild=guild, channel=object(), send=mock.AsyncMock())
            chart = object()

            with mock.patch.object(
                module, "render_ranked_donut_chart", return_value=chart
            ) as renderer:
                await module.NHModeration.banchart.callback(
                    subject, ctx, arguments="30 10"
                )

            query = subject.history.get_ban_chart.await_args.args[0]
            self.assertEqual(query.guild_id, 10)
            self.assertEqual(query.amount, 10)
            self.assertFalse(query.include_automation)
            renderer.assert_called_once()
            ctx.send.assert_awaited_once()
            self.assertIs(ctx.send.await_args.kwargs["file"], chart)
            subject._mark_operational_recovered.assert_awaited_once_with(
                guild,
                "banchart",
            )

    async def test_banchart_rejects_incomplete_migration_without_reading_chart(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._mark_operational_recovered = mock.AsyncMock()
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(migration_state="running")
                ),
                get_ban_chart=mock.AsyncMock(
                    return_value=SimpleNamespace(
                        rows=(),
                        other_count=0,
                        total_count=0,
                    )
                ),
            )
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10, name="Test guild"),
                channel=object(),
                clean_prefix="!",
                send=mock.AsyncMock(),
            )

            await module.NHModeration.banchart.callback(subject, ctx)

            subject.history.get_ban_chart.assert_not_awaited()
            ctx.send.assert_awaited_once()
            self.assertEqual(
                ctx.send.await_args.args[0],
                "Initial migration is currently running. Try banchart again after it completes.",
            )
            self.assertIn("allowed_mentions", ctx.send.await_args.kwargs)
            subject._mark_operational_recovered.assert_not_awaited()

    async def test_banchart_tells_moderator_to_start_pending_migration(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._mark_operational_recovered = mock.AsyncMock()
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(migration_state="pending")
                ),
                get_ban_chart=mock.AsyncMock(),
            )
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10, name="Test guild"),
                channel=object(),
                clean_prefix="!",
                send=mock.AsyncMock(),
            )

            await module.NHModeration.banchart.callback(subject, ctx)

            subject.history.get_ban_chart.assert_not_awaited()
            self.assertEqual(
                ctx.send.await_args.args[0],
                "Run `!nhmod migrate run` before using banchart.",
            )

    async def test_migrate_plan_reports_cached_source_and_command_readiness(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = mock.Mock()
            subject._mark_operational_recovered = mock.AsyncMock()
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(migration_state="running")
                )
            )
            subject.bot = SimpleNamespace(
                get_command=mock.Mock(
                    return_value=SimpleNamespace(cog=SimpleNamespace(qualified_name="OtherCog"))
                )
            )
            guild = SimpleNamespace(
                id=10,
                me=SimpleNamespace(
                    guild_permissions=SimpleNamespace(
                        view_audit_log=False,
                        ban_members=True,
                    )
                ),
            )
            ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())

            with mock.patch.object(module.modlog, "get_all_cases", None, create=True):
                await module.NHModeration.nhmod_migrate_plan.callback(subject, ctx)

            self.assertEqual(
                ctx.send.await_args.args[0],
                "\n".join(
                    (
                        "Migration: running",
                        "Database: ready",
                        "Discord audit history: missing View Audit Log",
                        "Active ban snapshot: ready",
                        "Red ModLog: unavailable",
                        "BanChart command: conflict with OtherCog",
                    )
                ),
            )
            subject._mark_operational_recovered.assert_awaited_once_with(
                guild,
                "nhmod migrate plan",
            )

    async def test_migrate_run_reports_when_initial_import_is_already_complete(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = mock.Mock()
            subject._mark_operational_recovered = mock.AsyncMock()
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(migration_state="complete")
                )
            )
            subject._run_sync = mock.AsyncMock()
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10),
                send=mock.AsyncMock(),
            )

            await module.NHModeration.nhmod_migrate_run.callback(subject, ctx)

            subject._run_sync.assert_not_awaited()
            self.assertEqual(
                ctx.send.await_args.args[0],
                "Initial migration is already complete.",
            )
            subject._mark_operational_recovered.assert_awaited_once_with(
                ctx.guild,
                "nhmod migrate run",
            )

    async def test_migrate_run_acknowledges_start_before_waiting_for_import(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = mock.Mock()
            subject._mark_operational_recovered = mock.AsyncMock()
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(migration_state="pending")
                )
            )
            import_started = asyncio.Event()
            finish_import = asyncio.Event()

            async def run_sync(*_args):
                import_started.set()
                await finish_import.wait()
                return SimpleNamespace(inserted_observations=4)

            subject._run_sync = mock.AsyncMock(side_effect=run_sync)
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10),
                send=mock.AsyncMock(),
            )

            task = asyncio.create_task(
                module.NHModeration.nhmod_migrate_run.callback(subject, ctx)
            )
            try:
                await asyncio.wait_for(import_started.wait(), timeout=1)
                self.assertEqual(ctx.send.await_count, 1)
                self.assertEqual(
                    ctx.send.await_args.args[0],
                    "Migration started. I will post the result here when it completes.",
                )
            finally:
                finish_import.set()
                await task

            self.assertEqual(ctx.send.await_count, 2)
            self.assertEqual(
                ctx.send.await_args_list[1].args[0],
                "Migration complete. Imported 4 new observations.",
            )

    async def test_status_reports_possible_historical_gap(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = mock.Mock()
            subject._sync_tasks = {}
            subject._mark_operational_recovered = mock.AsyncMock()
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(
                        migration_state="complete",
                        last_sync_at=None,
                        last_reconciliation_at=None,
                        historical_gap=True,
                    )
                )
            )
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10),
                send=mock.AsyncMock(),
            )

            class Embed:
                def __init__(self, **_kwargs):
                    self.fields = []

                def add_field(self, *, name, value, inline):
                    self.fields.append(
                        SimpleNamespace(name=name, value=value, inline=inline)
                    )

            with mock.patch.object(module.discord, "Embed", Embed):
                await module.NHModeration.nhmod_status.callback(subject, ctx)

            embed = ctx.send.await_args.kwargs["embed"]
            fields = {field.name: field.value for field in embed.fields}
            self.assertEqual(fields["Historical coverage gap"], "possible")
            subject._mark_operational_recovered.assert_awaited_once_with(
                ctx.guild,
                "nhmod status",
            )

    async def test_successful_migration_sync_and_repair_recover_command_failures(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = mock.Mock()
            subject._mark_operational_recovered = mock.AsyncMock()
            subject._run_sync = mock.AsyncMock(
                return_value=SimpleNamespace(inserted_observations=2)
            )
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(migration_state="pending")
                )
            )
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10),
                clean_prefix="!",
                send=mock.AsyncMock(),
            )

            await module.NHModeration.nhmod_migrate_run.callback(subject, ctx)
            await module.NHModeration.nhmod_sync.callback(subject, ctx)
            await module.NHModeration.nhmod_repair.callback(
                subject,
                ctx,
                confirmation="confirm",
            )

            self.assertEqual(
                subject._mark_operational_recovered.await_args_list,
                [
                    mock.call(ctx.guild, "nhmod migrate run"),
                    mock.call(ctx.guild, "nhmod sync"),
                    mock.call(ctx.guild, "nhmod repair"),
                ],
            )

    async def test_unexpected_command_error_is_reported_and_acknowledged(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject.report_operational_error = mock.AsyncMock()
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10),
                channel=SimpleNamespace(id=20),
                message=SimpleNamespace(id=30),
                command=SimpleNamespace(qualified_name="banchart"),
                send=mock.AsyncMock(),
            )
            error = RuntimeError("render failed")

            await module.NHModeration.cog_command_error(subject, ctx, error)

            subject.report_operational_error.assert_awaited_once()
            ctx.send.assert_awaited_once()
            self.assertEqual(
                ctx.send.await_args.args[0],
                "Something went wrong while running this command. The error was logged.",
            )

    async def test_permission_check_failure_returns_user_feedback(self):
        class CheckFailure(Exception):
            pass

        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject.report_operational_error = mock.AsyncMock()
            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10),
                channel=SimpleNamespace(id=20),
                message=SimpleNamespace(id=30),
                command=SimpleNamespace(qualified_name="nhmod migrate run"),
                send=mock.AsyncMock(),
            )

            with mock.patch.object(
                module.commands,
                "CheckFailure",
                CheckFailure,
                create=True,
            ):
                await module.NHModeration.cog_command_error(
                    subject,
                    ctx,
                    CheckFailure(),
                )

            subject.report_operational_error.assert_not_awaited()
            self.assertEqual(
                ctx.send.await_args.args[0],
                "You do not have permission to use this command.",
            )

    async def test_expected_command_error_is_delegated_to_red(self):
        class CheckFailure(Exception):
            pass

        class UserFeedbackCheckFailure(CheckFailure):
            pass

        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject.report_operational_error = mock.AsyncMock()
            bot = SimpleNamespace(on_command_error=mock.AsyncMock())
            ctx = SimpleNamespace(
                bot=bot,
                guild=SimpleNamespace(id=10),
                channel=SimpleNamespace(id=20),
                message=SimpleNamespace(id=30),
                command=SimpleNamespace(qualified_name="nhmod repair"),
                send=mock.AsyncMock(),
            )
            error = UserFeedbackCheckFailure("Run this command in a private channel")

            with (
                mock.patch.object(
                    module.commands,
                    "CheckFailure",
                    CheckFailure,
                    create=True,
                ),
                mock.patch.object(
                    module.commands,
                    "UserFeedbackCheckFailure",
                    UserFeedbackCheckFailure,
                ),
            ):
                await module.NHModeration.cog_command_error(subject, ctx, error)

            bot.on_command_error.assert_awaited_once_with(
                ctx,
                error,
                unhandled_by_cog=True,
            )
            subject.report_operational_error.assert_not_awaited()

    async def test_operational_error_is_written_to_python_logger(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._operational_errors = SimpleNamespace(
                report=mock.AsyncMock(return_value=object())
            )
            error = RuntimeError("sync failed")

            with mock.patch.object(module.log, "error") as logger:
                result = await module.NHModeration.report_operational_error(
                    subject,
                    guild_id=10,
                    action="weekly reconciliation",
                    error=error,
                )

            self.assertIsNotNone(result)
            logger.assert_called_once()
            self.assertEqual(logger.call_args.kwargs["exc_info"][1], error)

    async def test_successful_startup_sync_marks_prior_failures_recovered(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            guild = SimpleNamespace(id=10)
            subject.bot = SimpleNamespace(
                wait_until_red_ready=mock.AsyncMock(),
                guilds=(guild,),
            )
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(migration_state="complete")
                )
            )
            subject._run_sync = mock.AsyncMock()
            subject._operational_errors = SimpleNamespace(
                mark_action_recovered=mock.AsyncMock(return_value=1)
            )

            await module.NHModeration._startup_catchup(subject)

            subject._operational_errors.mark_action_recovered.assert_awaited_once_with(
                guild_id=10,
                source="NHModeration",
                action="startup sync",
            )

    async def test_ready_event_runs_debounced_incremental_catchup(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            guild = SimpleNamespace(id=10)
            subject.bot = SimpleNamespace(guilds=(guild,))
            subject.history = SimpleNamespace(
                status=mock.AsyncMock(
                    return_value=SimpleNamespace(migration_state="complete")
                )
            )
            subject._run_sync = mock.AsyncMock()
            subject._operational_errors = SimpleNamespace(
                mark_action_recovered=mock.AsyncMock(return_value=0)
            )
            subject._gateway_catchup_task = None

            with mock.patch.object(module.asyncio, "sleep", new=mock.AsyncMock()):
                await module.NHModeration.on_ready(subject)
                await subject._gateway_catchup_task

            subject._run_sync.assert_awaited_once_with(
                guild,
                module.SyncMode.INCREMENTAL,
            )
            subject._operational_errors.mark_action_recovered.assert_awaited_once_with(
                guild_id=10,
                source="NHModeration",
                action="gateway catch-up",
            )

    async def test_cog_unload_cancels_and_awaits_every_owned_task(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            started = asyncio.Event()
            finished = asyncio.Event()

            async def work():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finished.set()

            scheduler = asyncio.create_task(work())
            startup = asyncio.create_task(work())
            gateway_catchup = asyncio.create_task(work())
            sync = asyncio.create_task(work())
            await started.wait()
            subject._scheduler_task = scheduler
            subject._startup_task = startup
            subject._gateway_catchup_task = gateway_catchup
            subject._sync_tasks = {10: sync}

            await module.NHModeration.cog_unload(subject)

            self.assertTrue(finished.is_set())
            self.assertTrue(
                all(
                    task.done()
                    for task in (scheduler, startup, gateway_catchup, sync)
                )
            )
            self.assertEqual(subject._sync_tasks, {})
            self.assertIsNone(subject._scheduler_task)
            self.assertIsNone(subject._startup_task)
            self.assertIsNone(subject._gateway_catchup_task)
