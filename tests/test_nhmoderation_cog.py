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
                "nhmod errors",
                "nhmod errors channel",
                "nhmod errors channel clear",
                "nhmod errors maintainer",
                "nhmod errors maintainer clear",
            },
        )

    async def test_banchart_reads_history_and_sends_shared_chart(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = mock.Mock()
            subject.history = SimpleNamespace(
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
            self.assertNotIn("render failed", ctx.send.await_args.args[0])

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
            sync = asyncio.create_task(work())
            await started.wait()
            subject._scheduler_task = scheduler
            subject._startup_task = startup
            subject._sync_tasks = {10: sync}

            await module.NHModeration.cog_unload(subject)

            self.assertTrue(finished.is_set())
            self.assertTrue(all(task.done() for task in (scheduler, startup, sync)))
            self.assertEqual(subject._sync_tasks, {})
