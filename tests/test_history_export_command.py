import asyncio
import io
import json
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.test_nhmoderation_cog import loaded_nhmoderation


class HistoryExportCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_export_combines_stored_histories_without_fetching_discord(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = Mock()
            subject._mark_operational_recovered = AsyncMock()
            subject._history_export_lock = asyncio.Lock()
            subject.history = SimpleNamespace(export_history=AsyncMock(return_value={
                "read_at": "2026-10-04T12:00:00+00:00", "coverage": {"historical_gap": True},
                "observations": [{"observation_id": "1", "target_user_id": "20"}],
                "actions": [{"action_id": "1", "observation_ids": ["1"]}],
            }))
            honeypot = SimpleNamespace(
                _joinwatch_verification=SimpleNamespace(export_history=AsyncMock(return_value={
                    "incidents": [{"user_id": "20", "outcome": "passed"}], "events": [], "coverage": {},
                })),
                _joinwatch_groups=SimpleNamespace(read_history=AsyncMock(return_value={
                    "observations": {"20": {"first_joined_at": "2026-10-01T00:00:00+00:00", "imported": True}},
                    "sources": [],
                })),
                _case_store=SimpleNamespace(export_detection_history=Mock(return_value=[{"case_id": "case", "user_id": "20"}])),
            )
            subject.bot = SimpleNamespace(get_cog=Mock(return_value=honeypot))
            captured = {}

            async def send(*, file, **kwargs):
                captured["bytes"] = file.fp.read()
                captured["mentions"] = kwargs["allowed_mentions"]

            ctx = SimpleNamespace(
                guild=SimpleNamespace(id=10, me=object(), filesize_limit=100000),
                channel=SimpleNamespace(permissions_for=lambda member: SimpleNamespace(attach_files=True)),
                send=AsyncMock(side_effect=send),
            )
            await module.NHModeration.nhmod_history_export.callback(subject, ctx)
            with zipfile.ZipFile(io.BytesIO(captured["bytes"])) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["guild_id"], "10")
                self.assertEqual(manifest["counts"]["first_joins"], 1)
                self.assertEqual(manifest["counts"]["detection_cases"], 1)
                self.assertIn(b'"user_id": "20"', archive.read("joinwatch_incidents.jsonl"))
            self.assertFalse(captured["mentions"].everyone)
            subject.history.export_history.assert_awaited_once_with(10)

    async def test_public_rejection_happens_before_history_reads(self):
        with loaded_nhmoderation() as module:
            subject = object.__new__(module.NHModeration)
            subject._require_private_channel = Mock(side_effect=module.commands.UserFeedbackCheckFailure("private channel"))
            subject.history = SimpleNamespace(export_history=AsyncMock())
            with self.assertRaisesRegex(module.commands.UserFeedbackCheckFailure, "private channel"):
                await module.NHModeration.nhmod_history_export.callback(subject, SimpleNamespace())
            subject.history.export_history.assert_not_awaited()

    async def test_export_inherits_manage_messages_permission(self):
        with loaded_nhmoderation() as module:
            command = module.NHModeration.nhmod_history_export
            for granted in (False, True):
                ctx = SimpleNamespace(is_red_mod=False, is_red_admin=False, author=SimpleNamespace(
                    guild_permissions=SimpleNamespace(manage_messages=granted, administrator=False)))
                self.assertEqual(await command.can_run(ctx), granted)

    async def test_export_preconditions_reject_without_reading_history(self):
        with loaded_nhmoderation() as module:
            for reason in ("Attach Files", "Load Honeypot", "already running"):
                with self.subTest(reason=reason):
                    subject = object.__new__(module.NHModeration)
                    subject._require_private_channel = Mock()
                    subject.history = SimpleNamespace(export_history=AsyncMock())
                    subject.bot = SimpleNamespace(get_cog=Mock(return_value=None if reason == "Load Honeypot" else object()))
                    subject._history_export_lock = asyncio.Lock()
                    if reason == "already running":
                        await subject._history_export_lock.acquire()
                    ctx = SimpleNamespace(
                        guild=SimpleNamespace(id=10, me=object()),
                        channel=SimpleNamespace(permissions_for=Mock(return_value=SimpleNamespace(attach_files=reason != "Attach Files"))),
                    )
                    with self.assertRaisesRegex(module.commands.UserFeedbackCheckFailure, reason):
                        await module.NHModeration.nhmod_history_export.callback(subject, ctx)
                    subject.history.export_history.assert_not_awaited()
