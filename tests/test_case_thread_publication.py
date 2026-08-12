"""Thread-backed case publication: the summary message, the case thread and
the timeline messages projected into it.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.detection_case_fixtures import capture_attachment
from tests.harness import _Bot, _isolated_honeypot_modules


class ThreadBackedCasePublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_case_publication_serializes_overlapping_renders(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                first_started = asyncio.Event()
                release_first = asyncio.Event()
                active = 0
                max_active = 0
                calls = 0

                async def render(*args, **kwargs):
                    nonlocal active, max_active, calls
                    calls += 1
                    active += 1
                    max_active = max(max_active, active)
                    try:
                        if calls == 1:
                            first_started.set()
                            await release_first.wait()
                    finally:
                        active -= 1

                cog._publish_detection_case_serial = render
                first = asyncio.create_task(
                    cog._publish_detection_case("case-1", None, None)
                )
                await asyncio.wait_for(first_started.wait(), timeout=1)
                second = asyncio.create_task(
                    cog._publish_detection_case("case-1", None, None)
                )
                await asyncio.sleep(0)

                self.assertEqual(max_active, 1)
                release_first.set()
                await asyncio.gather(first, second)
                self.assertEqual(calls, 2)
                self.assertEqual(max_active, 1)

    async def test_reclaimed_timeline_publication_adopts_same_nonce_message(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime.now(timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "content", now, None, ()
                    ),
                    (),
                )
                publication = await asyncio.to_thread(
                    cog._case_store.ensure_timeline_publication,
                    appended.case.case_id,
                    kind="message",
                    message_sequence=1,
                )
                stale = await asyncio.to_thread(
                    cog._case_store.claim_timeline_publication,
                    publication.logical_key,
                    now,
                )
                winner = await asyncio.to_thread(
                    cog._case_store.claim_timeline_publication,
                    publication.logical_key,
                    now + timedelta(minutes=6),
                )
                await asyncio.to_thread(
                    cog._case_store.complete_timeline_publication,
                    winner.logical_key,
                    winner.claim_token,
                    channel_id=60,
                    message_id=70,
                    revision=1,
                )
                sent = SimpleNamespace(id=70, delete=mock.AsyncMock())

                await honeypot.review_publication._complete_case_timeline_publication(cog, stale, sent, 60)

                sent.delete.assert_not_awaited()

    async def test_failed_orphan_compensation_is_retried_durably(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime.now(timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "content", now, None, ()
                    ),
                    (),
                )
                orphan = SimpleNamespace(
                    id=70,
                    delete=mock.AsyncMock(
                        side_effect=honeypot.discord.HTTPException()
                    ),
                )

                await honeypot.review_publication._compensate_case_publication(cog,
                    appended.case.case_id, 60, orphan
                )
                await honeypot.review_publication._compensate_case_publication(cog,
                    appended.case.case_id, 60, orphan
                )

                self.assertEqual(
                    await asyncio.to_thread(
                        cog._case_store.list_orphan_publications
                    ),
                    ((appended.case.case_id, 10, 60, 70),),
                )
                recovered = SimpleNamespace(delete=mock.AsyncMock())
                channel = SimpleNamespace(
                    fetch_message=mock.AsyncMock(return_value=recovered)
                )
                cog.bot.get_guild = mock.Mock(return_value=SimpleNamespace(id=10))
                cog._fetch_text_channel_or_thread = mock.AsyncMock(
                    return_value=channel
                )

                await honeypot.review_publication._retry_detection_orphan_publications(cog)

                recovered.delete.assert_awaited_once()
                self.assertEqual(
                    await asyncio.to_thread(
                        cog._case_store.list_orphan_publications
                    ),
                    (),
                )

    async def test_timeline_card_keeps_source_url_and_compact_image_match_details(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                attachment = SimpleNamespace(
                    key=honeypot.AttachmentKey("case-1", 1, 0),
                    filename="proof.png",
                    capture_status="captured",
                    match_metadata={
                        "matched_filename": "known-scam.png",
                        "hash_diff": 3,
                        "threshold": 17,
                    },
                    learning_decision=None,
                    publication_error=None,
                )
                message = SimpleNamespace(
                    sequence=1,
                    channel_id=30,
                    created_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
                    delete_status="Deleted",
                    signal_reasons=("Image matched",),
                    content="suspicious",
                    jump_url="https://discord.com/channels/10/30/40",
                    attachments=(attachment,),
                )

                content = honeypot.Honeypot._case_timeline_message_content(message)

                self.assertIn(message.jump_url, content)
                self.assertIn("Files: 1·?·HD 3/17", content)
                self.assertNotIn("known-scam.png", content)

    async def test_timeline_card_shows_detector_score_and_effective_threshold(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                attachment = SimpleNamespace(
                    key=honeypot.AttachmentKey("case-1", 1, 0),
                    filename="proof.png",
                    capture_status="captured",
                    match_metadata={
                        "matched": True,
                        "score": 3,
                        "threshold": 17,
                    },
                    learning_decision=None,
                    publication_error=None,
                )
                message = SimpleNamespace(
                    sequence=1,
                    channel_id=30,
                    created_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
                    delete_status="Deleted",
                    signal_reasons=("Image matched",),
                    content="suspicious",
                    jump_url="https://discord.com/channels/10/30/40",
                    attachments=(attachment,),
                )

                content = honeypot.Honeypot._case_timeline_message_content(message)

                self.assertIn("Files: 1·?·HD 3/17", content)

    async def test_timeline_card_distinguishes_sha_and_optical_hash_matches(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                attachments = (
                    SimpleNamespace(
                        key=honeypot.AttachmentKey("case-1", 1, 0),
                        filename="same-bytes.png",
                        capture_status="captured",
                        match_metadata={
                            "matched": True,
                            "score": 0,
                            "threshold": 17,
                            "exact_decision": "true_positive",
                        },
                        learning_decision="true_positive",
                        publication_error=None,
                    ),
                    SimpleNamespace(
                        key=honeypot.AttachmentKey("case-1", 1, 1),
                        filename="same-image.png",
                        capture_status="captured",
                        match_metadata={
                            "matched": True,
                            "score": 0,
                            "threshold": 17,
                            "exact_decision": None,
                        },
                        learning_decision="false_positive",
                        publication_error=None,
                    ),
                    SimpleNamespace(
                        key=honeypot.AttachmentKey("case-1", 1, 2),
                        filename="missing.png",
                        capture_status="capture_failed",
                        match_metadata={},
                        learning_decision=None,
                        publication_error=None,
                    ),
                )
                message = SimpleNamespace(
                    sequence=1,
                    channel_id=30,
                    created_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
                    delete_status="Deleted",
                    signal_reasons=("Image matched",),
                    content="suspicious",
                    jump_url=None,
                    attachments=attachments,
                )

                content = honeypot.Honeypot._case_timeline_message_content(message)

                self.assertIn("Files: 1·TP·SHA  2·FP·OH  3·CF", content)

    async def test_long_timeline_message_preserves_fenced_content_source_and_attachment_details(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                source_url = "https://discord.com/channels/10/30/40"
                source_content = "before ``` embedded fence\n" + ("x" * 3500)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10,
                        20,
                        30,
                        40,
                        source_content,
                        now,
                        source_url,
                        (
                            honeypot.NewAttachment(
                                0,
                                "proof.png",
                                123,
                                "image/png",
                                640,
                                480,
                                "https://cdn.test/proof.png",
                                description="evidence",
                                spoiler=False,
                            ),
                        ),
                    ),
                    (),
                )
                await asyncio.to_thread(
                    cog._case_store.update_attachment_scan,
                    appended.case.case_id,
                    1,
                    0,
                    "sha256",
                    "phash",
                    match_metadata={"matched_filename": "known-scam.png", "hash_diff": 3},
                    error=None,
                )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                next_id = 70

                async def send(*args, **kwargs):
                    nonlocal next_id
                    result = SimpleNamespace(id=next_id)
                    next_id += 1
                    return result

                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(side_effect=send),
                    fetch_message=mock.AsyncMock(),
                )
                with mock.patch.object(
                    honeypot.discord,
                    "AllowedMentions",
                    SimpleNamespace(none=lambda: None),
                ):
                    await honeypot.review_publication._publish_case_timeline(cog, snapshot, thread, resolved=False)

                payloads = [
                    call.args[0]
                    for call in thread.send.await_args_list
                    if call.args[0].startswith("**M1")
                ]
                message_calls = [
                    call
                    for call in thread.send.await_args_list
                    if call.args[0].startswith("**M1")
                ]
                self.assertIsInstance(
                    message_calls[0].kwargs.get("view"),
                    honeypot.DetectionCaseView,
                )
                self.assertEqual(
                    message_calls[0].kwargs["view"].message_sequence,
                    1,
                )
                self.assertTrue(
                    all(
                        call.kwargs.get("view") is None
                        for call in message_calls[1:]
                    )
                )
                self.assertTrue(all(len(payload) <= 2000 for payload in payloads))
                self.assertTrue(all(payload.count("```") == 2 for payload in payloads))
                visible_content = "".join(
                    payload.split("```\n", 1)[1].split("\n```", 1)[0]
                    for payload in payloads
                ).replace("\u200b", "")
                rendered = "\n".join(payloads)
                self.assertEqual(visible_content, source_content)
                self.assertEqual(rendered.count(source_url), 1)
                self.assertNotIn("<#30>", rendered)
                self.assertNotIn("\n\n```\n", payloads[0])
                self.assertIn("Files: 1·?·HD 3", rendered)
                publications = sorted(
                    (
                        item
                        for item in await asyncio.to_thread(
                            cog._case_store.list_timeline_publications,
                            appended.case.case_id,
                        )
                        if item.kind == "message"
                    ),
                    key=lambda item: item.chunk_index,
                )
                self.assertEqual(
                    [item.chunk_index for item in publications],
                    list(range(len(payloads))),
                )
                self.assertTrue(
                    all(item.message_sequence == 1 for item in publications)
                )
                self.assertEqual(
                    len({item.logical_key for item in publications}),
                    len(publications),
                )

    async def test_each_timeline_message_receives_one_case_control_panel(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "first", now, None, ()
                    ),
                    (),
                )
                await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10,
                        20,
                        30,
                        41,
                        "second",
                        now + timedelta(seconds=1),
                        None,
                        (),
                    ),
                    (),
                )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                next_id = 70

                async def send(*args, **kwargs):
                    nonlocal next_id
                    result = SimpleNamespace(id=next_id)
                    next_id += 1
                    return result

                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(side_effect=send),
                    fetch_message=mock.AsyncMock(),
                )
                with mock.patch.object(
                    honeypot.discord,
                    "AllowedMentions",
                    SimpleNamespace(none=lambda: None),
                ):
                    await honeypot.review_publication._publish_case_timeline(cog, snapshot, thread, resolved=False)

                message_calls = [
                    call
                    for call in thread.send.await_args_list
                    if call.args and call.args[0].startswith("**M")
                ]
                self.assertEqual(len(message_calls), 2)
                self.assertTrue(
                    all(
                        isinstance(call.kwargs.get("view"), honeypot.DetectionCaseView)
                        for call in message_calls
                    )
                )
                self.assertTrue(
                    all(
                        call.kwargs["view"].case_id == appended.case.case_id
                        for call in message_calls
                    )
                )
                self.assertEqual(
                    [call.kwargs["view"].message_sequence for call in message_calls],
                    [1, 2],
                )

    async def test_incremental_timeline_publish_fills_earlier_gaps_in_order(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(10, 20, 30, 40, "first", now, None, ()),
                    (),
                )
                await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 41, "second", now + timedelta(seconds=1), None, ()
                    ),
                    (),
                )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                next_id = 70

                async def send(*args, **kwargs):
                    nonlocal next_id
                    result = SimpleNamespace(id=next_id)
                    next_id += 1
                    return result

                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(side_effect=send),
                    fetch_message=mock.AsyncMock(),
                )

                with mock.patch.object(
                    honeypot.discord,
                    "AllowedMentions",
                    SimpleNamespace(none=lambda: None),
                ):
                    await honeypot.review_publication._publish_case_timeline(cog,
                        snapshot, thread, resolved=False, message_sequence=2
                    )

                payloads = [call.args[0] for call in thread.send.await_args_list]
                self.assertTrue(payloads[0].startswith("**M1**"))
                self.assertTrue(payloads[1].startswith("**M2**"))
                thread.fetch_message.assert_not_awaited()

    async def test_incremental_timeline_does_not_edit_older_published_messages(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(10, 20, 30, 40, "first", now, None, ()),
                    (),
                )
                first_snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                next_id = 70

                async def send(*args, **kwargs):
                    nonlocal next_id
                    result = SimpleNamespace(id=next_id)
                    next_id += 1
                    return result

                fetched = {}

                async def fetch_message(message_id):
                    return fetched.setdefault(
                        message_id, SimpleNamespace(id=message_id, edit=mock.AsyncMock())
                    )

                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(side_effect=send),
                    fetch_message=mock.AsyncMock(side_effect=fetch_message),
                )
                with mock.patch.object(
                    honeypot.discord,
                    "AllowedMentions",
                    SimpleNamespace(none=lambda: None),
                ):
                    await honeypot.review_publication._publish_case_timeline(cog,
                        first_snapshot, thread, resolved=False, message_sequence=1
                    )
                    await asyncio.to_thread(
                        cog._case_store.append_message,
                        honeypot.NewMessage(
                            10, 20, 30, 41, "second", now + timedelta(seconds=1), None, ()
                        ),
                        (),
                    )
                    second_snapshot = await asyncio.to_thread(
                        cog._case_store.get_case, appended.case.case_id
                    )
                    thread.fetch_message.reset_mock()
                    await honeypot.review_publication._publish_case_timeline(cog,
                        second_snapshot, thread, resolved=False, message_sequence=2
                    )

                fetched_ids = [call.args[0] for call in thread.fetch_message.await_args_list]
                self.assertNotIn(71, fetched_ids)
                new_payloads = [
                    call.args[0]
                    for call in thread.send.await_args_list
                    if call.args and call.args[0].startswith("**M2**")
                ]
                self.assertEqual(len(new_payloads), 1)

    async def test_first_publication_creates_one_summary_and_a_thread_timeline(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                cog = honeypot.Honeypot(bot)
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="copied scam content",
                        created_at=now,
                        jump_url="https://discord.test/channels/10/30/40",
                        attachments=(),
                    ),
                    (),
                )
                sent_messages = iter((SimpleNamespace(id=70), SimpleNamespace(id=71)))
                thread = SimpleNamespace(
                    id=60,
                    send=mock.AsyncMock(side_effect=lambda *args, **kwargs: next(sent_messages)),
                    fetch_message=mock.AsyncMock(),
                )
                summary = SimpleNamespace(
                    id=60,
                    edit=mock.AsyncMock(),
                    fetch_thread=mock.AsyncMock(side_effect=honeypot.discord.NotFound()),
                    create_thread=mock.AsyncMock(return_value=thread),
                )
                channel = SimpleNamespace(
                    id=50,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(return_value=summary),
                    fetch_message=mock.AsyncMock(return_value=summary),
                )
                summary.channel = channel
                guild = SimpleNamespace(
                    id=10,
                    get_channel=lambda channel_id: channel if channel_id == 50 else None,
                    get_thread=lambda thread_id: thread if thread_id == 60 else None,
                )
                bot.get_guild = lambda guild_id: guild
                cog._get_text_channel_or_thread = mock.Mock(
                    side_effect=lambda _guild, channel_id: channel if channel_id == 50 else thread
                )

                class Embed:
                    def __init__(self, **kwargs):
                        self.kwargs = kwargs
                        self.fields = []

                    def add_field(self, **kwargs):
                        self.fields.append(kwargs)

                with (
                    mock.patch.object(honeypot.discord, "Embed", Embed),
                    mock.patch.object(
                        honeypot.discord,
                        "Color",
                        SimpleNamespace(dark_red=lambda: 1, gold=lambda: 2),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        SimpleNamespace(none=lambda: None),
                    ),
                ):
                    await cog._publish_detection_case(
                        appended.case.case_id,
                        50,
                        None,
                    )

                endpoint = await asyncio.to_thread(
                    cog._case_store.ensure_projection_endpoint,
                    appended.case.case_id,
                )
                timeline = await asyncio.to_thread(
                    cog._case_store.list_timeline_publications,
                    appended.case.case_id,
                )
                channel.send.assert_awaited_once()
                self.assertEqual(
                    channel.send.await_args.kwargs["nonce"],
                    int(appended.case.case_id.replace("-", ""), 16)
                    & ((1 << 63) - 1),
                )
                self.assertNotIn("enforce_nonce", channel.send.await_args.kwargs)
                summary.fetch_thread.assert_awaited_once()
                summary.create_thread.assert_awaited_once()
                self.assertEqual(thread.send.await_count, 1)
                self.assertNotIn("enforce_nonce", thread.send.await_args_list[0].kwargs)
                self.assertEqual(endpoint.summary_message_id, 60)
                self.assertEqual(endpoint.thread_id, 60)
                self.assertEqual(timeline[0].kind, "message")
                self.assertEqual(timeline[0].message_id, 70)
                payload = thread.send.await_args_list[0].args[0]
                source_url = "https://discord.test/channels/10/30/40"
                self.assertEqual(payload.count(source_url), 1)
                self.assertNotIn("<#30>", payload)
                self.assertIn(
                    "Signals:\n- Detection signal recorded\n```\n"
                    "copied scam content\n```",
                    payload,
                )

    async def test_thread_create_conflict_adopts_the_existing_attached_thread(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "evidence", now, None, ()
                    ),
                    (),
                )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                thread = SimpleNamespace(id=60)
                summary = SimpleNamespace(
                    id=60,
                    channel=SimpleNamespace(id=50),
                    fetch_thread=mock.AsyncMock(
                        side_effect=[honeypot.discord.NotFound(), thread]
                    ),
                    create_thread=mock.AsyncMock(
                        side_effect=honeypot.discord.HTTPException()
                    ),
                )

                adopted = await honeypot.review_publication._ensure_detection_case_thread(cog, snapshot, summary)

                self.assertIs(adopted, thread)
                self.assertEqual(summary.fetch_thread.await_count, 2)
                endpoint = await asyncio.to_thread(
                    cog._case_store.ensure_projection_endpoint,
                    appended.case.case_id,
                )
                self.assertEqual(endpoint.thread_id, 60)

    async def test_thread_creation_failure_preserves_the_discord_error(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "evidence", now, None, ()
                    ),
                    (),
                )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                create_error = honeypot.discord.HTTPException(
                    "Create Public Threads denied"
                )
                summary = SimpleNamespace(
                    id=60,
                    channel=SimpleNamespace(id=50),
                    fetch_thread=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("missing")
                    ),
                    create_thread=mock.AsyncMock(side_effect=create_error),
                )

                with self.assertRaises(honeypot.discord.HTTPException) as raised:
                    await honeypot.review_publication._ensure_detection_case_thread(cog, snapshot, summary)

                self.assertIs(raised.exception, create_error)

    async def test_evidence_batches_wait_until_every_attachment_is_terminal(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10,
                        20,
                        30,
                        40,
                        "copied content",
                        now,
                        None,
                        (
                            honeypot.NewAttachment(
                                0, "first.png", 5, "image/png", 10, 20, "https://cdn/first"
                            ),
                            honeypot.NewAttachment(
                                1, "second.png", 5, "image/png", 10, 20, "https://cdn/second"
                            ),
                        ),
                    ),
                    (),
                )
                evidence = data_path / "first.png"
                evidence.write_bytes(b"image")
                await asyncio.to_thread(
                    capture_attachment,
                    cog._case_store,
                    appended.case.case_id,
                    1,
                    0,
                    evidence,
                )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                message = snapshot.messages[0]
                message_attachments = tuple(
                    item for item in snapshot.attachments if item.message_sequence == 1
                )
                projected_message = SimpleNamespace(
                    attachments=message_attachments,
                    sequence=message.sequence,
                )
                thread = SimpleNamespace(
                    filesize_limit=8 * 1024 * 1024,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                )

                batches, oversized, _limit = honeypot.review_publication._case_timeline_evidence_batches(
                    projected_message, thread
                )

                self.assertEqual(batches, ())
                self.assertEqual(oversized, ())

    async def test_timeline_combines_message_text_files_and_controls(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                attachments = tuple(
                    honeypot.NewAttachment(
                        position,
                        f"proof-{position}.png",
                        5,
                        "image/png",
                        10,
                        20,
                        f"https://cdn.test/{position}",
                        description=f"evidence {position}",
                        spoiler=position == 0,
                    )
                    for position in range(4)
                )
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "copied content", now, None, attachments
                    ),
                    (),
                )
                for position in range(4):
                    evidence = data_path / f"proof-{position}.png"
                    evidence.write_bytes(b"image")
                    await asyncio.to_thread(
                        capture_attachment,
                        cog._case_store,
                        appended.case.case_id,
                        1,
                        position,
                        evidence,
                    )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                next_id = 70

                async def send(*args, **kwargs):
                    nonlocal next_id
                    result = SimpleNamespace(id=next_id)
                    next_id += 1
                    return result

                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(side_effect=send),
                    fetch_message=mock.AsyncMock(),
                )
                created_files = []

                def make_file(path, **kwargs):
                    result = SimpleNamespace(path=path, **kwargs)
                    created_files.append(result)
                    return result

                with (
                    mock.patch.object(honeypot.discord, "File", side_effect=make_file),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        SimpleNamespace(none=lambda: None),
                    ),
                ):
                    await honeypot.review_publication._publish_case_timeline(cog, snapshot, thread, resolved=False)

                self.assertEqual(thread.send.await_count, 1)
                publication = thread.send.await_args
                self.assertIn("copied content", publication.args[0])
                self.assertEqual(len(publication.kwargs["files"]), 4)
                self.assertIsInstance(
                    publication.kwargs.get("view"),
                    honeypot.DetectionCaseView,
                )
                self.assertEqual(
                    publication.kwargs["view"].message_sequence,
                    1,
                )
                self.assertEqual(len(created_files), 4)
                self.assertTrue(created_files[0].spoiler)
                self.assertEqual(created_files[0].description, "evidence 0")
                publications = await asyncio.to_thread(
                    cog._case_store.list_timeline_publications,
                    appended.case.case_id,
                )
                self.assertEqual(
                    [item.kind for item in publications],
                    ["message"],
                )
                self.assertTrue(all(item.state == "published" for item in publications))

    async def test_timeline_rerender_edits_known_message_without_fetching_it(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "copied content",
                        datetime(2026, 7, 14, 12, tzinfo=timezone.utc), None, (),
                    ),
                    (),
                )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                partial = SimpleNamespace(edit=mock.AsyncMock())
                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(return_value=SimpleNamespace(id=70)),
                    fetch_message=mock.AsyncMock(),
                    get_partial_message=mock.Mock(return_value=partial),
                )
                with mock.patch.object(
                    honeypot.discord,
                    "AllowedMentions",
                    SimpleNamespace(none=lambda: None),
                ):
                    await honeypot.review_publication._publish_case_timeline(
                        cog, snapshot, thread, resolved=False
                    )
                    await honeypot.review_publication._publish_case_timeline(
                        cog, snapshot, thread, resolved=True
                    )

                thread.get_partial_message.assert_called_once_with(70)
                thread.fetch_message.assert_not_awaited()
                partial.edit.assert_awaited_once()
                self.assertNotIn("attachments", partial.edit.await_args.kwargs)

    async def test_timeline_rerender_replaces_a_missing_known_message(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "copied content",
                        datetime(2026, 7, 14, 12, tzinfo=timezone.utc), None, (),
                    ),
                    (),
                )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                partial = SimpleNamespace(
                    edit=mock.AsyncMock(side_effect=honeypot.discord.NotFound())
                )
                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(
                        side_effect=(SimpleNamespace(id=70), SimpleNamespace(id=71))
                    ),
                    fetch_message=mock.AsyncMock(),
                    get_partial_message=mock.Mock(return_value=partial),
                )
                with mock.patch.object(
                    honeypot.discord,
                    "AllowedMentions",
                    SimpleNamespace(none=lambda: None),
                ):
                    await honeypot.review_publication._publish_case_timeline(
                        cog, snapshot, thread, resolved=False
                    )
                    await honeypot.review_publication._publish_case_timeline(
                        cog, snapshot, thread, resolved=True
                    )

                self.assertEqual(thread.send.await_count, 2)
                publication = (
                    await asyncio.to_thread(
                        cog._case_store.list_timeline_publications,
                        appended.case.case_id,
                    )
                )[0]
                self.assertEqual(publication.message_id, 71)

    async def test_timeline_upload_limit_applies_to_each_file_not_batch_total(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                sizes = (14, 14, 11, 11)
                attachments = tuple(
                    honeypot.NewAttachment(
                        position,
                        f"proof-{position}.png",
                        size,
                        "image/png",
                        10,
                        20,
                        f"https://cdn.test/{position}",
                    )
                    for position, size in enumerate(sizes)
                )
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "copied content", now, None, attachments
                    ),
                    (),
                )
                for position, size in enumerate(sizes):
                    evidence = data_path / f"proof-{position}.png"
                    evidence.write_bytes(b"x" * size)
                    await asyncio.to_thread(
                        capture_attachment,
                        cog._case_store,
                        appended.case.case_id,
                        1,
                        position,
                        evidence,
                    )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                next_id = 70

                async def send(*args, **kwargs):
                    nonlocal next_id
                    result = SimpleNamespace(id=next_id)
                    next_id += 1
                    return result

                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=25),
                    send=mock.AsyncMock(side_effect=send),
                    fetch_message=mock.AsyncMock(),
                )
                with (
                    mock.patch.object(
                        honeypot.discord,
                        "File",
                        side_effect=lambda path, **kwargs: SimpleNamespace(
                            path=path, **kwargs
                        ),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        SimpleNamespace(none=lambda: None),
                    ),
                ):
                    await honeypot.review_publication._publish_case_timeline(cog, snapshot, thread, resolved=False)

                self.assertEqual(thread.send.await_count, 1)
                self.assertEqual(len(thread.send.await_args.kwargs["files"]), 4)

    async def test_evidence_rerender_replaces_batches_and_neutralizes_old_chunks(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                attachments = tuple(
                    honeypot.NewAttachment(
                        position,
                        f"proof-{position}.png",
                        5,
                        "image/png",
                        10,
                        20,
                        f"https://cdn.test/{position}",
                    )
                    for position in range(11)
                )
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "copied content", now, None, attachments
                    ),
                    (),
                )
                for position in range(11):
                    evidence = data_path / f"proof-{position}.png"
                    evidence.write_bytes(b"image")
                    await asyncio.to_thread(
                        capture_attachment,
                        cog._case_store,
                        appended.case.case_id,
                        1,
                        position,
                        evidence,
                    )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                published = {}
                next_id = 70

                async def send(content, **kwargs):
                    nonlocal next_id
                    item = SimpleNamespace(
                        id=next_id,
                        content=content,
                        attachments=list(kwargs.get("files", ())),
                        view=kwargs.get("view"),
                    )

                    async def edit(**changes):
                        if "content" in changes:
                            item.content = changes["content"]
                        if "attachments" in changes:
                            item.attachments = list(changes["attachments"])
                        if "view" in changes:
                            item.view = changes["view"]
                        return item

                    item.edit = mock.AsyncMock(side_effect=edit)
                    published[next_id] = item
                    next_id += 1
                    return item

                thread = SimpleNamespace(
                    id=60,
                    filesize_limit=20,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(side_effect=send),
                    fetch_message=mock.AsyncMock(
                        side_effect=lambda message_id: published[message_id]
                    ),
                    get_partial_message=mock.Mock(
                        side_effect=lambda message_id: published[message_id]
                    ),
                )
                with (
                    mock.patch.object(
                        honeypot.discord,
                        "File",
                        side_effect=lambda path, **kwargs: SimpleNamespace(
                            path=path, **kwargs
                        ),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        SimpleNamespace(none=lambda: None),
                    ),
                ):
                    await honeypot.review_publication._publish_case_timeline(cog, snapshot, thread, resolved=False)
                    first_send_count = thread.send.await_count
                    self.assertTrue(
                        all(
                            "nonce" in call.kwargs
                            for call in thread.send.await_args_list
                        )
                    )
                    (data_path / "proof-10.png").unlink()
                    await honeypot.review_publication._publish_case_timeline(cog, snapshot, thread, resolved=True)

                receipts = await asyncio.to_thread(
                    cog._case_store.list_timeline_publications,
                    appended.case.case_id,
                )
                evidence_receipts = sorted(
                    (item for item in receipts if item.kind == "evidence"),
                    key=lambda item: item.chunk_index,
                )
                self.assertEqual([item.chunk_index for item in evidence_receipts], [1])
                obsolete = published[evidence_receipts[0].message_id]
                self.assertEqual(thread.send.await_count, first_send_count)
                self.assertEqual(obsolete.attachments, [])
                self.assertIsNone(obsolete.view)
                self.assertIn("No additional attachments", obsolete.content)

    async def test_review_destination_requires_thread_publication_permissions(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                guild = SimpleNamespace(me=object())
                permissions = SimpleNamespace(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    create_public_threads=False,
                    send_messages_in_threads=True,
                    embed_links=True,
                    attach_files=True,
                    manage_threads=True,
                )
                channel = SimpleNamespace(
                    mention="#review",
                    permissions_for=lambda _member: permissions,
                )

                missing = cog._missing_channel_permissions(
                    guild,
                    channel,
                    read_history=True,
                    create_public_threads=True,
                    send_in_threads=True,
                    embed_links=True,
                    attach_files=True,
                    manage_threads=True,
                )

                self.assertIn("Create Public Threads", missing)

    async def test_honeypot_source_does_not_require_send_messages(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                guild = SimpleNamespace(me=object())
                permissions = SimpleNamespace(
                    view_channel=True,
                    send_messages=False,
                    read_message_history=True,
                    manage_messages=True,
                )
                channel = SimpleNamespace(
                    mention="#honeypot",
                    permissions_for=lambda _member: permissions,
                )

                missing = cog._missing_channel_permissions(
                    guild,
                    channel,
                    send_messages=False,
                    read_history=True,
                    manage_messages=True,
                )

                self.assertIsNone(missing)

    async def test_joinwatch_thread_requires_send_messages_in_threads(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                guild = SimpleNamespace(me=object())
                permissions = SimpleNamespace(
                    view_channel=True,
                    send_messages=True,
                    send_messages_in_threads=False,
                )
                thread = SimpleNamespace(
                    mention="#joinwatch",
                    permissions_for=lambda _member: permissions,
                )

                missing = cog._missing_channel_permissions(
                    guild,
                    thread,
                    send_messages=False,
                    send_in_threads=True,
                )

                self.assertIn("Send Messages in Threads", missing)

    async def test_review_channel_cache_miss_uses_authoritative_lookup(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                class TextChannel:
                    pass

                honeypot.discord.TextChannel = TextChannel
                channel = TextChannel()
                guild = SimpleNamespace(
                    get_channel=lambda channel_id: None,
                    get_thread=lambda channel_id: None,
                    fetch_channel=mock.AsyncMock(return_value=channel),
                )
                bot = _Bot()
                bot.get_channel = lambda channel_id: None
                cog = honeypot.Honeypot(bot)

                resolved = await cog._fetch_text_channel_or_thread(guild, 123)

                self.assertIs(resolved, channel)
                guild.fetch_channel.assert_awaited_once_with(123)

    async def test_review_channel_lookup_failure_is_not_silenced(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild = SimpleNamespace(
                    get_channel=lambda channel_id: None,
                    get_thread=lambda channel_id: None,
                    fetch_channel=mock.AsyncMock(
                        side_effect=honeypot.discord.Forbidden("denied")
                    ),
                )
                bot = _Bot()
                bot.get_channel = lambda channel_id: None
                cog = honeypot.Honeypot(bot)

                with self.assertRaises(honeypot.discord.Forbidden):
                    await cog._fetch_text_channel_or_thread(guild, 123)

    async def test_archived_case_thread_is_reopened_before_publication(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                honeypot.Honeypot(_Bot())
                thread = SimpleNamespace(
                    archived=True,
                    locked=True,
                    edit=mock.AsyncMock(),
                )
                thread.edit.return_value = thread

                active = await honeypot.review_publication._activate_detection_case_thread(thread)

                self.assertIs(active, thread)
                thread.edit.assert_awaited_once_with(
                    archived=False,
                    locked=False,
                    reason="Honeypot detection case update",
                )

    async def test_terminal_case_thread_is_locked_and_archived(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                honeypot.Honeypot(_Bot())
                thread = SimpleNamespace(edit=mock.AsyncMock())

                await honeypot.review_publication._finalize_detection_case_thread(thread)

                thread.edit.assert_awaited_once_with(
                    archived=True,
                    locked=True,
                    reason="Honeypot detection case resolved",
                )

    async def test_terminal_timeline_does_not_duplicate_resolution_from_summary(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=now,
                        jump_url=None,
                        attachments=(),
                    ),
                    (),
                )
                lease = cog._case_store.claim_resolution(
                    appended.case.case_id, now
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.RESOLVED,
                        "ignore",
                        99,
                        now,
                    )
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                sent_messages = {}

                async def send(content, **_kwargs):
                    sent = SimpleNamespace(
                        id=70 + len(sent_messages),
                        content=content,
                        edit=mock.AsyncMock(),
                        delete=mock.AsyncMock(),
                    )
                    sent_messages[sent.id] = sent
                    return sent

                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(side_effect=send),
                    fetch_message=mock.AsyncMock(
                        side_effect=lambda message_id: sent_messages[message_id]
                    ),
                    get_partial_message=mock.Mock(
                        side_effect=lambda message_id: sent_messages[message_id]
                    ),
                )
                with mock.patch.object(
                    honeypot.discord,
                    "AllowedMentions",
                    SimpleNamespace(none=lambda: None),
                ):
                    await asyncio.gather(
                        honeypot.review_publication._publish_case_timeline(cog, snapshot, thread, resolved=True),
                        honeypot.review_publication._publish_case_timeline(cog, snapshot, thread, resolved=True),
                    )
                    await honeypot.review_publication._publish_case_timeline(cog, snapshot, thread, resolved=True)

                self.assertEqual(thread.send.await_count, 1)
                resolution_messages = [
                    message
                    for message in sent_messages.values()
                    if "Resolved" in message.content
                ]
                self.assertEqual(resolution_messages, [])
                publications = cog._case_store.list_timeline_publications(
                    appended.case.case_id
                )
                self.assertEqual(
                    [item.kind for item in publications].count("resolution"), 0
                )
