import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import (
    DetectionPipelineTestCase,
    _Bot,
    _isolated_honeypot_modules,
    _operational_support,
)


class IntentBacklogTests(DetectionPipelineTestCase):
    async def test_unavailable_content_queues_metadata_before_any_detector_or_content_read(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            bot = _Bot()
            bot.intents.message_content = False
            cog = module.Honeypot(bot, _operational_support())
            self._configure_public_boundary(cog, {"enabled": True})
            await cog._intent_backlog.initialize()

            class RedactedMessage:
                id = 300
                guild = SimpleNamespace(id=100)
                channel = SimpleNamespace(id=400)
                author = SimpleNamespace(id=200, bot=False)
                webhook_id = None

                @property
                def content(self):
                    raise AssertionError("Redacted content must not be read")

                @property
                def attachments(self):
                    raise AssertionError("Redacted attachments must not be read")

            with mock.patch.object(module.gif_detector, "on_message", new=mock.AsyncMock()) as gif, \
                    mock.patch.object(module.detection, "on_message", new=mock.AsyncMock()) as detection:
                await cog.on_message(RedactedMessage())
                await cog.on_message(RedactedMessage())
            gif.assert_not_awaited()
            detection.assert_not_awaited()
            rows = await cog._intent_backlog.due()
            self.assertEqual([(row.guild_id, row.channel_id, row.message_id) for row in rows],
                             [(100, 400, 300)])
            with closing(sqlite3.connect(cog._intent_backlog.path)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(pending_content_messages)")}
            self.assertEqual(columns, {"guild_id", "channel_id", "message_id", "enqueued_at",
                                       "attempts", "retry_at"})

    async def test_queue_is_restart_durable_bounded_and_expires_after_fourteen_days(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            backlog_module = module.intent_backlog
            path = Path(directory) / "queue.sqlite"
            store = backlog_module.IntentBacklog(path)
            await store.initialize()
            for message_id in range(12):
                await store.enqueue(1, 2, message_id, now=1000)
            reopened = backlog_module.IntentBacklog(path)
            await reopened.initialize()
            self.assertEqual(len(await reopened.due(now=1000)), 10)
            self.assertEqual(await reopened.due(now=1000 + backlog_module.RETENTION_SECONDS), ())

    async def test_disabled_features_bots_webhooks_and_dms_are_not_queued(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            bot = _Bot()
            bot.intents.message_content = False
            cog = module.Honeypot(bot, _operational_support())
            self._configure_public_boundary(cog, {"enabled": False, "gif_detector_enabled": False})
            await cog._intent_backlog.initialize()
            message = self._message(module)
            await module.intent_backlog.queue_if_unavailable(cog, message)
            for field, value in (("guild", None), ("webhook_id", 99)):
                old = getattr(message, field)
                setattr(message, field, value)
                await module.intent_backlog.queue_if_unavailable(cog, message)
                setattr(message, field, old)
            message.author.bot = True
            await module.intent_backlog.queue_if_unavailable(cog, message)
            self.assertEqual(await cog._intent_backlog.due(), ())

    async def test_failed_batch_does_not_starve_later_messages_after_restart(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            path = Path(directory) / "queue.sqlite"
            store = module.intent_backlog.IntentBacklog(path)
            await store.initialize()
            for message_id in range(11):
                await store.enqueue(1, 2, message_id, now=1000)
            first_batch = await store.due(now=1000)
            self.assertEqual([row.message_id for row in first_batch], list(range(10)))
            for pending in first_batch:
                await store.retry(pending, now=1000)
            reopened = module.intent_backlog.IntentBacklog(path)
            await reopened.initialize()
            second_batch = await reopened.due(now=1030)
            self.assertEqual(second_batch[0].message_id, 10)
            self.assertEqual(len(second_batch), 10)
            self.assertEqual(len(await reopened.due(now=1030, limit=20)), 11)

    async def test_restored_content_fetches_known_id_and_acks_only_after_processing(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            bot = _Bot()
            cog = module.Honeypot(bot, _operational_support())
            await cog._intent_backlog.initialize()
            await cog._intent_backlog.enqueue(100, 400, 300)
            message = self._message(module)
            bot.get_guild = lambda _guild_id: message.guild
            channel = SimpleNamespace(fetch_message=mock.AsyncMock(return_value=message), history=mock.Mock())
            cog._fetch_message_channel = mock.AsyncMock(return_value=channel)
            entered, release = asyncio.Event(), asyncio.Event()

            async def process(actual):
                self.assertIs(actual, message)
                entered.set()
                await release.wait()

            cog.on_message = process
            task = asyncio.create_task(module.intent_backlog.drain_once(cog))
            await entered.wait()
            self.assertEqual(len(await cog._intent_backlog.due()), 1)
            release.set()
            self.assertEqual(await task, 1)
            self.assertEqual(await cog._intent_backlog.due(), ())
            channel.fetch_message.assert_awaited_once_with(300)
            channel.history.assert_not_called()

    async def test_transient_error_keeps_message_and_lost_grant_after_fetch_prevents_dispatch(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog = module.Honeypot(_Bot(), _operational_support())
            await cog._intent_backlog.initialize()
            await cog._intent_backlog.enqueue(100, 400, 300, now=1000)
            message = self._message(module)
            cog.bot.get_guild = lambda _guild_id: message.guild
            channel = SimpleNamespace(fetch_message=mock.AsyncMock(side_effect=PermissionError("Forbidden")))
            cog._fetch_message_channel = mock.AsyncMock(return_value=channel)
            cog.on_message = mock.AsyncMock()
            with mock.patch.object(module.intent_backlog.time, "time", return_value=1001):
                await module.intent_backlog.drain_once(cog)
            self.assertEqual(await cog._intent_backlog.due(now=1002), ())
            retry = (await cog._intent_backlog.due(now=1031))[0]
            self.assertEqual(retry.attempts, 1)

            async def fetch(_message_id):
                cog.bot.intents.message_content = False
                return message

            channel.fetch_message = mock.AsyncMock(side_effect=fetch)
            with mock.patch.object(module.intent_backlog.time, "time", return_value=1032):
                await module.intent_backlog.drain_once(cog)
            cog.on_message.assert_not_awaited()
            self.assertEqual(len(await cog._intent_backlog.due(now=1062)), 1)

    async def test_proven_missing_message_is_discarded_but_unready_cache_is_not(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog = module.Honeypot(_Bot(), _operational_support())
            await cog._intent_backlog.initialize()
            await cog._intent_backlog.enqueue(100, 400, 300)
            cog.bot.is_ready = lambda: False
            cog._fetch_message_channel = mock.AsyncMock()
            await module.intent_backlog.drain_once(cog)
            cog._fetch_message_channel.assert_not_awaited()
            self.assertEqual(len(await cog._intent_backlog.due()), 1)
            cog.bot.is_ready = lambda: True
            cog.bot.get_guild = lambda _guild_id: SimpleNamespace(id=100)
            missing = module.discord.NotFound()
            cog._fetch_message_channel.return_value = SimpleNamespace(
                fetch_message=mock.AsyncMock(side_effect=missing),
            )
            await module.intent_backlog.drain_once(cog)
            self.assertEqual(await cog._intent_backlog.due(), ())
