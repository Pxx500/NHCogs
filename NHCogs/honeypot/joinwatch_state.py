"""Persisted incident and timer state for JoinWatch."""

from __future__ import annotations

import typing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

JOINWATCH_RETRY_DELAY_MINUTES = 1
JOINWATCH_MAX_RETRIES = 5


@dataclass(frozen=True, slots=True)
class JoinwatchSelectedAction:
    action: typing.Literal[
        "discard_assignment", "apply_role", "discard_role", "expire_role"
    ]
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
    if assignments_enabled:
        for member_key_value, data in pending_assignments.items():
            member_key = str(member_key_value)
            try:
                member_id = int(member_key_value)
                role_id = int(typing.cast(typing.Any, data["role_id"]))
                due_at = datetime.fromisoformat(
                    typing.cast(str, data["apply_at"])
                )
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
        member_key = str(member_key_value)
        try:
            member_id = int(member_key_value)
            role_id = int(typing.cast(typing.Any, data["role_id"]))
            due_at = datetime.fromisoformat(
                typing.cast(str, data["expires_at"])
            )
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
        clear_assignments=bool(pending_assignments and not assignments_enabled),
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
            "member_label": incident.get("member_label")
            or f"{member.display_name} ({member})",
            "member_id": member.id,
            "member_mention": member.mention,
            "member_display_name": incident.get("member_display_name")
            or member.display_name,
            "member_avatar_url": incident.get("member_avatar_url")
            or (str(member.display_avatar) if member.display_avatar else None),
            "account_age_hours": incident.get("account_age_hours")
            or account_age_hours,
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
    if alert_channel_id is not None and alert_message_id is not None:
        pending_role["alert_channel_id"] = alert_channel_id
        pending_role["alert_message_id"] = alert_message_id
    async with cog.config.guild(member.guild).joinwatch_pending_roles() as pending_roles:
        pending_roles[str(member.id)] = pending_role


async def delete_pending_role(
    cog, guild: discord.Guild, member_id: int | str
) -> None:
    async with cog.config.guild(guild).joinwatch_pending_roles() as pending_roles:
        pending_roles.pop(str(member_id), None)


async def store_pending_assignment(
    cog,
    member: discord.Member,
    role_id: int,
    apply_at: datetime,
    *,
    expires_at: datetime | None = None,
    incident: typing.Mapping[str, typing.Any] | None = None,
) -> None:
    async with cog.config.guild(member.guild).joinwatch_pending_role_assignments() as pending_assignments:
        pending_assignment = dict(incident or {})
        pending_assignment.update(
            {
                "role_id": role_id,
                "apply_at": apply_at.isoformat(),
            }
        )
        if expires_at is not None:
            pending_assignment["expires_at"] = expires_at.isoformat()
        pending_assignments[str(member.id)] = pending_assignment


async def delete_pending_assignment(
    cog, guild: discord.Guild, member_id: int | str
) -> None:
    async with cog.config.guild(guild).joinwatch_pending_role_assignments() as pending_assignments:
        pending_assignments.pop(str(member_id), None)


async def clear_pending_assignments(cog, guild: discord.Guild) -> None:
    await cog.config.guild(guild).joinwatch_pending_role_assignments.clear()


async def store_alert_reference(
    cog,
    guild: discord.Guild,
    member_id: int,
    channel_id: int,
    message_id: int,
) -> bool:
    member_key = str(member_id)
    stored = False
    guild_config = cog.config.guild(guild)
    for store_name in (
        "joinwatch_pending_role_assignments",
        "joinwatch_pending_roles",
    ):
        store = getattr(guild_config, store_name)
        async with store() as entries:
            incident = entries.get(member_key)
            if incident is None:
                continue
            incident["alert_channel_id"] = channel_id
            incident["alert_message_id"] = message_id
            stored = True
    return stored


async def disable_alert_updates(
    cog,
    guild: discord.Guild,
    member_id: int,
    *,
    incident: dict[str, typing.Any] | None = None,
) -> None:
    member_key = str(member_id)
    guild_config = cog.config.guild(guild)
    for store_name in (
        "joinwatch_pending_role_assignments",
        "joinwatch_pending_roles",
    ):
        store = getattr(guild_config, store_name)
        async with store() as entries:
            stored_incident = entries.get(member_key)
            if stored_incident is not None:
                stored_incident["alert_updates_disabled"] = True
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
    store = getattr(cog.config.guild(guild), store_name)
    if retry_count is None:
        async with store() as entries:
            entries.pop(member_key, None)
        return JoinwatchRetryTransition(
            attempts=JOINWATCH_MAX_RETRIES + 1,
            retry_at=None,
        )

    retry_at = now + timedelta(minutes=JOINWATCH_RETRY_DELAY_MINUTES)
    async with store() as entries:
        if member_key in entries:
            entries[member_key][deadline_key] = retry_at.isoformat()
            entries[member_key]["retry_count"] = retry_count
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
    async with cog.config.guild(guild).joinwatch_pending_roles() as pending_roles:
        for data in pending_roles.values():
            try:
                role_id = int(data["role_id"])
                if data.get("applied_at") is not None:
                    applied_at = datetime.fromisoformat(data["applied_at"])
                else:
                    old_expires_at = datetime.fromisoformat(data["expires_at"])
                    applied_at = old_expires_at - timedelta(
                        minutes=old_timer_minutes
                    )
            except (KeyError, TypeError, ValueError):
                continue
            expires_at = applied_at + timedelta(minutes=new_timer_minutes)
            data["applied_at"] = applied_at.isoformat()
            data["expires_at"] = expires_at.isoformat()
            updates.append((dict(data), role_id, expires_at))
    return tuple(updates)
