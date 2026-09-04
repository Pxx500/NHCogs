import importlib.util
import sys
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules, _operational_support

CLEANUP_PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "cleanup"


@contextmanager
def loaded_managed_cleanup():
    names = ("NHCogs.cleanup", "NHCogs.cleanup.converters", "NHCogs.cleanup.cog")
    previous = {name: sys.modules.get(name) for name in names}
    package = types.ModuleType("NHCogs.cleanup")
    package.__path__ = [str(CLEANUP_PACKAGE_PATH)]
    sys.modules["NHCogs.cleanup"] = package
    try:
        for leaf in ("converters", "cog"):
            name = f"NHCogs.cleanup.{leaf}"
            spec = importlib.util.spec_from_file_location(
                name,
                CLEANUP_PACKAGE_PATH / f"{leaf}.py",
            )
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        yield sys.modules["NHCogs.cleanup.cog"]
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class CleanupOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def record(honeypot, message_id, *, channel_id=20, pinned=False):
        return honeypot.MessageRecord(
            message_id=message_id,
            guild_id=10,
            channel_id=channel_id,
            author_id=30,
            created_at=datetime.now(timezone.utc),
            pinned=pinned,
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
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                records = (self.record(honeypot, 101), self.record(honeypot, 102))
                cog._message_registry.recent_in_channel = mock.AsyncMock(return_value=records)
                cog._message_registry.forget_many = mock.AsyncMock()
                cog._message_registry.forget = mock.AsyncMock()
                partials = {
                    record.message_id: SimpleNamespace(id=record.message_id) for record in records
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
                    since_utc=mock.ANY,
                )
                channel.delete_messages.assert_awaited_once_with(
                    tuple(partials.values()),
                    reason="NHCogs managed cleanup",
                )
                cog._message_registry.forget_many.assert_awaited_once_with((101, 102))
                ctx.message.delete.assert_awaited_once()
                self.assertEqual((result.requested, result.selected), (2, 2))
                self.assertEqual((result.deleted, result.failed), (2, 0))
                self.assertEqual(
                    result.public_message,
                    "Cleanup complete: requested 2, selected 2, deleted 2, "
                    "already missing 0, failed 0",
                )

    async def test_channel_cleanup_with_no_candidates_only_removes_invocation(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._message_registry.recent_in_channel = mock.AsyncMock(return_value=())
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
                    "already missing 0, failed 0",
                )

    async def test_permission_failure_retains_candidates_and_reports_aggregate(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                records = (self.record(honeypot, 101), self.record(honeypot, 102))
                cog._message_registry.recent_in_channel = mock.AsyncMock(return_value=records)
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
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                records = (self.record(honeypot, 101), self.record(honeypot, 102))
                cog._message_registry.recent_in_channel = mock.AsyncMock(return_value=records)
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
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                records = (self.record(honeypot, 101), self.record(honeypot, 102))
                cog._message_registry.recent_in_channel = mock.AsyncMock(return_value=records)
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
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                records = (
                    self.record(honeypot, 101, channel_id=20),
                    self.record(honeypot, 102, channel_id=21),
                )
                cog._message_registry.recent_by_author = mock.AsyncMock(return_value=records)
                cog._message_registry.forget_many = mock.AsyncMock()
                cog._message_registry.forget = mock.AsyncMock()
                partial = SimpleNamespace(id=101)
                available = SimpleNamespace(
                    id=20,
                    get_partial_message=lambda message_id: partial,
                    delete_messages=mock.AsyncMock(),
                )
                ctx = self.context(available)
                ctx.guild.get_channel = lambda channel_id: available if channel_id == 20 else None
                ctx.guild.get_thread = lambda channel_id: None

                result = await honeypot.cleanup.cleanup_user(cog, ctx, 30, 2)

                cog._message_registry.recent_by_author.assert_awaited_once_with(
                    10,
                    30,
                    limit=2,
                    since_utc=mock.ANY,
                    before_message_id=999,
                )
                available.delete_messages.assert_awaited_once()
                self.assertEqual((result.deleted, result.failed), (1, 1))
                self.assertNotIn("21", result.public_message)

    async def test_after_cleanup_validates_boundary_and_uses_registry_range(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                self.assertTrue(hasattr(honeypot.cleanup, "cleanup_after"))
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                boundary = self.record(honeypot, 100)
                records = (self.record(honeypot, 200), self.record(honeypot, 300))
                cog._message_registry.get_in_channel = mock.AsyncMock(return_value=boundary)
                cog._message_registry.recent_in_channel = mock.AsyncMock(return_value=records)
                cog._message_registry.forget_many = mock.AsyncMock()
                cog._message_registry.forget = mock.AsyncMock()
                partials = {
                    record.message_id: SimpleNamespace(id=record.message_id) for record in records
                }
                channel = SimpleNamespace(
                    get_partial_message=lambda message_id: partials[message_id],
                    delete_messages=mock.AsyncMock(),
                    history=mock.Mock(side_effect=AssertionError("history fetch forbidden")),
                    fetch_message=mock.Mock(side_effect=AssertionError("fetch forbidden")),
                )
                ctx = self.context(channel)

                result = await honeypot.cleanup.cleanup_after(cog, ctx, 100)

                cog._message_registry.get_in_channel.assert_awaited_once_with(
                    10,
                    20,
                    100,
                    since_utc=mock.ANY,
                )
                cog._message_registry.recent_in_channel.assert_awaited_once_with(
                    10,
                    20,
                    limit=1001,
                    before_message_id=999,
                    after_message_id=100,
                    since_utc=mock.ANY,
                    exclude_pinned=True,
                )
                channel.delete_messages.assert_awaited_once()
                self.assertEqual((result.requested, result.selected), (2, 2))

    async def test_range_over_one_thousand_is_rejected_before_deletion(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._message_registry.get_in_channel = mock.AsyncMock(
                    return_value=self.record(honeypot, 100)
                )
                cog._message_registry.recent_in_channel = mock.AsyncMock(
                    return_value=tuple(
                        self.record(honeypot, message_id) for message_id in range(101, 1102)
                    )
                )
                channel = SimpleNamespace(
                    get_partial_message=mock.Mock(),
                    delete_messages=mock.AsyncMock(),
                )
                ctx = self.context(channel)

                with self.assertRaisesRegex(ValueError, "more than 1000"):
                    await honeypot.cleanup.cleanup_after(cog, ctx, 100)

                channel.delete_messages.assert_not_awaited()
                ctx.message.delete.assert_not_awaited()

    async def test_before_cleanup_uses_exclusive_boundary_and_can_include_pins(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._message_registry.get_in_channel = mock.AsyncMock(
                    return_value=self.record(honeypot, 500)
                )
                records = (
                    self.record(honeypot, 300),
                    self.record(honeypot, 400, pinned=True),
                )
                cog._message_registry.recent_in_channel = mock.AsyncMock(return_value=records)
                cog._message_registry.forget_many = mock.AsyncMock()
                cog._message_registry.forget = mock.AsyncMock()
                channel = SimpleNamespace(
                    get_partial_message=lambda message_id: SimpleNamespace(id=message_id),
                    delete_messages=mock.AsyncMock(),
                )
                ctx = self.context(channel)

                result = await honeypot.cleanup.cleanup_before(
                    cog,
                    ctx,
                    500,
                    2,
                    delete_pinned=True,
                )

                cog._message_registry.recent_in_channel.assert_awaited_once_with(
                    10,
                    20,
                    limit=2,
                    before_message_id=500,
                    since_utc=mock.ANY,
                    exclude_pinned=False,
                )
                self.assertEqual((result.requested, result.selected), (2, 2))

    async def test_between_cleanup_rejects_reversed_boundaries_without_deleting(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._message_registry.get_in_channel = mock.AsyncMock()
                cog._message_registry.recent_in_channel = mock.AsyncMock()
                channel = SimpleNamespace(
                    get_partial_message=mock.Mock(),
                    delete_messages=mock.AsyncMock(),
                )
                ctx = self.context(channel)

                with self.assertRaisesRegex(ValueError, "older boundary must precede"):
                    await honeypot.cleanup.cleanup_between(cog, ctx, 500, 400)

                cog._message_registry.get_in_channel.assert_not_awaited()
                cog._message_registry.recent_in_channel.assert_not_awaited()
                channel.delete_messages.assert_not_awaited()

    async def test_missing_or_cross_channel_boundary_is_rejected_before_selection(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._message_registry.get_in_channel = mock.AsyncMock(return_value=None)
                cog._message_registry.recent_in_channel = mock.AsyncMock()
                ctx = self.context(SimpleNamespace())

                with self.assertRaisesRegex(ValueError, "not retained in this channel"):
                    await honeypot.cleanup.cleanup_after(cog, ctx, 500)

                cog._message_registry.recent_in_channel.assert_not_awaited()
                ctx.message.delete.assert_not_awaited()

    async def test_exactly_one_thousand_records_use_ten_bulk_batches(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog._message_registry.get_in_channel = mock.AsyncMock(
                    return_value=self.record(honeypot, 100)
                )
                records = tuple(
                    self.record(honeypot, message_id) for message_id in range(101, 1101)
                )
                cog._message_registry.recent_in_channel = mock.AsyncMock(return_value=records)
                cog._message_registry.forget_many = mock.AsyncMock()
                cog._message_registry.forget = mock.AsyncMock()
                channel = SimpleNamespace(
                    get_partial_message=lambda message_id: SimpleNamespace(id=message_id),
                    delete_messages=mock.AsyncMock(),
                )
                ctx = self.context(channel)

                result = await honeypot.cleanup.cleanup_after(cog, ctx, 100)

                self.assertEqual(channel.delete_messages.await_count, 10)
                self.assertTrue(
                    all(
                        len(call.args[0]) == 100 for call in channel.delete_messages.await_args_list
                    )
                )
                self.assertEqual((result.selected, result.deleted), (1000, 1000))


class CleanupCommandAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_cleanup_commands_require_manage_messages(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                with loaded_managed_cleanup() as managed:
                    root = managed.Cleanup.cleanup
                    self.assertIsNone(root.parent)
                    self.assertEqual(root.name, "cleanup")
                    self.assertTrue(root.callback.guild_only)
                    self.assertEqual(
                        root.callback.has_permissions,
                        {"manage_messages": True},
                    )
                    self.assertEqual(
                        {command.name for command in root.commands},
                        {"messages", "user", "after", "before", "between"},
                    )
                    before = next(command for command in root.commands if command.name == "before")
                    self.assertEqual(
                        before.usage,
                        "[message_id] <count> [delete_pinned]",
                    )

    async def test_bare_cleanup_group_renders_registered_command_overview(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                with loaded_managed_cleanup() as managed:
                    cog = managed.Cleanup(object(), SimpleNamespace(), SimpleNamespace())
                    ctx = SimpleNamespace()
                    managed.send_group_overview = mock.AsyncMock()

                    await managed.Cleanup.cleanup.callback(cog, ctx)

                    managed.send_group_overview.assert_awaited_once_with(
                        ctx,
                        title="Cleanup",
                    )

    async def test_channel_command_delegates_to_loaded_honeypot(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                with loaded_managed_cleanup() as managed:
                    result = SimpleNamespace(public_message="sanitized result")
                    honeypot = SimpleNamespace(
                        cleanup_channel=mock.AsyncMock(return_value=result),
                    )
                    cog = managed.Cleanup(object(), SimpleNamespace(), honeypot)
                    ctx = SimpleNamespace(send=mock.AsyncMock())

                    await managed.Cleanup.cleanup_messages.callback(cog, ctx, 25)

                    honeypot.cleanup_channel.assert_awaited_once_with(ctx, 25)
                    ctx.send.assert_awaited_once_with(
                        "sanitized result",
                        delete_after=10,
                    )

    async def test_user_command_accepts_raw_id_and_delegates(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                with loaded_managed_cleanup() as managed:
                    result = SimpleNamespace(public_message="sanitized result")
                    honeypot = SimpleNamespace(
                        cleanup_user=mock.AsyncMock(return_value=result),
                    )
                    cog = managed.Cleanup(object(), SimpleNamespace(), honeypot)
                    ctx = SimpleNamespace(send=mock.AsyncMock())

                    await managed.Cleanup.cleanup_user.callback(cog, ctx, 123456, 10)

                    honeypot.cleanup_user.assert_awaited_once_with(ctx, 123456, 10)

    async def test_cleanup_rejects_out_of_range_count(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot_module:
                cog = honeypot_module.Honeypot(_Bot(), _operational_support())
                ctx = SimpleNamespace()
                with self.assertRaisesRegex(ValueError, "between 1 and 1000"):
                    await honeypot_module.cleanup.cleanup_channel(cog, ctx, 1001)

    async def test_cleanup_operational_failure_reaches_private_reporter(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                with loaded_managed_cleanup() as managed:
                    failure = RuntimeError("cleanup failed")
                    honeypot = SimpleNamespace(
                        cleanup_channel=mock.AsyncMock(side_effect=failure),
                    )
                    nhmisc = SimpleNamespace(report_operational_error=mock.AsyncMock())
                    cog = managed.Cleanup(object(), nhmisc, honeypot)
                    ctx = SimpleNamespace(
                        guild=SimpleNamespace(id=100),
                        channel=SimpleNamespace(id=200),
                        message=SimpleNamespace(id=300),
                        send=mock.AsyncMock(),
                    )

                    await managed.Cleanup.cleanup_messages.callback(cog, ctx, 10)

                    nhmisc.report_operational_error.assert_awaited_once_with(
                        guild_id=100,
                        source="Cleanup",
                        action="clean up observed channel messages",
                        error=failure,
                        channel_id=200,
                        message_id=300,
                    )

    async def test_reply_supplies_after_boundary_without_fetching(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                with loaded_managed_cleanup() as managed:
                    result = SimpleNamespace(public_message="sanitized result")
                    honeypot = SimpleNamespace(
                        cleanup_after=mock.AsyncMock(return_value=result),
                    )
                    cog = managed.Cleanup(object(), SimpleNamespace(), honeypot)
                    ctx = SimpleNamespace(
                        message=SimpleNamespace(
                            reference=SimpleNamespace(message_id=12345678901234567)
                        ),
                        send=mock.AsyncMock(),
                    )

                    await managed.Cleanup.cleanup_after.callback(cog, ctx)

                    honeypot.cleanup_after.assert_awaited_once_with(
                        ctx,
                        12345678901234567,
                        delete_pinned=False,
                    )

    async def test_reply_supplies_before_boundary_with_count_only_syntax(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                with loaded_managed_cleanup() as managed:
                    result = SimpleNamespace(public_message="sanitized result")
                    honeypot = SimpleNamespace(
                        cleanup_before=mock.AsyncMock(return_value=result),
                    )
                    cog = managed.Cleanup(object(), SimpleNamespace(), honeypot)
                    ctx = SimpleNamespace(
                        message=SimpleNamespace(
                            reference=SimpleNamespace(message_id=12345678901234567)
                        ),
                        send=mock.AsyncMock(),
                    )

                    await managed.Cleanup.cleanup_before.callback(cog, ctx, arguments="25")

                    honeypot.cleanup_before.assert_awaited_once_with(
                        ctx,
                        12345678901234567,
                        25,
                        delete_pinned=False,
                    )

    async def test_before_accepts_explicit_boundary_count_and_pin_flag(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                with loaded_managed_cleanup() as managed:
                    result = SimpleNamespace(public_message="sanitized result")
                    honeypot = SimpleNamespace(
                        cleanup_before=mock.AsyncMock(return_value=result),
                    )
                    cog = managed.Cleanup(object(), SimpleNamespace(), honeypot)
                    ctx = SimpleNamespace(
                        message=SimpleNamespace(reference=None),
                        send=mock.AsyncMock(),
                    )

                    await managed.Cleanup.cleanup_before.callback(
                        cog,
                        ctx,
                        arguments="12345678901234567 25 true",
                    )

                    honeypot.cleanup_before.assert_awaited_once_with(
                        ctx,
                        12345678901234567,
                        25,
                        delete_pinned=True,
                    )


if __name__ == "__main__":
    unittest.main()
