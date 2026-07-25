from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tests.test_detection_pipeline import _Bot, _isolated_honeypot_modules


class SourceDeleteHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _append_source(honeypot, cog, created_at, *, signals=()):
        cog._case_store.initialize()
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=10,
                user_id=20,
                channel_id=30,
                message_id=40,
                content="evidence",
                created_at=created_at,
                jump_url="https://discord.test/messages/40",
                attachments=(),
            ),
            signals,
        )

    async def test_registered_handler_deletes_and_completes_with_durable_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                try:
                    handler_module = import_module("Honeypot.operations.source_delete")
                except ModuleNotFoundError:
                    self.fail("source_delete has no dedicated handler module")
                now = datetime.now(timezone.utc)
                deleted_message_ids = []

                async def delete_message():
                    deleted_message_ids.append(40)

                async def fetch_message(message_id):
                    return SimpleNamespace(delete=delete_message)

                channel = SimpleNamespace(fetch_message=fetch_message)
                guild = SimpleNamespace(
                    get_channel=lambda channel_id: channel if channel_id == 30 else None,
                    get_thread=lambda channel_id: None,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild if guild_id == 10 else None
                bot.get_channel = lambda channel_id: None
                cog = honeypot.Honeypot(bot)
                appended = self._append_source(honeypot, cog, now)
                cog._case_store.update_message_delete(
                    appended.case.case_id,
                    appended.message.sequence,
                    honeypot.DeleteStatus.FORBIDDEN,
                    "initial delete failed",
                    False,
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.SOURCE_DELETE,
                    f"source_delete:{appended.case.case_id}:30:40",
                    appended.message.sequence,
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                stat_names = []

                async def increment_stat(stat_guild, name):
                    self.assertIs(stat_guild, guild)
                    stat_names.append(name)

                cog._increment_stat = increment_stat

                self.assertIs(
                    cog._detection_operation_handlers.resolve(
                        honeypot.OperationType.SOURCE_DELETE
                    ),
                    handler_module.source_delete_handler,
                )

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(deleted_message_ids, [40])
                self.assertIs(completed.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(completed.result, "deleted")
                self.assertIs(
                    snapshot.messages[0].delete_status,
                    honeypot.DeleteStatus.DELETED,
                )
                self.assertEqual(stat_names, ["purged_messages"])

    async def test_missing_source_completes_retry_as_already_gone(self):
        for missing_message in (False, True):
            with (
                self.subTest(missing_message=missing_message),
                TemporaryDirectory() as directory,
            ):
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    handler_module = import_module("Honeypot.operations.source_delete")
                    now = datetime.now(timezone.utc)

                    async def fetch_missing_message(message_id):
                        raise honeypot.discord.NotFound("source message is gone")

                    channel = SimpleNamespace(fetch_message=fetch_missing_message)

                    async def fetch_missing_channel(channel_id):
                        raise honeypot.discord.NotFound("source channel is gone")

                    guild = SimpleNamespace(
                        get_channel=lambda channel_id: (
                            channel if missing_message and channel_id == 30 else None
                        ),
                        get_thread=lambda channel_id: None,
                        fetch_channel=fetch_missing_channel,
                    )
                    bot = _Bot()
                    bot.get_guild = lambda guild_id: guild
                    bot.get_channel = lambda channel_id: None
                    cog = honeypot.Honeypot(bot)
                    appended = self._append_source(honeypot, cog, now)
                    cog._case_store.update_message_delete(
                        appended.case.case_id,
                        appended.message.sequence,
                        honeypot.DeleteStatus.FORBIDDEN,
                        "initial delete failed",
                        False,
                    )
                    operation = cog._case_store.ensure_operation(
                        appended.case.case_id,
                        honeypot.OperationType.SOURCE_DELETE,
                        f"source_delete:{appended.case.case_id}:30:40",
                        appended.message.sequence,
                    )
                    claimed = cog._case_store.claim_operation(
                        operation.operation_id, now
                    )
                    snapshot = cog._case_store.get_case(appended.case.case_id)
                    context = honeypot.OperationContext(
                        operation=claimed,
                        snapshot=snapshot,
                        lease=honeypot.OperationLease(
                            operation_id=claimed.operation_id,
                            claim_token=claimed.claim_token,
                        ),
                        now=now,
                    )
                    stat_names = []

                    async def increment_stat(stat_guild, name):
                        stat_names.append(name)

                    cog._increment_stat = increment_stat

                    outcome = await handler_module.source_delete_handler(cog, context)

                    refreshed = cog._case_store.get_case(appended.case.case_id)
                    self.assertEqual(outcome.result, "already_gone")
                    self.assertIsNone(outcome.error)
                    self.assertIs(
                        refreshed.messages[0].delete_status,
                        honeypot.DeleteStatus.ALREADY_GONE,
                    )
                    self.assertEqual(stat_names, [])

    async def test_delete_failures_keep_result_and_fast_retry_at_high_attempts(self):
        cases = (
            (
                "Forbidden",
                "missing permissions",
                "forbidden",
                "RuntimeError: Forbidden: missing permissions",
            ),
            (
                "HTTPException",
                "temporarily unavailable",
                "transient_failure",
                "RuntimeError: HTTPException: temporarily unavailable",
            ),
        )
        for exception_name, message, expected_result, expected_error in cases:
            with (
                self.subTest(result=expected_result),
                TemporaryDirectory() as directory,
            ):
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    handler_module = import_module("Honeypot.operations.source_delete")
                    now = datetime.now(timezone.utc)
                    exception_type = getattr(honeypot.discord, exception_name)

                    async def delete_message():
                        raise exception_type(message)

                    async def fetch_message(message_id):
                        return SimpleNamespace(delete=delete_message)

                    channel = SimpleNamespace(fetch_message=fetch_message)
                    guild = SimpleNamespace(
                        get_channel=lambda channel_id: channel,
                        get_thread=lambda channel_id: None,
                    )
                    bot = _Bot()
                    bot.get_guild = lambda guild_id: guild
                    bot.get_channel = lambda channel_id: None
                    cog = honeypot.Honeypot(bot)
                    appended = self._append_source(honeypot, cog, now)
                    cog._case_store.update_message_delete(
                        appended.case.case_id,
                        appended.message.sequence,
                        honeypot.DeleteStatus.FORBIDDEN,
                        "initial delete failed",
                        False,
                    )
                    operation = cog._case_store.ensure_operation(
                        appended.case.case_id,
                        honeypot.OperationType.SOURCE_DELETE,
                        f"source_delete:{appended.case.case_id}:30:40",
                        appended.message.sequence,
                    )
                    claimed = cog._case_store.claim_operation(
                        operation.operation_id, now
                    )
                    while claimed.attempts < 6:
                        self.assertTrue(
                            cog._case_store.fail_operation(
                                claimed.operation_id,
                                claimed.claim_token,
                                "previous failure",
                                now,
                                now,
                            )
                        )
                        claimed = cog._case_store.claim_operation(
                            operation.operation_id, now
                        )
                    self.assertEqual(claimed.attempts, 6)

                    await cog._execute_detection_case_operation(claimed, now)

                    snapshot = cog._case_store.get_case(appended.case.case_id)
                    failed = next(
                        item
                        for item in snapshot.operations
                        if item.operation_id == operation.operation_id
                    )
                    self.assertIs(failed.status, honeypot.OperationStatus.FAILED)
                    self.assertEqual(failed.attempts, 6)
                    self.assertEqual(failed.result, expected_result)
                    self.assertEqual(failed.last_error, expected_error)
                    self.assertEqual(
                        failed.retry_at - failed.updated_at,
                        timedelta(seconds=10),
                    )
                    self.assertFalse(snapshot.case.needs_attention)

    async def test_post_result_failures_keep_deleted_result_for_retry(self):
        for failing_step in ("store_completion", "stat_update"):
            with (
                self.subTest(failing_step=failing_step),
                TemporaryDirectory() as directory,
            ):
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    handler_module = import_module("Honeypot.operations.source_delete")
                    now = datetime.now(timezone.utc)

                    async def delete_message():
                        return None

                    async def fetch_message(message_id):
                        return SimpleNamespace(delete=delete_message)

                    channel = SimpleNamespace(fetch_message=fetch_message)
                    guild = SimpleNamespace(
                        get_channel=lambda channel_id: (
                            channel if channel_id == 30 else None
                        ),
                        get_thread=lambda channel_id: None,
                    )
                    bot = _Bot()
                    bot.get_guild = lambda guild_id: guild
                    bot.get_channel = lambda channel_id: None
                    cog = honeypot.Honeypot(bot)
                    appended = self._append_source(honeypot, cog, now)
                    cog._case_store.update_message_delete(
                        appended.case.case_id,
                        appended.message.sequence,
                        honeypot.DeleteStatus.FORBIDDEN,
                        "initial delete failed",
                        False,
                    )
                    operation = cog._case_store.ensure_operation(
                        appended.case.case_id,
                        honeypot.OperationType.SOURCE_DELETE,
                        f"source_delete:{appended.case.case_id}:30:40",
                        appended.message.sequence,
                    )
                    claimed = cog._case_store.claim_operation(
                        operation.operation_id, now
                    )
                    expected_error = RuntimeError(f"{failing_step} failed")

                    if failing_step == "store_completion":

                        def fail_completion(case_id, message_sequence, status):
                            raise expected_error

                        cog._case_store.complete_message_delete_retry = fail_completion

                        context = honeypot.OperationContext(
                            operation=claimed,
                            snapshot=cog._case_store.get_case(
                                appended.case.case_id
                            ),
                            lease=honeypot.OperationLease(
                                operation_id=claimed.operation_id,
                                claim_token=claimed.claim_token,
                            ),
                            now=now,
                        )
                        outcome = await handler_module.source_delete_handler(
                            cog, context
                        )
                        self.assertEqual(outcome.result, "deleted")
                        self.assertIs(outcome.error, expected_error)
                    else:

                        async def fail_stat(stat_guild, name):
                            raise expected_error

                        cog._increment_stat = fail_stat

                    await cog._execute_detection_case_operation(claimed, now)

                    snapshot = cog._case_store.get_case(appended.case.case_id)
                    failed = next(
                        item
                        for item in snapshot.operations
                        if item.operation_id == operation.operation_id
                    )
                    self.assertIs(failed.status, honeypot.OperationStatus.FAILED)
                    self.assertEqual(failed.result, "deleted")
                    self.assertEqual(
                        failed.last_error,
                        f"RuntimeError: {failing_step} failed",
                    )
                    self.assertEqual(
                        failed.retry_at - failed.updated_at,
                        timedelta(seconds=10),
                    )
                    expected_message_status = (
                        honeypot.DeleteStatus.FORBIDDEN
                        if failing_step == "store_completion"
                        else honeypot.DeleteStatus.DELETED
                    )
                    self.assertIs(
                        snapshot.messages[0].delete_status,
                        expected_message_status,
                    )

    async def test_stats_require_new_delete_and_matching_forward_signal(self):
        cases = (
            ("matching_forward", ("purged_messages", "forward_purge_deletes")),
            ("other_message_forward", ("purged_messages",)),
            ("duplicate_completion", ()),
        )
        for scenario, expected_stats in cases:
            with (
                self.subTest(scenario=scenario),
                TemporaryDirectory() as directory,
            ):
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    handler_module = import_module("Honeypot.operations.source_delete")
                    now = datetime.now(timezone.utc)
                    forward_signal = honeypot.DetectionSignal(
                        detector="forward_purge",
                        reason="active containment",
                        action=honeypot.ActionIntent.REVIEW,
                        decisive=True,
                        metadata={},
                    )
                    source_signals = (
                        (forward_signal,) if scenario == "matching_forward" else ()
                    )

                    async def delete_message():
                        return None

                    async def fetch_message(message_id):
                        return SimpleNamespace(delete=delete_message)

                    channel = SimpleNamespace(fetch_message=fetch_message)
                    guild = SimpleNamespace(
                        get_channel=lambda channel_id: channel,
                        get_thread=lambda channel_id: None,
                    )
                    bot = _Bot()
                    bot.get_guild = lambda guild_id: guild
                    bot.get_channel = lambda channel_id: None
                    cog = honeypot.Honeypot(bot)
                    appended = self._append_source(
                        honeypot, cog, now, signals=source_signals
                    )
                    if scenario == "other_message_forward":
                        cog._case_store.append_message(
                            honeypot.NewMessage(
                                guild_id=10,
                                user_id=20,
                                channel_id=31,
                                message_id=41,
                                content="other evidence",
                                created_at=now,
                                jump_url="https://discord.test/messages/41",
                                attachments=(),
                            ),
                            (forward_signal,),
                        )
                    cog._case_store.update_message_delete(
                        appended.case.case_id,
                        appended.message.sequence,
                        honeypot.DeleteStatus.FORBIDDEN,
                        "initial delete failed",
                        False,
                    )
                    if scenario == "duplicate_completion":
                        self.assertTrue(
                            cog._case_store.complete_message_delete_retry(
                                appended.case.case_id,
                                appended.message.sequence,
                                honeypot.DeleteStatus.DELETED,
                            )
                        )
                    operation = cog._case_store.ensure_operation(
                        appended.case.case_id,
                        honeypot.OperationType.SOURCE_DELETE,
                        f"source_delete:{appended.case.case_id}:30:40",
                        appended.message.sequence,
                    )
                    claimed = cog._case_store.claim_operation(
                        operation.operation_id, now
                    )
                    snapshot = cog._case_store.get_case(appended.case.case_id)
                    context = honeypot.OperationContext(
                        operation=claimed,
                        snapshot=snapshot,
                        lease=honeypot.OperationLease(
                            operation_id=claimed.operation_id,
                            claim_token=claimed.claim_token,
                        ),
                        now=now,
                    )
                    stat_names = []

                    async def increment_stat(stat_guild, name):
                        stat_names.append(name)

                    cog._increment_stat = increment_stat

                    outcome = await handler_module.source_delete_handler(cog, context)

                    self.assertEqual(outcome.result, "deleted")
                    self.assertIsNone(outcome.error)
                    self.assertEqual(tuple(stat_names), expected_stats)

    async def test_pre_result_identity_and_key_errors_are_unchanged(self):
        cases = (
            (
                "guild_unavailable",
                None,
                None,
                True,
                RuntimeError,
                "detection case guild is unavailable",
            ),
            (
                "case_mismatch",
                object(),
                "source_delete:other-case:30:40",
                True,
                RuntimeError,
                "source delete operation case identity does not match",
            ),
            (
                "malformed_key",
                object(),
                "source_delete:malformed",
                True,
                ValueError,
                None,
            ),
            (
                "invalid_channel_id",
                object(),
                None,
                True,
                ValueError,
                None,
            ),
            (
                "missing_message_identity",
                object(),
                None,
                False,
                RuntimeError,
                "source delete operation has no message identity",
            ),
        )
        for (
            name,
            guild,
            idempotency_key,
            has_message_identity,
            expected_type,
            expected_message,
        ) in cases:
            with (
                self.subTest(case=name),
                TemporaryDirectory() as directory,
            ):
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    handler_module = import_module("Honeypot.operations.source_delete")
                    now = datetime.now(timezone.utc)
                    bot = _Bot()
                    bot.get_guild = lambda guild_id: guild
                    bot.get_channel = lambda channel_id: None
                    cog = honeypot.Honeypot(bot)
                    appended = self._append_source(honeypot, cog, now)
                    if idempotency_key is None:
                        channel_id = (
                            "not-an-integer"
                            if name == "invalid_channel_id"
                            else "30"
                        )
                        idempotency_key = (
                            f"source_delete:{appended.case.case_id}:{channel_id}:40"
                        )
                    operation = cog._case_store.ensure_operation(
                        appended.case.case_id,
                        honeypot.OperationType.SOURCE_DELETE,
                        idempotency_key,
                        (
                            appended.message.sequence
                            if has_message_identity
                            else None
                        ),
                    )
                    claimed = cog._case_store.claim_operation(
                        operation.operation_id, now
                    )
                    context = honeypot.OperationContext(
                        operation=claimed,
                        snapshot=cog._case_store.get_case(appended.case.case_id),
                        lease=honeypot.OperationLease(
                            operation_id=claimed.operation_id,
                            claim_token=claimed.claim_token,
                        ),
                        now=now,
                    )

                    with self.assertRaises(expected_type) as captured:
                        await handler_module.source_delete_handler(cog, context)

                    if expected_message is not None:
                        self.assertEqual(str(captured.exception), expected_message)

    async def test_fetch_helper_failure_reaches_executor_without_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                guild = SimpleNamespace(
                    get_channel=lambda channel_id: None,
                    get_thread=lambda channel_id: None,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                bot.get_channel = lambda channel_id: None
                cog = honeypot.Honeypot(bot)
                appended = self._append_source(honeypot, cog, now)
                cog._case_store.update_message_delete(
                    appended.case.case_id,
                    appended.message.sequence,
                    honeypot.DeleteStatus.FORBIDDEN,
                    "initial delete failed",
                    False,
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.SOURCE_DELETE,
                    f"source_delete:{appended.case.case_id}:30:40",
                    appended.message.sequence,
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                failed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertIs(failed.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(failed.result)
                self.assertEqual(
                    failed.last_error,
                    "RuntimeError: detection source channel cannot be fetched",
                )
                self.assertEqual(
                    failed.retry_at - failed.updated_at,
                    timedelta(seconds=10),
                )
