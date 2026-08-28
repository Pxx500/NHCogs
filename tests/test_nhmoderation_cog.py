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
                "nhmod sync",
                "nhmod repair",
            },
        )

    async def test_bare_nhmod_groups_use_runtime_overviews(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
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

    async def test_banchart_reads_history_and_sends_shared_chart(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = mock.Mock()
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
            subject._require_private_channel = mock.Mock()
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
                "Run `!nhmod migrate run` before using banchart.",
            )
            self.assertIn("allowed_mentions", ctx.send.await_args.kwargs)
            subject._mark_operational_recovered.assert_not_awaited()

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
