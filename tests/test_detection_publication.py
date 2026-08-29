"""Review publication driven by the detection pipeline: destination
resolution, summary and thread creation, reclaim and rerender.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.detection_case_fixtures import capture_attachment, publish_evidence, publish_primary
from tests.harness import DetectionPipelineTestCase, _Bot, _isolated_honeypot_modules, active_case


class DetectionPublicationTests(DetectionPipelineTestCase):
    async def test_disabled_review_keeps_containment_but_suppresses_interactive_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0, channel_id=9)
                config = {
                    "enabled": True,
                    "review_enabled": False,
                    "dry_run": False,
                    "review_channel": 50,
                    "honeypot_channels": [9],
                    "whitelisted_roles": [],
                    "action": "review",
                    "fallback_action": "review",
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                cog._is_forward_purge_active.return_value = False
                cog._suspicion_reasons = mock.AsyncMock(return_value=["young account"])
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store,
                    message.guild.id,
                    message.author.id,
                )
                message.delete.assert_awaited_once()
                cog._publish_detection_case.assert_not_awaited()
                self.assertNotIn(
                    "review_publish",
                    {operation.operation_type for operation in snapshot.operations},
                )

    async def test_queued_message_with_ready_evidence_publishes_only_final_state(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "dry_run": True,
                        "review_channel": None,
                        "review_enabled": True,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                        "imagescan_detector_enabled": False,
                    },
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case_serial = mock.AsyncMock()
                capture_finished = asyncio.Event()
                capture_case_attachments = cog._capture_case_attachments

                async def capture_and_signal(*args, **kwargs):
                    try:
                        return await capture_case_attachments(*args, **kwargs)
                    finally:
                        asyncio.get_running_loop().call_soon(
                            capture_finished.set
                        )

                cog._capture_case_attachments = mock.AsyncMock(
                    side_effect=capture_and_signal
                )
                for publication_lock in cog._detection_publication_locks:
                    await publication_lock.acquire()

                try:
                    processing = asyncio.create_task(cog.on_message(message))
                    await asyncio.wait_for(capture_finished.wait(), timeout=1)
                finally:
                    for publication_lock in cog._detection_publication_locks:
                        publication_lock.release()
                await processing

                self.assertEqual(cog._publish_detection_case_serial.await_count, 1)

    async def test_publication_failure_happens_after_delete_and_leaves_retryable_operation(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "review_enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._publish_detection_case = mock.AsyncMock(
                    side_effect=RuntimeError("review unavailable")
                )

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(snapshot.messages[0].delete_status.value, "deleted")
                operation = next(
                    item for item in snapshot.operations if item.operation_type == "review_publish"
                )
                self.assertEqual(operation.status.value, "failed")
                self.assertEqual(
                    operation.retry_at - operation.updated_at,
                    timedelta(seconds=10),
                )
                self.assertIn("review unavailable", operation.last_error)
                message.delete.assert_awaited_once()

    async def test_preview_thread_failure_and_recovery_share_operation_identity(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "review_enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                scan_started = asyncio.Event()
                finish_scan = asyncio.Event()

                async def scan_images(*args, **kwargs):
                    scan_started.set()
                    await finish_scan.wait()

                cog._scan_all_case_message_images = mock.AsyncMock(
                    side_effect=scan_images
                )
                cog._publish_detection_case = mock.AsyncMock(
                    side_effect=[
                        RuntimeError(
                            "summary was posted but the case thread could not be created"
                        ),
                        True,
                    ]
                )
                cog._record_operational_failure = mock.AsyncMock()
                honeypot.detection.mark_operational_error_recovered = mock.AsyncMock()

                processing = asyncio.create_task(cog.on_message(message))
                await asyncio.wait_for(scan_started.wait(), timeout=1)

                failure = next(
                    call
                    for call in cog._record_operational_failure.await_args_list
                    if call.args[1] == "review_publish"
                )
                operation_id = failure.kwargs["operation_id"]
                self.assertIn("case thread", failure.args[2])

                finish_scan.set()
                await processing
                honeypot.detection.mark_operational_error_recovered.assert_awaited_once_with(
                    cog.bot,
                    guild_id=message.guild.id,
                    source="Honeypot",
                    action="review_publish",
                    correlation_key=operation_id,
                )

    async def test_missing_publication_destination_is_durable_after_delete(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                self._configure_public_boundary(
                    cog,
                    {
                        "enabled": True,
                        "review_enabled": True,
                        "dry_run": False,
                        "review_channel": None,
                        "spam_enabled": False,
                        "firstpost_enabled": False,
                        "firstpost_collect_enabled": False,
                    },
                )
                cog._scan_all_case_message_images = mock.AsyncMock()

                await cog.on_message(message)

                snapshot = await asyncio.to_thread(
                    active_case, cog._case_store, message.guild.id, message.author.id
                )
                self.assertEqual(snapshot.messages[0].delete_status.value, "deleted")
                operation = next(
                    item for item in snapshot.operations if item.operation_type == "review_publish"
                )
                self.assertEqual(operation.status.value, "failed")
                self.assertIsNotNone(operation.retry_at)
                self.assertIn("destination", operation.last_error.lower())
                message.delete.assert_awaited_once()

    async def test_missing_saved_review_message_is_replaced(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (honeypot.DetectionSignal(
                        "forward_purge", "active", honeypot.ActionIntent.REVIEW, True, {}
                    ),),
                )
                await asyncio.to_thread(
                    publish_primary, cog._case_store, appended.case.case_id, 50, 60
                )
                next_id = 70

                async def thread_send(*args, **kwargs):
                    nonlocal next_id
                    sent_message = SimpleNamespace(id=next_id)
                    next_id += 1
                    return sent_message

                thread = SimpleNamespace(
                    id=61,
                    send=mock.AsyncMock(side_effect=thread_send),
                    fetch_message=mock.AsyncMock(),
                )
                sent = SimpleNamespace(
                    id=61,
                    fetch_thread=mock.AsyncMock(side_effect=honeypot.discord.NotFound()),
                    create_thread=mock.AsyncMock(return_value=thread),
                )
                channel = SimpleNamespace(
                    id=50,
                    get_partial_message=mock.Mock(
                        return_value=SimpleNamespace(
                            edit=mock.AsyncMock(
                                side_effect=honeypot.discord.NotFound("missing")
                            )
                        )
                    ),
                    fetch_message=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("missing")
                    ),
                    send=mock.AsyncMock(return_value=sent),
                )
                sent.channel = channel
                cog.bot.get_guild = mock.Mock(return_value=message.guild)
                cog._get_text_channel_or_thread = mock.Mock(return_value=channel)
                embed = SimpleNamespace(add_field=mock.Mock())

                with (
                    mock.patch.object(
                        honeypot.discord, "Color",
                        SimpleNamespace(gold=lambda: 1, dark_red=lambda: 2),
                    ),
                    mock.patch.object(honeypot.discord, "Embed", return_value=embed),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        SimpleNamespace(none=lambda: None),
                    ),
                ):
                    await cog._publish_detection_case(
                        appended.case.case_id, 50
                    )

                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                self.assertEqual(
                    (snapshot.case.review_channel_id, snapshot.case.review_message_id),
                    (50, 61),
                )
                channel.send.assert_awaited_once()
                self.assertEqual(thread.send.await_count, 1)

    async def test_restart_reprojects_primary_and_evidence_into_the_case_thread(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                original = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(original._case_store.initialize)
                message = self._message(honeypot, attachment_count=11)
                appended = await asyncio.to_thread(
                    original._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (honeypot.DetectionSignal(
                        "forward_purge", "active", honeypot.ActionIntent.REVIEW, True, {}
                    ),),
                )
                case_id = appended.case.case_id
                for attachment in range(11):
                    evidence = data_path / f"evidence-{attachment}.png"
                    evidence.write_bytes(b"image")
                    await asyncio.to_thread(
                        capture_attachment,
                        original._case_store, case_id, 1, attachment, evidence,
                    )
                await asyncio.to_thread(
                    publish_primary, original._case_store, case_id, 50, 60
                )
                snapshot = await asyncio.to_thread(original._case_store.get_case, case_id)
                await asyncio.to_thread(
                    publish_evidence,
                    original._case_store,
                    case_id, 0, 50, 61,
                    tuple(item.key for item in snapshot.attachments[:10]),
                )
                await asyncio.to_thread(
                    publish_evidence,
                    original._case_store,
                    case_id, 1, 50, 62,
                    (snapshot.attachments[10].key,),
                )

                thread_messages = {}
                next_thread_id = 70

                async def thread_send(*args, **kwargs):
                    nonlocal next_thread_id
                    result = SimpleNamespace(id=next_thread_id, edit=mock.AsyncMock())
                    thread_messages[next_thread_id] = result
                    next_thread_id += 1
                    return result

                thread = SimpleNamespace(
                    id=60,
                    archived=False,
                    locked=False,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    edit=mock.AsyncMock(),
                    send=mock.AsyncMock(side_effect=thread_send),
                    fetch_message=mock.AsyncMock(
                        side_effect=lambda message_id: thread_messages[message_id]
                    ),
                    get_partial_message=mock.Mock(
                        side_effect=lambda message_id: thread_messages[message_id]
                    ),
                )
                primary = SimpleNamespace(
                    id=60,
                    edit=mock.AsyncMock(),
                    fetch_thread=mock.AsyncMock(return_value=thread),
                )
                evidence_one = SimpleNamespace(edit=mock.AsyncMock())
                evidence_two = SimpleNamespace(edit=mock.AsyncMock())
                channel = SimpleNamespace(
                    id=50,
                    get_partial_message=mock.Mock(return_value=primary),
                    fetch_message=mock.AsyncMock(
                        side_effect=lambda message_id: {
                            60: primary, 61: evidence_one, 62: evidence_two
                        }[message_id]
                    ),
                    send=mock.AsyncMock(),
                )
                primary.channel = channel
                fresh = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(fresh._case_store.initialize)
                fresh.bot.get_guild = mock.Mock(return_value=message.guild)
                fresh._get_text_channel_or_thread = mock.Mock(return_value=channel)
                fresh.config = SimpleNamespace(
                    guild_from_id=lambda guild_id: SimpleNamespace(
                        all=mock.AsyncMock(return_value={"review_channel": 50})
                    )
                )
                embed = SimpleNamespace(add_field=mock.Mock())
                with (
                    mock.patch.object(
                        honeypot.discord, "Color",
                        SimpleNamespace(dark_red=lambda: 1, gold=lambda: 2),
                    ),
                    mock.patch.object(honeypot.discord, "Embed", return_value=embed),
                    mock.patch.object(
                        honeypot.discord,
                        "File",
                        side_effect=lambda *args, **kwargs: object(),
                    ),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        SimpleNamespace(none=lambda: None),
                    ),
                    mock.patch.object(
                        honeypot, "DetectionCaseView",
                        side_effect=lambda *args, **kwargs: SimpleNamespace(resolved=kwargs["resolved"]),
                    ) as primary_view,
                ):
                    await fresh._case_review_service.apply_message(
                        case_id, 1, "tp", moderator_id=7
                    )
                    await fresh._case_review_rerender(case_id)

                primary.edit.assert_awaited_once()
                evidence_one.edit.assert_not_awaited()
                evidence_two.edit.assert_not_awaited()
                channel.send.assert_not_awaited()
                self.assertEqual(thread.send.await_count, 2)
                thread.edit.assert_not_awaited()
                self.assertTrue(
                    all(not call.kwargs["resolved"] for call in primary_view.call_args_list)
                )
                message_calls = [
                    call
                    for call in thread.send.await_args_list
                    if call.args[0].startswith("**M1")
                ]
                self.assertIsNotNone(message_calls[0].kwargs.get("view"))
                self.assertTrue(
                    all(
                        call.kwargs.get("view") is None
                        for call in thread.send.await_args_list
                        if call not in message_calls
                    )
                )

    async def test_concurrent_publishers_create_one_summary_and_one_thread_timeline(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                first = honeypot.Honeypot(_Bot())
                second = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(first._case_store.initialize)
                message = self._message(honeypot, attachment_count=1)
                appended = await asyncio.to_thread(
                    first._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (honeypot.DetectionSignal(
                        "forward_purge", "active", honeypot.ActionIntent.REVIEW, True, {}
                    ),),
                )
                evidence = data_path / "evidence.png"
                evidence.write_bytes(b"image")
                await asyncio.to_thread(
                    capture_attachment,
                    first._case_store, appended.case.case_id, 1, 0, evidence,
                )
                thread_messages = {}
                next_thread_id = 70

                async def thread_send(*args, **kwargs):
                    nonlocal next_thread_id
                    await asyncio.sleep(0)
                    result = SimpleNamespace(id=next_thread_id, edit=mock.AsyncMock())
                    thread_messages[next_thread_id] = result
                    next_thread_id += 1
                    return result

                thread = SimpleNamespace(
                    id=60,
                    guild=SimpleNamespace(filesize_limit=8 * 1024 * 1024),
                    send=mock.AsyncMock(side_effect=thread_send),
                    fetch_message=mock.AsyncMock(
                        side_effect=lambda message_id: thread_messages[message_id]
                    ),
                    get_partial_message=mock.Mock(
                        side_effect=lambda message_id: thread_messages[message_id]
                    ),
                )
                thread_created = False

                async def fetch_thread():
                    if not thread_created:
                        raise honeypot.discord.NotFound()
                    return thread

                async def create_thread(**kwargs):
                    nonlocal thread_created
                    if thread_created:
                        raise honeypot.discord.HTTPException()
                    thread_created = True
                    return thread

                summary = SimpleNamespace(
                    id=60,
                    edit=mock.AsyncMock(),
                    fetch_thread=mock.AsyncMock(side_effect=fetch_thread),
                    create_thread=mock.AsyncMock(side_effect=create_thread),
                )
                channel = SimpleNamespace(id=50)
                summary.channel = channel
                channel.send = mock.AsyncMock(return_value=summary)
                channel.get_partial_message = mock.Mock(return_value=summary)
                channel.fetch_message = mock.AsyncMock(return_value=summary)
                for cog in (first, second):
                    cog.bot.get_guild = mock.Mock(return_value=message.guild)
                    cog._get_text_channel_or_thread = mock.Mock(return_value=channel)
                embed = SimpleNamespace(add_field=mock.Mock())
                with (
                    mock.patch.object(
                        honeypot.discord, "Color",
                        SimpleNamespace(dark_red=lambda: 1, gold=lambda: 2),
                    ),
                    mock.patch.object(honeypot.discord, "Embed", return_value=embed),
                    mock.patch.object(honeypot.discord, "File", side_effect=lambda *a, **k: object()),
                    mock.patch.object(
                        honeypot.discord, "AllowedMentions", SimpleNamespace(none=lambda: None)
                    ),
                ):
                    await asyncio.gather(
                        first._publish_detection_case(
                            appended.case.case_id, 50
                        ),
                        second._publish_detection_case(
                            appended.case.case_id, 50
                        ),
                    )

                snapshot = await asyncio.to_thread(
                    first._case_store.get_case, appended.case.case_id
                )
                timeline = await asyncio.to_thread(
                    first._case_store.list_timeline_publications,
                    appended.case.case_id,
                )
                self.assertEqual(channel.send.await_count, 1)
                self.assertEqual(thread.send.await_count, 1)
                self.assertEqual(snapshot.case.review_message_id, 60)
                self.assertEqual(len(timeline), 1)
                self.assertTrue(all(item.state == "published" for item in timeline))

    async def test_reclaimed_primary_publication_deletes_loser_orphan(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                loser = honeypot.Honeypot(_Bot())
                winner = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(loser._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                appended = await asyncio.to_thread(
                    loser._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (honeypot.DetectionSignal(
                        "spam", "active", honeypot.ActionIntent.REVIEW, True, {}
                    ),),
                )
                orphan = SimpleNamespace(id=61, delete=mock.AsyncMock())
                channel = SimpleNamespace(id=50)

                async def send(*args, **kwargs):
                    winner_token = await asyncio.to_thread(
                        winner._case_store.claim_publication,
                        appended.case.case_id,
                        "primary",
                        datetime.now(timezone.utc) + timedelta(minutes=6),
                    )
                    self.assertIsNotNone(winner_token)
                    self.assertTrue(
                        await asyncio.to_thread(
                            winner._case_store.complete_primary_publication,
                            appended.case.case_id,
                            winner_token,
                            channel.id,
                            60,
                        )
                    )
                    return orphan

                channel.send = mock.AsyncMock(side_effect=send)
                loser.bot.get_guild = mock.Mock(return_value=message.guild)
                loser._get_text_channel_or_thread = mock.Mock(return_value=channel)
                embed = SimpleNamespace(add_field=mock.Mock())
                with (
                    mock.patch.object(
                        honeypot.discord, "Color",
                        SimpleNamespace(dark_red=lambda: 1, gold=lambda: 2),
                    ),
                    mock.patch.object(honeypot.discord, "Embed", return_value=embed),
                ):
                    with self.assertRaisesRegex(RuntimeError, "lease was lost"):
                        await loser._publish_detection_case(
                            appended.case.case_id, 50
                        )

                snapshot = await asyncio.to_thread(
                    loser._case_store.get_case, appended.case.case_id
                )
                orphan.delete.assert_awaited_once()
                self.assertEqual(snapshot.case.review_channel_id, 50)
                self.assertEqual(snapshot.case.review_message_id, 60)

    async def test_rerender_uses_persisted_log_channel_without_configured_destination(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                message = self._message(honeypot, attachment_count=0)
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.review_publication._new_case_message(message),
                    (honeypot.DetectionSignal(
                        "forward_purge", "active", honeypot.ActionIntent.REVIEW, True, {}
                    ),),
                )
                await asyncio.to_thread(
                    publish_primary, cog._case_store, appended.case.case_id, 90, 91
                )
                next_id = 92

                async def thread_send(*args, **kwargs):
                    nonlocal next_id
                    sent_message = SimpleNamespace(id=next_id)
                    next_id += 1
                    return sent_message

                thread = SimpleNamespace(
                    id=91,
                    send=mock.AsyncMock(side_effect=thread_send),
                    fetch_message=mock.AsyncMock(),
                )
                existing = SimpleNamespace(
                    id=91,
                    edit=mock.AsyncMock(),
                    channel=SimpleNamespace(id=90),
                    fetch_thread=mock.AsyncMock(return_value=thread),
                )
                log_channel = SimpleNamespace(
                    id=90,
                    get_partial_message=mock.Mock(return_value=existing),
                    fetch_message=mock.AsyncMock(return_value=existing),
                    send=mock.AsyncMock(),
                )
                cog.bot.get_guild = mock.Mock(return_value=message.guild)
                cog._get_text_channel_or_thread = mock.Mock(return_value=log_channel)
                embed = SimpleNamespace(add_field=mock.Mock())
                with (
                    mock.patch.object(
                        honeypot.discord, "Color",
                        SimpleNamespace(dark_red=lambda: 1, gold=lambda: 2),
                    ),
                    mock.patch.object(honeypot.discord, "Embed", return_value=embed),
                    mock.patch.object(
                        honeypot.discord,
                        "AllowedMentions",
                        SimpleNamespace(none=lambda: None),
                    ),
                ):
                    await cog._publish_detection_case(
                        appended.case.case_id, None
                    )

                existing.edit.assert_awaited_once()
                log_channel.get_partial_message.assert_called_once_with(91)
                log_channel.fetch_message.assert_not_awaited()
                log_channel.send.assert_not_awaited()
                self.assertEqual(thread.send.await_count, 1)

    async def test_spam_review_deletes_before_review_publication(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await asyncio.to_thread(cog._case_store.initialize)
                publication_started = asyncio.Event()
                release_publication = asyncio.Event()

                async def publish(**kwargs):
                    publication_started.set()
                    await release_publication.wait()
                    return SimpleNamespace(id=900)

                review_channel = SimpleNamespace(
                    id=700, send=mock.AsyncMock(side_effect=publish)
                )
                config = {
                    "enabled": True,
                    "review_enabled": True,
                    "dry_run": False,
                    "review_channel": 700,
                    "spam_enabled": True,
                    "spam_action": "review",
                    "spam_min_channels": 2,
                    "spam_window_seconds": 10,
                    "firstpost_enabled": False,
                    "firstpost_collect_enabled": False,
                    "imagescan_detector_enabled": False,
                }
                self._configure_public_boundary(cog, config)
                del cog._handle_spam_message
                cog._handle_firstpost_message.return_value = False
                cog._handle_imagescan_detector_message.return_value = False
                cog._spam_suspicion_reasons = mock.AsyncMock(return_value=["duplicate"])
                honeypot.discord.Color = SimpleNamespace(
                    red=lambda: 1, orange=lambda: 2, dark_red=lambda: 3, gold=lambda: 4
                )
                honeypot.discord.Embed = lambda **kwargs: SimpleNamespace(
                    color=kwargs.get("color"),
                    add_field=lambda **field: None,
                    set_author=lambda **author: None,
                    set_thumbnail=lambda **thumbnail: None,
                    set_footer=lambda **footer: None,
                )
                cog._scan_all_case_message_images = mock.AsyncMock()
                cog._is_forward_purge_active.return_value = False
                cog._get_text_channel_or_thread = mock.Mock(
                    side_effect=lambda guild, channel_id: (
                        review_channel if channel_id == review_channel.id else None
                    )
                )

                first = self._message(
                    honeypot, attachment_count=1, message_id=299, channel_id=399
                )
                second = self._message(
                    honeypot, attachment_count=1, message_id=300, channel_id=400
                )
                first.guild.get_channel = lambda channel_id: (
                    review_channel if channel_id == review_channel.id else None
                )
                second.guild = first.guild
                cog.bot.get_guild = lambda guild_id: first.guild

                await cog._observe_message(first)
                task = asyncio.create_task(cog.on_message(second))
                await asyncio.wait_for(publication_started.wait(), timeout=1)
                try:
                    second.delete.assert_awaited_once()
                finally:
                    release_publication.set()
                    await task

    async def test_review_retry_restores_thread_evidence_after_ban_without_repeating_ban(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                now = datetime.now(timezone.utc)
                member = SimpleNamespace(id=20)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    filesize_limit=8 * 1024 * 1024,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                await asyncio.to_thread(cog._case_store.initialize)
                cog._execute_action = mock.AsyncMock(return_value=("banned", None))
                cog._send_operational_alert = mock.AsyncMock()
                config_values = {
                    "dry_run": False,
                    "review_channel": 50,
                }
                cog.config = SimpleNamespace(
                    guild_from_id=lambda guild_id: SimpleNamespace(
                        all=mock.AsyncMock(return_value=config_values)
                    )
                )
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=now,
                        jump_url="https://discord.test/messages/40",
                        attachments=(
                            honeypot.NewAttachment(
                                0,
                                "proof.png",
                                5,
                                "image/png",
                                None,
                                None,
                                "https://cdn.test/proof.png",
                                spoiler=False,
                            ),
                        ),
                    ),
                    (
                        honeypot.DetectionSignal(
                            "honeypot", "matched", honeypot.ActionIntent.BAN, True, {}
                        ),
                    ),
                )
                evidence_path = data_path / "proof.png"
                evidence_path.write_bytes(b"image")
                await asyncio.to_thread(
                    capture_attachment,
                    cog._case_store,
                    appended.case.case_id,
                    appended.message.sequence,
                    0,
                    evidence_path,
                )
                await asyncio.to_thread(
                    publish_primary,
                    cog._case_store,
                    appended.case.case_id,
                    50,
                    60,
                )
                moderation = await asyncio.to_thread(
                    cog._case_store.ensure_operation,
                    appended.case.case_id,
                    "moderation_action",
                    f"moderation_action:{appended.case.case_id}:1:ban",
                    appended.message.sequence,
                )
                review = await asyncio.to_thread(
                    cog._case_store.ensure_operation,
                    appended.case.case_id,
                    "review_publish",
                    f"review-publish:{appended.case.case_id}:1",
                    appended.message.sequence,
                )
                claimed_moderation = await asyncio.to_thread(
                    cog._case_store.claim_operation, moderation.operation_id, now
                )
                await cog._execute_detection_case_operation(claimed_moderation, now)

                thread_messages = {}
                next_message_id = 70

                async def thread_send(*args, **kwargs):
                    nonlocal next_message_id
                    sent = SimpleNamespace(id=next_message_id, edit=mock.AsyncMock())
                    thread_messages[next_message_id] = sent
                    next_message_id += 1
                    return sent

                thread = SimpleNamespace(
                    id=60,
                    archived=False,
                    locked=False,
                    guild=guild,
                    edit=mock.AsyncMock(),
                    send=mock.AsyncMock(side_effect=thread_send),
                    fetch_message=mock.AsyncMock(
                        side_effect=lambda message_id: thread_messages[message_id]
                    ),
                )
                summary = SimpleNamespace(
                    id=60,
                    edit=mock.AsyncMock(),
                    fetch_thread=mock.AsyncMock(
                        side_effect=[
                            honeypot.discord.NotFound("missing"),
                            honeypot.discord.NotFound("missing"),
                            thread,
                        ]
                    ),
                    create_thread=mock.AsyncMock(
                        side_effect=honeypot.discord.HTTPException("temporary")
                    ),
                )
                channel = SimpleNamespace(
                    id=50,
                    get_partial_message=mock.Mock(return_value=summary),
                    fetch_message=mock.AsyncMock(return_value=summary),
                    send=mock.AsyncMock(),
                )
                summary.channel = channel
                cog._get_text_channel_or_thread = mock.Mock(return_value=channel)
                embed = SimpleNamespace(add_field=mock.Mock())
                with (
                    mock.patch.object(
                        honeypot.discord, "Color",
                        SimpleNamespace(dark_red=lambda: 1, gold=lambda: 2),
                    ),
                    mock.patch.object(honeypot.discord, "Embed", return_value=embed),
                    mock.patch.object(honeypot.discord, "File", return_value=object()),
                    mock.patch.object(
                        honeypot.discord, "AllowedMentions",
                        SimpleNamespace(none=lambda: None),
                    ),
                ):
                    first_review = await asyncio.to_thread(
                        cog._case_store.claim_operation, review.operation_id, now
                    )
                    await cog._execute_detection_case_operation(first_review, now)
                    failed = await asyncio.to_thread(
                        cog._case_store.get_case, appended.case.case_id
                    )
                    failed_review = next(
                        item for item in failed.operations
                        if item.operation_id == review.operation_id
                    )
                    retry = await asyncio.to_thread(
                        cog._case_store.claim_operation,
                        review.operation_id,
                        failed_review.retry_at,
                    )
                    await cog._execute_detection_case_operation(
                        retry, failed_review.retry_at
                    )

                completed = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                completed_review = next(
                    item for item in completed.operations
                    if item.operation_id == review.operation_id
                )
                self.assertEqual(completed_review.status.value, "succeeded")
                self.assertEqual(completed_review.attempts, 2)
                self.assertTrue(
                    any(call.kwargs.get("files") for call in thread.send.await_args_list)
                )
                cog._execute_action.assert_awaited_once()
