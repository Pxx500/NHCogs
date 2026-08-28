import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.storage_loader import load_shared_storage

load_shared_storage()

from NHCogs.nhmoderation.history import NHModerationHistory  # noqa: E402
from NHCogs.nhmoderation.models import (  # noqa: E402
    BanChartQuery,
    ModerationObservation,
)
from NHCogs.nhmoderation.projection import PROJECTION_VERSION  # noqa: E402

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def observation(
    *,
    source_kind: str,
    source_key: str | None,
    action_hint: str = "ban",
    target_user_id: int = 100,
    executor_user_id: int | None = None,
    credited_moderator_hint: int | None = None,
    attribution_hint: str | None = None,
    occurred_at: datetime | None = NOW,
    observed_at: datetime = NOW,
    reason: str | None = None,
    import_batch_id: str | None = None,
) -> ModerationObservation:
    return ModerationObservation(
        guild_id=1,
        source_kind=source_kind,
        source_key=source_key,
        action_hint=action_hint,
        target_user_id=target_user_id,
        executor_user_id=executor_user_id,
        credited_moderator_hint=credited_moderator_hint,
        attribution_hint=attribution_hint,
        occurred_at=occurred_at,
        observed_at=observed_at,
        reason=reason,
        import_batch_id=import_batch_id,
    )


class ModerationHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_source_observation_is_idempotent(self):
      with TemporaryDirectory() as directory:
        history = NHModerationHistory(Path(directory) / "moderation.sqlite")
        await history.initialize()
        item = observation(source_kind="discord_audit", source_key="500")

        self.assertIs(await history.observe(item), True)
        self.assertIs(await history.observe(item), False)

        chart = await history.get_ban_chart(BanChartQuery(guild_id=1))
        self.assertEqual(chart.total_count, 1)


    async def test_modlog_and_audit_evidence_project_one_human_ban(self):
      with TemporaryDirectory() as directory:
        history = NHModerationHistory(Path(directory) / "moderation.sqlite")
        await history.initialize()
        await history.observe(
            observation(
                source_kind="discord_audit",
                source_key="500",
                executor_user_id=55,
                attribution_hint="human_direct",
            )
        )
        await history.observe(
            observation(
                source_kind="red_modlog",
                source_key="10",
                credited_moderator_hint=55,
                attribution_hint="human_direct",
                occurred_at=NOW + timedelta(seconds=2),
            )
        )

        chart = await history.get_ban_chart(BanChartQuery(guild_id=1))

        self.assertEqual(chart.total_count, 1)
        self.assertEqual(
            [(row.moderator_user_id, row.label, row.count) for row in chart.rows],
            [(55, None, 1)],
        )

    async def test_red_automation_outweighs_conflicting_human_audit_entry(self):
      with TemporaryDirectory() as directory:
        history = NHModerationHistory(Path(directory) / "moderation.sqlite")
        await history.initialize()
        await history.observe(
            observation(
                source_kind="red_modlog",
                source_key="10",
                attribution_hint="automation",
            )
        )
        await history.observe(
            observation(
                source_kind="discord_audit",
                source_key="500",
                executor_user_id=55,
                credited_moderator_hint=55,
                attribution_hint="human_direct",
                occurred_at=NOW + timedelta(seconds=1),
            )
        )

        default_chart = await history.get_ban_chart(BanChartQuery(guild_id=1))
        automation_chart = await history.get_ban_chart(
            BanChartQuery(guild_id=1, include_automation=True)
        )

        self.assertEqual(default_chart.total_count, 0)
        self.assertEqual(
            [(row.label, row.count) for row in automation_chart.rows],
            [("Automation", 1)],
        )


    async def test_automation_is_opt_in_and_unknown_is_last(self):
      with TemporaryDirectory() as directory:
        history = NHModerationHistory(Path(directory) / "moderation.sqlite")
        await history.initialize()
        await history.observe(
            observation(
                source_kind="red_modlog",
                source_key="10",
                target_user_id=100,
                attribution_hint="automation",
            )
        )
        await history.observe(
            observation(
                source_kind="discord_gateway",
                source_key=None,
                target_user_id=101,
                occurred_at=NOW + timedelta(minutes=10),
                observed_at=NOW + timedelta(minutes=10),
            )
        )

        default_chart = await history.get_ban_chart(BanChartQuery(guild_id=1))
        automation_chart = await history.get_ban_chart(
            BanChartQuery(guild_id=1, include_automation=True)
        )

        self.assertEqual(
            [(row.label, row.count) for row in default_chart.rows], [("Unknown", 1)]
        )
        self.assertEqual(
            [(row.label, row.count) for row in automation_chart.rows],
            [("Automation", 1), ("Unknown", 1)],
        )


    async def test_softban_is_one_action_excluded_from_ban_chart(self):
      with TemporaryDirectory() as directory:
        history = NHModerationHistory(Path(directory) / "moderation.sqlite")
        await history.initialize()
        await history.observe(
            observation(
                source_kind="discord_audit",
                source_key="ban-1",
                action_hint="ban",
                executor_user_id=55,
                attribution_hint="human_direct",
            )
        )
        await history.observe(
            observation(
                source_kind="discord_audit",
                source_key="unban-1",
                action_hint="unban",
                executor_user_id=55,
                attribution_hint="human_direct",
                occurred_at=NOW + timedelta(seconds=3),
            )
        )
        await history.observe(
            observation(
                source_kind="red_modlog",
                source_key="10",
                action_hint="softban",
                credited_moderator_hint=55,
                attribution_hint="human_direct",
                occurred_at=NOW + timedelta(seconds=1),
            )
        )

        chart = await history.get_ban_chart(BanChartQuery(guild_id=1))

        self.assertEqual(chart.total_count, 0)

    async def test_unban_closes_prior_ban_in_projection(self):
      with TemporaryDirectory() as directory:
        database_path = Path(directory) / "moderation.sqlite"
        history = NHModerationHistory(database_path)
        await history.initialize()
        await history.observe(
            observation(source_kind="discord_audit", source_key="ban-1")
        )
        ended_at = NOW + timedelta(minutes=10)
        await history.observe(
            observation(
                source_kind="discord_audit",
                source_key="unban-1",
                action_hint="unban",
                occurred_at=ended_at,
                observed_at=ended_at,
            )
        )

        with closing(sqlite3.connect(database_path)) as connection:
            current_state, stored_ended_at = connection.execute(
                """SELECT current_state, ended_at FROM moderation_actions
                   WHERE action_kind = 'ban'"""
            ).fetchone()
        self.assertEqual(current_state, "ended")
        self.assertEqual(datetime.fromisoformat(stored_ended_at), ended_at)

    async def test_snapshot_marks_correlated_ban_active_without_changing_time(self):
      with TemporaryDirectory() as directory:
        database_path = Path(directory) / "moderation.sqlite"
        history = NHModerationHistory(database_path)
        await history.initialize()
        await history.observe(
            observation(source_kind="discord_audit", source_key="ban-1")
        )
        await history.observe(
            observation(
                source_kind="discord_ban_snapshot",
                source_key="batch:100",
                occurred_at=None,
                observed_at=NOW + timedelta(seconds=1),
                import_batch_id="batch",
            )
        )

        with closing(sqlite3.connect(database_path)) as connection:
            occurred_at, current_state = connection.execute(
                """SELECT occurred_at, current_state FROM moderation_actions
                   WHERE action_kind = 'ban'"""
            ).fetchone()
        self.assertEqual(datetime.fromisoformat(occurred_at), NOW)
        self.assertEqual(current_state, "active")

    async def test_unban_ends_earlier_undated_snapshot_state(self):
      with TemporaryDirectory() as directory:
        database_path = Path(directory) / "moderation.sqlite"
        history = NHModerationHistory(database_path)
        await history.initialize()
        await history.observe(
            observation(
                source_kind="discord_ban_snapshot",
                source_key="batch:100",
                occurred_at=None,
                observed_at=NOW,
                import_batch_id="batch",
            )
        )
        ended_at = NOW + timedelta(days=1)
        await history.observe(
            observation(
                source_kind="discord_audit",
                source_key="unban-1",
                action_hint="unban",
                occurred_at=ended_at,
                observed_at=ended_at,
            )
        )

        with closing(sqlite3.connect(database_path)) as connection:
            occurred_at, current_state, stored_ended_at = connection.execute(
                """SELECT occurred_at, current_state, ended_at
                   FROM moderation_actions WHERE action_kind = 'ban'"""
            ).fetchone()
        self.assertIsNone(occurred_at)
        self.assertEqual(current_state, "ended")
        self.assertEqual(datetime.fromisoformat(stored_ended_at), ended_at)

    async def test_initialize_rebuilds_a_stale_projection_from_observations(self):
      with TemporaryDirectory() as directory:
        database_path = Path(directory) / "moderation.sqlite"
        history = NHModerationHistory(database_path)
        await history.initialize()
        await history.observe(
            observation(source_kind="discord_audit", source_key="ban-1")
        )
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute("DELETE FROM moderation_actions")
            connection.execute(
                """UPDATE moderation_sync_state
                   SET projection_version = 0, projection_checkpoint = 0
                   WHERE guild_id = 1"""
            )

        reopened = NHModerationHistory(database_path)
        await reopened.initialize()

        self.assertEqual(
            (await reopened.get_ban_chart(BanChartQuery(guild_id=1))).total_count,
            1,
        )
        self.assertEqual(
            (await reopened.status(1)).projection_version,
            PROJECTION_VERSION,
        )

    async def test_repeated_gateway_delivery_projects_one_ban(self):
      with TemporaryDirectory() as directory:
        history = NHModerationHistory(Path(directory) / "moderation.sqlite")
        await history.initialize()
        await history.observe(
            observation(
                source_kind="discord_gateway",
                source_key=None,
                observed_at=NOW,
            )
        )
        await history.observe(
            observation(
                source_kind="discord_gateway",
                source_key=None,
                occurred_at=NOW + timedelta(seconds=2),
                observed_at=NOW + timedelta(seconds=2),
            )
        )

        chart = await history.get_ban_chart(BanChartQuery(guild_id=1))

        self.assertEqual(chart.total_count, 1)

    async def test_gateway_unban_separates_two_real_ban_transitions(self):
      with TemporaryDirectory() as directory:
        history = NHModerationHistory(Path(directory) / "moderation.sqlite")
        await history.initialize()
        for action, seconds in (("ban", 0), ("unban", 1), ("ban", 2)):
            occurred_at = NOW + timedelta(seconds=seconds)
            await history.observe(
                observation(
                    source_kind="discord_gateway",
                    source_key=None,
                    action_hint=action,
                    occurred_at=occurred_at,
                    observed_at=occurred_at,
                )
            )

        chart = await history.get_ban_chart(BanChartQuery(guild_id=1))

        self.assertEqual(chart.total_count, 2)

    async def test_user_deletion_anonymizes_moderator_and_rebuilds_chart(self):
      with TemporaryDirectory() as directory:
        history = NHModerationHistory(Path(directory) / "moderation.sqlite")
        await history.initialize()
        await history.observe(
            observation(
                source_kind="red_modlog",
                source_key="10",
                credited_moderator_hint=55,
                attribution_hint="human_direct",
                reason="private reason",
            )
        )

        await history.delete_user_data(55)
        chart = await history.get_ban_chart(BanChartQuery(guild_id=1))

        self.assertEqual(
            [(row.label, row.count) for row in chart.rows], [("Unknown", 1)]
        )

    async def test_user_deletion_removes_id_from_snapshot_source_key(self):
      with TemporaryDirectory() as directory:
        database_path = Path(directory) / "moderation.sqlite"
        history = NHModerationHistory(database_path)
        await history.initialize()
        await history.observe(
            observation(
                source_kind="discord_ban_snapshot",
                source_key="batch:123456789012345678",
                target_user_id=123456789012345678,
                occurred_at=None,
                import_batch_id="batch",
            )
        )

        await history.delete_user_data(123456789012345678)

        with closing(sqlite3.connect(database_path)) as connection:
            source_key, target_user_id = connection.execute(
                "SELECT source_key, target_user_id FROM moderation_observations"
            ).fetchone()
        self.assertNotIn("123456789012345678", source_key)
        self.assertIsNone(target_user_id)

    async def test_direct_and_assisted_actions_share_one_moderator_row(self):
      with TemporaryDirectory() as directory:
        history = NHModerationHistory(Path(directory) / "moderation.sqlite")
        await history.initialize()
        await history.observe(
            observation(
                source_kind="red_modlog",
                source_key="10",
                target_user_id=100,
                credited_moderator_hint=55,
                attribution_hint="human_direct",
            )
        )
        await history.observe(
            observation(
                source_kind="red_modlog",
                source_key="11",
                target_user_id=101,
                credited_moderator_hint=55,
                attribution_hint="automation_assisted",
                occurred_at=NOW + timedelta(minutes=10),
                observed_at=NOW + timedelta(minutes=10),
            )
        )

        chart = await history.get_ban_chart(BanChartQuery(guild_id=1))

        self.assertEqual(
            [(row.moderator_user_id, row.count) for row in chart.rows], [(55, 2)]
        )
