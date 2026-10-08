"""Publication followers wait for committed ownership without duplicating sends."""

import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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


def _publication_destinations(hp):
    messages = {}

    async def send_timeline(*_args, **_kwargs):
        message = SimpleNamespace(id=70 + len(messages), edit=mock.AsyncMock())
        messages[message.id] = message
        return message

    thread = SimpleNamespace(
        id=60,
        guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
        send=mock.AsyncMock(side_effect=send_timeline),
        fetch_message=mock.AsyncMock(side_effect=lambda message_id: messages[message_id]),
        get_partial_message=mock.Mock(side_effect=lambda message_id: messages[message_id]),
    )
    created = False

    async def fetch_thread():
        if not created:
            raise hp.discord.NotFound()
        return thread

    async def create_thread(**_kwargs):
        nonlocal created
        if created:
            raise hp.discord.HTTPException()
        created = True
        return thread

    summary = SimpleNamespace(
        id=60, edit=mock.AsyncMock(), fetch_thread=mock.AsyncMock(side_effect=fetch_thread),
        create_thread=mock.AsyncMock(side_effect=create_thread),
    )
    channel = SimpleNamespace(
        id=50, send=mock.AsyncMock(return_value=summary),
        get_partial_message=mock.Mock(return_value=summary),
        fetch_message=mock.AsyncMock(return_value=summary),
    )
    summary.channel = channel
    return channel, thread


@asynccontextmanager
async def _fixture(test):
    with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as hp:
        first = hp.Honeypot(_Bot(), _operational_support())
        second = hp.Honeypot(_Bot(), _operational_support())
        await asyncio.to_thread(first._case_store.initialize)
        message = test._message(hp, attachment_count=0)
        appended = await asyncio.to_thread(
            first._case_store.append_message, hp.review_publication._new_case_message(message),
            (hp.DetectionSignal("forward_purge", "active", hp.ActionIntent.REVIEW, True, {}),),
        )
        channel, thread = _publication_destinations(hp)
        for cog in (first, second):
            cog.bot.get_guild = mock.Mock(return_value=message.guild)
            cog._get_text_channel_or_thread = mock.Mock(return_value=channel)
        with (
            mock.patch.object(hp.discord, "Color", SimpleNamespace(dark_red=lambda: 1, gold=lambda: 2)),
            mock.patch.object(hp.discord, "Embed", return_value=SimpleNamespace(add_field=mock.Mock())),
            mock.patch.object(hp.discord, "AllowedMentions", SimpleNamespace(none=lambda: None)),
        ):
            yield SimpleNamespace(
                hp=hp, first=first, second=second, channel=channel, thread=thread,
                case_id=appended.case.case_id,
            )


class _CommitBarrier:
    def __init__(self):
        self.started = threading.Event()
        self.entered = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.release = threading.Event()
        self.reads = 0
        self.lock = threading.Lock()

    def block_commit(self, callback):
        def commit(*args, **kwargs):
            self.started.set()
            self.loop.call_soon_threadsafe(self.entered.set)
            if not self.release.wait(10):
                raise AssertionError("Publication commit was not released")
            return callback(*args, **kwargs)

        return commit

    def count_reads(self, callback):
        def read(*args, **kwargs):
            result = callback(*args, **kwargs)
            if self.started.is_set():
                with self.lock:
                    self.reads += 1
                    if self.reads >= 25:
                        self.release.set()
            return result

        return read


class PublicationFollowerWaitTests(DetectionPipelineTestCase):
    async def test_delayed_commits_create_one_summary_and_one_timeline(self):
        async with _fixture(self) as scene:
            primary = _CommitBarrier()
            timeline = _CommitBarrier()
            first = scene.first._case_store
            second = scene.second._case_store
            with (
                mock.patch.object(first, "complete_primary_publication", primary.block_commit(first.complete_primary_publication)),
                mock.patch.object(second, "get_case", primary.count_reads(second.get_case)),
                mock.patch.object(first, "complete_timeline_publication", timeline.block_commit(first.complete_timeline_publication)),
                mock.patch.object(second, "complete_timeline_publication", timeline.block_commit(second.complete_timeline_publication)),
                mock.patch.object(first, "list_timeline_publications", timeline.count_reads(first.list_timeline_publications)),
                mock.patch.object(second, "list_timeline_publications", timeline.count_reads(second.list_timeline_publications)),
            ):
                owner = asyncio.create_task(scene.first._publish_detection_case(scene.case_id, 50))
                follower = None
                try:
                    await asyncio.wait_for(primary.entered.wait(), 5)
                    follower = asyncio.create_task(scene.second._publish_detection_case(scene.case_id, 50))
                    await asyncio.gather(owner, follower)
                finally:
                    primary.release.set()
                    timeline.release.set()
                    await asyncio.gather(owner, *([follower] if follower is not None else []), return_exceptions=True)
            snapshot = await asyncio.to_thread(first.get_case, scene.case_id)
            publications = await asyncio.to_thread(first.list_timeline_publications, scene.case_id)
            self.assertGreaterEqual(primary.reads, 25)
            self.assertGreaterEqual(timeline.reads, 25)
            self.assertEqual(scene.channel.send.await_count, 1)
            self.assertEqual(scene.thread.send.await_count, 1)
            self.assertEqual(snapshot.case.review_message_id, 60)
            self.assertEqual(len(publications), 1)
            self.assertEqual(publications[0].state, "published")

    async def test_stalled_primary_follower_times_out_without_taking_ownership(self):
        async with _fixture(self) as scene:
            store = scene.first._case_store
            token = await asyncio.to_thread(store.claim_publication, scene.case_id, "primary", datetime.now(timezone.utc))
            loop = asyncio.get_running_loop()
            started = loop.time()
            with (
                mock.patch.object(scene.hp.review_publication, "DETECTION_PUBLICATION_FOLLOWER_WAIT_SECONDS", 0.1),
                mock.patch.object(scene.hp.review_publication, "DETECTION_PUBLICATION_FOLLOWER_POLL_SECONDS", 0.01),
                self.assertRaisesRegex(RuntimeError, "summary publication is unavailable"),
            ):
                await scene.second._publish_detection_case(scene.case_id, 50)
            self.assertGreaterEqual(loop.time() - started, 0.1)
            self.assertLess(loop.time() - started, 2)
            scene.channel.send.assert_not_awaited()
            scene.thread.send.assert_not_awaited()
            self.assertTrue(await asyncio.to_thread(store.complete_primary_publication, scene.case_id, token, 50, 60))

    async def test_stalled_timeline_follower_times_out_without_taking_ownership(self):
        async with _fixture(self) as scene:
            store = scene.first._case_store
            publication = await asyncio.to_thread(store.ensure_timeline_publication, scene.case_id, kind="message", message_sequence=1)
            owner = await asyncio.to_thread(store.claim_timeline_publication, publication.logical_key, datetime.now(timezone.utc))
            loop = asyncio.get_running_loop()
            started = loop.time()
            with (
                mock.patch.object(scene.hp.review_publication, "DETECTION_PUBLICATION_FOLLOWER_WAIT_SECONDS", 0.1),
                mock.patch.object(scene.hp.review_publication, "DETECTION_PUBLICATION_FOLLOWER_POLL_SECONDS", 0.01),
                self.assertRaisesRegex(RuntimeError, "timeline publication claim is unavailable"),
            ):
                await scene.hp.review_publication._acquire_case_timeline_publication(scene.second, publication)
            self.assertGreaterEqual(loop.time() - started, 0.1)
            self.assertLess(loop.time() - started, 2)
            current = (await asyncio.to_thread(store.list_timeline_publications, scene.case_id))[0]
            self.assertEqual(current.claim_token, owner.claim_token)
            self.assertEqual(current.state, "pending")
            scene.thread.send.assert_not_awaited()
            completed = await asyncio.to_thread(
                store.complete_timeline_publication, owner.logical_key, owner.claim_token,
                channel_id=60, message_id=70, revision=1,
            )
            self.assertEqual(completed.state, "published")

    async def test_cancelled_timeline_follower_preserves_owner_claim(self):
        async with _fixture(self) as scene:
            store = scene.first._case_store
            publication = await asyncio.to_thread(store.ensure_timeline_publication, scene.case_id, kind="message", message_sequence=1)
            owner = await asyncio.to_thread(store.claim_timeline_publication, publication.logical_key, datetime.now(timezone.utc))
            entered = asyncio.Event()
            loop = asyncio.get_running_loop()
            original = scene.second._case_store.list_timeline_publications

            def follower_read(*args):
                result = original(*args)
                loop.call_soon_threadsafe(entered.set)
                return result

            with mock.patch.object(scene.second._case_store, "list_timeline_publications", follower_read):
                follower = asyncio.create_task(
                    scene.hp.review_publication._acquire_case_timeline_publication(scene.second, publication)
                )
                await entered.wait()
                follower.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await follower
            current = (await asyncio.to_thread(store.list_timeline_publications, scene.case_id))[0]
            self.assertEqual(current.claim_token, owner.claim_token)
            scene.thread.send.assert_not_awaited()
            completed = await asyncio.to_thread(
                store.complete_timeline_publication, owner.logical_key, owner.claim_token,
                channel_id=60, message_id=70, revision=1,
            )
            self.assertEqual(completed.state, "published")
