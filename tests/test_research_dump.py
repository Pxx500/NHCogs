import asyncio
import importlib.util
import json
import random
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
from zipfile import ZipFile

MODULE_PATH = Path(__file__).parents[1] / "NHCogs" / "honeypot" / "research_dump.py"
SPEC = importlib.util.spec_from_file_location("honeypot_research_dump_test", MODULE_PATH)
research_dump = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = research_dump
SPEC.loader.exec_module(research_dump)

BOT = 100000000000000001
USER = 100000000000000002
START = datetime(2026, 9, 1, tzinfo=timezone.utc)


class LogChannel:
    def __init__(self, channel_id, messages):
        self.id = channel_id
        self.name = f"logs-{channel_id}"
        self.messages = messages
        self.history_calls = []
        self.guild = SimpleNamespace(roles=[SimpleNamespace(id=77, name="French")])

    def history(self, **kwargs):
        self.history_calls.append(kwargs)

        async def iterator():
            for item in sorted(self.messages, key=lambda item: item.created_at):
                if item.created_at < kwargs["before"]:
                    yield item

        return iterator()


def message(number, minutes, embed=None, *, author_id=BOT, content=""):
    return SimpleNamespace(
        id=number,
        author=SimpleNamespace(
            id=author_id, name="example", display_name="Example", bot=author_id == BOT
        ),
        created_at=START + timedelta(minutes=minutes),
        edited_at=None,
        type=SimpleNamespace(value=0),
        jump_url=f"https://discord.com/channels/10/20/{number}",
        content=content,
        embeds=[] if embed is None else [SimpleNamespace(to_dict=lambda: embed)],
        attachments=[],
        reference=None,
    )


def read_dump(result):
    contents = {}
    for path in result.archives:
        with ZipFile(path) as archive:
            for name in archive.namelist():
                contents[name] = contents.get(name, b"") + archive.read(name)
    return contents


class ResearchDumpTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_waits_for_server_delay_instead_of_abandoning_history(self):
        failure = Exception("Rate limited")
        failure.status = 429
        failure.retry_after = 90
        source = LogChannel(20, [])
        attempts = 0

        async def history(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise failure
            yield message(1, 1)

        source.history = history
        with (
            TemporaryDirectory() as directory,
            mock.patch.object(research_dump.asyncio, "sleep", new=mock.AsyncMock()) as sleep,
        ):
            result = await research_dump.dump_channels(
                source,
                LogChannel(30, []),
                Path(directory),
                bot_id=BOT,
                upload_limit=100_000,
                cutoff=START + timedelta(days=1),
            )
            self.assertTrue(result.complete)
            self.assertEqual(result.moderation_messages, 1)
            sleep.assert_awaited_once_with(90)

    async def test_large_dump_parts_fit_upload_limit_and_keep_every_record(self):
        rng = random.Random(42)
        items = [message(index + 1, index, content=rng.randbytes(200).hex()) for index in range(60)]
        with TemporaryDirectory() as directory:
            result = await research_dump.dump_channels(
                LogChannel(20, items),
                LogChannel(30, []),
                Path(directory),
                bot_id=BOT,
                upload_limit=6000,
                cutoff=START + timedelta(days=1),
            )
            self.assertGreater(len(result.archives), 1)
            self.assertTrue(all(path.stat().st_size <= 6000 for path in result.archives))
            data = read_dump(result)
            records = [json.loads(line) for line in data["moderation-messages.jsonl"].splitlines()]
            self.assertEqual(
                [record["content"] for record in records], [item.content for item in items]
            )
            self.assertEqual(data["member-messages.jsonl"], b"")
            self.assertTrue(json.loads(data["metadata.json"])["complete"])

    async def test_cancelling_packaging_waits_for_file_handles_to_close(self):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()

        class GatedArchive(ZipFile):
            def open(self, name, mode="r", pwd=None, *, force_zip64=False):
                stream = super().open(name, mode, pwd, force_zip64=force_zip64)
                if mode != "w":
                    return stream
                original_write = stream.write

                def write(data):
                    entered.set()
                    release.wait()
                    return original_write(data)

                stream.write = write
                return stream

            def __exit__(self, *args):
                try:
                    return super().__exit__(*args)
                finally:
                    closed.set()

        with (
            TemporaryDirectory() as directory,
            mock.patch.object(research_dump, "ZipFile", GatedArchive),
        ):
            task = asyncio.create_task(
                research_dump.dump_channels(
                    LogChannel(20, [message(1, 1)]),
                    LogChannel(30, []),
                    Path(directory),
                    bot_id=BOT,
                    upload_limit=100_000,
                    cutoff=START + timedelta(days=1),
                )
            )
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(closed.wait, 3))
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_permanent_api_error_preserves_a_clearly_incomplete_dump(self):
        failure = Exception("Missing access")
        failure.status = 403
        moderation = LogChannel(20, [])

        async def history(**kwargs):
            yield message(1, 1, content="Already fetched")
            raise failure

        moderation.history = history
        with TemporaryDirectory() as directory:
            result = await research_dump.dump_channels(
                moderation,
                LogChannel(30, [message(2, 2)]),
                Path(directory),
                bot_id=BOT,
                upload_limit=100_000,
                cutoff=START + timedelta(days=1),
            )
            self.assertFalse(result.complete)
            data = read_dump(result)
            self.assertEqual(
                json.loads(data["moderation-messages.jsonl"])["content"], "Already fetched"
            )
            self.assertEqual(len(data["member-messages.jsonl"].splitlines()), 1)
            metadata = json.loads(data["metadata.json"])
            self.assertFalse(metadata["complete"])
            self.assertEqual(metadata["channel_errors"][0]["status"], 403)
            self.assertEqual(metadata["channel_errors"][0]["channel_id"], "20")

    async def test_transient_api_failure_resumes_after_last_message_without_duplicates(self):
        moderation = LogChannel(20, [message(1, 1), message(2, 2)])
        attempts = []
        failure = Exception("Temporary Discord failure")
        failure.status = 503

        async def history(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                yield moderation.messages[0]
                raise failure
            self.assertEqual(kwargs["after"].id, 1)
            yield moderation.messages[1]

        moderation.history = history
        with (
            TemporaryDirectory() as directory,
            mock.patch.object(research_dump.asyncio, "sleep", new=mock.AsyncMock()),
        ):
            result = await research_dump.dump_channels(
                moderation,
                LogChannel(30, []),
                Path(directory),
                bot_id=BOT,
                upload_limit=100_000,
                cutoff=START + timedelta(days=1),
            )
            records = [
                json.loads(line)
                for line in read_dump(result)["moderation-messages.jsonl"].splitlines()
            ]
            self.assertEqual([item["message_id"] for item in records], ["1", "2"])
            self.assertEqual(len(attempts), 2)
            self.assertEqual(result.moderation_messages, 2)

    async def test_dump_preserves_all_messages_without_parsing_or_author_filters(self):
        embed = {
            "title": "Case #7571 | Ban 🔨",
            "description": "**Reason:** bio scam",
            "author": {"name": f"example ({USER})"},
            "fields": [{"name": "Moderator", "value": f"example ({BOT})"}],
        }
        moderation = LogChannel(
            20,
            [
                message(1, 5, embed),
                message(
                    2,
                    6,
                    {"title": "Unknown future logger format"},
                    author_id=USER,
                    content="human comment",
                ),
                message(3, 2000, embed),
            ],
        )
        members = LogChannel(30, [message(4, 1, {"description": "Raw role and onboarding flags"})])
        with TemporaryDirectory() as directory:
            result = await research_dump.dump_channels(
                moderation,
                members,
                Path(directory),
                bot_id=BOT,
                upload_limit=100_000,
                cutoff=START + timedelta(days=1),
            )
            self.assertEqual(result.moderation_messages, 2)
            self.assertEqual(result.member_messages, 1)
            data = read_dump(result)
            records = [json.loads(line) for line in data["moderation-messages.jsonl"].splitlines()]
            self.assertEqual([record["message_id"] for record in records], ["1", "2"])
            self.assertEqual(records[0]["embeds"][0], embed)
            self.assertEqual(records[1]["author"]["id"], str(USER))
            self.assertEqual(records[1]["content"], "human comment")
            self.assertEqual(len(data["member-messages.jsonl"].splitlines()), 1)
            self.assertNotIn("accounts.jsonl", data)
            metadata = json.loads(data["metadata.json"])
            self.assertEqual(metadata["moderation_messages"], 2)
            self.assertEqual(metadata["member_messages"], 1)
            self.assertEqual(metadata["current_role_labels"], {"77": "French"})
            self.assertEqual(
                moderation.history_calls,
                [{"limit": None, "oldest_first": True, "before": START + timedelta(days=1)}],
            )
            self.assertEqual(len(members.history_calls), 1)
