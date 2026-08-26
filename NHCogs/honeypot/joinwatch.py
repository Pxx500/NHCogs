"""JoinWatch listeners, timer orchestration, and moderation decisions."""

from __future__ import annotations

import logging
import random
import typing
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import modlog
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import box

from . import joinwatch_publication, joinwatch_state
from .effects import EffectStatus, ModerationOrigin
from .settings import GuildSettings, JoinwatchAutoRoleActionOption

_ = Translator("Honeypot", __file__)
log = logging.getLogger("red.Honeypot")

JOINWATCH_RETRY_DELAY_MINUTES = joinwatch_state.JOINWATCH_RETRY_DELAY_MINUTES
JOINWATCH_MAX_RETRIES = joinwatch_state.JOINWATCH_MAX_RETRIES


def joinwatch_channel_id(settings: GuildSettings) -> int | None:
    return settings.joinwatch_channel


async def _reschedule_joinwatch_assignment_retry(
    cog,
    guild: discord.Guild,
    member_id_str: str,
    data: dict,
    now: datetime,
    *,
    failure: str,
) -> bool:
    transition = await joinwatch_state.reschedule_assignment_retry(
        cog,
        guild,
        member_id_str,
        data,
        now,
    )
    await cog._record_operational_failure(
        guild.id,
        "joinwatch_role_assignment",
        failure,
        attempts=transition.attempts,
        terminal=transition.terminal,
    )
    if transition.terminal:
        await joinwatch_publication.publish_joinwatch_incident(
            cog,
            guild,
            data,
            _("Failed: {reason}\nNo more automatic retries.").format(reason=failure),
        )
        return False
    retry_at = typing.cast(datetime, transition.retry_at)
    await joinwatch_publication.publish_joinwatch_incident(
        cog,
        guild,
        data,
        _("Failed: {reason}\nRetrying {time} ({count}/{max}).").format(
            reason=failure,
            time=discord.utils.format_dt(retry_at, style="R"),
            count=transition.attempts,
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
    transition = await joinwatch_state.reschedule_role_retry(
        cog,
        guild,
        member_id_str,
        data,
        now,
    )
    await cog._record_operational_failure(
        guild.id,
        "joinwatch_role_action",
        failure,
        attempts=transition.attempts,
        terminal=transition.terminal,
    )
    if transition.terminal:
        await joinwatch_publication.publish_joinwatch_incident(
            cog,
            guild,
            data,
            _("Failed: {reason}\nNo more automatic retries.").format(reason=failure),
        )
        return False
    retry_at = typing.cast(datetime, transition.retry_at)
    await joinwatch_publication.publish_joinwatch_incident(
        cog,
        guild,
        data,
        _("Failed: {reason}\nRetrying {time} ({count}/{max}).").format(
            reason=failure,
            time=discord.utils.format_dt(retry_at, style="R"),
            count=transition.attempts,
            max=JOINWATCH_MAX_RETRIES,
        ),
    )
    return True


def _joinwatch_kick_status_value(action_label: str | None, default: str) -> str:
    if action_label and action_label != _("The member has been kicked"):
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
        return (_("No joinwatch punishment configured"), None)
    if not await cog._punitive_effect_allowed(guild):
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
            await cog._record_daily_stat(
                guild,
                datetime.now(timezone.utc),
                "joinwatch_bans",
            )
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
    label = _("The member has been kicked") if action == "kick" else _("The member has been banned")
    return (label, None)


async def _apply_joinwatch_assignment_actions(
    cog,
    guild,
    guild_settings: GuildSettings,
    actions: tuple[joinwatch_state.JoinwatchSelectedAction, ...],
    now: datetime,
    *,
    joinwatch_channel: discord.TextChannel | discord.Thread | None,
) -> None:
    for selected_action in actions:
        if selected_action.action == "discard_assignment":
            await joinwatch_state.delete_pending_assignment(
                cog,
                guild,
                selected_action.member_key,
            )
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
                status = _("Banned")
            elif (
                guild_settings.joinwatch_auto_role_action
                is JoinwatchAutoRoleActionOption.KICK
            ):
                status = _joinwatch_kick_status_value(
                    action_label,
                    _("Left server"),
                )
            else:
                status = _("Auto-role timer expired")
            await joinwatch_state.delete_pending_assignment(cog, guild, member_id)
            await joinwatch_publication.publish_joinwatch_incident(
                cog,
                guild,
                data,
                status,
            )
            if data.get("member_id") is None:
                await joinwatch_publication.publish_legacy_timer_result(
                    cog,
                    guild,
                    joinwatch_channel,
                    member_id=member_id,
                    title=_("Joinwatch auto-role timer expired"),
                    description=_("{mention} ({id}) left before the scheduled role could be applied.").format(
                        mention=f"<@{member_id}>",
                        id=member_id,
                    ),
                    action=action_label,
                    failed=False,
                    occurred_at=now,
                )
            continue
        if role is None:
            await joinwatch_state.delete_pending_assignment(cog, guild, member_id)
            continue
        if await cog._is_protected_member(member):
            await joinwatch_state.delete_pending_assignment(cog, guild, member_id)
            continue
        if role not in member.roles:
            if not await cog._punitive_effect_allowed(guild):
                await joinwatch_state.delete_pending_assignment(cog, guild, member_id)
                await joinwatch_publication.publish_joinwatch_incident(
                    cog,
                    guild,
                    data,
                    _("{role} planned (dry run)").format(role=role.mention),
                )
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
            try:
                await member.add_roles(role, reason="Automated account status update.")
                await cog._record_daily_stat(
                    guild,
                    datetime.now(timezone.utc),
                    "shadowbans",
                )
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
        try:
            expires_at = datetime.fromisoformat(
                typing.cast(str, data["expires_at"])
            )
        except (KeyError, TypeError, ValueError):
            expires_at = now + timedelta(
                minutes=guild_settings.joinwatch_auto_role_timer_minutes
            )
        await joinwatch_state.store_pending_role(
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
            incident=data,
        )
        await joinwatch_state.delete_pending_assignment(cog, guild, member_id)
        await joinwatch_publication.publish_joinwatch_incident(
            cog,
            guild,
            data,
            _("{role} applied until {time}").format(
                role=role.mention,
                time=discord.utils.format_dt(expires_at, style="R"),
            ),
        )


async def _apply_joinwatch_role_actions(
    cog,
    guild,
    guild_settings: GuildSettings,
    actions: tuple[joinwatch_state.JoinwatchSelectedAction, ...],
    now: datetime,
    *,
    joinwatch_channel: discord.TextChannel | discord.Thread | None,
) -> None:
    for selected_action in actions:
        if selected_action.action == "discard_role":
            await joinwatch_state.delete_pending_role(
                cog,
                guild,
                selected_action.member_key,
            )
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
                    status = _("Banned")
                elif (
                    guild_settings.joinwatch_auto_role_action
                    is JoinwatchAutoRoleActionOption.KICK
                ):
                    status = _joinwatch_kick_status_value(
                        action_label,
                        _("Left server"),
                    )
                else:
                    status = _("Auto-role timer expired")
                await joinwatch_state.delete_pending_role(cog, guild, member_id)
                await joinwatch_publication.publish_joinwatch_incident(
                    cog,
                    guild,
                    data,
                    status,
                )
            if data.get("member_id") is None:
                await joinwatch_publication.publish_legacy_timer_result(
                    cog,
                    guild,
                    joinwatch_channel,
                    member_id=member_id,
                    title=_("Joinwatch auto-role timer expired"),
                    description=_("{mention} ({id}) left before the auto-role timer expired.").format(
                        mention=f"<@{member_id}>",
                        id=member_id,
                    ),
                    action=failed if failed else action_label,
                    failed=bool(failed),
                    occurred_at=now,
                )
            continue
        if role is None:
            await joinwatch_state.delete_pending_role(cog, guild, member_id)
            continue
        if role not in member.roles:
            await joinwatch_state.delete_pending_role(cog, guild, member_id)
            await joinwatch_publication.publish_joinwatch_incident(
                cog,
                guild,
                data,
                _("Role manually removed"),
            )
            await cog._increment_stat(guild, "joinwatch_auto_roles_cleared")
            continue
        if await cog._is_protected_member(member):
            await joinwatch_state.delete_pending_role(cog, guild, member_id)
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
                status = _("Banned")
            elif (
                guild_settings.joinwatch_auto_role_action
                is JoinwatchAutoRoleActionOption.KICK
            ):
                status = _joinwatch_kick_status_value(
                    action_label,
                    _("Kicked"),
                )
            else:
                status = _("Auto-role timer expired")
            await joinwatch_state.delete_pending_role(cog, guild, member_id)
            await joinwatch_publication.publish_joinwatch_incident(
                cog,
                guild,
                data,
                status,
            )
        if data.get("member_id") is None:
            await joinwatch_publication.publish_legacy_timer_result(
                cog,
                guild,
                joinwatch_channel,
                member_id=member.id,
                title=_("Joinwatch auto-role timer expired"),
                description=_("{mention} ({id}) still had {role} when the timer expired.").format(
                    mention=member.mention,
                    id=member.id,
                    role=role.mention if role is not None else _("the auto-role"),
                ),
                action=failed if failed else action_label,
                failed=bool(failed),
                occurred_at=now,
            )


async def _apply_joinwatch_selected_work(
    cog,
    guild,
    guild_settings: GuildSettings,
    selected: joinwatch_state.JoinwatchSelection,
    now: datetime,
) -> None:
    if (
        selected.clear_assignments
        or selected.assignment_actions
        or selected.role_actions
    ):
        try:
            if selected.clear_assignments:
                await joinwatch_state.clear_pending_assignments(cog, guild)
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
            selected = joinwatch_state.select_due_joinwatch_assignments(
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
        member_key = str(member.id)
        existing_incident = guild_settings.joinwatch_pending_role_assignments.get(
            member_key
        ) or guild_settings.joinwatch_pending_roles.get(member_key)
        default_expires_at = now + timedelta(
            minutes=guild_settings.joinwatch_auto_role_timer_minutes
        )
        incident = joinwatch_state.build_incident(
            member,
            now=now,
            expires_at=default_expires_at,
            account_age_hours=hours,
            existing=existing_incident,
        )
        try:
            expires_at = datetime.fromisoformat(
                typing.cast(str, incident["expires_at"])
            )
        except (KeyError, TypeError, ValueError):
            expires_at = default_expires_at
            incident["expires_at"] = expires_at.isoformat()
        status = None
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
                    status = role_permission_error
                elif guild_settings.joinwatch_auto_role_random_delay_enabled:
                    existing_assignment = (
                        guild_settings.joinwatch_pending_role_assignments.get(
                            member_key
                        )
                    )
                    try:
                        apply_at = datetime.fromisoformat(
                            typing.cast(str, existing_assignment["apply_at"])
                        )
                    except (KeyError, TypeError, ValueError):
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
                        await cog._increment_stat(
                            member.guild,
                            "joinwatch_auto_roles_scheduled",
                        )
                    await joinwatch_state.store_pending_assignment(
                        cog,
                        member,
                        role.id,
                        apply_at,
                        expires_at=expires_at,
                        incident=incident,
                    )
                    status = _("{role} scheduled for {time}").format(
                        role=role.mention,
                        time=discord.utils.format_dt(apply_at, style="R"),
                    )
                elif not await cog._punitive_effect_allowed(member.guild):
                    status = _("{role} planned (dry run)").format(
                        role=role.mention,
                    )
                else:
                    try:
                        await member.add_roles(role, reason="Automated account status update.")
                        await cog._record_daily_stat(
                            member.guild,
                            datetime.now(timezone.utc),
                            "shadowbans",
                        )
                        await cog._increment_stat(member.guild, "joinwatch_auto_roles")
                        await joinwatch_state.store_pending_role(
                            cog,
                            member,
                            role.id,
                            expires_at,
                            applied_at=now,
                            incident=incident,
                        )
                        status = _("{role} applied until {time}").format(
                            role=role.mention,
                            time=discord.utils.format_dt(expires_at, style="R"),
                        )
                    except discord.HTTPException as exc:
                        await cog._increment_stat(member.guild, "joinwatch_auto_role_failures")
                        await cog._record_operational_failure(
                            member.guild.id,
                            "joinwatch_role_assignment",
                            f"Could not apply auto-role to user {member.id}: {exc}",
                            terminal=True,
                        )
                        status = _(
                            "I couldn't apply the configured joinwatch auto-role."
                        )
        destination = (
            channel
            if existing_incident is None and guild_settings.joinwatch_alert_enabled
            else None
        )
        await joinwatch_publication.publish_joinwatch_incident(
            cog,
            member.guild,
            incident,
            status,
            destination=destination,
            member=member,
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
            await joinwatch_state.delete_pending_role(cog, after.guild, after.id)
        else:
            role_removed = any(role.id == pending_role_id for role in before.roles) and not any(
                role.id == pending_role_id for role in after.roles
            )
            if role_removed:
                await joinwatch_state.delete_pending_role(cog, after.guild, after.id)
                await joinwatch_publication.publish_joinwatch_incident(
                    cog,
                    after.guild,
                    pending_role,
                    _("Role manually removed"),
                )
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
        effect = await cog._execute_action(
            after.guild,
            after,
            datetime.now(timezone.utc),
            guild_settings,
            reason=reason,
            origin=ModerationOrigin.AUTOMATIC,
            action=action,
            moderator=after.guild.me,
        )
        if effect.status is EffectStatus.PLANNED:
            description = _(
                "{mention} ({id}) took the bait role and would be {action} (dry run)."
            ).format(mention=after.mention, id=after.id, action=action)
        elif effect.status is EffectStatus.FAILED:
            await cog._record_operational_failure(
                after.guild.id,
                "bait_role_action",
                f"Could not {action} bait-role target {after.id}: {effect.failed_message or 'unknown error'}",
                terminal=True,
            )
            description = _(
                "{mention} ({id}) took the bait role, but the configured action failed."
            ).format(mention=after.mention, id=after.id)
        elif effect.status is EffectStatus.SUCCEEDED and effect.modlog_failed:
            await cog._record_operational_failure(
                after.guild.id,
                "bait_role_modlog",
                f"Could not create the modlog case after the {action} action for bait-role target {after.id}",
                terminal=True,
            )
            action_past = _("banned") if action == "ban" else _("kicked")
            description = _(
                "{mention} ({id}) took the bait role and was {action}, but the modlog case failed."
            ).format(
                mention=after.mention,
                id=after.id,
                action=action_past,
            )
        elif effect.status is EffectStatus.SUCCEEDED:
            action_past = _("banned") if action == "ban" else _("kicked")
            description = _(
                "{mention} ({id}) took the bait role and was {action}."
            ).format(
                mention=after.mention,
                id=after.id,
                action=action_past,
            )
        else:
            description = _("{mention} ({id}) took the bait role.").format(
                mention=after.mention,
                id=after.id,
            )
        bait_channel = cog._get_text_channel_or_thread(
            after.guild, guild_settings.baitrole_channel
        )
        if bait_channel is not None:
            embed = discord.Embed(
                title=_("Bait role triggered"),
                description=description,
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_thumbnail(url=after.display_avatar)
            try:
                await bait_channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                log.debug("Failed to send bait role log for user %s in guild %s", after.id, after.guild.id)
                await cog._record_operational_failure(
                    after.guild.id,
                    "bait_role_alert",
                    f"Could not publish bait-role alert for user {after.id}: {exc}",
                    terminal=True,
                )
