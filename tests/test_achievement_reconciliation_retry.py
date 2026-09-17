import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.test_gatecount import nhmisc


class MaintenanceChannel:
    def __init__(self):
        self.id = 20
        self.public = False
        self.messages = []
        self.edited = asyncio.Event()

    def permissions_for(self, _member):
        return SimpleNamespace(view_channel=self.public)

    async def send(self, content, **kwargs):
        message = SimpleNamespace(content=content, channel=self, guild=self.guild, id=30)

        async def edit(*, content, **kwargs):
            message.content = content
            self.edited.set()
            return message

        message.edit = edit
        self.messages.append(message)
        return message


class AchievementReconciliationRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.before_tasks = asyncio.all_tasks()
        self.release = asyncio.Event()
        self.sleeping = asyncio.Event()
        self.delays = []

        async def sleep(delay):
            self.delays.append(delay)
            self.sleeping.set()
            await self.release.wait()

        self.channel = MaintenanceChannel()
        role = SimpleNamespace(id=nhmisc.GATE_TIER_ROLE_IDS[0], managed=False, position=1)
        self.member = SimpleNamespace(
            id=10, roles=[], top_role=SimpleNamespace(position=1), edit=mock.AsyncMock(),
        )
        self.guild = SimpleNamespace(
            id=1, default_role=SimpleNamespace(id=1),
            me=SimpleNamespace(top_role=SimpleNamespace(position=100),
                               guild_permissions=SimpleNamespace(manage_roles=True)),
            get_role=lambda _: role, get_channel=lambda _: self.channel,
            fetch_member=mock.AsyncMock(side_effect=[nhmisc.discord.HTTPException(), self.member]),
        )
        self.channel.guild = self.guild
        self.cog = object.__new__(nhmisc.NHMisc)
        self.cog.bot = SimpleNamespace(guilds=[self.guild])
        self.cog._achievement_reconciliations = {}
        self.cog._achievement_reconciliation_runs = set()
        self.cog._achievement_reconciliation_closing = False
        self.cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=()),
            list_gate_projections=mock.AsyncMock(return_value={10: 1}),
        )
        self.cog._role_analytics = SimpleNamespace(reconcile_enabled_guilds=mock.AsyncMock())
        self.cog._role_analytics_store = SimpleNamespace(matching_user_ids=mock.AsyncMock(return_value=()))
        support = object.__new__(nhmisc.OperationalSupport)
        support.log_config = SimpleNamespace(
            guild=lambda _: SimpleNamespace(maintenance_channel=mock.AsyncMock(return_value=20)),
        )
        support.operational_errors = SimpleNamespace(report=mock.AsyncMock())
        self.cog._support = support
        self.patches = (
            mock.patch.object(nhmisc, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": sleep}))),
            mock.patch.object(nhmisc.time, "time", return_value=1_800_000_000),
            mock.patch.object(nhmisc.log, "exception"),
            mock.patch.object(support.get_log_channel.__globals__["discord"],
                              "TextChannel", MaintenanceChannel, create=True),
        )
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        tasks = asyncio.all_tasks() - self.before_tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for patch in reversed(self.patches):
            patch.stop()

    async def test_failed_member_is_retried_in_one_hour_and_same_message_is_updated(self):
        await self.cog.on_resumed()
        self.assertEqual(len(self.channel.messages), 1)
        self.assertIn("Retrying <t:1800003600:R>", self.channel.messages[0].content)
        await asyncio.wait_for(self.sleeping.wait(), 1)
        self.assertEqual(self.delays, [3600])
        self.release.set()
        await asyncio.wait_for(self.channel.edited.wait(), 1)
        self.assertEqual(len(self.channel.messages), 1)
        self.assertIn("Members corrected: 1", self.channel.messages[0].content)
        self.assertTrue(self.channel.messages[0].content.endswith("Retry completed"))
        self.member.edit.assert_awaited_once()

    async def test_failed_retry_updates_message_without_scheduling_another(self):
        self.guild.fetch_member.side_effect = nhmisc.discord.HTTPException()
        await self.cog.on_resumed()
        await asyncio.wait_for(self.sleeping.wait(), 1)
        self.release.set()
        await asyncio.wait_for(self.channel.edited.wait(), 1)
        self.assertTrue(self.channel.messages[0].content.endswith("Retry failed"))
        self.assertEqual(self.delays, [3600])
        self.assertEqual(self.guild.fetch_member.await_count, 2)
        self.assertEqual(len(self.channel.messages), 1)

    async def test_role_hierarchy_failure_is_counted_as_skipped(self):
        self.member.top_role.position = 100
        self.guild.fetch_member.side_effect = None
        self.guild.fetch_member.return_value = self.member
        await self.cog.on_resumed()
        self.assertEqual(len(self.channel.messages), 1)
        self.assertIn("Members skipped: 1", self.channel.messages[0].content)
        self.assertIn("Retrying <t:", self.channel.messages[0].content)
        self.member.edit.assert_not_awaited()

    async def test_aborted_pass_is_reported_and_retried(self):
        self.cog._achievement_store.list_definitions.side_effect = [OSError("database unavailable"), ()]
        self.guild.fetch_member.side_effect = None
        self.guild.fetch_member.return_value = self.member
        await self.cog.on_resumed()
        self.assertTrue(self.channel.messages[0].content.startswith("Achievement role reconciliation failed"))
        self.assertIn("Members skipped: unknown", self.channel.messages[0].content)
        self.assertIn("Retrying <t:", self.channel.messages[0].content)
        await asyncio.wait_for(self.sleeping.wait(), 1)
        self.release.set()
        await asyncio.wait_for(self.channel.edited.wait(), 1)
        self.assertTrue(self.channel.messages[0].content.endswith("Retry completed"))

    async def test_departed_members_do_not_schedule_retry(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="solo_gater", display_name="Solo Gater",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind, role_id=123,
        )
        self.cog._achievement_store.list_definitions.return_value = (definition,)
        self.cog._achievement_store.projected_users_for_boolean = mock.AsyncMock(return_value=(10,))
        self.guild.fetch_member.side_effect = nhmisc.discord.NotFound()
        await self.cog.on_resumed()
        self.assertEqual(self.channel.messages, [])
        self.assertEqual(self.delays, [])
        self.assertEqual(self.guild.fetch_member.await_count, 2)

    async def test_repeated_sync_keeps_one_retry_and_original_deadline(self):
        self.guild.fetch_member.side_effect = nhmisc.discord.HTTPException()
        await self.cog.on_resumed()
        await asyncio.wait_for(self.sleeping.wait(), 1)
        await self.cog.on_resumed()
        self.assertEqual(self.delays, [3600])
        self.assertEqual(len(self.channel.messages), 1)
        self.assertIn("Retrying <t:1800003600:R>", self.channel.messages[0].content)
        self.channel.edited.clear()
        self.release.set()
        await asyncio.wait_for(self.channel.edited.wait(), 1)
        self.assertTrue(self.channel.messages[0].content.endswith("Retry failed"))

    async def test_successful_manual_sync_cancels_pending_retry(self):
        await self.cog.on_resumed()
        await asyncio.wait_for(self.sleeping.wait(), 1)
        self.cog._role_analytics.sync_guild = mock.AsyncMock(return_value=SimpleNamespace(
            member_count=1, membership_count=1, elapsed_seconds=0.1,
        ))
        self.cog._role_analytics.is_syncing = lambda _: False
        await self.cog.rolesync(SimpleNamespace(guild=self.guild, send=mock.AsyncMock()))
        self.assertTrue(self.channel.messages[0].content.endswith("Retry completed"))
        self.release.set()
        await asyncio.sleep(0)
        self.assertEqual(self.guild.fetch_member.await_count, 2)

    def prepare_unload(self):
        self.cog._audit_log_tasks = set()
        self.cog._activity_task = None
        self.cog._role_analytics_startup_task = None
        self.cog._role_analytics_daily_task = None
        self.cog._gate_increment_recovery_task = None
        self.cog._gate_increment_context_registered = False
        self.cog._achievement_commands_registered = False
        self.cog._role_analytics.shutdown = mock.AsyncMock()

    async def test_unload_cancels_retry_and_removes_countdown(self):
        self.prepare_unload()
        await self.cog.on_resumed()
        await asyncio.wait_for(self.sleeping.wait(), 1)
        await self.cog.cog_unload()
        self.assertTrue(self.channel.messages[0].content.endswith("Retry cancelled"))
        self.release.set()
        await asyncio.sleep(0)
        self.assertEqual(self.guild.fetch_member.await_count, 1)

    async def test_unload_stops_inflight_sync_before_it_can_schedule_retry(self):
        self.prepare_unload()
        fetching = asyncio.Event()
        finish_fetch = asyncio.Event()

        async def fetch(_user_id):
            fetching.set()
            await finish_fetch.wait()
            raise nhmisc.discord.HTTPException()

        self.guild.fetch_member.side_effect = fetch
        running = asyncio.create_task(self.cog.on_resumed())
        await asyncio.wait_for(fetching.wait(), 1)
        await self.cog.cog_unload()
        finish_fetch.set()
        await asyncio.gather(running, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(self.channel.messages, [])
        self.assertEqual(self.delays, [])
