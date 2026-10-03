"""JoinWatch's first-join observations and creation-time cohort rules."""

from __future__ import annotations

import asyncio
import copy
import json
from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

HISTORY_VERSION = 1
MAX_HISTORY_BYTES = 20 * 1024 * 1024
MAX_HISTORY_ACCOUNTS = 100_000
MAX_HISTORY_SOURCES = 100
MAX_SOURCE_LENGTH = 200
MAX_JOIN_WINDOW_MINUTES = 1440
MAX_CREATION_DISTANCE_HOURS = 8760
MINIMUM_GROUP_ACCOUNTS = 2
PREVIEW_LIFETIME_MINUTES = 10
LIVE_HISTORY_RETENTION_DAYS = 90
DISCORD_EPOCH_MILLISECONDS = 1420070400000
MAX_SNOWFLAKE = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class GroupCriteria:
    minimum_accounts: int
    join_window_minutes: int
    creation_distance_hours: int

    def __post_init__(self):
        values = (self.minimum_accounts, self.join_window_minutes, self.creation_distance_hours)
        if any(type(value) is not int for value in values):
            raise ValueError("Group criteria must use whole numbers")
        if not MINIMUM_GROUP_ACCOUNTS <= self.minimum_accounts <= MAX_HISTORY_ACCOUNTS:
            raise ValueError("Minimum accounts is outside supported history bounds")
        if not 1 <= self.join_window_minutes <= MAX_JOIN_WINDOW_MINUTES:
            raise ValueError("Join window must be between 1 and 1440 minutes")
        if not 1 <= self.creation_distance_hours <= MAX_CREATION_DISTANCE_HOURS:
            raise ValueError("Creation distance must be between 1 and 8760 hours")


@dataclass(frozen=True, slots=True)
class JoinObservation:
    user_id: int
    first_joined_at: datetime
    created_at: datetime


def match_cohort(
    observations: Iterable[JoinObservation],
    trigger: JoinObservation,
    criteria: GroupCriteria,
) -> tuple[int, ...]:
    """Include earlier peers within both inclusive windows around the trigger."""
    earliest = trigger.first_joined_at - timedelta(minutes=criteria.join_window_minutes)
    distance = timedelta(hours=criteria.creation_distance_hours)
    first_by_user = {}
    for row in observations:
        previous = first_by_user.get(row.user_id)
        if previous is None or row.first_joined_at < previous.first_joined_at:
            first_by_user[row.user_id] = row
    original = first_by_user.get(trigger.user_id)
    if original is not None and original.first_joined_at != trigger.first_joined_at:
        return ()
    first_by_user.setdefault(trigger.user_id, trigger)
    matching = sorted(
        (row for row in first_by_user.values()
         if earliest <= row.first_joined_at <= trigger.first_joined_at
         and abs(row.created_at - trigger.created_at) <= distance),
        key=lambda row: (row.first_joined_at, row.user_id),
    )
    if len(matching) < criteria.minimum_accounts:
        return ()
    return tuple(row.user_id for row in matching)


def utc_timestamp(value: Any) -> datetime:
    """Require timezone-aware timestamps rather than guessing an archive timezone."""
    if not isinstance(value, str):
        raise TypeError("History timestamps must be timezone-aware ISO 8601 strings")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid history timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("History timestamps need an explicit timezone")
    return result.astimezone(timezone.utc)


def snowflake_id(value: Any) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("History identifiers must be decimal strings")
    result = int(value)
    if not 0 < result <= MAX_SNOWFLAKE:
        raise ValueError("History identifier is outside Discord's supported range")
    return result


def created_at(user_id: int) -> datetime:
    milliseconds = (user_id >> 22) + DISCORD_EPOCH_MILLISECONDS
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc)


def empty_history() -> dict[str, Any]:
    return {"version": HISTORY_VERSION, "revision": 0, "import_revision": 0,
            "sources": [], "observations": {}}


def _decode_history(payload):
    if not isinstance(payload, bytes):
        return payload
    if len(payload) > MAX_HISTORY_BYTES:
        raise ValueError("History file exceeds 20 MiB")
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("History file must contain UTF-8 JSON") from error


def normalize_history(payload: bytes | dict, *, guild_id: int) -> dict[str, Any]:
    """Validate a bounded normalized export. Import never infers current membership."""
    payload = _decode_history(payload)
    if not isinstance(payload, dict) or payload.get("version") != HISTORY_VERSION:
        raise ValueError("Unsupported history schema version")
    if snowflake_id(payload.get("guild_id")) != guild_id:
        raise ValueError("History belongs to another server")
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip() or len(source) > MAX_SOURCE_LENGTH:
        raise ValueError("History needs a source label of at most 200 characters")
    complete = payload.get("complete")
    if type(complete) is not bool:
        raise ValueError("History completeness must be true or false")
    generated = utc_timestamp(payload.get("generated_at"))
    start = utc_timestamp(payload.get("range_start"))
    end = utc_timestamp(payload.get("range_end"))
    if not start <= end <= generated:
        raise ValueError("History date range must end before its generation date")
    rows = payload.get("observations")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_HISTORY_ACCOUNTS:
        raise ValueError("History needs between 1 and 100000 observations")
    result = empty_history()
    result["sources"] = [{"source": source.strip(), "generated_at": generated.isoformat(),
                          "range_start": start.isoformat(), "range_end": end.isoformat(),
                          "complete": complete}]
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("Each history observation must be an object")
        uid = snowflake_id(row.get("user_id"))
        joined = utc_timestamp(row.get("first_joined_at"))
        if not start <= joined <= end or joined < created_at(uid):
            raise ValueError("First observed join is outside the source range or before account creation")
        key = str(uid)
        previous = result["observations"].get(key)
        if previous is None or joined < utc_timestamp(previous["first_joined_at"]):
            result["observations"][key] = {"first_joined_at": joined.isoformat(), "imported": True}
    return result


def merge_history(existing: dict, incoming: dict) -> dict[str, Any]:
    result = copy.deepcopy(existing)
    for source in incoming["sources"]:
        if source not in result["sources"]:
            result["sources"].append(source)
    if len(result["sources"]) > MAX_HISTORY_SOURCES:
        raise ValueError("History source limit reached. Review retained history before another import")
    for uid, row in incoming["observations"].items():
        previous = result["observations"].get(uid)
        if previous is None or utc_timestamp(row["first_joined_at"]) < utc_timestamp(previous["first_joined_at"]):
            result["observations"][uid] = copy.deepcopy(row)
        elif row.get("imported"):
            previous["imported"] = True
    if len(result["observations"]) > MAX_HISTORY_ACCOUNTS:
        raise ValueError("History account limit reached. No observations were discarded")
    if result != existing:
        result["revision"] = int(existing["revision"]) + 1
    return result


def history_observations(history: dict) -> tuple[JoinObservation, ...]:
    return tuple(sorted(
        (JoinObservation(int(uid), utc_timestamp(row["first_joined_at"]), created_at(int(uid)))
         for uid, row in history["observations"].items()),
        key=lambda row: (row.first_joined_at, row.user_id),
    ))


class _CreationWindow:
    """Count active peers separately from the as-yet-unmatched output index."""

    def __init__(self, dates):
        self.dates = dates
        self.size = 1 << (len(dates) - 1).bit_length()
        self.counts = [0] * (self.size * 2)
        self.unmatched = [0] * (self.size * 2)
        self.users = [set() for _ in dates]

    def _update(self, position, total_delta, unmatched_delta):
        node = position + self.size
        while node:
            self.counts[node] += total_delta
            self.unmatched[node] += unmatched_delta
            node //= 2

    def add(self, row):
        position = bisect_left(self.dates, row.created_at)
        self.users[position].add(row.user_id)
        self._update(position, 1, 1)

    def remove(self, row):
        position = bisect_left(self.dates, row.created_at)
        was_unmatched = row.user_id in self.users[position]
        self.users[position].discard(row.user_id)
        self._update(position, -1, -int(was_unmatched))

    def match(self, created, distance, minimum):
        left = bisect_left(self.dates, created - distance)
        right = bisect_right(self.dates, created + distance)
        node_left, node_right = left + self.size, right + self.size
        count = 0
        while node_left < node_right:
            if node_left % 2:
                count += self.counts[node_left]
                node_left += 1
            if node_right % 2:
                node_right -= 1
                count += self.counts[node_right]
            node_left //= 2
            node_right //= 2
        if count < minimum:
            return set()
        matched = set()
        stack = [(1, 0, self.size)]
        while stack:
            node, start, end = stack.pop()
            if not self.unmatched[node] or end <= left or start >= right:
                continue
            if end - start == 1:
                matched.update(self.users[start])
                self._update(start, 0, -len(self.users[start]))
                self.users[start].clear()
            else:
                middle = (start + end) // 2
                stack.extend(((node * 2, start, middle), (node * 2 + 1, middle, end)))
        return matched


def historical_matches(history: dict, criteria: GroupCriteria) -> tuple[int, ...]:
    """Replay first joins in O(n log n), including already-matched threshold peers."""
    rows = history_observations(history)
    if not rows:
        return ()
    window = _CreationWindow(sorted({row.created_at for row in rows}))
    active = deque()
    matched = set()
    distance = timedelta(hours=criteria.creation_distance_hours)
    for trigger in rows:
        earliest = trigger.first_joined_at - timedelta(minutes=criteria.join_window_minutes)
        while active and active[0].first_joined_at < earliest:
            window.remove(active.popleft())
        window.add(trigger)
        active.append(trigger)
        matched.update(window.match(trigger.created_at, distance, criteria.minimum_accounts))
    return tuple(row.user_id for row in rows if row.user_id in matched)


class JoinwatchGroups:
    """Persist history and criteria without granting punishment ownership."""

    def __init__(self, cog):
        self.cog = cog
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild):
        return self._locks.setdefault(guild.id, asyncio.Lock())

    async def read_history(self, guild):
        return await asyncio.to_thread(self.cog._case_store.get_joinwatch_history, guild.id)

    async def _save_history(self, guild, history):
        await asyncio.to_thread(self.cog._case_store.save_joinwatch_history, guild.id, history)

    async def _read_criteria(self, guild) -> GroupCriteria:
        cfg = self.cog.config.guild(guild)
        return GroupCriteria(await cfg.joinwatch_groups_minimum_accounts(),
                             await cfg.joinwatch_groups_join_window_minutes(),
                             await cfg.joinwatch_groups_creation_distance_hours())

    async def criteria(self, guild) -> GroupCriteria:
        async with self._lock(guild):
            return await self._read_criteria(guild)

    async def import_history(self, guild, payload, *, now=None) -> dict:
        normalized = await asyncio.to_thread(normalize_history, payload, guild_id=guild.id)
        if utc_timestamp(normalized["sources"][0]["generated_at"]) > (now or datetime.now(timezone.utc)):
            raise ValueError("History generation date is in the future")
        async with self._lock(guild):
            existing = await self.read_history(guild)
            merged = merge_history(existing, normalized)
            if merged != existing:
                merged["import_revision"] = existing.get("import_revision", 0) + 1
            await self._save_history(guild, merged)
        return {"imported": len(normalized["observations"]), "total": len(merged["observations"]),
                "revision": merged["revision"], **normalized["sources"][0]}

    async def observe(self, member, *, now=None) -> tuple[int, ...]:
        observed = now or datetime.now(timezone.utc)
        # Use the event time. Current joined_at cannot prove a first historical join.
        guild = member.guild
        async with self._lock(guild):
            history = await self.read_history(guild)
            key = str(member.id)
            if key in history["observations"]:
                return ()
            incoming = empty_history()
            incoming["observations"][key] = {"first_joined_at": observed.isoformat(), "imported": False}
            history = merge_history(history, incoming)
            await self._save_history(guild, history)
        if not await self.cog.config.guild(guild).joinwatch_groups_enabled():
            return ()
        criteria = await self.criteria(guild)
        earliest = observed - timedelta(minutes=criteria.join_window_minutes)
        rows = tuple(row for row in history_observations(history) if row.first_joined_at >= earliest)
        trigger = JoinObservation(member.id, observed, created_at(member.id))
        return match_cohort(rows, trigger, criteria)

    async def criteria_preview(self, guild, criteria, moderator_id, *, now=None) -> dict:
        preview = await self.cog._joinwatch_waves.preview(guild, criteria, moderator_id, now=now, persist=False)
        preview["previous"] = asdict(await self.criteria(guild))
        preview["proposed"] = asdict(criteria)
        return preview

    async def confirm_criteria(self, guild, preview, moderator_id, can_manage_messages, *, now=None) -> dict:
        async with self._lock(guild):
            await self.cog._joinwatch_waves.validate_preview(guild, preview, moderator_id, can_manage_messages, now=now)
            if asdict(await self._read_criteria(guild)) != preview["previous"]:
                raise ValueError("Group settings changed. Prepare a fresh preview")
            criteria = GroupCriteria(**preview["proposed"])
            cfg = self.cog.config.guild(guild)
            await cfg.joinwatch_groups_minimum_accounts.set(criteria.minimum_accounts)
            await cfg.joinwatch_groups_join_window_minutes.set(criteria.join_window_minutes)
            await cfg.joinwatch_groups_creation_distance_hours.set(criteria.creation_distance_hours)
        return {"criteria": asdict(criteria), "historical_enrollments": 0}

    async def prune(self, guild, *, now=None) -> int:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=LIVE_HISTORY_RETENTION_DAYS)
        async with self._lock(guild):
            history = await self.read_history(guild)
            keys = [uid for uid, row in history["observations"].items()
                    if not row.get("imported") and utc_timestamp(row["first_joined_at"]) < cutoff]
            for uid in keys:
                del history["observations"][uid]
            if keys:
                history["revision"] += 1
                await self._save_history(guild, history)
            return len(keys)

    async def delete_user(self, guild, user_id) -> None:
        async with self._lock(guild):
            history = await self.read_history(guild)
            if history["observations"].pop(str(user_id), None) is not None:
                history["revision"] += 1
                history["import_revision"] = history.get("import_revision", 0) + 1
                await self._save_history(guild, history)

    async def delete_guild(self, guild) -> None:
        async with self._lock(guild):
            await asyncio.to_thread(self.cog._case_store.clear_joinwatch_auxiliary, guild.id)
