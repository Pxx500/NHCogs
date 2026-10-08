"""Replayed evidence gets a review window without changing its original timestamp."""

import asyncio
from datetime import datetime, timedelta, timezone
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


class DelayedDetectionCaseLifetimeTests(DetectionPipelineTestCase):
    async def _replay(self, module, cog, message, admitted_at):
        self._configure_public_boundary(cog, {"enabled": True, "review_enabled": False})
        cog._is_forward_purge_active = mock.Mock(return_value=False)
        cog._collect_detection_signals = mock.AsyncMock(return_value=(
            module.DetectionSignal("spam", "retained evidence", module.ActionIntent.NONE,
                                   True, {}),
        ))
        cog.bot.get_guild = lambda _guild: message.guild
        cog._fetch_message_channel = mock.AsyncMock(return_value=SimpleNamespace(
            fetch_message=mock.AsyncMock(return_value=message),
        ))
        await cog._intent_backlog.initialize()
        await cog._intent_backlog.enqueue(message.guild.id, message.channel.id, message.id,
                                          now=message.created_at.timestamp())
        with mock.patch.object(module.intent_backlog.time, "time", return_value=admitted_at.timestamp()), \
                mock.patch.object(module.intent_backlog, "datetime", create=True) as clock, \
                mock.patch.object(module.gif_detector, "on_message", new=mock.AsyncMock()):
            clock.now.return_value = admitted_at
            self.assertEqual(await module.intent_backlog.drain_once(cog), 1)
        return cog._case_store.list_open_cases()[0]

    async def test_two_day_old_replay_gets_full_new_review_window(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog = module.Honeypot(_Bot(), _operational_support())
            cog._case_store.initialize()
            admitted_at = datetime.now(timezone.utc).replace(microsecond=0)
            message = self._message(module, attachment_count=0)
            message.created_at = admitted_at - timedelta(days=2)

            snapshot = await self._replay(module, cog, message, admitted_at)

            self.assertEqual(snapshot.case.created_at, admitted_at)
            self.assertEqual(snapshot.case.expires_at, admitted_at + timedelta(hours=24))
            self.assertEqual(snapshot.messages[0].created_at, message.created_at)
            self.assertEqual(snapshot.messages[0].content, message.content)
            self.assertEqual(cog._case_store.list_due_cases(admitted_at), ())
            self.assertEqual(cog._case_store.list_due_cases(admitted_at + timedelta(hours=23)), ())
            self.assertEqual([case.case_id for case in cog._case_store.list_due_cases(
                admitted_at + timedelta(hours=24),
            )], [snapshot.case.case_id])

    async def test_replay_does_not_extend_an_existing_case_deadline(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog = module.Honeypot(_Bot(), _operational_support())
            cog._case_store.initialize()
            admitted_at = datetime.now(timezone.utc).replace(microsecond=0)
            message = self._message(module, attachment_count=0)
            message.created_at = admitted_at - timedelta(days=2)
            first = cog._case_store.append_message(module.NewMessage(
                guild_id=message.guild.id, user_id=message.author.id, channel_id=message.channel.id,
                message_id=message.id - 1, content="earlier admission",
                created_at=admitted_at - timedelta(hours=6), jump_url=None, attachments=(),
            ), ())

            snapshot = await self._replay(module, cog, message, admitted_at)

            self.assertEqual(snapshot.case.case_id, first.case.case_id)
            self.assertEqual(snapshot.case.created_at, first.case.created_at)
            self.assertEqual(snapshot.case.expires_at, first.case.expires_at)
            self.assertEqual(snapshot.messages[-1].created_at, message.created_at)

    async def test_replay_context_is_cleared_after_processing_error(self):
        await self._assert_context_cleared(cancel=False)

    async def test_replay_context_is_cleared_after_cancellation(self):
        await self._assert_context_cleared(cancel=True)

    async def _assert_context_cleared(self, *, cancel):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog = module.Honeypot(_Bot(), _operational_support())
            message = self._message(module, attachment_count=0)
            cog.bot.get_guild = lambda _guild: message.guild
            cog._fetch_message_channel = mock.AsyncMock(return_value=SimpleNamespace(
                fetch_message=mock.AsyncMock(return_value=message),
            ))
            await cog._intent_backlog.initialize()
            await cog._intent_backlog.enqueue(message.guild.id, message.channel.id, message.id)
            entered = asyncio.Event()
            blocked = asyncio.Event()
            during, after = [], []

            async def process(_message):
                during.append(module.intent_backlog.REPLAY_ADMITTED_AT.get())
                entered.set()
                if cancel:
                    await blocked.wait()
                raise RuntimeError("controlled replay failure")

            async def drain_and_check():
                try:
                    return await module.intent_backlog.drain_once(cog)
                finally:
                    after.append(module.intent_backlog.REPLAY_ADMITTED_AT.get())

            cog.on_message = process
            task = asyncio.create_task(drain_and_check())
            try:
                await entered.wait()
                self.assertIsNone(module.intent_backlog.REPLAY_ADMITTED_AT.get())
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assertEqual(await task, 0)
                self.assertIsNotNone(during[0])
                self.assertEqual(after, [None])
                self.assertIsNone(module.intent_backlog.REPLAY_ADMITTED_AT.get())
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
