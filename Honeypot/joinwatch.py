from __future__ import annotations

import logging
import random
import typing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import commands, modlog
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import box, pagify

from .settings import (
    BOOL_OPTIONS,
    JOINWATCH_AUTO_ROLE_ACTION_OPTIONS,
    GuildSettings,
    JoinwatchAutoRoleActionOption,
)

_ = Translator("Honeypot", __file__)
log = logging.getLogger("red.Honeypot")

JOINWATCH_MAX_ACCOUNT_AGE_HOURS = 1_000_000
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


def joinwatch_channel_id(settings: GuildSettings) -> int | None:
    return settings.joinwatch_channel


async def _store_joinwatch_pending_role(
    cog,
    member: discord.Member,
    role_id: int,
    expires_at: datetime,
    *,
    applied_at: datetime | None = None,
    alert_channel_id: int | None = None,
    alert_message_id: int | None = None,
) -> None:
    pending_role = {
        "role_id": role_id,
        "applied_at": (applied_at or datetime.now(timezone.utc)).isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    if alert_channel_id is not None and alert_message_id is not None:
        pending_role["alert_channel_id"] = alert_channel_id
        pending_role["alert_message_id"] = alert_message_id
    async with cog.config.guild(member.guild).joinwatch_pending_roles() as pending_roles:
        pending_roles[str(member.id)] = pending_role


async def _delete_joinwatch_pending_role(cog, guild: discord.Guild, member_id: int) -> None:
    async with cog.config.guild(guild).joinwatch_pending_roles() as pending_roles:
        pending_roles.pop(str(member_id), None)


async def _store_joinwatch_pending_role_alert(
    cog,
    guild: discord.Guild,
    member_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    async with cog.config.guild(guild).joinwatch_pending_roles() as pending_roles:
        pending_role = pending_roles.get(str(member_id))
        if pending_role is None:
            return
        pending_role["alert_channel_id"] = channel_id
        pending_role["alert_message_id"] = message_id


async def _store_joinwatch_pending_assignment(
    cog,
    member: discord.Member,
    role_id: int,
    apply_at: datetime,
) -> None:
    async with cog.config.guild(member.guild).joinwatch_pending_role_assignments() as pending_assignments:
        pending_assignments[str(member.id)] = {
            "role_id": role_id,
            "apply_at": apply_at.isoformat(),
        }


async def _delete_joinwatch_pending_assignment(cog, guild: discord.Guild, member_id: int) -> None:
    async with cog.config.guild(guild).joinwatch_pending_role_assignments() as pending_assignments:
        pending_assignments.pop(str(member_id), None)


async def _store_joinwatch_pending_assignment_alert(
    cog,
    guild: discord.Guild,
    member_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    async with cog.config.guild(guild).joinwatch_pending_role_assignments() as pending_assignments:
        pending_assignment = pending_assignments.get(str(member_id))
        if pending_assignment is None:
            return
        pending_assignment["alert_channel_id"] = channel_id
        pending_assignment["alert_message_id"] = message_id


async def _edit_joinwatch_alert_auto_role(
    cog,
    guild: discord.Guild,
    pending_assignment: dict,
    value: str,
) -> None:
    channel_id = pending_assignment.get("alert_channel_id")
    message_id = pending_assignment.get("alert_message_id")
    if channel_id is None or message_id is None:
        return
    channel = cog._get_text_channel_or_thread(guild, channel_id)
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, TypeError, ValueError):
        return
    except (discord.Forbidden, discord.HTTPException) as exc:
        await cog._record_operational_failure(
            guild.id,
            "joinwatch_alert_update",
            f"Could not fetch joinwatch alert {message_id}: {exc}",
        )
        return
    if not message.embeds:
        return
    embed = discord.Embed.from_dict(message.embeds[0].to_dict())
    field_name = _("Auto-role:")
    legacy_field_name = _("Auto role:")
    for index, field in enumerate(embed.fields):
        if field.name in (field_name, legacy_field_name):
            embed.set_field_at(index, name=field_name, value=value, inline=field.inline)
            break
    else:
        embed.add_field(name=field_name, value=value, inline=False)
    try:
        await message.edit(embed=embed)
    except discord.HTTPException as exc:
        log.debug("Failed to edit joinwatch alert message %s in guild %s", message_id, guild.id)
        await cog._record_operational_failure(
            guild.id,
            "joinwatch_alert_update",
            f"Could not update joinwatch alert {message_id}: {exc}",
        )


def _joinwatch_next_retry(data: dict) -> int | None:
    try:
        retry_count = int(data.get("retry_count", 0)) + 1
    except (TypeError, ValueError):
        retry_count = 1
    return retry_count if retry_count <= JOINWATCH_MAX_RETRIES else None


async def _reschedule_joinwatch_assignment_retry(
    cog,
    guild: discord.Guild,
    member_id_str: str,
    data: dict,
    now: datetime,
    *,
    failure: str,
) -> bool:
    retry_count = _joinwatch_next_retry(data)
    await cog._record_operational_failure(
        guild.id,
        "joinwatch_role_assignment",
        failure,
        attempts=JOINWATCH_MAX_RETRIES + 1 if retry_count is None else retry_count,
        terminal=retry_count is None,
    )
    if retry_count is None:
        await _edit_joinwatch_alert_auto_role(
            cog,
            guild,
            data,
            _("Failed: {reason}\nNo more automatic retries.").format(reason=failure),
        )
        async with cog.config.guild(guild).joinwatch_pending_role_assignments() as pending_assignments:
            pending_assignments.pop(member_id_str, None)
        return False
    retry_at = now + timedelta(minutes=JOINWATCH_RETRY_DELAY_MINUTES)
    async with cog.config.guild(guild).joinwatch_pending_role_assignments() as pending_assignments:
        if member_id_str in pending_assignments:
            pending_assignments[member_id_str]["apply_at"] = retry_at.isoformat()
            pending_assignments[member_id_str]["retry_count"] = retry_count
    data["apply_at"] = retry_at.isoformat()
    data["retry_count"] = retry_count
    await _edit_joinwatch_alert_auto_role(
        cog,
        guild,
        data,
        _("Failed: {reason}\nRetrying {time} ({count}/{max}).").format(
            reason=failure,
            time=discord.utils.format_dt(retry_at, style="R"),
            count=retry_count,
            max=JOINWATCH_MAX_RETRIES,
        ),
    )
    return True


async def _reschedule_joinwatch_role_retry(
    cog,
    guild: discord.Guild,
    member_id_str: str,
    data: dict,
    now: datetime,
    *,
    failure: str,
) -> bool:
    retry_count = _joinwatch_next_retry(data)
    await cog._record_operational_failure(
        guild.id,
        "joinwatch_role_action",
        failure,
        attempts=JOINWATCH_MAX_RETRIES + 1 if retry_count is None else retry_count,
        terminal=retry_count is None,
    )
    if retry_count is None:
        await _edit_joinwatch_alert_auto_role(
            cog,
            guild,
            data,
            _("Failed: {reason}\nNo more automatic retries.").format(reason=failure),
        )
        async with cog.config.guild(guild).joinwatch_pending_roles() as pending_roles:
            pending_roles.pop(member_id_str, None)
        return False
    retry_at = now + timedelta(minutes=JOINWATCH_RETRY_DELAY_MINUTES)
    async with cog.config.guild(guild).joinwatch_pending_roles() as pending_roles:
        if member_id_str in pending_roles:
            pending_roles[member_id_str]["expires_at"] = retry_at.isoformat()
            pending_roles[member_id_str]["retry_count"] = retry_count
    data["expires_at"] = retry_at.isoformat()
    data["retry_count"] = retry_count
    await _edit_joinwatch_alert_auto_role(
        cog,
        guild,
        data,
        _("Failed: {reason}\nRetrying {time} ({count}/{max}).").format(
            reason=failure,
            time=discord.utils.format_dt(retry_at, style="R"),
            count=retry_count,
            max=JOINWATCH_MAX_RETRIES,
        ),
    )
    return True


async def _reschedule_joinwatch_pending_roles(
    cog,
    guild: discord.Guild,
    old_timer_minutes: int,
    new_timer_minutes: int,
) -> int:
    alert_updates: list[tuple[dict, int, datetime]] = []
    updated = 0
    async with cog.config.guild(guild).joinwatch_pending_roles() as pending_roles:
        for data in pending_roles.values():
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
            alert_updates.append((dict(data), role_id, expires_at))
            updated += 1
    for data, role_id, expires_at in alert_updates:
        role = guild.get_role(role_id)
        if role is None:
            continue
        await _edit_joinwatch_alert_auto_role(
            cog,
            guild,
            data,
            _("{role} applied until {time}.").format(
                role=role.mention,
                time=discord.utils.format_dt(expires_at, style="R"),
            ),
        )
    return updated


def _joinwatch_kick_status_value(action_label: str | None, default: str) -> str:
    if action_label and action_label != _("The member has been kicked."):
        return action_label
    return default


async def _execute_joinwatch_action(
    cog,
    guild: discord.Guild,
    member: discord.Member | None,
    member_id: int,
    settings: GuildSettings,
    *,
    reason: str,
) -> tuple[str | None, str | None]:
    action = settings.joinwatch_auto_role_action.value
    if action not in ("kick", "ban"):
        return (_("No joinwatch punishment configured."), None)
    if settings.dry_run:
        await cog._increment_stat(guild, "dry_run_actions")
        return (cog._dry_run_label(action), None)
    missing_permission = cog._missing_action_permission(guild, action)
    if missing_permission is not None:
        await cog._increment_stat(guild, "failed_actions")
        return (None, missing_permission)
    try:
        if action == "kick":
            if member is None:
                if cog._automated_kick_fail_warning_enabled(
                    settings.automated_kick_fail_warning
                ):
                    return await cog._create_kick_fail_warning(guild, member_id)
                return (_("The member is no longer in the server."), None)
            try:
                await member.kick(reason=reason)
            except discord.NotFound:
                if cog._automated_kick_fail_warning_enabled(
                    settings.automated_kick_fail_warning
                ):
                    return await cog._create_kick_fail_warning(guild, member_id)
                raise
        elif action == "ban":
            target = member if member is not None else await cog._get_user_or_object(member_id)
            await guild.ban(
                target,
                reason=reason,
                delete_message_seconds=cog._ban_delete_message_seconds(),
            )
            cog._schedule_post_ban_sweep(guild, target.id)
        await cog._increment_stat(guild, "joinwatch_auto_role_punishments")
    except discord.HTTPException as exc:
        await cog._increment_stat(guild, "failed_actions")
        return (None, _("**Action failed:**\n") + box(str(exc), lang="py"))
    user = member if member is not None else await cog._get_user_or_object(member_id)
    try:
        await modlog.create_case(
            cog.bot,
            guild,
            datetime.now(timezone.utc),
            action_type=action,
            user=user,
            moderator=guild.me,
            reason=reason,
        )
    except Exception:
        log.exception("Failed to create modlog case in _execute_joinwatch_action")
    label = _("The member has been kicked.") if action == "kick" else _("The member has been banned.")
    return (label, None)


async def _apply_joinwatch_assignment_actions(
    cog,
    guild,
    guild_settings: GuildSettings,
    actions: tuple[JoinwatchSelectedAction, ...],
    now: datetime,
    *,
    joinwatch_channel: discord.TextChannel | discord.Thread | None,
) -> None:
    for selected_action in actions:
        if selected_action.action == "discard_assignment":
            async with cog.config.guild(guild).joinwatch_pending_role_assignments() as stored_assignments:
                stored_assignments.pop(selected_action.member_key, None)
            continue
        member_id_str = selected_action.member_key
        data = typing.cast(dict, selected_action.data)
        member_id = typing.cast(int, selected_action.member_id)
        role_id = typing.cast(int, selected_action.role_id)
        member = await cog._get_member_or_fetch(guild, member_id)
        role = guild.get_role(role_id)
        if member is None:
            action_label, failed = await _execute_joinwatch_action(
                cog,
                guild,
                None,
                member_id,
                guild_settings,
                reason="Suspicious Account",
            )
            if failed:
                await _reschedule_joinwatch_assignment_retry(
                    cog,
                    guild,
                    member_id_str,
                    data,
                    now,
                    failure=failed,
                )
                continue
            if (
                guild_settings.joinwatch_auto_role_action
                is JoinwatchAutoRoleActionOption.BAN
            ):
                await _edit_joinwatch_alert_auto_role(cog, guild, data, _("Banned."))
            elif (
                guild_settings.joinwatch_auto_role_action
                is JoinwatchAutoRoleActionOption.KICK
            ):
                await _edit_joinwatch_alert_auto_role(
                    cog,
                    guild,
                    data,
                    _joinwatch_kick_status_value(action_label, _("Left server.")),
                )
            else:
                await _edit_joinwatch_alert_auto_role(cog, guild, data, _("Auto-role timer expired."))
            await _delete_joinwatch_pending_assignment(cog, guild, member_id)
            if joinwatch_channel is not None:
                embed = discord.Embed(
                    title=_("Joinwatch auto-role timer expired"),
                    description=_("{mention} ({id}) left before the scheduled role could be applied.").format(
                        mention=f"<@{member_id}>",
                        id=member_id,
                    ),
                    color=discord.Color.dark_red() if failed else discord.Color.orange(),
                    timestamp=now,
                )
                embed.add_field(
                    name=_("Action:"),
                    value=failed if failed else action_label,
                    inline=False,
                )
                try:
                    await joinwatch_channel.send(embed=embed)
                except discord.HTTPException as exc:
                    log.debug(
                        "Failed to send joinwatch missing-member log for user %s in guild %s",
                        member_id,
                        guild.id,
                    )
                    await cog._record_operational_failure(
                        guild.id,
                        "joinwatch_timer_alert",
                        f"Could not publish timer result for user {member_id}: {exc}",
                    )
            continue
        if role is None:
            await _delete_joinwatch_pending_assignment(cog, guild, member_id)
            continue
        if await cog._is_protected_member(member):
            await _delete_joinwatch_pending_assignment(cog, guild, member_id)
            continue
        role_permission_error = cog._missing_role_assignment_permission(guild, role)
        if role_permission_error is not None:
            await cog._increment_stat(guild, "joinwatch_auto_role_failures")
            await _reschedule_joinwatch_assignment_retry(
                cog,
                guild,
                member_id_str,
                data,
                now,
                failure=role_permission_error,
            )
            continue
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Automated account status update.")
                await cog._increment_stat(guild, "joinwatch_auto_roles")
            except discord.HTTPException:
                await cog._increment_stat(guild, "joinwatch_auto_role_failures")
                await _reschedule_joinwatch_assignment_retry(
                    cog,
                    guild,
                    member_id_str,
                    data,
                    now,
                    failure=_("I couldn't apply the configured joinwatch auto-role."),
                )
                continue
        expires_at = now + timedelta(
            minutes=guild_settings.joinwatch_auto_role_timer_minutes
        )
        await _store_joinwatch_pending_role(
            cog,
            member,
            role.id,
            expires_at,
            applied_at=now,
            alert_channel_id=typing.cast(
                int | None, data.get("alert_channel_id")
            ),
            alert_message_id=typing.cast(
                int | None, data.get("alert_message_id")
            ),
        )
        await _edit_joinwatch_alert_auto_role(
            cog,
            guild,
            data,
            _("{role} applied until {time}.").format(
                role=role.mention,
                time=discord.utils.format_dt(expires_at, style="R"),
            ),
        )
        await _delete_joinwatch_pending_assignment(cog, guild, member_id)


async def _apply_joinwatch_role_actions(
    cog,
    guild,
    guild_settings: GuildSettings,
    actions: tuple[JoinwatchSelectedAction, ...],
    now: datetime,
    *,
    joinwatch_channel: discord.TextChannel | discord.Thread | None,
) -> None:
    for selected_action in actions:
        if selected_action.action == "discard_role":
            async with cog.config.guild(guild).joinwatch_pending_roles() as stored_pending_roles:
                stored_pending_roles.pop(selected_action.member_key, None)
            continue
        member_id_str = selected_action.member_key
        data = typing.cast(dict, selected_action.data)
        member_id = typing.cast(int, selected_action.member_id)
        role_id = typing.cast(int, selected_action.role_id)
        member = await cog._get_member_or_fetch(guild, member_id)
        role = guild.get_role(role_id)
        if member is None:
            action_label, failed = await _execute_joinwatch_action(
                cog,
                guild,
                None,
                member_id,
                guild_settings,
                reason="Suspicious Account",
            )
            if failed:
                await _reschedule_joinwatch_role_retry(
                    cog,
                    guild,
                    member_id_str,
                    data,
                    now,
                    failure=failed,
                )
            else:
                if (
                    guild_settings.joinwatch_auto_role_action
                    is JoinwatchAutoRoleActionOption.BAN
                ):
                    await _edit_joinwatch_alert_auto_role(cog, guild, data, _("Banned."))
                elif (
                    guild_settings.joinwatch_auto_role_action
                    is JoinwatchAutoRoleActionOption.KICK
                ):
                    await _edit_joinwatch_alert_auto_role(
                        cog,
                        guild,
                        data,
                        _joinwatch_kick_status_value(action_label, _("Left server.")),
                    )
                else:
                    await _edit_joinwatch_alert_auto_role(cog, guild, data, _("Auto-role timer expired."))
                await _delete_joinwatch_pending_role(cog, guild, member_id)
            if joinwatch_channel is not None:
                embed = discord.Embed(
                    title=_("Joinwatch auto-role timer expired"),
                    description=_("{mention} ({id}) left before the auto-role timer expired.").format(
                        mention=f"<@{member_id}>",
                        id=member_id,
                    ),
                    color=discord.Color.dark_red() if failed else discord.Color.orange(),
                    timestamp=now,
                )
                embed.add_field(
                    name=_("Action:"),
                    value=failed if failed else action_label,
                    inline=False,
                )
                try:
                    await joinwatch_channel.send(embed=embed)
                except discord.HTTPException as exc:
                    log.debug(
                        "Failed to send joinwatch missing-member log for user %s in guild %s",
                        member_id,
                        guild.id,
                    )
                    await cog._record_operational_failure(
                        guild.id,
                        "joinwatch_timer_alert",
                        f"Could not publish timer result for user {member_id}: {exc}",
                    )
            continue
        if role is None:
            await _delete_joinwatch_pending_role(cog, guild, member_id)
            continue
        if role not in member.roles:
            await _edit_joinwatch_alert_auto_role(
                cog,
                guild,
                data,
                _("Role manually removed."),
            )
            await _delete_joinwatch_pending_role(cog, guild, member_id)
            await cog._increment_stat(guild, "joinwatch_auto_roles_cleared")
            continue
        if await cog._is_protected_member(member):
            await _delete_joinwatch_pending_role(cog, guild, member_id)
            continue
        action_label, failed = await _execute_joinwatch_action(
            cog,
            guild,
            member,
            member_id,
            guild_settings,
            reason="Suspicious Account",
        )
        if failed:
            await _reschedule_joinwatch_role_retry(
                cog,
                guild,
                member_id_str,
                data,
                now,
                failure=failed,
            )
        else:
            if (
                guild_settings.joinwatch_auto_role_action
                is JoinwatchAutoRoleActionOption.BAN
            ):
                await _edit_joinwatch_alert_auto_role(cog, guild, data, _("Banned."))
            elif (
                guild_settings.joinwatch_auto_role_action
                is JoinwatchAutoRoleActionOption.KICK
            ):
                await _edit_joinwatch_alert_auto_role(
                    cog,
                    guild,
                    data,
                    _joinwatch_kick_status_value(action_label, _("Kicked.")),
                )
            else:
                await _edit_joinwatch_alert_auto_role(cog, guild, data, _("Auto-role timer expired."))
            await _delete_joinwatch_pending_role(cog, guild, member_id)
        if joinwatch_channel is not None:
            embed = discord.Embed(
                title=_("Joinwatch auto-role timer expired"),
                description=_("{mention} ({id}) still had {role} when the timer expired.").format(
                    mention=member.mention,
                    id=member.id,
                    role=role.mention if role is not None else _("the auto-role"),
                ),
                color=discord.Color.dark_red() if failed else discord.Color.orange(),
                timestamp=now,
            )
            embed.add_field(
                name=_("Action:"),
                value=failed if failed else action_label,
                inline=False,
            )
            try:
                await joinwatch_channel.send(embed=embed)
            except discord.HTTPException as exc:
                log.debug("Failed to send joinwatch auto-role log for user %s in guild %s", member.id, guild.id)
                await cog._record_operational_failure(
                    guild.id,
                    "joinwatch_timer_alert",
                    f"Could not publish timer result for user {member.id}: {exc}",
                )


async def _apply_joinwatch_selected_work(
    cog,
    guild,
    guild_settings: GuildSettings,
    selected: JoinwatchSelection,
    now: datetime,
) -> None:
    if (
        selected.clear_assignments
        or selected.assignment_actions
        or selected.role_actions
    ):
        try:
            if selected.clear_assignments:
                async with cog.config.guild(guild).joinwatch_pending_role_assignments() as stored_assignments:
                    stored_assignments.clear()
            if not selected.assignment_actions and not selected.role_actions:
                return
            joinwatch_channel = cog._get_text_channel_or_thread(
                guild, joinwatch_channel_id(guild_settings)
            )
            await _apply_joinwatch_assignment_actions(
                cog,
                guild,
                guild_settings,
                selected.assignment_actions,
                now,
                joinwatch_channel=joinwatch_channel,
            )
            await _apply_joinwatch_role_actions(
                cog,
                guild,
                guild_settings,
                selected.role_actions,
                now,
                joinwatch_channel=joinwatch_channel,
            )
        except Exception as exc:
            log.exception("Failed to process joinwatch auto-role timers for guild %s", guild.id)
            await cog._record_operational_failure(
                guild.id,
                "joinwatch_timer_processing",
                f"Could not process joinwatch timers: {exc}",
            )


async def joinwatch_auto_role_loop(cog) -> None:
    now = datetime.now(timezone.utc)
    for guild in cog.bot.guilds:
        try:
            raw_config = await cog.config.guild(guild).all()
            guild_settings = GuildSettings.from_mapping(raw_config)
            selected = select_due_joinwatch_assignments(
                now=now,
                assignments_enabled=guild_settings.joinwatch_auto_role_enabled,
                pending_assignments=guild_settings.joinwatch_pending_role_assignments,
                pending_roles=guild_settings.joinwatch_pending_roles,
            )
        except Exception as exc:
            log.exception(
                "Failed to process joinwatch auto-role timers for guild %s",
                guild.id,
            )
            await cog._record_operational_failure(
                guild.id,
                "joinwatch_timer_processing",
                f"Could not process joinwatch timers: {exc}",
            )
            continue
        await _apply_joinwatch_selected_work(
            cog,
            guild,
            guild_settings,
            selected,
            now,
        )


async def on_member_join(cog, member: discord.Member) -> None:
    if await cog.bot.cog_disabled_in_guild(cog, member.guild):
        return
    if member.bot:
        return
    raw_config = await cog.config.guild(member.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    if not guild_settings.joinwatch_enabled:
        return
    await cog._increment_stat(member.guild, "joinwatch_total_joins")
    channel = cog._get_text_channel_or_thread(
        member.guild, guild_settings.joinwatch_channel
    )
    now = datetime.now(timezone.utc)
    min_age = timedelta(hours=guild_settings.joinwatch_min_age_hours)
    if member.created_at > now - min_age:
        await cog._increment_stat(member.guild, "joinwatch_young_joins")
        hours = max(1, round((now - member.created_at).total_seconds() / 3600))
        member_label = f"{member.display_name} ({member})"
        embed = discord.Embed(
            title=_("New account joined"),
            description=_("**{member}**\nMention: {mention}\nID: `{id}`\nAccount is ~{hours} hours old.").format(
                member=member_label, mention=member.mention, id=member.id, hours=hours,
            ),
            color=discord.Color.orange(),
            timestamp=member.joined_at or now,
        )
        embed.set_author(name=f"{member.display_name} ({member.id})", icon_url=member.display_avatar)
        embed.set_thumbnail(url=member.display_avatar)
        if (
            guild_settings.joinwatch_auto_role_enabled
            and guild_settings.joinwatch_auto_role_id is not None
        ):
            role = member.guild.get_role(guild_settings.joinwatch_auto_role_id)
            if role is not None and role not in member.roles and not await cog._is_protected_member(member):
                role_permission_error = cog._missing_role_assignment_permission(member.guild, role)
                if role_permission_error is not None:
                    await cog._increment_stat(member.guild, "joinwatch_auto_role_failures")
                    await cog._record_operational_failure(
                        member.guild.id,
                        "joinwatch_role_assignment",
                        role_permission_error,
                        terminal=True,
                    )
                    embed.add_field(
                        name=_("Auto-role:"),
                        value=role_permission_error,
                        inline=False,
                    )
                else:
                    if guild_settings.joinwatch_auto_role_random_delay_enabled:
                        min_delay = max(
                            1,
                            guild_settings.joinwatch_auto_role_random_delay_min_minutes,
                        )
                        max_delay = max(
                            min_delay,
                            guild_settings.joinwatch_auto_role_random_delay_max_minutes,
                        )
                        delay_minutes = random.randint(min_delay, max_delay)
                        apply_at = now + timedelta(minutes=delay_minutes)
                        await _store_joinwatch_pending_assignment(cog, member, role.id, apply_at)
                        await cog._increment_stat(member.guild, "joinwatch_auto_roles_scheduled")
                        embed.add_field(
                            name=_("Auto-role:"),
                            value=_("{role} scheduled for {time}.").format(
                                role=role.mention,
                                time=discord.utils.format_dt(apply_at, style="R"),
                            ),
                            inline=False,
                        )
                    else:
                        try:
                            await member.add_roles(role, reason="Automated account status update.")
                            await cog._increment_stat(member.guild, "joinwatch_auto_roles")
                            expires_at = now + timedelta(
                                minutes=guild_settings.joinwatch_auto_role_timer_minutes
                            )
                            await _store_joinwatch_pending_role(
                                cog,
                                member,
                                role.id,
                                expires_at,
                                applied_at=now,
                            )
                            embed.add_field(
                                name=_("Auto-role:"),
                                value=_("{role} applied until {time}.").format(
                                    role=role.mention,
                                    time=discord.utils.format_dt(expires_at, style="R"),
                                ),
                                inline=False,
                            )
                        except discord.HTTPException as exc:
                            await cog._increment_stat(member.guild, "joinwatch_auto_role_failures")
                            await cog._record_operational_failure(
                                member.guild.id,
                                "joinwatch_role_assignment",
                                f"Could not apply auto-role to user {member.id}: {exc}",
                                terminal=True,
                            )
                            embed.add_field(
                                name=_("Auto-role:"),
                                value=_("I couldn't apply the configured joinwatch auto-role."),
                                inline=False,
                            )
        if guild_settings.joinwatch_alert_enabled and channel is not None:
            try:
                alert_message = await channel.send(embed=embed)
                if guild_settings.joinwatch_auto_role_random_delay_enabled:
                    await _store_joinwatch_pending_assignment_alert(
                        cog,
                        member.guild,
                        member.id,
                        alert_message.channel.id,
                        alert_message.id,
                    )
                else:
                    await _store_joinwatch_pending_role_alert(
                        cog,
                        member.guild,
                        member.id,
                        alert_message.channel.id,
                        alert_message.id,
                    )
            except discord.HTTPException as exc:
                log.debug("Failed to send joinwatch alert for user %s in guild %s", member.id, member.guild.id)
                await cog._record_operational_failure(
                    member.guild.id,
                    "joinwatch_alert_publish",
                    f"Could not publish joinwatch alert for user {member.id}: {exc}",
                    terminal=True,
                )


async def on_member_update(cog, before: discord.Member, after: discord.Member) -> None:
    if await cog.bot.cog_disabled_in_guild(cog, after.guild):
        return
    if after.bot:
        return
    raw_config = await cog.config.guild(after.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    pending_roles = guild_settings.joinwatch_pending_roles
    pending_role = pending_roles.get(str(after.id))
    if pending_role is not None:
        try:
            pending_role_id = int(
                typing.cast(typing.Any, pending_role["role_id"])
            )
        except (KeyError, TypeError, ValueError):
            await _delete_joinwatch_pending_role(cog, after.guild, after.id)
        else:
            role_removed = any(role.id == pending_role_id for role in before.roles) and not any(
                role.id == pending_role_id for role in after.roles
            )
            if role_removed:
                await _edit_joinwatch_alert_auto_role(
                    cog,
                    after.guild,
                    pending_role,
                    _("Role manually removed."),
                )
                await _delete_joinwatch_pending_role(cog, after.guild, after.id)
                await cog._increment_stat(after.guild, "joinwatch_auto_roles_cleared")
    if not guild_settings.baitrole_enabled or guild_settings.baitrole_id is None:
        return
    bait_role = after.guild.get_role(guild_settings.baitrole_id)
    if bait_role is None:
        return
    if bait_role not in before.roles and bait_role in after.roles:
        if await cog._is_protected_member(after):
            return
        action = guild_settings.baitrole_action.value
        reason = "Took the bait role - potential DM bot/scammer."
        try:
            if action == "ban":
                await after.ban(
                    reason=reason,
                    delete_message_seconds=cog._ban_delete_message_seconds(),
                )
                cog._schedule_post_ban_sweep(after.guild, after.id)
                await cog._increment_stat(after.guild, "banned")
            elif action == "kick":
                await after.kick(reason=reason)
                await cog._increment_stat(after.guild, "kicked")
        except discord.HTTPException as exc:
            log.warning("Failed to %s bait-role target %s in guild %s", action, after.id, after.guild.id)
            await cog._record_operational_failure(
                after.guild.id,
                "bait_role_action",
                f"Could not {action} bait-role target {after.id}: {exc}",
                terminal=True,
            )
        logs_channel_id = guild_settings.logs_channel
        logs_channel = cog._get_text_channel_or_thread(after.guild, logs_channel_id)
        if logs_channel is not None:
            embed = discord.Embed(
                title=_("Bait role triggered"),
                description=_("{mention} ({id}) took the bait role and was {action}.").format(
                    mention=after.mention, id=after.id, action=action,
                ),
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_thumbnail(url=after.display_avatar)
            try:
                await logs_channel.send(embed=embed)
            except discord.HTTPException as exc:
                log.debug("Failed to send bait role log for user %s in guild %s", after.id, after.guild.id)
                await cog._record_operational_failure(
                    after.guild.id,
                    "bait_role_alert",
                    f"Could not publish bait-role alert for user {after.id}: {exc}",
                    terminal=True,
                )


async def joinwatch_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).joinwatch_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_enabled.set(value)
        await ctx.send(_("✅ Joinwatch enabled set to {value}").format(value=value))


async def channel(cog, ctx: commands.Context, target: discord.TextChannel | discord.Thread = None) -> None:
    if target is None:
        v = await cog.config.guild(ctx.guild).joinwatch_channel()
        await ctx.send(_("Joinwatch channel: {channel}").format(channel=ctx.guild.get_channel(v) if v else _("not set")))
    else:
        is_thread = isinstance(target, discord.Thread)
        missing = cog._missing_channel_permissions(
            ctx.guild,
            target,
            send_messages=not is_thread,
            send_in_threads=is_thread,
        )
        if missing is not None:
            raise commands.UserFeedbackCheckFailure(missing)
        await cog.config.guild(ctx.guild).joinwatch_channel.set(target.id)
        await ctx.send(_("✅ Joinwatch channel set to {channel.mention}").format(channel=target))


async def joinwatch_alert_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).joinwatch_alert_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_alert_enabled.set(value)
        await ctx.send(_("✅ Joinwatch alerts set to {value}").format(value=value))


async def max_age(cog, ctx: commands.Context, hours: int = None) -> None:
    if hours is None:
        v = await cog.config.guild(ctx.guild).joinwatch_min_age_hours()
        await ctx.send(_("Joinwatch max age: {value} hours").format(value=v))
    elif hours < 1 or hours > JOINWATCH_MAX_ACCOUNT_AGE_HOURS:
        await ctx.send(
            _("Hours must be between 1 and {maximum}.").format(
                maximum=JOINWATCH_MAX_ACCOUNT_AGE_HOURS
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_min_age_hours.set(hours)
        await ctx.send(_("✅ Joinwatch max age set to {value} hours").format(value=hours))


async def joinwatch_autorole_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).joinwatch_auto_role_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_auto_role_enabled.set(value)
        await ctx.send(_("✅ Joinwatch auto-role set to {value}").format(value=value))


async def joinwatch_autorole_role(cog, ctx: commands.Context, role: discord.Role = None) -> None:
    if role is None:
        role_id = await cog.config.guild(ctx.guild).joinwatch_auto_role_id()
        configured_role = ctx.guild.get_role(role_id) if role_id else None
        await ctx.send(
            _("Joinwatch auto-role: {role}").format(
                role=configured_role.mention if configured_role else _("not set"),
            )
        )
    else:
        role_permission_error = cog._missing_role_assignment_permission(ctx.guild, role)
        if role_permission_error is not None:
            raise commands.UserFeedbackCheckFailure(role_permission_error)
        await cog.config.guild(ctx.guild).joinwatch_auto_role_id.set(role.id)
        await ctx.send(_("✅ Joinwatch auto-role set to {role.mention}").format(role=role))


async def joinwatch_autorole_timer(cog, ctx: commands.Context, minutes: int = None) -> None:
    if minutes is None:
        v = await cog.config.guild(ctx.guild).joinwatch_auto_role_timer_minutes()
        await ctx.send(_("Joinwatch auto-role timer: {value} minutes").format(value=v))
    elif minutes < 1 or minutes > 10080:
        await ctx.send(_("Timer must be between 1 and 10080 minutes."))
    else:
        old_minutes = await cog.config.guild(ctx.guild).joinwatch_auto_role_timer_minutes()
        await cog.config.guild(ctx.guild).joinwatch_auto_role_timer_minutes.set(minutes)
        updated = await _reschedule_joinwatch_pending_roles(cog, ctx.guild, old_minutes, minutes)
        await ctx.send(
            _("✅ Joinwatch auto-role timer set to {value} minutes. Updated {count} active timer(s).").format(
                value=minutes,
                count=updated,
            )
        )


async def joinwatch_autorole_action(cog, ctx: commands.Context, value: str = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).joinwatch_auto_role_action()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=v,
                options=cog._format_options(JOINWATCH_AUTO_ROLE_ACTION_OPTIONS),
            )
        )
    elif value not in JOINWATCH_AUTO_ROLE_ACTION_OPTIONS:
        await ctx.send(_("Choose one of: {options}").format(options=cog._format_options(JOINWATCH_AUTO_ROLE_ACTION_OPTIONS)))
    else:
        await cog.config.guild(ctx.guild).joinwatch_auto_role_action.set(value)
        await ctx.send(_("✅ Joinwatch auto-role action set to {value}").format(value=value))


async def joinwatch_autorole_bantimers(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    pending_roles = guild_settings.joinwatch_pending_roles
    if not pending_roles:
        await ctx.send(_("No active joinwatch punishment timers."))
        return

    now = datetime.now(timezone.utc)
    invalid = 0
    entries: list[tuple[datetime, str]] = []
    for member_id_str, data in pending_roles.items():
        try:
            member_id = int(member_id_str)
            expires_at = datetime.fromisoformat(
                typing.cast(str, data["expires_at"])
            )
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue

        member = await cog._get_member_or_fetch(ctx.guild, member_id)
        member_label = (
            f"{member.display_name} ({member.id})"
            if member is not None
            else _("Unknown member ({id})").format(id=member_id)
        )
        applied_at = None
        if data.get("applied_at") is not None:
            try:
                applied_at = datetime.fromisoformat(
                    typing.cast(str, data["applied_at"])
                )
            except (TypeError, ValueError):
                applied_at = None
        deadline = (
            _("due now")
            if expires_at <= now
            else discord.utils.format_dt(expires_at, style="R")
        )
        applied = (
            discord.utils.format_dt(applied_at, style="R")
            if applied_at is not None
            else _("unknown")
        )
        entries.append(
            (
                expires_at,
                _(
                    "{member} | deadline: {deadline} | applied: {applied}"
                ).format(
                    member=member_label,
                    deadline=deadline,
                    applied=applied,
                ),
            )
        )

    if not entries:
        await ctx.send(_("No readable joinwatch punishment timers."))
        return

    entries.sort(key=lambda item: item[0])
    header = _("Joinwatch active punishment timers: {count}").format(
        count=len(entries),
    )
    if invalid:
        header += _("\nSkipped invalid entries: {count}").format(count=invalid)
    lines = [header, ""]
    lines.extend(f"{index}. {entry}" for index, (_, entry) in enumerate(entries, 1))
    for page in pagify("\n".join(lines), page_length=1900):
        await ctx.send(page, allowed_mentions=discord.AllowedMentions.none())


async def joinwatch_autorole_randomize_toggle(
    cog, ctx: commands.Context, value: bool = None
) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_enabled.set(value)
        await ctx.send(_("✅ Joinwatch auto-role randomized delay set to {value}").format(value=value))


async def joinwatch_autorole_randomize_min_time(
    cog, ctx: commands.Context, minutes: int = None
) -> None:
    if minutes is None:
        v = await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_min_minutes()
        await ctx.send(_("Joinwatch auto-role randomized minimum: {value} minutes").format(value=v))
    elif minutes < 1 or minutes > 10080:
        await ctx.send(_("Minimum delay must be between 1 and 10080 minutes."))
    else:
        current_max = await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_max_minutes()
        await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_min_minutes.set(minutes)
        if minutes > current_max:
            await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_max_minutes.set(minutes)
            await ctx.send(
                _("✅ Joinwatch randomized delay minimum and maximum set to {value} minutes").format(
                    value=minutes,
                )
            )
        else:
            await ctx.send(
                _("✅ Joinwatch randomized delay minimum set to {value} minutes").format(
                    value=minutes,
                )
            )


async def joinwatch_autorole_randomize_max_time(
    cog, ctx: commands.Context, minutes: int = None
) -> None:
    if minutes is None:
        v = await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_max_minutes()
        await ctx.send(_("Joinwatch auto-role randomized maximum: {value} minutes").format(value=v))
    elif minutes < 1 or minutes > 10080:
        await ctx.send(_("Maximum delay must be between 1 and 10080 minutes."))
    else:
        current_min = await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_min_minutes()
        if minutes < current_min:
            await ctx.send(
                _("Maximum delay must be greater than or equal to the current minimum ({value} minutes).").format(
                    value=current_min,
                )
            )
            return
        await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_max_minutes.set(minutes)
        await ctx.send(_("✅ Joinwatch randomized delay maximum set to {value} minutes").format(value=minutes))


async def config_joinwatch(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    lines = [
        _("Joinwatch:"),
        f"  {_('Enabled')}: {cog._format_bool_setting(guild_settings.joinwatch_enabled)}",
        f"  {_('Alerts')}: {cog._format_bool_setting(guild_settings.joinwatch_alert_enabled)}",
        f"  {_('Channel')}: {cog._format_channel_setting(ctx.guild, guild_settings.joinwatch_channel)}",
        f"  {_('Maximum account age')}: {_('{hours} hours').format(hours=guild_settings.joinwatch_min_age_hours)}",
        "",
        _("Auto-role:"),
        f"  {_('Enabled')}: {cog._format_bool_setting(guild_settings.joinwatch_auto_role_enabled)}",
        f"  {_('Role')}: {cog._format_role_setting(ctx.guild, guild_settings.joinwatch_auto_role_id)}",
        f"  {_('Timer')}: {_('{minutes} minutes').format(minutes=guild_settings.joinwatch_auto_role_timer_minutes)}",
        f"  {_('Action')}: {guild_settings.joinwatch_auto_role_action.value}",
        f"  {_('Randomized delay')}: {cog._format_bool_setting(guild_settings.joinwatch_auto_role_random_delay_enabled)}",
        f"  {_('Delay range')}: {_('{min} to {max} minutes').format(min=guild_settings.joinwatch_auto_role_random_delay_min_minutes, max=guild_settings.joinwatch_auto_role_random_delay_max_minutes)}",
        f"  {_('Pending role applications')}: {len(guild_settings.joinwatch_pending_role_assignments)}",
        f"  {_('Active joinwatch timers')}: {len(guild_settings.joinwatch_pending_roles)}",
    ]
    await ctx.send(_("Joinwatch config:\n") + box("\n".join(lines)))
