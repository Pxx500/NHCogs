"""Safety policy at the Discord punishment boundary."""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.harness import _Bot, _isolated_honeypot_modules


class PunitiveEffectPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_current_dry_run_plans(self, action: str) -> None:
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                from Honeypot.effects import EffectStatus

                cog = honeypot.Honeypot(_Bot())
                current_config = SimpleNamespace(
                    all=mock.AsyncMock(return_value={"dry_run": True})
                )
                cog.config = SimpleNamespace(
                    guild=mock.Mock(return_value=current_config)
                )
                cog._increment_stat = mock.AsyncMock()
                cog._missing_action_permission = mock.Mock(return_value=None)
                cog._schedule_post_ban_sweep = mock.Mock()
                honeypot.detection._activate_forward_purge = mock.Mock()

                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=11),
                    ban=mock.AsyncMock(),
                )
                member = SimpleNamespace(
                    id=20,
                    ban=mock.AsyncMock(),
                    kick=mock.AsyncMock(),
                )
                modlog_create_case = mock.AsyncMock()
                honeypot.detection.modlog.create_case = modlog_create_case
                stale_settings = honeypot.GuildSettings.from_mapping(
                    {"dry_run": False}
                )

                result = await cog._execute_action(
                    guild,
                    member,
                    datetime.now(timezone.utc),
                    stale_settings,
                    reason="Punishment safety test",
                    action=action,
                )

                member.ban.assert_not_awaited()
                member.kick.assert_not_awaited()
                modlog_create_case.assert_not_awaited()
                self.assertEqual(result.status, EffectStatus.PLANNED)

    async def test_current_dry_run_blocks_ban_with_stale_settings(self):
        await self._assert_current_dry_run_plans("ban")

    async def test_current_dry_run_blocks_kick_with_stale_settings(self):
        await self._assert_current_dry_run_plans("kick")
