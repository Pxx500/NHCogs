"""Persisted incident and timer state for JoinWatch."""

from __future__ import annotations

import asyncio
import logging
import typing
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial

import discord

log = logging.getLogger("red.Honeypot")

_LIVE_KINDS = ("verified", "pending_role", "pending_assignment")
_KIND_CONFIG = {
    "verified": "joinwatch_verified_members",
    "pending_role": "joinwatch_pending_roles",
    "pending_assignment": "joinwatch_pending_role_assignments",
}
_STORE_KIND = {config_key: kind for kind, config_key in _KIND_CONFIG.items()}

JOINWATCH_RETRY_DELAY_MINUTES = 1
JOINWATCH_MAX_RETRIES = 5


@asynccontextmanager
async def member_lock(cog, guild_id: int, member_id: int):
    """Serialize timer, verification, and shared-role effects for one member."""
    locks = getattr(cog, "_joinwatch_member_locks", None)
    if locks is None:
        locks = cog._joinwatch_member_locks = {}
    owners = getattr(cog, "_joinwatch_lock_owners", None)
    if owners is None:
        owners = cog._joinwatch_lock_owners = {}
    key = (guild_id, member_id)
    task = asyncio.current_task()
    if owners.get(key) is task:
        yield
        return
    async with locks.setdefault(key, asyncio.Lock()):
        owners[key] = task
        try:
            yield
        finally:
            owners.pop(key, None)


def _source_lock(cog) -> asyncio.Lock:
    """Serialize a live-source check with export and restore.

    Callers that also need member_lock take that lock first.
    """
    lock = getattr(cog, "_joinwatch_live_source_lock", None)
    if lock is None:
        lock = cog._joinwatch_live_source_lock = asyncio.Lock()
    return lock


def kind_for_store(store_name: str) -> str:
    """Map a Config key to a live-state kind. See Honeypot stored data."""
    return _STORE_KIND[store_name]


async def _uses_sqlite(cog, guild_id: int | None) -> bool:
    store = getattr(cog, "_case_store", None)
    if store is None or guild_id is None or not hasattr(store, "cutover_source"):
        return False
    source = await asyncio.to_thread(store.cutover_source, int(guild_id))
    return source == "sqlite"


async def read_row(cog, guild, user_id: int, kind: str) -> dict | None:
    """Read one live row from SQLite or Config. See Honeypot stored data."""
    if await _uses_sqlite(cog, guild.id):
        return await asyncio.to_thread(cog._case_store.get, int(guild.id), int(user_id), kind)
    config = cog.config.guild(guild)
    accessor = getattr(config, _KIND_CONFIG[kind], None)
    member_key = str(user_id)
    if accessor is None:
        raw = await config.all()
        current = (raw.get(_KIND_CONFIG[kind]) or {}).get(member_key)
        return dict(current) if isinstance(current, dict) else None
    opened = accessor()
    if hasattr(opened, "__aenter__"):
        async with opened as entries:
            current = entries.get(member_key)
            return dict(current) if isinstance(current, dict) else None
    entries = await opened
    current = entries.get(member_key) if isinstance(entries, dict) else None
    return dict(current) if isinstance(current, dict) else None


async def rows_for_member(cog, guild, user_id: int) -> dict[str, dict]:
    """Read one member's live rows. See Honeypot stored data."""
    if await _uses_sqlite(cog, guild.id):
        return await asyncio.to_thread(
            cog._case_store.rows_for_member, int(guild.id), int(user_id)
        )
    rows = {}
    for kind in _LIVE_KINDS:
        row = await read_row(cog, guild, user_id, kind)
        if row is not None:
            rows[kind] = row
    return rows


async def is_verified(cog, guild, user_id: int) -> bool:
    """Report a verified member without loading other accounts. See Honeypot stored data."""
    if await _uses_sqlite(cog, guild.id):
        return await asyncio.to_thread(cog._case_store.is_verified, int(guild.id), int(user_id))
    return await read_row(cog, guild, user_id, "verified") is not None


async def write_row(
    cog,
    guild,
    user_id: int,
    kind: str,
    payload: dict,
    *,
    expected_incident_id: str | None = None,
    compare: bool = False,
) -> bool:
    """Write one live row. A compare refuses a different incident. See Honeypot stored data."""
    stored = dict(payload)
    async with _source_lock(cog):
        if await _uses_sqlite(cog, guild.id):
            return await asyncio.to_thread(
                partial(
                    cog._case_store.upsert,
                    int(guild.id),
                    int(user_id),
                    kind,
                    stored,
                    expected_incident_id=expected_incident_id,
                    compare=compare,
                )
            )
        async with getattr(cog.config.guild(guild), _KIND_CONFIG[kind])() as entries:
            current = entries.get(str(user_id))
            if compare and (
                not isinstance(current, dict) or current.get("incident_id") != expected_incident_id
            ):
                return False
            entries[str(user_id)] = stored
        return True


async def delete_row(
    cog,
    guild,
    user_id: int,
    kind: str,
    *,
    expected_incident_id: str | None = None,
    compare: bool = False,
) -> bool:
    """Delete one live row. A compare refuses a different incident. See Honeypot stored data."""
    async with _source_lock(cog):
        if await _uses_sqlite(cog, guild.id):
            return await asyncio.to_thread(
                cog._case_store.delete,
                int(guild.id),
                int(user_id),
                kind,
                expected_incident_id,
                compare,
            )
        async with getattr(cog.config.guild(guild), _KIND_CONFIG[kind])() as entries:
            current = entries.get(str(user_id))
            if compare and (
                not isinstance(current, dict) or current.get("incident_id") != expected_incident_id
            ):
                return False
            entries.pop(str(user_id), None)
        return True


async def _read_config_map(cog, guild, kind: str) -> dict:
    config = cog.config.guild(guild)
    accessor = getattr(config, _KIND_CONFIG[kind], None)
    if accessor is None:
        all_config = getattr(config, "all", None)
        if all_config is None:
            return {}
        raw = await all_config()
        entries = raw.get(_KIND_CONFIG[kind], {})
    else:
        opened = accessor()
        if hasattr(opened, "__aenter__"):
            async with opened as current:
                entries = dict(current)
        else:
            entries = await opened
    if not isinstance(entries, dict):
        return {}
    return {
        str(user_id): dict(entry) if isinstance(entry, dict) else entry
        for user_id, entry in entries.items()
    }


async def open_maps(cog, guild) -> dict[str, dict]:
    """Read open rows for the timer and restore, never the verified map. See Honeypot stored data."""
    if await _uses_sqlite(cog, getattr(guild, "id", None)):
        return await asyncio.to_thread(cog._case_store.list_open, int(guild.id))
    return {
        "pending_role": await _read_config_map(cog, guild, "pending_role"),
        "pending_assignment": await _read_config_map(cog, guild, "pending_assignment"),
    }


async def live_counts(cog, guild) -> dict[str, int]:
    """Count live rows for status commands. See Honeypot stored data."""
    if await _uses_sqlite(cog, getattr(guild, "id", None)):
        return await asyncio.to_thread(cog._case_store.counts, int(guild.id))
    totals = {}
    for kind in _KIND_CONFIG:
        entries = await _read_config_map(cog, guild, kind)
        totals[kind] = len(entries) if isinstance(entries, dict) else 0
    return totals


async def _clear_config_maps(cog, guild_id: int) -> None:
    config = cog.config.guild_from_id(guild_id)
    for config_key in _KIND_CONFIG.values():
        await config.clear_raw(config_key)


async def cutover_guild(cog, guild_id: int, values: dict | None = None) -> bool:
    """Copy one guild into SQLite when the rows match. See Honeypot stored data."""
    store = cog._case_store
    source = await asyncio.to_thread(store.cutover_source, int(guild_id))
    if source == "sqlite":
        await _clear_config_maps(cog, guild_id)
        return True
    config = cog.config.guild_from_id(guild_id)
    if values is None:
        values = await config.all()
    maps = {}
    for kind, config_key in _KIND_CONFIG.items():
        raw_map = values.get(config_key, {})
        if not isinstance(raw_map, dict):
            await cog._record_operational_failure(
                guild_id,
                "joinwatch_live_cutover",
                "JoinWatch live state stayed in Config because a map was unreadable",
            )
            return False
        maps[kind] = raw_map
    if all(not item for item in maps.values()):
        return True
    copied = await asyncio.to_thread(store.replace_from_config, int(guild_id), maps)
    if not copied:
        await cog._record_operational_failure(
            guild_id,
            "joinwatch_live_cutover",
            "JoinWatch live state stayed in Config because the SQLite copy did not match",
        )
        return False
    try:
        await _clear_config_maps(cog, guild_id)
    except Exception as error:
        log.exception("JoinWatch Config clear failed for guild %s", guild_id)
        await cog._record_operational_failure(
            guild_id,
            "joinwatch_live_cutover",
            "JoinWatch live state is in SQLite but Config was not cleared: "
            f"{type(error).__name__}",
        )
        return False
    return True


async def cutover_live_state(cog) -> None:
    """Copy JoinWatch Config maps on cog load, including a reload. See Honeypot stored data."""
    store = getattr(cog, "_case_store", None)
    if store is None or not hasattr(store, "replace_from_config"):
        return
    await asyncio.to_thread(store.expire_live_backups, datetime.now(timezone.utc))
    for guild_id, values in (await cog.config.all_guilds()).items():
        try:
            await cutover_guild(cog, int(guild_id), values)
        except Exception as error:
            log.exception("JoinWatch live cutover failed for guild %s", guild_id)
            await cog._record_operational_failure(
                int(guild_id),
                "joinwatch_live_cutover",
                f"JoinWatch live state stayed in Config: {type(error).__name__}",
            )


async def _write_config_map(config, config_key: str, entries: dict) -> None:
    group = getattr(config, config_key)
    if hasattr(group, "set"):
        await group.set(entries)
        return
    async with group() as current:
        current.clear()
        current.update(entries)


async def export_live_state(cog, guild) -> None:
    """Copy SQLite live rows back into Config, then clear the marker. See Honeypot stored data."""
    async with _source_lock(cog):
        maps = await asyncio.to_thread(cog._case_store.config_maps, int(guild.id))
        config = cog.config.guild(guild)
        for kind, config_key in _KIND_CONFIG.items():
            await _write_config_map(config, config_key, maps[kind])
        await asyncio.to_thread(cog._case_store.clear_cutover, int(guild.id))


async def restore_live_backup(cog, guild) -> bool:
    """Write the one-time backup into Config and clear the marker. See Honeypot stored data."""
    async with _source_lock(cog):
        backup = await asyncio.to_thread(cog._case_store.live_backup, int(guild.id))
        if backup is None:
            return False
        config = cog.config.guild(guild)
        for kind, config_key in _KIND_CONFIG.items():
            await _write_config_map(config, config_key, backup.get(kind, {}))
        await asyncio.to_thread(cog._case_store.clear_cutover, int(guild.id))
        return True


@dataclass(frozen=True, slots=True)
class JoinwatchSelectedAction:
    action: typing.Literal["discard_assignment", "apply_role", "discard_role", "expire_role"]
    member_key: str
    member_id: int | None
    role_id: int | None
    due_at: datetime | None
    data: typing.Any


@dataclass(frozen=True, slots=True)
class JoinwatchSelection:
    clear_assignments: bool
    assignment_actions: tuple[JoinwatchSelectedAction, ...]
    role_actions: tuple[JoinwatchSelectedAction, ...]


@dataclass(frozen=True, slots=True)
class JoinwatchRetryTransition:
    attempts: int
    retry_at: datetime | None

    @property
    def terminal(self) -> bool:
        return self.retry_at is None


def select_due_joinwatch_assignments(
    *,
    now: datetime,
    assignments_enabled: bool,
    pending_assignments: typing.Mapping[str, typing.Any],
    pending_roles: typing.Mapping[str, typing.Any],
) -> JoinwatchSelection:
    assignment_actions: list[JoinwatchSelectedAction] = []
    has_group_assignments = any(
        isinstance(data, dict) and (data.get("source") in ("group", "wave", "test") or data.get("restore_incident"))
        for data in pending_assignments.values()
    )
    if assignments_enabled or has_group_assignments:
        for member_key_value, data in pending_assignments.items():
            if isinstance(data, dict) and data.get("history_terminal"):
                continue
            member_key = str(member_key_value)
            active = pending_roles.get(member_key)
            if isinstance(data, dict) and (
                data.get("verification_state") == "enrolling"
                or (data.get("source") in ("wave", "test") and (
                    not isinstance(active, dict) or active.get("incident_id") != data.get("incident_id")
                ))
            ):
                continue
            if not assignments_enabled and (
                not isinstance(data, dict) or (data.get("source") != "group" and not data.get("restore_incident"))
            ):
                assignment_actions.append(
                    JoinwatchSelectedAction(
                        "discard_assignment", member_key, None, None, None, data
                    )
                )
                continue
            try:
                member_id = int(member_key_value)
                role_id = int(typing.cast(typing.Any, data["role_id"]))
                due_at = datetime.fromisoformat(typing.cast(str, data["apply_at"]))
            except (KeyError, TypeError, ValueError):
                assignment_actions.append(
                    JoinwatchSelectedAction(
                        "discard_assignment",
                        member_key,
                        None,
                        None,
                        None,
                        data,
                    )
                )
                continue
            if due_at <= now:
                assignment_actions.append(
                    JoinwatchSelectedAction(
                        "apply_role",
                        member_key,
                        member_id,
                        role_id,
                        due_at,
                        data,
                    )
                )

    role_actions: list[JoinwatchSelectedAction] = []
    for member_key_value, data in pending_roles.items():
        if isinstance(data, dict) and (
            data.get("test") or data.get("history_terminal")
            or data.get("verification_state") == "release_pending"
        ):
            continue
        member_key = str(member_key_value)
        try:
            member_id = int(member_key_value)
            role_id = int(typing.cast(typing.Any, data["role_id"]))
            due_at = datetime.fromisoformat(typing.cast(str, data["expires_at"]))
        except (KeyError, TypeError, ValueError):
            role_actions.append(
                JoinwatchSelectedAction(
                    "discard_role",
                    member_key,
                    None,
                    None,
                    None,
                    data,
                )
            )
            continue
        if due_at <= now:
            role_actions.append(
                JoinwatchSelectedAction(
                    "expire_role",
                    member_key,
                    member_id,
                    role_id,
                    due_at,
                    data,
                )
            )

    return JoinwatchSelection(
        clear_assignments=bool(
            pending_assignments and not assignments_enabled and not has_group_assignments
        ),
        assignment_actions=tuple(assignment_actions),
        role_actions=tuple(role_actions),
    )


def build_incident(
    member: discord.Member,
    *,
    now: datetime,
    expires_at: datetime,
    account_age_hours: int,
    existing: typing.Mapping[str, typing.Any] | None = None,
) -> dict[str, typing.Any]:
    joined_at = member.joined_at or now
    incident = dict(existing or {})
    first_joined_at = incident.get("first_joined_at")
    if not isinstance(first_joined_at, str):
        first_joined_at = incident.get("applied_at")
    if not isinstance(first_joined_at, str):
        first_joined_at = joined_at.isoformat()
    try:
        previous_count = max(0, int(incident.get("join_count", 0)))
    except (TypeError, ValueError):
        previous_count = 0
    if existing is not None and previous_count == 0:
        previous_count = 1
    stored_deadline = incident.get("expires_at")
    if not isinstance(stored_deadline, str):
        stored_deadline = expires_at.isoformat()
    incident.update(
        {
            "first_joined_at": first_joined_at,
            "last_joined_at": joined_at.isoformat(),
            "join_count": previous_count + 1,
            "expires_at": stored_deadline,
            "member_label": incident.get("member_label") or f"{member.display_name} ({member})",
            "member_id": member.id,
            "member_mention": member.mention,
            "member_display_name": incident.get("member_display_name") or member.display_name,
            "member_avatar_url": incident.get("member_avatar_url")
            or (str(member.display_avatar) if member.display_avatar else None),
            "account_age_hours": incident.get("account_age_hours") or account_age_hours,
        }
    )
    return incident


async def store_pending_role(
    cog,
    member: discord.Member,
    role_id: int,
    expires_at: datetime,
    *,
    applied_at: datetime | None = None,
    alert_channel_id: int | None = None,
    alert_message_id: int | None = None,
    incident: typing.Mapping[str, typing.Any] | None = None,
) -> None:
    pending_role = dict(incident or {})
    pending_role.update(
        {
            "role_id": role_id,
            "applied_at": (applied_at or datetime.now(timezone.utc)).isoformat(),
            "expires_at": expires_at.isoformat(),
        }
    )
    owner = getattr(cog, "_joinwatch_verification", None)
    if owner is not None:
        await owner.capture_enrollment(member, pending_role)
        pending_role.setdefault("verification_state", "active")
        if not (incident or {}).get("applied_at"):
            owner.event(pending_role, "enrolled")
    if alert_channel_id is not None and alert_message_id is not None:
        pending_role["alert_channel_id"] = alert_channel_id
        pending_role["alert_message_id"] = alert_message_id
    await write_row(cog, member.guild, member.id, "pending_role", pending_role)
    if owner is not None:
        await owner.persist_history(member.guild, member.id, pending_role)


async def delete_pending_role(cog, guild: discord.Guild, member_id: int | str, *, outcome="cancelled") -> None:
    owner = getattr(cog, "_joinwatch_verification", None)
    if owner is not None:
        entry = await read_row(cog, guild, int(member_id), "pending_role")
        if entry is not None:
            await owner.finish(guild, int(member_id), entry, outcome)
        return
    await delete_row(cog, guild, int(member_id), "pending_role")


async def mark_manual_role_reason(cog, member, role_ids, *, remove=False) -> None:
    current = await read_row(cog, member.guild, member.id, "pending_role")
    if current is None or current.get("role_id") not in role_ids:
        return set()
    reasons = set(current.get("manual_role_reasons", []))
    added = {current["role_id"]} - reasons
    if remove:
        reasons.difference_update(role_ids)
    else:
        reasons.add(current["role_id"])
    current["manual_role_reasons"] = sorted(reasons)
    await write_row(cog, member.guild, member.id, "pending_role", current)
    return added


async def store_pending_assignment(
    cog,
    member: discord.Member,
    role_id: int,
    apply_at: datetime,
    *,
    expires_at: datetime | None = None,
    incident: typing.Mapping[str, typing.Any] | None = None,
) -> None:
    pending_assignment = dict(incident or {})
    pending_assignment.update(
        {
            "role_id": role_id,
            "apply_at": apply_at.isoformat(),
        }
    )
    if expires_at is not None:
        pending_assignment["expires_at"] = expires_at.isoformat()
    await write_row(cog, member.guild, member.id, "pending_assignment", pending_assignment)
    owner = getattr(cog, "_joinwatch_verification", None)
    if owner is not None:
        await owner.capture_enrollment(member, pending_assignment)
        await owner.persist_history(member.guild, member.id, pending_assignment,
                                    store_name="joinwatch_pending_role_assignments")


async def delete_pending_assignment(cog, guild: discord.Guild, member_id: int | str, *, outcome="cancelled") -> None:
    owner = getattr(cog, "_joinwatch_verification", None)
    if owner is not None:
        entry = await read_row(cog, guild, int(member_id), "pending_assignment")
        active = await read_row(cog, guild, int(member_id), "pending_role")
        if entry is not None and not (active and active.get("incident_id") == entry.get("incident_id")):
            await owner.finish(guild, int(member_id), entry, outcome,
                               store_name="joinwatch_pending_role_assignments")
            return
    await delete_row(cog, guild, int(member_id), "pending_assignment")


async def clear_pending_assignments(cog, guild: discord.Guild) -> None:
    pending = (await open_maps(cog, guild))["pending_assignment"]
    for member_id in tuple(pending):
        async with member_lock(cog, guild.id, int(member_id)):
            await delete_pending_assignment(cog, guild, member_id)


async def store_alert_reference(
    cog,
    guild: discord.Guild,
    member_id: int,
    channel_id: int,
    message_id: int,
) -> bool:
    stored = False
    async with member_lock(cog, guild.id, member_id):
        for kind in ("pending_assignment", "pending_role"):
            incident = await read_row(cog, guild, member_id, kind)
            if incident is None:
                continue
            incident["alert_channel_id"] = channel_id
            incident["alert_message_id"] = message_id
            await write_row(cog, guild, member_id, kind, incident)
            stored = True
    return stored


async def disable_alert_updates(
    cog,
    guild: discord.Guild,
    member_id: int,
    *,
    incident: dict[str, typing.Any] | None = None,
) -> None:
    async with member_lock(cog, guild.id, member_id):
        for kind in ("pending_assignment", "pending_role"):
            stored_incident = await read_row(cog, guild, member_id, kind)
            if stored_incident is None:
                continue
            stored_incident["alert_updates_disabled"] = True
            await write_row(cog, guild, member_id, kind, stored_incident)
        if incident is not None:
            incident["alert_updates_disabled"] = True


def next_retry_count(data: typing.Mapping[str, typing.Any]) -> int | None:
    try:
        retry_count = int(data.get("retry_count", 0)) + 1
    except (TypeError, ValueError):
        retry_count = 1
    return retry_count if retry_count <= JOINWATCH_MAX_RETRIES else None


async def _reschedule_retry(
    cog,
    guild: discord.Guild,
    member_key: str,
    data: dict[str, typing.Any],
    now: datetime,
    *,
    store_name: str,
    deadline_key: str,
) -> JoinwatchRetryTransition:
    retry_count = next_retry_count(data)
    kind = kind_for_store(store_name)
    if retry_count is None:
        owner = getattr(cog, "_joinwatch_verification", None)
        if owner is not None:
            await owner.finish(guild, int(member_key), data, "action_failed", store_name=store_name)
        else:
            await delete_row(cog, guild, int(member_key), kind)
        return JoinwatchRetryTransition(
            attempts=JOINWATCH_MAX_RETRIES + 1,
            retry_at=None,
        )

    retry_at = now + timedelta(minutes=JOINWATCH_RETRY_DELAY_MINUTES)
    current = await read_row(cog, guild, int(member_key), kind)
    if current is not None:
        current[deadline_key] = retry_at.isoformat()
        current["retry_count"] = retry_count
        await write_row(cog, guild, int(member_key), kind, current)
    data[deadline_key] = retry_at.isoformat()
    data["retry_count"] = retry_count
    return JoinwatchRetryTransition(
        attempts=retry_count,
        retry_at=retry_at,
    )


async def reschedule_assignment_retry(
    cog,
    guild: discord.Guild,
    member_key: str,
    data: dict[str, typing.Any],
    now: datetime,
) -> JoinwatchRetryTransition:
    return await _reschedule_retry(
        cog,
        guild,
        member_key,
        data,
        now,
        store_name="joinwatch_pending_role_assignments",
        deadline_key="apply_at",
    )


async def reschedule_role_retry(
    cog,
    guild: discord.Guild,
    member_key: str,
    data: dict[str, typing.Any],
    now: datetime,
) -> JoinwatchRetryTransition:
    return await _reschedule_retry(
        cog,
        guild,
        member_key,
        data,
        now,
        store_name="joinwatch_pending_roles",
        deadline_key="expires_at",
    )


async def reschedule_pending_roles(
    cog,
    guild: discord.Guild,
    old_timer_minutes: int,
    new_timer_minutes: int,
) -> tuple[tuple[dict[str, typing.Any], int, datetime], ...]:
    updates: list[tuple[dict[str, typing.Any], int, datetime]] = []
    pending_roles = (await open_maps(cog, guild))["pending_role"]
    for member_key in tuple(pending_roles):
        try:
            member_id = int(member_key)
        except (TypeError, ValueError):
            continue
        async with member_lock(cog, guild.id, member_id):
            data = await read_row(cog, guild, member_id, "pending_role")
            if data is None:
                continue
            try:
                role_id = int(data["role_id"])
                if data.get("applied_at") is not None:
                    applied_at = datetime.fromisoformat(data["applied_at"])
                else:
                    old_expires_at = datetime.fromisoformat(data["expires_at"])
                    applied_at = old_expires_at - timedelta(minutes=old_timer_minutes)
            except (KeyError, TypeError, ValueError):
                continue
            expires_at = applied_at + timedelta(minutes=new_timer_minutes)
            data["applied_at"] = applied_at.isoformat()
            data["expires_at"] = expires_at.isoformat()
            await write_row(cog, guild, member_id, "pending_role", data)
            published = (dict(data), role_id, expires_at)
        updates.append(published)
    return tuple(updates)
