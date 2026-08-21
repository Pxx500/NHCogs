import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

MODULE_PATH = Path(__file__).parents[1] / "NHCogs" / "nhmisc" / "role_analytics_store.py"
SPEC = importlib.util.spec_from_file_location(
    "nhmisc_role_analytics_store_test", MODULE_PATH
)
role_analytics_store = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = role_analytics_store
SPEC.loader.exec_module(role_analytics_store)

EXPRESSION_MODULE_PATH = (
    Path(__file__).parents[1] / "NHCogs" / "nhmisc" / "role_expression.py"
)
EXPRESSION_SPEC = importlib.util.spec_from_file_location(
    "nhmisc_role_expression_for_store_test", EXPRESSION_MODULE_PATH
)
role_expression = importlib.util.module_from_spec(EXPRESSION_SPEC)
sys.modules[EXPRESSION_SPEC.name] = role_expression
EXPRESSION_SPEC.loader.exec_module(role_expression)


class RoleAnalyticsStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = role_analytics_store.RoleAnalyticsStore(
            Path(self.temp_dir.name) / "role_analytics.sqlite"
        )
        await self.store.initialize()

    async def test_unknown_guild_starts_disabled_without_an_active_generation(self):
        state = await self.store.get_state(123)

        self.assertFalse(state.enabled)
        self.assertEqual(state.status, role_analytics_store.SyncStatus.DISABLED)
        self.assertIsNone(state.active_generation)

    async def test_staged_generation_is_unqueryable_until_atomic_activation(self):
        generation = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            generation,
            [role_analytics_store.MemberSnapshot(10, False, (100,))],
        )

        with self.assertRaises(role_analytics_store.AnalyticsUnavailableError):
            await self.store.count_matching(123, "1", ())

        await self.store.activate_generation(123, generation, 1)

        self.assertEqual(await self.store.count_matching(123, "1", ()), 1)

    async def test_boolean_queries_use_non_bot_active_member_universe(self):
        generation = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            generation,
            [
                role_analytics_store.MemberSnapshot(1, False, (10,)),
                role_analytics_store.MemberSnapshot(2, False, (20,)),
                role_analytics_store.MemberSnapshot(3, False, (10, 20)),
                role_analytics_store.MemberSnapshot(4, True, (10,)),
                role_analytics_store.MemberSnapshot(5, False, ()),
            ],
        )
        await self.store.activate_generation(123, generation, 5)

        and_sql, and_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("10 AND 20")
        )
        or_sql, or_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("10 OR 20")
        )
        not_sql, not_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("NOT 10")
        )

        self.assertEqual(
            await self.store.count_matching(123, and_sql, and_parameters), 1
        )
        self.assertEqual(
            await self.store.count_matching(123, or_sql, or_parameters), 3
        )
        self.assertEqual(
            await self.store.count_matching(123, not_sql, not_parameters), 2
        )
        self.assertEqual(
            await self.store.matching_user_ids(123, or_sql, or_parameters),
            (1, 2, 3),
        )

    async def test_replace_member_is_idempotent_and_replaces_complete_role_set(self):
        generation = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            generation,
            [role_analytics_store.MemberSnapshot(1, False, (10,))],
        )
        await self.store.activate_generation(123, generation, 1)
        role_10_sql, role_10_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("10")
        )
        role_20_sql, role_20_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("20")
        )

        replacement = role_analytics_store.MemberSnapshot(1, False, (20,))
        await self.store.replace_member(123, replacement)
        await self.store.replace_member(123, replacement)

        self.assertEqual(
            await self.store.count_matching(123, role_10_sql, role_10_parameters), 0
        )
        self.assertEqual(
            await self.store.count_matching(123, role_20_sql, role_20_parameters), 1
        )

    async def test_remove_member_deletes_member_from_not_universe(self):
        generation = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            generation,
            [
                role_analytics_store.MemberSnapshot(1, False, (10,)),
                role_analytics_store.MemberSnapshot(2, False, ()),
            ],
        )
        await self.store.activate_generation(123, generation, 2)
        not_sql, not_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("NOT 10")
        )

        await self.store.remove_member(123, 2)

        self.assertEqual(
            await self.store.count_matching(123, not_sql, not_parameters), 0
        )

    async def test_remove_role_deletes_only_that_membership(self):
        generation = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            generation,
            [role_analytics_store.MemberSnapshot(1, False, (10, 20))],
        )
        await self.store.activate_generation(123, generation, 1)
        role_10_sql, role_10_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("10")
        )
        role_20_sql, role_20_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("20")
        )

        await self.store.remove_role(123, 10)

        self.assertEqual(
            await self.store.count_matching(123, role_10_sql, role_10_parameters), 0
        )
        self.assertEqual(
            await self.store.count_matching(123, role_20_sql, role_20_parameters), 1
        )

    async def test_clear_guild_returns_it_to_disabled_state(self):
        generation = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            generation,
            [role_analytics_store.MemberSnapshot(1, False, (10,))],
        )
        await self.store.activate_generation(123, generation, 1)

        await self.store.clear_guild(123)

        state = await self.store.get_state(123)
        self.assertEqual(state.status, role_analytics_store.SyncStatus.DISABLED)
        self.assertFalse(state.enabled)

    async def test_failed_staging_can_be_discarded_without_replacing_active_data(self):
        first = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            first,
            [role_analytics_store.MemberSnapshot(1, False, (10,))],
        )
        await self.store.activate_generation(123, first, 1)
        second = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            second,
            [role_analytics_store.MemberSnapshot(1, False, (20,))],
        )

        await self.store.discard_generation(123, second)
        await self.store.set_status(123, role_analytics_store.SyncStatus.READY)
        role_10_sql, role_10_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("10")
        )
        role_20_sql, role_20_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("20")
        )

        self.assertEqual(
            await self.store.count_matching(123, role_10_sql, role_10_parameters), 1
        )
        self.assertEqual(
            await self.store.count_matching(123, role_20_sql, role_20_parameters), 0
        )

    async def test_delete_user_everywhere_removes_current_member_data(self):
        generation = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            generation,
            [role_analytics_store.MemberSnapshot(1, False, (10,))],
        )
        await self.store.activate_generation(123, generation, 1)

        await self.store.delete_user_everywhere(1)

        self.assertEqual(await self.store.count_matching(123, "1", ()), 0)

    async def _activate_single_member_guild(self):
        generation = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            generation,
            [role_analytics_store.MemberSnapshot(1, False, (10,))],
        )
        await self.store.activate_generation(123, generation, 1)

    async def test_active_generation_stays_queryable_while_a_replacement_stages(self):
        await self._activate_single_member_guild()

        staged = await self.store.next_generation(123)
        await self.store.write_generation(
            123,
            staged,
            [role_analytics_store.MemberSnapshot(2, False, (20,))],
        )

        state = await self.store.get_state(123)
        self.assertEqual(state.status, role_analytics_store.SyncStatus.SYNCING)
        self.assertEqual(await self.store.count_matching(123, "1", ()), 1)
        role_20_sql, role_20_parameters = role_expression.compile_role_expression(
            role_expression.parse_role_expression("20")
        )
        self.assertEqual(
            await self.store.count_matching(123, role_20_sql, role_20_parameters), 0
        )

    async def test_failed_status_does_not_hide_the_still_valid_active_generation(self):
        await self._activate_single_member_guild()

        await self.store.set_status(
            123, role_analytics_store.SyncStatus.FAILED, "sync_failed"
        )

        self.assertEqual(await self.store.count_matching(123, "1", ()), 1)

    async def test_disabled_guild_is_never_queryable_even_with_a_stale_generation(self):
        await self._activate_single_member_guild()

        await self.store.clear_guild(123)

        with self.assertRaises(role_analytics_store.AnalyticsUnavailableError):
            await self.store.count_matching(123, "1", ())

    async def test_repeated_state_reads_hit_the_database_once_until_invalidated(self):
        reads = 0
        original = self.store._get_state_sync

        def counting_get_state(guild_id):
            nonlocal reads
            reads += 1
            return original(guild_id)

        self.store._get_state_sync = counting_get_state

        await self.store.get_state(123)
        await self.store.get_state(123)
        await self.store.get_state(123)
        self.assertEqual(reads, 1)

        await self.store.set_status(123, role_analytics_store.SyncStatus.RETRYING)

        state = await self.store.get_state(123)
        self.assertEqual(reads, 2)
        self.assertEqual(state.status, role_analytics_store.SyncStatus.RETRYING)


if __name__ == "__main__":
    unittest.main()
