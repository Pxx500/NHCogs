from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    BanChartData,
    BanChartQuery,
    BanChartRow,
    MigrationRun,
    ModerationObservation,
    StoredObservation,
    SynchronizationState,
)
from .projection import PROJECTION_VERSION, project_actions
from .store import ModerationStore


class NHModerationHistory:
    def __init__(self, database_path: Path):
        self._store = ModerationStore(database_path)
        self._guild_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def initialize(self) -> None:
        await self._store.initialize()
        for guild_id in await self._store.guilds_needing_projection(
            PROJECTION_VERSION
        ):
            await self.rebuild(guild_id)

    async def start_initial_migration(
        self, guild_id: int, run_id: str, started_at: datetime
    ) -> MigrationRun:
        async with self._guild_locks[guild_id]:
            return await self._store.start_migration(guild_id, run_id, started_at)

    async def complete_migration_step(
        self, guild_id: int, run_id: str, step: str
    ) -> MigrationRun:
        async with self._guild_locks[guild_id]:
            return await self._store.mark_migration_step(guild_id, run_id, step)

    async def complete_initial_migration(
        self,
        guild_id: int,
        run_id: str,
        completed_at: datetime,
        report: str,
        historical_gap: bool,
    ) -> None:
        async with self._guild_locks[guild_id]:
            await self._store.complete_migration(
                guild_id,
                run_id,
                completed_at,
                report,
                historical_gap,
            )

    async def migration_run(self, guild_id: int) -> MigrationRun | None:
        return await self._store.migration_run(guild_id)

    async def observe(self, item: ModerationObservation) -> bool:
        stored = StoredObservation(**item.__dict__)
        async with self._guild_locks[item.guild_id]:
            inserted = await self._store.append(stored)
            if inserted:
                await self._rebuild_unlocked(item.guild_id)
            return inserted

    async def observe_many(self, items: list[ModerationObservation]) -> int:
        by_guild: defaultdict[int, list[ModerationObservation]] = defaultdict(list)
        for item in items:
            by_guild[item.guild_id].append(item)
        inserted_count = 0
        for guild_id, guild_items in by_guild.items():
            async with self._guild_locks[guild_id]:
                changed = False
                for item in guild_items:
                    inserted = await self._store.append(StoredObservation(**item.__dict__))
                    inserted_count += int(inserted)
                    changed = changed or inserted
                if changed:
                    await self._rebuild_unlocked(guild_id)
        return inserted_count

    async def ingest_batch(
        self,
        guild_id: int,
        items: list[ModerationObservation],
        *,
        audit_ban_cursor: int | None,
        audit_unban_cursor: int | None,
        red_modlog_cursor: int | None,
        completed_at: datetime | None,
        reconciliation: bool = False,
        migration_complete: bool = False,
        historical_gap: bool | None = None,
    ) -> int:
        async with self._guild_locks[guild_id]:
            inserted = await self._store.append_batch(
                [StoredObservation(**item.__dict__) for item in items]
            )
            await self._rebuild_unlocked(guild_id)
            await self._store.update_sync_state(
                guild_id,
                audit_ban_cursor=audit_ban_cursor,
                audit_unban_cursor=audit_unban_cursor,
                red_modlog_cursor=red_modlog_cursor,
                completed_at=completed_at,
                reconciliation=reconciliation,
                migration_complete=migration_complete,
                historical_gap=historical_gap,
            )
            return inserted

    async def rebuild(self, guild_id: int) -> None:
        async with self._guild_locks[guild_id]:
            await self._rebuild_unlocked(guild_id)

    async def _rebuild_unlocked(self, guild_id: int) -> None:
        observations = await self._store.observations(guild_id)
        actions = project_actions(observations)
        await self._store.replace_projection(guild_id, actions, PROJECTION_VERSION)

    async def get_ban_chart(self, query: BanChartQuery) -> BanChartData:
        rows = await self._store.chart_rows(
            query.guild_id, query.since, query.include_automation
        )
        human_counts: defaultdict[int, int] = defaultdict(int)
        automation_count = 0
        unknown_count = 0
        for row in rows:
            kind = row["attribution_kind"]
            count = int(row["count"])
            moderator_id = row["credited_moderator_id"]
            if kind == "automation":
                automation_count += count
            elif moderator_id is None:
                unknown_count += count
            else:
                human_counts[int(moderator_id)] += count
        human = [
            BanChartRow(moderator_id, None, count)
            for moderator_id, count in human_counts.items()
        ]
        human.sort(key=lambda row: (-row.count, row.moderator_user_id or 0))
        named = human[: query.amount]
        other_count = sum(row.count for row in human[query.amount :])
        output = list(named)
        if query.include_automation and automation_count:
            output.append(BanChartRow(None, "Automation", automation_count))
        if unknown_count:
            output.append(BanChartRow(None, "Unknown", unknown_count))
        total = sum(row.count for row in output) + other_count
        return BanChartData(tuple(output), other_count, total)

    async def delete_user_data(self, user_id: int) -> None:
        for guild_id in await self._store.delete_user(user_id):
            await self.rebuild(guild_id)

    async def delete_guild_data(self, guild_id: int) -> None:
        async with self._guild_locks[guild_id]:
            await self._store.delete_guild(guild_id)

    async def status(self, guild_id: int) -> SynchronizationState:
        return await self._store.sync_state(guild_id)

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(timezone.utc)
