import unittest
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tests.harness import _Bot, _isolated_honeypot_modules


class CachedPurgeHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _append_case(honeypot, cog, created_at):
        cog._case_store.initialize()
        cog._message_registry._initialize_sync()
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
            (),
        )

    @classmethod
    def _claim_context(cls, honeypot, cog, now, *, idempotency_key=None):
        appended = cls._append_case(honeypot, cog, now)
        operation = cog._case_store.ensure_operation(
            appended.case.case_id,
            honeypot.OperationType.CACHED_PURGE,
            idempotency_key
            or f"cached_purge:{appended.case.case_id}:399:299",
            appended.message.sequence,
        )
        claimed = cog._case_store.claim_operation(operation.operation_id, now)
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
        return appended, operation, claimed, context

    async def test_registered_handler_deletes_and_completes_with_durable_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                try:
                    handler_module = import_module("Honeypot.operations.cached_purge")
                except ModuleNotFoundError:
                    self.fail("cached_purge has no dedicated handler module")
                operations = import_module("Honeypot.operations")
                evidence_cleanup = import_module(
                    "Honeypot.operations.evidence_cleanup"
                )
                moderation = import_module("Honeypot.operations.moderation")
                moderator_decision = import_module(
                    "Honeypot.operations.moderator_decision"
                )
                message_process = import_module(
                    "Honeypot.operations.message_process"
                )
                review_publish = import_module("Honeypot.operations.review_publish")
                review_update = import_module("Honeypot.operations.review_update")
                role_apply = import_module("Honeypot.operations.role_apply")
                role_release = import_module("Honeypot.operations.role_release")
                source_delete = import_module("Honeypot.operations.source_delete")
                now = datetime.now(timezone.utc)
                resolved_message_ids = []
                deleted_message_ids = []

                async def delete_message():
                    deleted_message_ids.append(299)

                def get_partial_message(message_id):
                    resolved_message_ids.append(message_id)
                    return SimpleNamespace(delete=delete_message)

                channel = SimpleNamespace(
                    get_partial_message=get_partial_message
                )
                guild = SimpleNamespace(
                    get_channel=lambda channel_id: channel if channel_id == 399 else None,
                    get_thread=lambda channel_id: None,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild if guild_id == 10 else None
                cog = honeypot.Honeypot(bot)
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.CACHED_PURGE,
                    f"cached_purge:{appended.case.case_id}:399:299",
                    appended.message.sequence,
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                self.assertEqual(
                    dict(operations.HANDLERS),
                    {
                        honeypot.OperationType.MESSAGE_PROCESS: (
                            message_process.message_process_handler
                        ),
                        honeypot.OperationType.REVIEW_UPDATE: (
                            review_update.review_update_handler
                        ),
                        honeypot.OperationType.REVIEW_PUBLISH: (
                            review_publish.review_publish_handler
                        ),
                        honeypot.OperationType.CACHED_PURGE: (
                            handler_module.cached_purge_handler
                        ),
                        honeypot.OperationType.SOURCE_DELETE: (
                            source_delete.source_delete_handler
                        ),
                        honeypot.OperationType.EVIDENCE_CLEANUP: (
                            evidence_cleanup.evidence_cleanup_handler
                        ),
                        honeypot.OperationType.ROLE_RELEASE: (
                            role_release.role_release_handler
                        ),
                        honeypot.OperationType.ROLE_APPLY: (
                            role_apply.role_apply_handler
                        ),
                        honeypot.OperationType.MODERATION_ACTION: (
                            moderation.moderation_action_handler
                        ),
                        honeypot.OperationType.MODERATOR_BAN: (
                            moderator_decision.moderator_decision_handler
                        ),
                        honeypot.OperationType.MODERATOR_KICK: (
                            moderator_decision.moderator_decision_handler
                        ),
                    },
                )
                self.assertIs(
                    cog._detection_operation_handlers.resolve(
                        honeypot.OperationType.CACHED_PURGE
                    ),
                    handler_module.cached_purge_handler,
                )

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(resolved_message_ids, [299])
                self.assertEqual(deleted_message_ids, [299])
                self.assertIs(completed.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertEqual(completed.result, "deleted")

    async def test_channel_resolution_failures_return_partial_result_and_exact_error(
        self,
    ):
        cases = (
            (
                None,
                "channel_unavailable",
                "cached purge channel is unavailable",
            ),
            (
                SimpleNamespace(id=399),
                "unsupported_channel",
                "cached purge channel cannot resolve messages",
            ),
        )
        for channel, expected_result, expected_error in cases:
            with self.subTest(result=expected_result), TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    handler_module = import_module("Honeypot.operations.cached_purge")
                    guild = SimpleNamespace(
                        get_channel=lambda channel_id: channel,
                        get_thread=lambda channel_id: None,
                    )
                    bot = _Bot()
                    bot.get_guild = lambda guild_id: guild
                    cog = honeypot.Honeypot(bot)
                    now = datetime.now(timezone.utc)
                    _appended, _operation, _claimed, context = self._claim_context(
                        honeypot, cog, now
                    )

                    try:
                        outcome = await handler_module.cached_purge_handler(cog, context)
                    except Exception as error:
                        self.fail(
                            "cached purge handler raised before returning its partial "
                            f"result: {error!r}"
                        )

                    self.assertEqual(outcome.result, expected_result)
                    self.assertIsInstance(outcome.error, RuntimeError)
                    self.assertEqual(str(outcome.error), expected_error)

    async def test_delete_failures_return_runtime_status_and_exact_error(self):
        cases = (
            (
                "Forbidden",
                "missing permissions",
                "forbidden",
                "Forbidden: missing permissions",
            ),
            (
                "HTTPException",
                "temporary unavailable",
                "transient_failure",
                "HTTPException: temporary unavailable",
            ),
        )
        for exception_name, exception_message, expected_result, expected_error in cases:
            with self.subTest(result=expected_result), TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    handler_module = import_module("Honeypot.operations.cached_purge")
                    exception_type = getattr(honeypot.discord, exception_name)

                    async def delete_message():
                        raise exception_type(exception_message)

                    channel = SimpleNamespace(
                        get_partial_message=lambda message_id: SimpleNamespace(
                            delete=delete_message
                        )
                    )
                    guild = SimpleNamespace(
                        get_channel=lambda channel_id: channel,
                        get_thread=lambda channel_id: None,
                    )
                    bot = _Bot()
                    bot.get_guild = lambda guild_id: guild
                    cog = honeypot.Honeypot(bot)
                    now = datetime.now(timezone.utc)
                    _appended, _operation, _claimed, context = self._claim_context(
                        honeypot, cog, now
                    )

                    outcome = await handler_module.cached_purge_handler(cog, context)

                    self.assertEqual(outcome.result, expected_result)
                    self.assertIsInstance(outcome.error, RuntimeError)
                    self.assertEqual(str(outcome.error), expected_error)

    async def test_pre_result_failures_preserve_identity_and_parsing_contracts(self):
        cases = (
            (
                "guild_unavailable",
                None,
                None,
                RuntimeError,
                "detection case guild is unavailable",
            ),
            (
                "case_mismatch",
                SimpleNamespace(
                    get_channel=lambda channel_id: None,
                    get_thread=lambda channel_id: None,
                ),
                "cached_purge:other-case:399:299",
                RuntimeError,
                "cached purge operation case identity does not match",
            ),
            (
                "malformed_key",
                SimpleNamespace(
                    get_channel=lambda channel_id: None,
                    get_thread=lambda channel_id: None,
                ),
                "cached_purge:malformed",
                ValueError,
                None,
            ),
        )
        for name, guild, idempotency_key, expected_type, expected_message in cases:
            with self.subTest(case=name), TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    handler_module = import_module("Honeypot.operations.cached_purge")
                    bot = _Bot()
                    bot.get_guild = lambda guild_id: guild
                    cog = honeypot.Honeypot(bot)
                    now = datetime.now(timezone.utc)
                    _appended, _operation, _claimed, context = self._claim_context(
                        honeypot,
                        cog,
                        now,
                        idempotency_key=idempotency_key,
                    )

                    try:
                        await handler_module.cached_purge_handler(cog, context)
                    except Exception as error:
                        captured = error
                    else:
                        self.fail("cached purge handler unexpectedly succeeded")

                    self.assertIsInstance(captured, expected_type)
                    if expected_message is not None:
                        self.assertEqual(str(captured), expected_message)
