import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules


class _GuildConfig:
    def __init__(self, values, stats):
        self._values = values
        self._stats = stats

    async def all(self):
        return dict(self._values)

    @asynccontextmanager
    async def stats(self):
        yield self._stats


class _Config:
    def __init__(self, values, *, current_values=None):
        self._values = values
        self._current_values = current_values or values
        self._stats = {}

    def guild(self, guild):
        return _GuildConfig(self._current_values, self._stats)

    def guild_from_id(self, guild_id):
        return _GuildConfig(self._values, self._stats)


class ModeratorDecisionHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _append_case(honeypot, cog, created_at, *, pending_attachment=False):
        cog._case_store.initialize()
        attachments = (
            (
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
            )
            if pending_attachment
            else ()
        )
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=10,
                user_id=20,
                channel_id=30,
                message_id=40,
                content="evidence",
                created_at=created_at,
                jump_url="https://discord.test/messages/40",
                attachments=attachments,
            ),
            (),
        )

    @staticmethod
    def _claim_moderator_action(honeypot, cog, appended, action, actor_id, now):
        operation = cog._case_store.claim_moderator_action(
            appended.case.case_id,
            action,
            actor_id,
            now,
        )
        if operation is None:
            raise AssertionError("moderator action was not created")
        claimed = cog._case_store.claim_operation(operation.operation_id, now)
        if claimed is None:
            raise AssertionError("moderator action was not claimable")
        return claimed

    @staticmethod
    def _context(honeypot, cog, claimed, now):
        return honeypot.OperationContext(
            operation=claimed,
            snapshot=cog._case_store.get_case(claimed.case_id),
            lease=honeypot.OperationLease(
                operation_id=claimed.operation_id,
                claim_token=claimed.claim_token,
            ),
            now=now,
        )

    async def test_dry_run_ban_returns_exact_planned_result(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog.config = _Config({"dry_run": True})
                appended = self._append_case(honeypot, cog, now)
                claimed = self._claim_moderator_action(
                    honeypot, cog, appended, "ban", 777, now
                )
                registry = import_module(
                    "Honeypot.operations"
                ).OperationHandlerRegistry()
                handler = registry.resolve(honeypot.OperationType.MODERATOR_BAN)

                self.assertIsNotNone(handler)
                outcome = await handler(
                    cog,
                    self._context(honeypot, cog, claimed, now),
                )

                self.assertEqual(outcome.result, "planned_ban")

    async def test_effect_boundary_dry_run_persists_moderator_kick_as_planned(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                member = SimpleNamespace(
                    id=20,
                    kick=mock.AsyncMock(
                        side_effect=AssertionError("planned kick must not execute")
                    ),
                )
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog.config = _Config(
                    {"dry_run": False},
                    current_values={"dry_run": True},
                )
                appended = self._append_case(honeypot, cog, now)
                claimed = self._claim_moderator_action(
                    honeypot, cog, appended, "kick", 777, now
                )

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == claimed.operation_id
                )
                self.assertEqual(
                    (persisted.status, persisted.result),
                    (honeypot.OperationStatus.SUCCEEDED, "planned_kick"),
                )

class ModeratorIgnoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_ignore_is_succeeded_without_a_dispatch_claim(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog.config = _Config({})
                appended = ModeratorDecisionHandlerTests._append_case(
                    honeypot, cog, now, pending_attachment=True
                )
                module = import_module(
                    "Honeypot.operations.moderator_decision"
                )
                apply_ignore = getattr(module, "apply_moderator_ignore", None)

                self.assertIsNotNone(apply_ignore)
                await apply_ignore(
                    cog,
                    appended.case.case_id,
                    777,
                    now,
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type
                    is honeypot.OperationType.MODERATOR_IGNORE
                )
                self.assertEqual(
                    (
                        operation.status,
                        operation.result,
                        operation.actor_id,
                        operation.attempts,
                        operation.claim_token,
                    ),
                    (
                        honeypot.OperationStatus.SUCCEEDED,
                        "ignore",
                        777,
                        1,
                        None,
                    ),
                )
