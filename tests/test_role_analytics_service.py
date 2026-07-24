import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PACKAGE_NAME = "nhmisc_role_analytics_service_test_package"
PACKAGE_PATH = Path(__file__).parents[1] / "NHMisc"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package


def load_module(name):
    qualified_name = f"{PACKAGE_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(
        qualified_name, PACKAGE_PATH / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)
    return module


role_analytics_store = load_module("role_analytics_store")
role_expression = load_module("role_expression")
role_analytics_service = load_module("role_analytics_service")


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeMember:
    def __init__(self, user_id, role_ids, *, bot=False):
        self.id = user_id
        self.bot = bot
        self.roles = [FakeRole(role_id) for role_id in role_ids]


class FakeGuild:
    def __init__(self, members, *, chunked=True):
        self.id = 123
        self.default_role = FakeRole(123)
        self.members = members
        self.chunked = chunked
        self.chunk_calls = 0

    async def chunk(self, *, cache):
        self.chunk_calls += 1
        self.chunked = True


class FakeBot:
    def __init__(self, *, members_intent=True):
        self.intents = types.SimpleNamespace(members=members_intent)


class GatedStore:
    def __init__(self, store):
        self._store = store
        self.write_started = __import__("asyncio").Event()
        self.allow_write = __import__("asyncio").Event()

    def __getattr__(self, name):
        return getattr(self._store, name)

    async def write_generation(self, guild_id, generation, members):
        self.write_started.set()
        await self.allow_write.wait()
        await self._store.write_generation(guild_id, generation, members)


class RoleAnalyticsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = role_analytics_store.RoleAnalyticsStore(
            Path(self.temp_dir.name) / "role_analytics.sqlite"
        )
        await self.store.initialize()

    async def test_manual_sync_reuses_complete_cache_without_chunk_request(self):
        guild = FakeGuild(
            [FakeMember(1, (123, 10)), FakeMember(2, (123, 20))],
            chunked=True,
        )
        service = role_analytics_service.RoleAnalyticsService(
            FakeBot(), self.store
        )

        result = await service.sync_guild(guild, manual=True)

        self.assertEqual(guild.chunk_calls, 0)
        self.assertEqual(result.member_count, 2)
        self.assertEqual(result.membership_count, 2)
        state = await self.store.get_state(guild.id)
        self.assertEqual(state.status, role_analytics_store.SyncStatus.READY)

    async def test_member_event_during_snapshot_is_replayed_before_activation(self):
        guild = FakeGuild([FakeMember(1, (123, 10))], chunked=True)
        gated_store = GatedStore(self.store)
        service = role_analytics_service.RoleAnalyticsService(
            FakeBot(), gated_store
        )
        sync_task = __import__("asyncio").create_task(
            service.sync_guild(guild, manual=True)
        )
        await gated_store.write_started.wait()

        await service.member_joined(
            guild.id,
            FakeMember(1, (123, 20)),
            guild.default_role.id,
        )
        gated_store.allow_write.set()
        await sync_task

        sql, parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("20")
        )
        self.assertEqual(
            await self.store.count_matching(guild.id, sql, parameters), 1
        )

    async def test_full_member_request_is_not_repeated_inside_thirty_seconds(self):
        guild = FakeGuild([FakeMember(1, (123, 10))], chunked=False)
        service = role_analytics_service.RoleAnalyticsService(
            FakeBot(), self.store, monotonic=lambda: 100.0
        )
        await service.sync_guild(guild, manual=True)
        guild.chunked = False

        with self.assertRaises(
            role_analytics_service.FullMemberRequestCooldownError
        ) as raised:
            await service.sync_guild(guild, manual=True)

        self.assertEqual(guild.chunk_calls, 1)
        self.assertEqual(raised.exception.retry_after, 30.0)

    async def test_cold_start_reconciles_enabled_guild_but_ignores_disabled_guild(self):
        enabled_guild = FakeGuild([FakeMember(1, (123, 10))])
        first_service = role_analytics_service.RoleAnalyticsService(
            FakeBot(), self.store
        )
        first_result = await first_service.sync_guild(enabled_guild, manual=True)
        disabled_guild = FakeGuild([FakeMember(2, (123, 20))])
        disabled_guild.id = 456
        restarted_service = role_analytics_service.RoleAnalyticsService(
            FakeBot(), self.store
        )

        results = await restarted_service.reconcile_enabled_guilds(
            [enabled_guild, disabled_guild]
        )

        self.assertEqual(len(results), 1)
        self.assertGreater(results[0].generation, first_result.generation)
        self.assertEqual(
            (await self.store.get_state(enabled_guild.id)).status,
            role_analytics_store.SyncStatus.READY,
        )
        self.assertEqual(
            (await self.store.get_state(disabled_guild.id)).status,
            role_analytics_store.SyncStatus.DISABLED,
        )

    async def test_resumed_check_is_debounced_and_reconciles_enabled_guild(self):
        guild = FakeGuild([FakeMember(1, (123, 10))])
        service = role_analytics_service.RoleAnalyticsService(FakeBot(), self.store)
        first = await service.sync_guild(guild, manual=True)

        first_task = service.schedule_resumed_check([guild], delay=60)
        second_task = service.schedule_resumed_check([guild], delay=0)
        await second_task

        self.assertTrue(first_task.cancelled())
        self.assertGreater(
            (await self.store.get_state(guild.id)).active_generation,
            first.generation,
        )


if __name__ == "__main__":
    unittest.main()
