import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests.storage_loader import load_shared_storage

load_shared_storage()

from NHCogs.nhmoderation import store as store_module  # noqa: E402
from NHCogs.nhmoderation.history import NHModerationHistory  # noqa: E402
from NHCogs.nhmoderation.models import (  # noqa: E402
    BanChartQuery,
    ModerationObservation,
    StoredObservation,  # noqa: E402
)
from NHCogs.nhmoderation.projection import PROJECTION_VERSION  # noqa: E402
from NHCogs.nhmoderation.store import ModerationStore  # noqa: E402

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
    async def test_requester_modes_keep_essential_history_and_other_targets_evidence(self):
        for requester in ("user", "user_strict", "owner", "discord_deleted_user"):
            with self.subTest(requester=requester), TemporaryDirectory() as directory:
                path = Path(directory) / "moderation.sqlite"
                history = NHModerationHistory(path)
                await history.initialize()
                await history.observe(replace(
                    observation(
                        source_kind="red_modlog", source_key="target-case",
                        target_user_id=100, executor_user_id=55,
                        credited_moderator_hint=55, attribution_hint="human_direct",
                        reason="Evidence-backed restriction for Nickname",
                    ),
                    account_snapshot={"nickname": "Nickname"},
                    activity_summary={"availability": "observed", "messages": 5},
                ))
                await history.observe(replace(
                    observation(
                        source_kind="red_modlog", source_key="moderator-case",
                        target_user_id=200, executor_user_id=100,
                        credited_moderator_hint=100, attribution_hint="human_direct",
                        reason="Another member's essential ban evidence",
                    ),
                    account_snapshot={"nickname": "Other member"},
                    activity_summary={"availability": "observed", "messages": 8},
                ))
                await history.delete_user_data(100, requester=requester)
                reopened = NHModerationHistory(path)
                await reopened.initialize()
                exported = await reopened.export_history(1)
                rows = {row["source_key"]: row for row in exported["observations"]}
                own = rows["target-case"]
                other = rows["moderator-case"]
                self.assertIsNone(own["account_snapshot"])
                self.assertIsNone(own["activity_summary"])
                self.assertEqual(other["account_snapshot"], {"nickname": "Other member"})
                self.assertEqual(other["activity_summary"]["messages"], 8)
                self.assertEqual(own["occurred_at"], NOW.isoformat())
                self.assertEqual(other["target_user_id"], "200")
                if requester in {"user", "user_strict"}:
                    self.assertEqual(own["target_user_id"], "100")
                    self.assertEqual(other["executor_user_id"], "100")
                    self.assertEqual(other["credited_moderator_hint"], "100")
                    self.assertEqual(other["reason"], "Another member's essential ban evidence")
                    self.assertEqual(own["reason"], "Evidence-backed restriction for Nickname")
                else:
                    self.assertIsNone(own["target_user_id"])
                    self.assertIsNone(other["executor_user_id"])
                    self.assertIsNone(other["credited_moderator_hint"])
                    self.assertIsNone(own["reason"])
                    self.assertIsNone(other["reason"])
                actions = {
                    action["observation_ids"][0]: action for action in exported["actions"]
                }
                self.assertEqual(actions[own["observation_id"]]["reason"], own["reason"])
                self.assertEqual(actions[other["observation_id"]]["reason"], other["reason"])

    async def test_unknown_requester_fails_before_mutating_moderation_history(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            await history.observe(observation(
                source_kind="red_modlog", source_key="retained-case",
                executor_user_id=55, reason="Essential ban evidence",
            ))
            before = await history.export_history(1)
            with self.assertRaisesRegex(ValueError, "Unsupported NHModeration"):
                await history.delete_user_data(100, requester="future_requester")
            after = await history.export_history(1)
            self.assertEqual(after["observations"], before["observations"])
            self.assertEqual(after["actions"], before["actions"])

    async def test_existing_database_upgrade_preserves_history_with_unknown_snapshots(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "moderation.sqlite"
            with mock.patch.object(store_module, "MIGRATIONS", store_module.MIGRATIONS[:1]):
                await ModerationStore(path).initialize()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """INSERT INTO moderation_observations
                       (guild_id, source_kind, source_key, action_hint, target_user_id,
                        observed_at, occurred_at, source_payload_version)
                       VALUES (1, 'discord_audit', '500', 'ban', 100, ?, ?, 1)""",
                    (NOW.isoformat(), NOW.isoformat()),
                )
            history = NHModerationHistory(path)
            await history.initialize()
            await history.initialize()
            exported = await history.export_history(1)
            self.assertEqual(len(exported["actions"]), 1)
            self.assertEqual(len(exported["observations"]), 1)
            self.assertIsNone(exported["observations"][0]["account_snapshot"])
            self.assertIsNone(exported["observations"][0]["activity_summary"])
            self.assertEqual(exported["observations"][0]["occurred_at"], NOW.isoformat())
            self.assertEqual((await history.get_ban_chart(BanChartQuery(guild_id=1))).total_count, 1)

    async def test_export_retains_evidence_links_and_erases_subject_data(self):
        with TemporaryDirectory() as directory:
            history = NHModerationHistory(Path(directory) / "moderation.sqlite")
            await history.initialize()
            first = replace(
                observation(source_kind="discord_audit", source_key="500", executor_user_id=55),
                account_snapshot={"username": "then-name"},
                activity_summary={"availability": "observed", "messages": 5},
            )
            await history.observe(first)
            await history.observe(observation(source_kind="red_modlog", source_key="10"))
            await history.observe(replace(first, guild_id=2))
            payload = json.loads(json.dumps(await history.export_history(1)))
            self.assertEqual(payload["guild_id"], "1")
            self.assertEqual(len(payload["observations"]), 2)
            self.assertEqual(len(payload["actions"]), 1)
            evidence = payload["observations"][0]
            self.assertEqual(evidence["target_user_id"], "100")
            self.assertEqual(evidence["executor_user_id"], "55")
            self.assertEqual(evidence["account_snapshot"], {"username": "then-name"})
            self.assertEqual(evidence["snapshot_observed_at"], NOW.isoformat())
            self.assertEqual(evidence["snapshot_source"], "discord_audit")
            self.assertEqual(set(payload["actions"][0]["observation_ids"]),
                             {item["observation_id"] for item in payload["observations"]})
            self.assertEqual(payload["coverage"]["first_observed_at"], NOW.isoformat())
            self.assertEqual(payload["coverage"]["synchronization"]["migration_state"], "pending")
            await history.rebuild(1)
            self.assertEqual((await history.export_history(1))["actions"], payload["actions"])
            await history.delete_user_data(100)
            erased = await history.export_history(1)
            self.assertTrue(all(row["target_user_id"] is None for row in erased["observations"]))
            self.assertTrue(all(row["account_snapshot"] is None for row in erased["observations"]))
            self.assertTrue(all(row["activity_summary"] is None for row in erased["observations"]))
            await history.delete_guild_data(1)
            empty = await history.export_history(1)
            self.assertEqual(empty["observations"], [])
            self.assertEqual(empty["actions"], [])
            self.assertIsNone(empty["coverage"]["synchronization"])

    async def test_account_snapshot_survives_restart_and_is_erased_with_its_subject(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "moderation.sqlite"
            store = ModerationStore(path)
            await store.initialize()
            item = StoredObservation(**{
                **observation(source_kind="discord_audit", source_key="500").__dict__,
                "account_snapshot": {"username": "old-name", "account_created_at": NOW.isoformat()},
                "activity_summary": {"availability": "observed", "messages": 4},
            })
            self.assertTrue(await store.append(item))
            reopened = ModerationStore(path)
            await reopened.initialize()
            self.assertFalse(await reopened.append(replace(item, account_snapshot=None, activity_summary=None)))
            self.assertEqual((await reopened.observations(1))[0].account_snapshot, item.account_snapshot)
            self.assertEqual((await reopened.observations(1))[0].activity_summary, item.activity_summary)
            await reopened.delete_user(100)
            retained = (await reopened.observations(1))[0]
            self.assertIsNone(retained.target_user_id)
            self.assertIsNone(retained.account_snapshot)
            self.assertIsNone(retained.activity_summary)

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
                executor_user_id=55,
                attribution_hint="human_direct",
                reason="private reason",
            )
        )

        await history.delete_user_data(55)
        chart = await history.get_ban_chart(BanChartQuery(guild_id=1))

        self.assertEqual(
            [(row.label, row.count) for row in chart.rows], [("Unknown", 1)]
        )
        exported = await history.export_history(1)
        self.assertIsNone(exported["observations"][0]["executor_user_id"])
        self.assertIsNone(exported["observations"][0]["credited_moderator_hint"])
        self.assertIsNone(exported["observations"][0]["reason"])
        self.assertIsNone(exported["actions"][0]["credited_moderator_id"])

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
