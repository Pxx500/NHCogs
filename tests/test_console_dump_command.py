"""The `consoledump` command surface: argument handling, the log dump
attachment and the moderator-visible errors it reports.
"""

import importlib
import logging
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules, _operational_support


@contextmanager
def _isolated_console_dump_modules(data_path: Path):
    with _isolated_honeypot_modules(data_path):
        yield importlib.import_module("NHCogs.consoledump.consoledump")


class ConsoleDumpCommandTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _context(
        console_dump,
        *,
        public=False,
        moderator=True,
        text_channel=True,
        bot_can_upload=True,
    ):
        class TextChannel:
            mention = "#private-logs"

            def permissions_for(self, target):
                if target is author:
                    return SimpleNamespace(manage_messages=moderator)
                if target is default_role:
                    return SimpleNamespace(view_channel=public)
                if target is bot_member:
                    return SimpleNamespace(
                        view_channel=bot_can_upload,
                        send_messages=bot_can_upload,
                        attach_files=bot_can_upload,
                    )
                raise AssertionError("unexpected permission target")

        class ThreadChannel:
            pass

        console_dump.discord.TextChannel = TextChannel
        console_dump.discord.Thread = ThreadChannel
        author = object()
        default_role = object()
        bot_member = object()
        channel = TextChannel() if text_channel else ThreadChannel()
        guild = SimpleNamespace(
            default_role=default_role,
            me=bot_member,
            filesize_limit=50 * 1024 * 1024,
        )
        return SimpleNamespace(
            author=author,
            channel=channel,
            guild=guild,
            send=mock.AsyncMock(),
        )

    async def test_no_arguments_show_complete_usage_in_one_response(self):
        with TemporaryDirectory() as directory:
            with _isolated_console_dump_modules(Path(directory)) as console_dump:
                cog = console_dump.ConsoleDump(_Bot(), _operational_support())
                cog._console_log_buffer = SimpleNamespace(snapshot=mock.Mock())
                ctx = self._context(console_dump)

                await cog.console_dump(ctx)

                ctx.send.assert_awaited_once_with(console_dump.CONSOLE_DUMP_USAGE)
                response = ctx.send.await_args.args[0]
                self.assertIn("consoledump <bot|honeypot> <hours 1-24>", response)
                self.assertIn("Scope:", response)
                self.assertIn("Hours:", response)
                self.assertIn("Level (optional):", response)
                self.assertIn("Examples:", response)
                cog._console_log_buffer.snapshot.assert_not_called()

    async def test_invalid_arguments_show_the_same_complete_usage(self):
        invalid_arguments = (
            ("other", "1", None),
            ("bot", "0", None),
            ("honeypot", "25", None),
            ("bot", "one", None),
            ("bot", "1", "verbose"),
        )
        with TemporaryDirectory() as directory:
            with _isolated_console_dump_modules(Path(directory)) as console_dump:
                cog = console_dump.ConsoleDump(_Bot(), _operational_support())
                cog._console_log_buffer = SimpleNamespace(snapshot=mock.Mock())
                for scope, hours, level in invalid_arguments:
                    with self.subTest(scope=scope, hours=hours, level=level):
                        ctx = self._context(console_dump)
                        await cog.console_dump(ctx, scope, hours, level)
                        ctx.send.assert_awaited_once_with(console_dump.CONSOLE_DUMP_USAGE)
                cog._console_log_buffer.snapshot.assert_not_called()

    async def test_command_rejects_unauthorized_public_and_non_text_destinations(self):
        cases = (
            ({"moderator": False}, "You need Manage Messages"),
            ({"public": True}, "visible to @everyone"),
            ({"text_channel": False}, "private text channel"),
        )
        with TemporaryDirectory() as directory:
            with _isolated_console_dump_modules(Path(directory)) as console_dump:
                cog = console_dump.ConsoleDump(_Bot(), _operational_support())
                cog._console_log_buffer = SimpleNamespace(snapshot=mock.Mock())
                for options, expected in cases:
                    with self.subTest(options=options):
                        ctx = self._context(console_dump, **options)
                        await cog.console_dump(ctx, "bot", "1")
                        self.assertIn(expected, ctx.send.await_args.args[0])
                cog._console_log_buffer.snapshot.assert_not_called()

    async def test_valid_command_sends_one_sanitized_file_using_guild_limit(self):
        with TemporaryDirectory() as directory:
            with _isolated_console_dump_modules(Path(directory)) as console_dump:
                cog = console_dump.ConsoleDump(_Bot(), _operational_support())
                created_at = datetime.now(timezone.utc)
                cog._console_log_buffer.emit(
                    logging.LogRecord(
                        "red.test",
                        logging.INFO,
                        "/srv/info.py",
                        1,
                        "excluded-info",
                        (),
                        None,
                    )
                )
                error_record = logging.LogRecord(
                    "red.test",
                    logging.ERROR,
                    "/srv/error.py",
                    2,
                    "included-error Authorization: Bearer secret-value",
                    (),
                    None,
                )
                error_record.created = created_at.timestamp()
                cog._console_log_buffer.emit(error_record)
                ctx = self._context(console_dump)
                no_mentions = object()

                class File:
                    def __init__(self, stream, *, filename):
                        self.stream = stream
                        self.filename = filename

                console_dump.discord.File = File
                console_dump.discord.AllowedMentions = SimpleNamespace(
                    none=lambda: no_mentions
                )

                await cog.console_dump(ctx, "BOT", "2", "ERROR")

                ctx.send.assert_awaited_once()
                sent = ctx.send.await_args.kwargs
                content = sent["file"].stream.getvalue().decode("utf-8")
                self.assertIn("included-error", content)
                self.assertIn("[REDACTED]", content)
                self.assertNotIn("secret-value", content)
                self.assertNotIn("excluded-info", content)
                self.assertTrue(sent["file"].filename.startswith("console-bot-2h-"))
                self.assertIs(sent["allowed_mentions"], no_mentions)

    async def test_cog_owns_root_log_handler_for_its_lifetime(self):
        with TemporaryDirectory() as directory:
            with _isolated_console_dump_modules(Path(directory)) as console_dump:
                cog = console_dump.ConsoleDump(_Bot(), _operational_support())
                root_logger = logging.getLogger()

                await cog.cog_load()
                await cog.cog_load()
                self.assertEqual(root_logger.handlers.count(cog._console_log_buffer), 1)

                await cog.cog_unload()
                self.assertNotIn(cog._console_log_buffer, root_logger.handlers)
