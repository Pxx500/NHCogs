import asyncio
import hashlib
import json
import random
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock
from zipfile import ZipFile

import aiohttp

from tests.harness import _isolated_honeypot_modules
from tests.test_research_dump import BOT, START, LogChannel, message, read_dump, research_dump
from tests.test_research_dump_commands import context


def attachment(number, filename, data):
    return SimpleNamespace(
        id=number, filename=filename, size=len(data), content_type="application/octet-stream",
        url=f"https://cdn.discordapp.com/attachments/20/{number}/file",
        proxy_url=f"https://media.discordapp.net/attachments/20/{number}/file",
    )


class _Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status
        self.content = self
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status != 200:
            raise aiohttp.ClientResponseError(SimpleNamespace(real_url="https://cdn.discordapp.com"), (), status=self.status, headers=self.headers)

    async def iter_chunked(self, size):
        for offset in range(0, len(self.data), min(size, 1234)):
            yield self.data[offset:offset + min(size, 1234)]


class _Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True
        return False

    def get(self, url, **_kwargs):
        self.calls.append(url)
        response = self.responses[url]
        return response.pop(0) if isinstance(response, list) else response


class AttachmentDumpTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_is_opt_in_and_missing_files_leave_an_incomplete_record(self):
        item = message(50, 1)
        item.attachments = [attachment(900, "../unsafe\\name.bin", b"missing")]
        for enabled in (False, True):
            session = _Session({item.attachments[0].url: _Response(b"", status=404)})
            with self.subTest(enabled=enabled), TemporaryDirectory() as directory, mock.patch.object(aiohttp, "ClientSession", return_value=session):
                result = await research_dump.dump_channels([LogChannel(20, [item])], Path(directory),
                    bot_id=BOT, upload_limit=100_000, cutoff=START + timedelta(days=1),
                    options=research_dump.DumpOptions(download_attachments=enabled))
                contents = read_dump(result)
                record = json.loads(contents["channel-20.jsonl"])
                self.assertEqual(result.complete, not enabled)
                self.assertFalse(any(name.startswith("attachments/") for name in contents))
                if enabled:
                    self.assertEqual(record["attachments"][0]["download_status"], "failed")
                    self.assertEqual(record["attachments"][0]["local_files"], [])
                    self.assertEqual(record["attachments"][0]["download_error"]["status"], 404)
                else:
                    self.assertEqual(session.calls, [])
                    self.assertNotIn("download_status", record["attachments"][0])

    async def test_cancelling_a_streaming_download_closes_it_and_cleans_temp_files(self):
        entered = asyncio.Event()

        class WaitingResponse(_Response):
            async def iter_chunked(self, size):
                yield b"partial"
                entered.set()
                await asyncio.Event().wait()

        item = attachment(801, "partial.bin", b"partial and the rest")
        session = _Session({item.url: WaitingResponse(b"")})
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
            mock.patch.object(honeypot.discord, "TextChannel", LogChannel),
            mock.patch.object(aiohttp, "ClientSession", return_value=session),
        ):
            cog = object.__new__(honeypot.Honeypot)
            cog.bot = SimpleNamespace(user=SimpleNamespace(id=BOT))
            cog._research_dump_jobs = {}
            ctx, source, _other = context()
            source.messages[0].attachments = [item]
            task = asyncio.create_task(honeypot.Honeypot.research_dump.callback(cog, ctx, arguments="20 --attachments"))
            await asyncio.wait_for(entered.wait(), timeout=3)
            await honeypot.Honeypot.research_cancel.callback(cog, ctx)
            await asyncio.wait_for(task, timeout=3)
            self.assertTrue(session.closed)
            self.assertEqual(cog._research_dump_jobs, {})
            self.assertFalse(any("file" in call.kwargs for call in ctx.send.await_args_list))
            remaining = await asyncio.to_thread(lambda: list(Path(directory).glob("research-dump-*")))
            self.assertEqual(remaining, [])

    async def test_attachment_rate_limit_uses_the_server_delay_before_retry(self):
        payload = b"downloaded after retry"
        item = message(50, 1)
        item.attachments = [attachment(900, "retry.bin", payload)]
        limited = _Response(b"", status=429)
        limited.headers = {"Retry-After": "90"}
        session = _Session({item.attachments[0].url: [limited, _Response(payload)]})
        with (
            TemporaryDirectory() as directory,
            mock.patch.object(aiohttp, "ClientSession", return_value=session),
            mock.patch.object(research_dump.asyncio, "sleep", new=mock.AsyncMock()) as sleep,
        ):
            result = await research_dump.dump_channels(
                [LogChannel(20, [item])], Path(directory), bot_id=BOT, upload_limit=100_000,
                cutoff=START + timedelta(days=1), options=research_dump.DumpOptions(download_attachments=True),
            )
            self.assertTrue(result.complete)
            sleep.assert_awaited_once_with(90)

    async def test_command_downloads_and_splits_files_using_the_live_guild_limit(self):
        large = random.Random(71).randbytes(25_000)
        small = b"second attachment with the same original filename"
        first, second = attachment(801, "../unsafe\\data.bin", large), attachment(802, "../unsafe\\data.bin", small)
        session = _Session({first.url: _Response(large), second.url: _Response(small)})
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
            mock.patch.object(honeypot.discord, "TextChannel", LogChannel),
            mock.patch.object(aiohttp, "ClientSession", return_value=session),
        ):
            cog = object.__new__(honeypot.Honeypot)
            cog.bot = SimpleNamespace(user=SimpleNamespace(id=BOT))
            cog._research_dump_jobs = {}
            ctx, source, _other = context()
            ctx.guild.filesize_limit = 8000
            source.messages[0].attachments = [first, second]
            contents, sizes = {}, []

            async def receive(*_args, **kwargs):
                if "file" in kwargs:
                    sizes.append((await asyncio.to_thread(Path(kwargs["file"].fp).stat)).st_size)
                    with ZipFile(kwargs["file"].fp) as archive:
                        for name in archive.namelist():
                            contents[name] = contents.get(name, b"") + archive.read(name)
                return SimpleNamespace(id=500, edit=mock.AsyncMock())

            ctx.send.side_effect = receive
            await honeypot.Honeypot.research_dump.callback(cog, ctx, arguments="20 --attachments")
            self.assertIn("channel-20.jsonl", contents)
            record = json.loads(contents["channel-20.jsonl"])
            for item, original in zip(record["attachments"], (large, small), strict=True):
                self.assertIn("local_files", item)
                restored = b"".join(contents[name] for name in item["local_files"])
                self.assertEqual(restored, original)
                self.assertEqual(item["sha256"], hashlib.sha256(original).hexdigest())
                self.assertTrue(all(name.startswith("attachments/") and ".." not in name and "\\" not in name for name in item["local_files"]))
                self.assertTrue(all(Path(name).name.startswith(record["message_id"] + "_") for name in item["local_files"]))
            self.assertGreater(len(record["attachments"][0]["local_files"]), 1)
            self.assertEqual(len(record["attachments"][1]["local_files"]), 1)
            self.assertTrue(all(size <= ctx.guild.filesize_limit for size in sizes))
            self.assertTrue(json.loads(contents["metadata.json"])["complete"])
            self.assertEqual(session.calls, [first.url, second.url])
