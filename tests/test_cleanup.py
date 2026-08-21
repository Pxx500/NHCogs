import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules
from tests.test_gatecount import commands as nhmisc_commands
from tests.test_gatecount import nhmisc as nhmisc_module


class CleanupOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def record(honeypot, message_id, *, channel_id=20):
        return honeypot.MessageRecord(
            message_id=message_id,
            guild_id=10,
            channel_id=channel_id,
            author_id=30,
            created_at=datetime.now(timezone.utc),
            pinned=False,
            author_kind="member",
        )

    @staticmethod
    def context(channel):
        invocation = SimpleNamespace(id=999, delete=mock.AsyncMock())
        guild = SimpleNamespace(id=10, me=object())
        if not hasattr(channel, "id"):
            channel.id = 20
        channel.guild = guild
        channel.permissions_for = lambda member: SimpleNamespace(
            view_channel=True,
            manage_messages=True,
        )
        return SimpleNamespace(guild=guild, channel=channel, message=invocation)

    async def test_channel_cleanup_uses_registry_and_bulk_delete_without_history_fetch(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                records = (self.record(honeypot, 101), self.record(honeypot, 102))
                cog._message_registry.recent_in_channel = mock.AsyncMock(
                    return_value=records
                )
                cog._message_registry.forget_many = mock.AsyncMock()
                cog._message_registry.forget = mock.AsyncMock()
                partials = {
                    record.message_id: SimpleNamespace(id=record.message_id)
                    for record in records
                }
                channel = SimpleNamespace(
                    get_partial_message=lambda message_id: partials[message_id],
                    delete_messages=mock.AsyncMock(),
                    history=mock.Mock(side_effect=AssertionError("history fetch forbidden")),
                )
                ctx = self.context(channel)

                result = await honeypot.cleanup.cleanup_channel(cog, ctx, 2)

                cog._message_registry.recent_in_channel.assert_awaited_once_with(
                    10,
                    20,
                    limit=2,
                    before_message_id=999,
                )
                channel.delete_messages.assert_awaited_once_with(
                    tuple(partials.values()),
                    reason="NHMisc cleanup",
                )
                cog._message_registry.forget_many.assert_awaited_once_with((101, 102))
                ctx.message.delete.assert_awaited_once()
                self.assertEqual((result.requested, result.selected), (2, 2))
                self.assertEqual((result.deleted, result.failed), (2, 0))
                self.assertEqual(
                    result.public_message,
                    "Cleanup complete: requested 2, selected 2, deleted 2, "
                    "already missing 0, failed 0.",
                )

    async def test_channel_cleanup_with_no_candidates_only_removes_invocation(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._message_registry.recent_in_channel = mock.AsyncMock(
                    return_value=()
                )
                cog._message_registry.forget = mock.AsyncMock()
                channel = SimpleNamespace(
                    delete_messages=mock.AsyncMock(),
                    get_partial_message=mock.Mock(),
                )
                ctx = self.context(channel)

                result = await honeypot.cleanup.cleanup_channel(cog, ctx, 10)

                channel.delete_messages.assert_not_awaited()
                ctx.message.delete.assert_awaited_once()
                self.assertEqual((result.requested, result.selected), (10, 0))
                self.assertEqual(
                    result.public_message,
                    "Cleanup complete: requested 10, selected 0, deleted 0, "
                    "already missing 0, failed 0.",
                )

    async def test_permission_failure_retains_candidates_and_reports_aggregate(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                records = (self.record(honeypot, 101), self.record(honeypot, 102))
                cog._message_registry.recent_in_channel = mock.AsyncMock(
                    return_value=records
                )
                cog._message_registry.forget = mock.AsyncMock()
                cog._message_registry.forget_many = mock.AsyncMock()
                channel = SimpleNamespace(
                    id=20,
                    delete_messages=mock.AsyncMock(),
                    get_partial_message=mock.Mock(),
                )
                ctx = self.context(channel)
                channel.permissions_for = lambda member: SimpleNamespace(
                    view_channel=True,
                    manage_messages=False,
                )

                result = await honeypot.cleanup.cleanup_channel(cog, ctx, 2)

                channel.delete_messages.assert_not_awaited()
                cog._message_registry.forget_many.assert_not_awaited()
                self.assertEqual((result.deleted, result.failed), (0, 2))

    async def test_stale_bulk_batch_falls_back_to_individual_outcomes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                records = (self.record(honeypot, 101), self.record(honeypot, 102))
                cog._message_registry.recent_in_channel = mock.AsyncMock(
                    return_value=records
                )
                cog._message_registry.forget_many = mock.AsyncMock()
                cog._message_registry.forget = mock.AsyncMock()
                stale = honeypot.discord.HTTPException("stale batch")
                stale.status = 400
                missing = honeypot.discord.NotFound("already gone")
                partials = {
                    101: SimpleNamespace(id=101, delete=mock.AsyncMock()),
                    102: SimpleNamespace(id=102, delete=mock.AsyncMock(side_effect=missing)),
                }
                channel = SimpleNamespace(
                    get_partial_message=lambda message_id: partials[message_id],
                    delete_messages=mock.AsyncMock(side_effect=stale),
                )
                ctx = self.context(channel)

                result = await honeypot.cleanup.cleanup_channel(cog, ctx, 2)

                partials[101].delete.assert_awaited_once()
                partials[102].delete.assert_awaited_once()
                self.assertEqual(
                    (result.deleted, result.already_missing, result.failed),
                    (1, 1, 0),
                )
                self.assertEqual(
                    {call.args[0] for call in cog._message_registry.forget.await_args_list},
                    {101, 102, 999},
                )

    async def test_bulk_not_found_is_terminal_without_individual_retry(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                records = (self.record(honeypot, 101), self.record(honeypot, 102))
                cog._message_registry.recent_in_channel = mock.AsyncMock(
                    return_value=records
                )
                cog._message_registry.forget_many = mock.AsyncMock()
                cog._message_registry.forget = mock.AsyncMock()
                partials = {
                    record.message_id: SimpleNamespace(
                        id=record.message_id,
                        delete=mock.AsyncMock(),
                    )
                    for record in records
                }
                channel = SimpleNamespace(
                    get_partial_message=lambda message_id: partials[message_id],
                    delete_messages=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("already gone")
                    ),
                )
                ctx = self.context(channel)

                result = await honeypot.cleanup.cleanup_channel(cog, ctx, 2)

                for partial in partials.values():
                    partial.delete.assert_not_awaited()
                cog._message_registry.forget_many.assert_awaited_once_with((101, 102))
                self.assertEqual(
                    (result.deleted, result.already_missing, result.failed),
                    (0, 2, 0),
                )

    async def test_user_cleanup_groups_channels_and_reports_unavailable_candidates(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                records = (
                    self.record(honeypot, 101, channel_id=20),
                    self.record(honeypot, 102, channel_id=21),
                )
                cog._message_registry.recent_by_author = mock.AsyncMock(
                    return_value=records
                )
                cog._message_registry.forget_many = mock.AsyncMock()
                cog._message_registry.forget = mock.AsyncMock()
                partial = SimpleNamespace(id=101)
                available = SimpleNamespace(
                    id=20,
                    get_partial_message=lambda message_id: partial,
                    delete_messages=mock.AsyncMock(),
                )
                ctx = self.context(available)
                ctx.guild.get_channel = lambda channel_id: (
                    available if channel_id == 20 else None
                )
                ctx.guild.get_thread = lambda channel_id: None

                result = await honeypot.cleanup.cleanup_user(cog, ctx, 30, 2)

                available.delete_messages.assert_awaited_once()
                self.assertEqual((result.deleted, result.failed), (1, 1))
                self.assertNotIn("21", result.public_message)


class CleanupCommandAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_cleanup_commands_allow_red_moderators_or_manage_messages(self):
        expected = {"manage_messages": True}
        self.assertEqual(
            nhmisc_module.NHMisc.nhmisc_cleanup.mod_or_permissions,
            expected,
        )
        self.assertEqual(
            nhmisc_module.NHMisc.nhmisc_cleanup_user.mod_or_permissions,
            expected,
        )

    async def test_channel_command_delegates_to_loaded_honeypot(self):
        result = SimpleNamespace(public_message="sanitized result")
        honeypot = SimpleNamespace(
            cleanup_channel=mock.AsyncMock(return_value=result),
            cleanup_user=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc_module.NHMisc)
        cog.bot = SimpleNamespace(get_cog=lambda name: honeypot if name == "Honeypot" else None)
        ctx = SimpleNamespace(guild=object(), send=mock.AsyncMock())

        await nhmisc_module.NHMisc.nhmisc_cleanup(cog, ctx, 25)

        honeypot.cleanup_channel.assert_awaited_once_with(ctx, 25)
        ctx.send.assert_awaited_once_with("sanitized result", delete_after=10)

    async def test_user_command_accepts_raw_id_and_delegates(self):
        result = SimpleNamespace(public_message="sanitized result")
        honeypot = SimpleNamespace(
            cleanup_channel=mock.AsyncMock(),
            cleanup_user=mock.AsyncMock(return_value=result),
        )
        cog = object.__new__(nhmisc_module.NHMisc)
        cog.bot = SimpleNamespace(get_cog=lambda name: honeypot)
        ctx = SimpleNamespace(guild=object(), send=mock.AsyncMock())

        await nhmisc_module.NHMisc.nhmisc_cleanup_user(cog, ctx, "123456", 10)

        honeypot.cleanup_user.assert_awaited_once_with(ctx, 123456, 10)
        ctx.send.assert_awaited_once_with("sanitized result", delete_after=10)

    async def test_cleanup_rejects_out_of_range_count(self):
        cog = object.__new__(nhmisc_module.NHMisc)
        cog.bot = SimpleNamespace(get_cog=lambda name: object())
        ctx = SimpleNamespace(guild=object(), send=mock.AsyncMock())

        with self.assertRaises(nhmisc_commands.UserFeedbackCheckFailure):
            await nhmisc_module.NHMisc.nhmisc_cleanup(cog, ctx, 101)

    async def test_cleanup_reports_when_honeypot_is_not_loaded(self):
        cog = object.__new__(nhmisc_module.NHMisc)
        cog.bot = SimpleNamespace(get_cog=lambda name: None)
        ctx = SimpleNamespace(guild=object(), send=mock.AsyncMock())

        await nhmisc_module.NHMisc.nhmisc_cleanup(cog, ctx, 10)

        self.assertIn("Honeypot", ctx.send.await_args.args[0])

    async def test_cleanup_operational_failure_reaches_private_reporter(self):
        failure = RuntimeError("cleanup failed")
        honeypot = SimpleNamespace(
            cleanup_channel=mock.AsyncMock(side_effect=failure),
            cleanup_user=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc_module.NHMisc)
        cog.bot = SimpleNamespace(get_cog=lambda name: honeypot)
        cog.report_operational_error = mock.AsyncMock()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
            message=SimpleNamespace(id=300),
            send=mock.AsyncMock(),
        )

        await nhmisc_module.NHMisc.nhmisc_cleanup(cog, ctx, 10)

        cog.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="NHMisc",
            action="clean up Honeypot channel",
            error=failure,
            channel_id=200,
            message_id=300,
        )

    async def test_user_cleanup_operational_failure_reaches_private_reporter(self):
        failure = RuntimeError("cleanup failed")
        honeypot = SimpleNamespace(
            cleanup_channel=mock.AsyncMock(),
            cleanup_user=mock.AsyncMock(side_effect=failure),
        )
        cog = object.__new__(nhmisc_module.NHMisc)
        cog.bot = SimpleNamespace(get_cog=lambda name: honeypot)
        cog.report_operational_error = mock.AsyncMock()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
            message=SimpleNamespace(id=300),
            send=mock.AsyncMock(),
        )

        await nhmisc_module.NHMisc.nhmisc_cleanup_user(cog, ctx, "42", 10)

        cog.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="NHMisc",
            action="clean up Honeypot user messages",
            error=failure,
            channel_id=200,
            message_id=300,
        )


if __name__ == "__main__":
    unittest.main()
