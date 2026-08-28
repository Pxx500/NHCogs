from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from .history import NHModerationHistory
from .models import ModerationObservation

DISCORD_EPOCH_MS = 1_420_070_400_000
AUDIT_OVERLAP = timedelta(days=14)
AUDIT_RETENTION = timedelta(days=45)
HONEYPOT_REVIEW_PREFIX = "Honeypot review:"
RED_DELETED_USER_ID = 0xDE1
RED_AUDIT_REASON = re.compile(
    r"^Action requested by .*? \(ID (?P<moderator_id>[0-9]{1,20})\)\."
    r"(?: Reason: .*)?$"
)

AuditFetcher = Callable[..., Awaitable[Sequence[Any]]]
ModLogFetcher = Callable[..., Awaitable[Sequence[Any]]]
SnapshotFetcher = Callable[[Any], Awaitable[Sequence[Any]]]
BotUserId = int | Callable[[], int]


class SyncMode(str, Enum):
    INITIAL = "initial"
    INCREMENTAL = "incremental"
    WEEKLY = "weekly"
    REPAIR = "repair"


@dataclass(frozen=True)
class SyncReport:
    mode: SyncMode
    inserted_observations: int
    completed_at: datetime


def snowflake_boundary(value: datetime) -> int:
    milliseconds = int(value.astimezone(timezone.utc).timestamp() * 1_000)
    return max(0, milliseconds - DISCORD_EPOCH_MS) << 22


def next_weekly_reconciliation(now: datetime) -> datetime:
    utc_now = now.astimezone(timezone.utc)
    days_until_sunday = (6 - utc_now.weekday()) % 7
    candidate = datetime.combine(
        utc_now.date() + timedelta(days=days_until_sunday),
        time(4, 20),
        tzinfo=timezone.utc,
    )
    if candidate <= utc_now:
        candidate += timedelta(days=7)
    return candidate


def _possible_historical_gap(guild: Any, observed_at: datetime) -> bool:
    created_at = _datetime(getattr(guild, "created_at", None))
    return created_at is None or created_at < observed_at - AUDIT_RETENTION


def _id(value: Any) -> int | None:
    raw = getattr(value, "id", value)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return None


def _red_id(value: Any) -> int | None:
    value_id = _id(value)
    return None if value_id == RED_DELETED_USER_ID else value_id


def _red_audit_moderator_id(reason: Any) -> int | None:
    if not isinstance(reason, str):
        return None
    match = RED_AUDIT_REASON.fullmatch(reason)
    return int(match.group("moderator_id")) if match is not None else None


def audit_observation(
    guild_id: int,
    entry: Any,
    action_hint: str,
    bot_user_id: int,
    observed_at: datetime,
) -> ModerationObservation:
    executor_id = _id(getattr(entry, "user", None))
    automated = executor_id == bot_user_id
    reason = getattr(entry, "reason", None)
    red_moderator_id = _red_audit_moderator_id(reason) if automated else None
    return ModerationObservation(
        guild_id=guild_id,
        source_kind="discord_audit",
        source_key=str(entry.id),
        action_hint=action_hint,
        target_user_id=_id(getattr(entry, "target", None)),
        executor_user_id=executor_id,
        credited_moderator_hint=red_moderator_id or (None if automated else executor_id),
        attribution_hint=(
            "human_direct"
            if red_moderator_id is not None or not automated
            else "automation"
        ),
        occurred_at=_datetime(getattr(entry, "created_at", None)),
        observed_at=observed_at,
        reason=reason,
    )


def modlog_observation(
    guild_id: int, case: Any, bot_user_id: int, observed_at: datetime
) -> ModerationObservation | None:
    action = str(getattr(case, "action_type", "")).casefold()
    if action not in {"ban", "hackban", "tempban", "softban", "unban"}:
        return None
    moderator_id = _red_id(getattr(case, "moderator", None))
    reason = getattr(case, "reason", None)
    if moderator_id == bot_user_id:
        attribution = "automation"
        credited = None
    elif moderator_id is not None and isinstance(reason, str) and reason.startswith(HONEYPOT_REVIEW_PREFIX):
        attribution = "automation_assisted"
        credited = moderator_id
    elif moderator_id is not None:
        attribution = "human_direct"
        credited = moderator_id
    else:
        attribution = None
        credited = None
    channel = getattr(case, "channel", None)
    return ModerationObservation(
        guild_id=guild_id,
        source_kind="red_modlog",
        source_key=str(case.case_number),
        action_hint=action,
        target_user_id=_red_id(getattr(case, "user", None)),
        executor_user_id=moderator_id,
        credited_moderator_hint=credited,
        attribution_hint=attribution,
        occurred_at=_datetime(getattr(case, "created_at", None)),
        observed_at=observed_at,
        reason=reason,
        expiry_at=_datetime(getattr(case, "until", None)),
        channel_id=_id(channel),
    )


class ModerationSynchronizer:
    def __init__(
        self,
        history: NHModerationHistory,
        *,
        bot_user_id: BotUserId,
        audit_fetcher: AuditFetcher,
        modlog_fetcher: ModLogFetcher,
        snapshot_fetcher: SnapshotFetcher,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._history = history
        self._bot_user_id_source = bot_user_id
        self._audit_fetcher = audit_fetcher
        self._modlog_fetcher = modlog_fetcher
        self._snapshot_fetcher = snapshot_fetcher
        self._clock = clock

    async def _fetch_audit_source(
        self,
        guild: Any,
        *,
        action: str,
        after_id: int | None,
        cursor_floor: int | None,
        bot_user_id: int,
        observed_at: datetime,
        import_batch_id: str | None = None,
    ) -> tuple[list[ModerationObservation], int | None, int]:
        inserted = 0
        max_entry_id = cursor_floor or 0

        async def commit_batch(entries: Sequence[Any]) -> None:
            nonlocal inserted, max_entry_id
            max_entry_id = max(
                [max_entry_id, *(_id(entry.id) or 0 for entry in entries)]
            )
            inserted += await self._history.observe_many(
                [
                    replace(
                        audit_observation(
                            guild.id,
                            entry,
                            action,
                            bot_user_id,
                            observed_at,
                        ),
                        import_batch_id=import_batch_id,
                    )
                    for entry in entries
                ]
            )

        remaining = await self._audit_fetcher(
            guild,
            action=action,
            after_id=after_id,
            on_batch=commit_batch,
        )
        max_entry_id = max(
            [max_entry_id, *(_id(entry.id) or 0 for entry in remaining)]
        )
        observations = [
            replace(
                audit_observation(
                    guild.id,
                    entry,
                    action,
                    bot_user_id,
                    observed_at,
                ),
                import_batch_id=import_batch_id,
            )
            for entry in remaining
        ]
        return observations, max_entry_id or None, inserted

    async def _synchronize_initial(
        self,
        guild: Any,
        bot_user_id: int,
        started_at: datetime,
    ) -> SyncReport:
        run = await self._history.start_initial_migration(
            guild.id,
            uuid4().hex,
            started_at,
        )
        if "red_modlog" not in run.completed_steps:
            cases = await self._modlog_fetcher(guild, after_case=None)
            observations = [
                item
                for case in cases
                if (
                    item := modlog_observation(
                        guild.id, case, bot_user_id, started_at
                    )
                )
                is not None
            ]
            observations = [
                replace(item, import_batch_id=run.run_id) for item in observations
            ]
            red_cursor = max(
                (_id(getattr(case, "case_number", None)) or 0 for case in cases),
                default=0,
            ) or None
            await self._history.ingest_batch(
                guild.id,
                observations,
                audit_ban_cursor=None,
                audit_unban_cursor=None,
                red_modlog_cursor=red_cursor,
                completed_at=None,
            )
            run = await self._history.complete_migration_step(
                guild.id, run.run_id, "red_modlog"
            )

        for action, step in (("ban", "audit_ban"), ("unban", "audit_unban")):
            if step in run.completed_steps:
                continue
            observations, cursor, _partial_inserted = await self._fetch_audit_source(
                guild,
                action=action,
                after_id=None,
                cursor_floor=None,
                bot_user_id=bot_user_id,
                observed_at=started_at,
                import_batch_id=run.run_id,
            )
            await self._history.ingest_batch(
                guild.id,
                observations,
                audit_ban_cursor=cursor if action == "ban" else None,
                audit_unban_cursor=cursor if action == "unban" else None,
                red_modlog_cursor=None,
                completed_at=None,
            )
            run = await self._history.complete_migration_step(
                guild.id, run.run_id, step
            )

        if "ban_snapshot" not in run.completed_steps:
            observations = []
            for entry in await self._snapshot_fetcher(guild):
                user = getattr(entry, "user", entry)
                user_id = _id(user)
                observations.append(
                    ModerationObservation(
                        guild_id=guild.id,
                        source_kind="discord_ban_snapshot",
                        source_key=f"{run.run_id}:{user_id}",
                        action_hint="ban",
                        target_user_id=user_id,
                        observed_at=started_at,
                        reason=getattr(entry, "reason", None),
                        import_batch_id=run.run_id,
                    )
                )
            await self._history.ingest_batch(
                guild.id,
                observations,
                audit_ban_cursor=None,
                audit_unban_cursor=None,
                red_modlog_cursor=None,
                completed_at=None,
            )
            run = await self._history.complete_migration_step(
                guild.id, run.run_id, "ban_snapshot"
            )

        completed_at = self._clock()
        historical_gap = _possible_historical_gap(guild, started_at)
        inserted_total = await self._history.migration_observation_count(
            guild.id,
            run.run_id,
        )
        await self._history.complete_initial_migration(
            guild.id,
            run.run_id,
            completed_at,
            json.dumps(
                {
                    "completed_at": completed_at.isoformat(),
                    "historical_gap": historical_gap,
                    "inserted_observations": inserted_total,
                },
                sort_keys=True,
            ),
            historical_gap,
        )
        return SyncReport(SyncMode.INITIAL, inserted_total, completed_at)

    async def synchronize(self, guild: Any, mode: SyncMode) -> SyncReport:
        started_at = self._clock()
        bot_user_id = (
            self._bot_user_id_source()
            if callable(self._bot_user_id_source)
            else self._bot_user_id_source
        )
        state = await self._history.status(guild.id)
        if mode is SyncMode.INITIAL and state.migration_state == "complete":
            return SyncReport(mode, 0, started_at)
        if mode is SyncMode.INITIAL:
            return await self._synchronize_initial(guild, bot_user_id, started_at)
        full = mode is SyncMode.REPAIR
        if mode is SyncMode.WEEKLY:
            boundary = snowflake_boundary(started_at - AUDIT_OVERLAP)
            ban_after = min(state.audit_ban_cursor, boundary) if state.audit_ban_cursor else boundary
            unban_after = min(state.audit_unban_cursor, boundary) if state.audit_unban_cursor else boundary
        elif full:
            ban_after = None
            unban_after = None
        else:
            ban_after = state.audit_ban_cursor
            unban_after = state.audit_unban_cursor

        cases = await self._modlog_fetcher(
            guild,
            after_case=None if full else state.red_modlog_cursor,
        )
        observations: list[ModerationObservation] = []
        for case in cases:
            item = modlog_observation(guild.id, case, bot_user_id, started_at)
            if item is not None:
                observations.append(item)
        red_cursor = max(
            [
                state.red_modlog_cursor or 0,
                *(
                    _id(getattr(case, "case_number", None)) or 0
                    for case in cases
                ),
            ]
        ) or None
        inserted = await self._history.ingest_batch(
            guild.id,
            observations,
            audit_ban_cursor=None,
            audit_unban_cursor=None,
            red_modlog_cursor=red_cursor,
            completed_at=None,
        )

        ban_observations, ban_cursor, partial_inserted = (
            await self._fetch_audit_source(
                guild,
                action="ban",
                after_id=ban_after,
                cursor_floor=state.audit_ban_cursor,
                bot_user_id=bot_user_id,
                observed_at=started_at,
            )
        )
        inserted += partial_inserted
        inserted += await self._history.ingest_batch(
            guild.id,
            ban_observations,
            audit_ban_cursor=ban_cursor,
            audit_unban_cursor=None,
            red_modlog_cursor=None,
            completed_at=None,
        )

        unban_observations, unban_cursor, partial_inserted = (
            await self._fetch_audit_source(
                guild,
                action="unban",
                after_id=unban_after,
                cursor_floor=state.audit_unban_cursor,
                bot_user_id=bot_user_id,
                observed_at=started_at,
            )
        )
        inserted += partial_inserted
        completed_at = None if full else self._clock()
        inserted += await self._history.ingest_batch(
            guild.id,
            unban_observations,
            audit_ban_cursor=None,
            audit_unban_cursor=unban_cursor,
            red_modlog_cursor=None,
            completed_at=completed_at,
            reconciliation=mode is SyncMode.WEEKLY,
        )

        if full:
            run_id = uuid4().hex
            observations = []
            for entry in await self._snapshot_fetcher(guild):
                user = getattr(entry, "user", entry)
                user_id = _id(user)
                observations.append(
                    ModerationObservation(
                        guild_id=guild.id,
                        source_kind="discord_ban_snapshot",
                        source_key=f"{run_id}:{user_id}",
                        action_hint="ban",
                        target_user_id=user_id,
                        observed_at=started_at,
                        reason=getattr(entry, "reason", None),
                        import_batch_id=run_id,
                    )
                )
            completed_at = self._clock()
            inserted += await self._history.ingest_batch(
                guild.id,
                observations,
                audit_ban_cursor=None,
                audit_unban_cursor=None,
                red_modlog_cursor=None,
                completed_at=completed_at,
                historical_gap=_possible_historical_gap(guild, started_at),
            )
        if completed_at is None:
            raise RuntimeError("Synchronization completion timestamp is missing")
        return SyncReport(mode, inserted, completed_at)
