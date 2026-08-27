import unittest
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
