from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from .history import NHModerationHistory
from .models import ModerationObservation

DISCORD_EPOCH_MS = 1_420_070_400_000
AUDIT_OVERLAP = timedelta(days=14)
HONEYPOT_REVIEW_PREFIX = "Honeypot review:"
RED_DELETED_USER_ID = 0xDE1

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


def audit_observation(
    guild_id: int,
    entry: Any,
    action_hint: str,
    bot_user_id: int,
    observed_at: datetime,
) -> ModerationObservation:
    executor_id = _id(getattr(entry, "user", None))
    automated = executor_id == bot_user_id
    return ModerationObservation(
        guild_id=guild_id,
        source_kind="discord_audit",
        source_key=str(entry.id),
        action_hint=action_hint,
        target_user_id=_id(getattr(entry, "target", None)),
        executor_user_id=executor_id,
        credited_moderator_hint=None if automated else executor_id,
        attribution_hint="automation" if automated else "human_direct",
        occurred_at=_datetime(getattr(entry, "created_at", None)),
        observed_at=observed_at,
        reason=getattr(entry, "reason", None),
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

    async def synchronize(self, guild: Any, mode: SyncMode) -> SyncReport:
        started_at = self._clock()
        bot_user_id = (
            self._bot_user_id_source()
            if callable(self._bot_user_id_source)
            else self._bot_user_id_source
        )
        state = await self._history.status(guild.id)
        full = mode in {SyncMode.INITIAL, SyncMode.REPAIR}
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
        ban_entries = await self._audit_fetcher(
            guild, action="ban", after_id=ban_after
        )
        unban_entries = await self._audit_fetcher(
            guild, action="unban", after_id=unban_after
        )
        observations: list[ModerationObservation] = []
        for case in cases:
            item = modlog_observation(guild.id, case, bot_user_id, started_at)
            if item is not None:
                observations.append(item)
        observations.extend(
            audit_observation(guild.id, entry, "ban", bot_user_id, started_at)
            for entry in ban_entries
        )
        observations.extend(
            audit_observation(guild.id, entry, "unban", bot_user_id, started_at)
            for entry in unban_entries
        )

        if full:
            run_id = uuid4().hex
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
        ban_cursor = max(
            [state.audit_ban_cursor or 0, *(_id(entry.id) or 0 for entry in ban_entries)]
        ) or None
        unban_cursor = max(
            [state.audit_unban_cursor or 0, *(_id(entry.id) or 0 for entry in unban_entries)]
        ) or None
        red_cursor = max(
            [state.red_modlog_cursor or 0, *(_id(getattr(case, "case_number", None)) or 0 for case in cases)]
        ) or None
        inserted = await self._history.ingest_batch(
            guild.id,
            observations,
            audit_ban_cursor=ban_cursor,
            audit_unban_cursor=unban_cursor,
            red_modlog_cursor=red_cursor,
            completed_at=completed_at,
            reconciliation=mode is SyncMode.WEEKLY,
            migration_complete=mode is SyncMode.INITIAL,
        )
        if mode is SyncMode.REPAIR:
            await self._history.rebuild(guild.id)
        return SyncReport(mode, inserted, completed_at)
