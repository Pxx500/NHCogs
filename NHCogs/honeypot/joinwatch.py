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

from NHCogs.account_snapshot import account_snapshot

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
            _("Failed: {reason}\nNo more automatic retries").format(reason=failure),
        )
        return False
    retry_at = typing.cast(datetime, transition.retry_at)
    await joinwatch_publication.publish_joinwatch_incident(
        cog,
        guild,
        data,
        _("Failed: {reason}\nRetrying {time} ({count}/{max})").format(
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
            _("Failed: {reason}\nNo more automatic retries").format(reason=failure),
        )
        return False
    retry_at = typing.cast(datetime, transition.retry_at)
    await joinwatch_publication.publish_joinwatch_incident(
        cog,
        guild,
        data,
        _("Failed: {reason}\nRetrying {time} ({count}/{max})").format(
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
    incident: dict | None = None,
    store_name: str = "joinwatch_pending_roles",
) -> tuple[str | None, str | None]:
    action = settings.joinwatch_auto_role_action.value
    owner = getattr(cog, "_joinwatch_verification", None)
    async def finish(outcome, *, retain_pending=False):
        if owner is not None and incident is not None:
            await owner.finish(guild, member_id, incident, outcome, store_name=store_name, retain_pending=retain_pending)

    if action not in ("kick", "ban"):
        await finish("no_action")
        return (_("No joinwatch punishment configured"), None)
    if not await cog._punitive_effect_allowed(guild):
        await finish("dry_run")
        return (cog._dry_run_label(action), None)
    if incident is not None and incident.get("punishment_started_at"):
        await finish(incident.get("history_terminal", "action_uncertain"))
        return (_("Action outcome needs moderator review"), None)
    missing_permission = cog._missing_action_permission(guild, action)
    if missing_permission is not None:
        await cog._increment_stat(guild, "failed_actions")
        return (None, missing_permission)
    try:
        if owner is not None and incident is not None and (action == "ban" or member is not None):
            incident["punishment_started_at"] = datetime.now(timezone.utc).isoformat()
            incident["pre_action"] = {
                "captured_at": incident["punishment_started_at"],
                "profile": account_snapshot(member if member is not None else member_id),
                "action": action, "reason": reason, "executor_id": str(guild.me.id),
            }
            owner.event(incident, "action_started")
            await owner.persist_history(guild, member_id, incident, store_name=store_name)
        if action == "kick":
            if member is None:
                await finish("member_absent")
                if cog._automated_kick_fail_warning_enabled(settings.automated_kick_fail_warning):
                    return await cog._create_kick_fail_warning(guild, member_id)
                return (_("The member is no longer in the server"), None)
            try:
                await member.kick(reason=reason)
            except discord.NotFound:
                if cog._automated_kick_fail_warning_enabled(settings.automated_kick_fail_warning):
                    return await cog._create_kick_fail_warning(guild, member_id)
                raise
            await finish("timer_kicked", retain_pending=True)
        elif action == "ban":
            target = member if member is not None else await cog._get_user_or_object(member_id)
            await guild.ban(
                target,
                reason=reason,
                delete_message_seconds=cog._ban_delete_message_seconds(),
            )
            await finish("timer_banned", retain_pending=True)
            cog._schedule_post_ban_sweep(guild, target.id)
            await cog._record_daily_stat(
                guild,
                datetime.now(timezone.utc),
                "joinwatch_bans",
            )
        await cog._increment_stat(guild, "joinwatch_auto_role_punishments")
    except discord.HTTPException as exc:
        if owner is not None and incident is not None:
            incident.pop("punishment_started_at", None)
            owner.event(incident, "action_failed")
            await owner.persist_history(guild, member_id, incident, store_name=store_name)
        await cog._increment_stat(guild, "failed_actions")
        return (None, _("**Action failed:**\n") + box(str(exc), lang="py"))
    user = member if member is not None else await cog._get_user_or_object(member_id)
    try:
        case = await modlog.create_case(
            cog.bot,
            guild,
            datetime.now(timezone.utc),
            action_type=action,
            user=user,
            moderator=guild.me,
            reason=reason,
        )
        if incident is not None and case is not None:
            incident["modlog_case_number"] = case.case_number
    except Exception as error:
        log.exception("Failed to create modlog case in _execute_joinwatch_action")
        await cog._support.report_operational_error(
            guild_id=guild.id, source="Honeypot", action="create joinwatch log case", error=error
        )
    await finish("timer_kicked" if action == "kick" else "timer_banned")
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
        member_id = selected_action.member_id
        if member_id is None:
            await _apply_joinwatch_assignment_actions_locked(
                cog,
                guild,
                guild_settings,
                (selected_action,),
                now,
                joinwatch_channel=joinwatch_channel,
            )
            continue
        async with joinwatch_state.member_lock(cog, guild.id, member_id):
            current = (
                (await cog.config.guild(guild).all())
                .get("joinwatch_pending_role_assignments", {})
                .get(str(member_id))
            )
            if current != selected_action.data:
                continue
            await _apply_joinwatch_assignment_actions_locked(
                cog,
                guild,
                guild_settings,
                (selected_action,),
                now,
                joinwatch_channel=joinwatch_channel,
            )


async def _apply_joinwatch_assignment_actions_locked(
    cog,
    guild,
    guild_settings,
    actions,
    now,
    *,
    joinwatch_channel,
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
        if data.get("source") == "group" and not await cog._joinwatch_verification.check_scheduled_group_assignment(guild, member_id, data):
            continue
        member = await cog._get_member_or_fetch(guild, member_id)
        role = guild.get_role(role_id)
        if member is None:
            if data.get("test"):
                await joinwatch_state.delete_pending_assignment(cog, guild, member_id)
                continue
            action_label, failed = await _execute_joinwatch_action(
                cog,
                guild,
                None,
                member_id,
                guild_settings,
                reason="Suspicious Account",
                incident=data,
                store_name="joinwatch_pending_role_assignments",
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
            if guild_settings.joinwatch_auto_role_action is JoinwatchAutoRoleActionOption.BAN:
                status = _("Banned")
            elif guild_settings.joinwatch_auto_role_action is JoinwatchAutoRoleActionOption.KICK:
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
                    description=_(
                        "{mention} ({id}) left before the scheduled role could be applied"
                    ).format(
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
        if role in member.roles:
            active = (await cog.config.guild(guild).all()).get("joinwatch_pending_roles", {}).get(member_id_str)
            await joinwatch_state.delete_pending_assignment(cog, guild, member_id)
            if not (active is not None and active.get("role_id") == role_id
                    and active.get("incident_id") == data.get("incident_id")):
                await cog._record_operational_failure(
                    guild.id, "joinwatch_preexisting_role",
                    "Scheduled JoinWatch role work was cancelled because the member already holds the role without the same active incident",
                    terminal=True,
                )
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
                if not data.get("test") and not data.get("restore_incident"):
                    await cog._record_daily_stat(guild, datetime.now(timezone.utc), "shadowbans")
                    await cog._increment_stat(guild, "joinwatch_auto_roles")
            except discord.HTTPException:
                await cog._increment_stat(guild, "joinwatch_auto_role_failures")
                await _reschedule_joinwatch_assignment_retry(
                    cog,
                    guild,
                    member_id_str,
                    data,
                    now,
                    failure=_("I couldn't apply the configured joinwatch auto-role"),
                )
                continue
        try:
            expires_at = datetime.fromisoformat(typing.cast(str, data["expires_at"]))
        except (KeyError, TypeError, ValueError):
            expires_at = now + timedelta(minutes=guild_settings.joinwatch_auto_role_timer_minutes)
        await joinwatch_state.store_pending_role(
            cog,
            member,
            role.id,
            expires_at,
            applied_at=now,
            alert_channel_id=typing.cast(int | None, data.get("alert_channel_id")),
            alert_message_id=typing.cast(int | None, data.get("alert_message_id")),
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
        member_id = selected_action.member_id
        if member_id is None:
            await _apply_joinwatch_role_actions_locked(
                cog,
                guild,
                guild_settings,
                (selected_action,),
                now,
                joinwatch_channel=joinwatch_channel,
            )
            continue
        async with joinwatch_state.member_lock(cog, guild.id, member_id):
            current = (
                (await cog.config.guild(guild).all())
                .get("joinwatch_pending_roles", {})
                .get(str(member_id))
            )
            if current is None or current != selected_action.data or current.get("test"):
                continue
            if current.get("verification_state") == "release_pending":
                continue
            await _apply_joinwatch_role_actions_locked(
                cog,
                guild,
                guild_settings,
                (selected_action,),
                now,
                joinwatch_channel=joinwatch_channel,
            )


async def _apply_joinwatch_role_actions_locked(
    cog,
    guild,
    guild_settings,
    actions,
    now,
    *,
    joinwatch_channel,
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
                incident=data,
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
                if guild_settings.joinwatch_auto_role_action is JoinwatchAutoRoleActionOption.BAN:
                    status = _("Banned")
                elif (
                    guild_settings.joinwatch_auto_role_action is JoinwatchAutoRoleActionOption.KICK
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
                    description=_(
                        "{mention} ({id}) left before the auto-role timer expired"
                    ).format(
                        mention=f"<@{member_id}>",
                        id=member_id,
                    ),
                    action=failed if failed else action_label,
                    failed=bool(failed),
                    occurred_at=now,
                )
            continue
        if role is None:
            await joinwatch_state.delete_pending_role(cog, guild, member_id, outcome="role_missing")
            continue
        if role not in member.roles:
            await joinwatch_state.delete_pending_role(cog, guild, member_id, outcome="manual")
            await joinwatch_publication.publish_joinwatch_incident(
                cog,
                guild,
                data,
                _("Role manually removed"),
            )
            await cog._increment_stat(guild, "joinwatch_auto_roles_cleared")
            continue
        if await cog._is_protected_member(member):
            await joinwatch_state.delete_pending_role(cog, guild, member_id, outcome="protected")
            continue
        action_label, failed = await _execute_joinwatch_action(
            cog,
            guild,
            member,
            member_id,
            guild_settings,
            reason="Suspicious Account",
            incident=data,
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
            if guild_settings.joinwatch_auto_role_action is JoinwatchAutoRoleActionOption.BAN:
                status = _("Banned")
            elif guild_settings.joinwatch_auto_role_action is JoinwatchAutoRoleActionOption.KICK:
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
                description=_("{mention} ({id}) still had {role} when the timer expired").format(
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
    if selected.clear_assignments or selected.assignment_actions or selected.role_actions:
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
            owner = getattr(cog, "_joinwatch_verification", None)
            if owner is not None:
                await owner.settle_history(guild)
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
    groups = getattr(cog, "_joinwatch_groups", None)
    candidates = ()
    if (
        groups is not None
        and not member.bot
        and not await cog.bot.cog_disabled_in_guild(cog, member.guild)
    ):
        try:
            candidates = await groups.observe(member)
        except Exception as error:
            await cog._record_operational_failure(
                member.guild.id,
                "joinwatch_group_observation",
                f"Could not observe a JoinWatch cohort: {error}",
            )
    async with joinwatch_state.member_lock(cog, member.guild.id, member.id):
        await _on_member_join_locked(cog, member)
    owner = getattr(cog, "_joinwatch_verification", None)
    if owner is not None:
        for user_id in candidates:
            candidate = await cog._get_member_or_fetch(member.guild, user_id)
            if candidate is not None:
                await owner.schedule_group(candidate)


async def _on_member_join_locked(cog, member: discord.Member) -> None:
    if await cog.bot.cog_disabled_in_guild(cog, member.guild):
        return
    if member.bot:
        return
    raw_config = await cog.config.guild(member.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    owner = getattr(cog, "_joinwatch_verification", None)
    member_key = str(member.id)
    active_incident = guild_settings.joinwatch_pending_roles.get(member_key)
    pending_incident = guild_settings.joinwatch_pending_role_assignments.get(member_key)
    existing_incident = active_incident or pending_incident
    if active_incident is not None and pending_incident is not None and active_incident.get("incident_id") == pending_incident.get("incident_id"):
        existing_incident = dict(active_incident)
        existing_incident["join_count"] = pending_incident.get("join_count", active_incident.get("join_count", 0))
    if active_incident is None and existing_incident is not None and existing_incident.get("source") in ("wave", "test"):
        await cog._record_operational_failure(
            member.guild.id, "joinwatch_enrollment_reconciliation",
            "Interrupted CAPTCHA role work needs moderator reconciliation before another enrollment",
        )
        return
    if existing_incident is None and member_key in raw_config.get("joinwatch_verified_members", {}):
        return
    if not guild_settings.joinwatch_enabled and existing_incident is None:
        return
    if not (existing_incident and existing_incident.get("test")):
        await cog._increment_stat(member.guild, "joinwatch_total_joins")
    channel = cog._get_text_channel_or_thread(member.guild, guild_settings.joinwatch_channel)
    now = datetime.now(timezone.utc)
    min_age = timedelta(hours=guild_settings.joinwatch_min_age_hours)
    if member.created_at > now - min_age or existing_incident is not None:
        if member.created_at > now - min_age and not (existing_incident and existing_incident.get("test")):
            await cog._increment_stat(member.guild, "joinwatch_young_joins")
        hours = max(1, round((now - member.created_at).total_seconds() / 3600))
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
        incident.setdefault("captcha_enabled", bool(raw_config.get("joinwatch_captcha_enabled")))
        if owner is not None:
            incident.setdefault("source", "join")
            incident.setdefault("reasons", ["age"])
            await owner.capture_enrollment(member, incident)
        if existing_incident is not None:
            incident["restore_incident"] = active_incident is not None
        if owner is not None and (incident.get("captcha_enabled") or incident.get("challenge")):
            await owner.prepare_assignment(member, incident)
        try:
            expires_at = datetime.fromisoformat(typing.cast(str, incident["expires_at"]))
        except (KeyError, TypeError, ValueError):
            expires_at = default_expires_at
            incident["expires_at"] = expires_at.isoformat()
        status = None
        if (
            (guild_settings.joinwatch_auto_role_enabled or existing_incident is not None)
            and incident.get("role_id", guild_settings.joinwatch_auto_role_id) is not None
        ):
            role = member.guild.get_role(incident.get("role_id", guild_settings.joinwatch_auto_role_id))
            if (
                role is not None
                and role not in member.roles
                and not await cog._is_protected_member(member)
            ):
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
                    existing_assignment = guild_settings.joinwatch_pending_role_assignments.get(
                        member_key
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
                        if not incident.get("test"):
                            await cog._increment_stat(member.guild, "joinwatch_auto_roles_scheduled")
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
                elif owner is not None and incident.get("captcha_enabled"):
                    await joinwatch_state.store_pending_assignment(
                        cog,
                        member,
                        role.id,
                        now,
                        expires_at=expires_at,
                        incident=incident,
                    )
                    selection = joinwatch_state.select_due_joinwatch_assignments(
                        now=now,
                        assignments_enabled=True,
                        pending_assignments={
                            member_key: {
                                **incident,
                                "role_id": role.id,
                                "apply_at": now.isoformat(),
                            }
                        },
                        pending_roles={},
                    )
                    await _apply_joinwatch_assignment_actions(
                        cog,
                        member.guild,
                        guild_settings,
                        selection.assignment_actions,
                        now,
                        joinwatch_channel=channel,
                    )
                    status = _("{role} applied until {time}").format(
                        role=role.mention, time=discord.utils.format_dt(expires_at, style="R")
                    )
                else:
                    try:
                        await member.add_roles(role, reason="Automated account status update.")
                        if not incident.get("test") and not incident.get("restore_incident"):
                            await cog._record_daily_stat(member.guild, datetime.now(timezone.utc), "shadowbans")
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
                        status = _("I couldn't apply the configured joinwatch auto-role")
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
    async with joinwatch_state.member_lock(cog, after.guild.id, after.id):
        await _on_member_update_locked(cog, before, after)


async def _on_member_update_locked(cog, before: discord.Member, after: discord.Member) -> None:
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
            pending_role_id = int(typing.cast(typing.Any, pending_role["role_id"]))
        except (KeyError, TypeError, ValueError):
            await joinwatch_state.delete_pending_role(cog, after.guild, after.id)
        else:
            role_removed = any(role.id == pending_role_id for role in before.roles) and not any(
                role.id == pending_role_id for role in after.roles
            )
            if role_removed:
                await joinwatch_state.delete_pending_role(cog, after.guild, after.id, outcome="manual")
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
                "{mention} ({id}) took the bait role and would be {action} (dry run)"
            ).format(mention=after.mention, id=after.id, action=action)
        elif effect.status is EffectStatus.FAILED:
            await cog._record_operational_failure(
                after.guild.id,
                "bait_role_action",
                f"Could not {action} bait-role target {after.id}: {effect.failed_message or 'unknown error'}",
                terminal=True,
            )
            description = _(
                "{mention} ({id}) took the bait role, but the configured action failed"
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
                "{mention} ({id}) took the bait role and was {action}, but the modlog case failed"
            ).format(
                mention=after.mention,
                id=after.id,
                action=action_past,
            )
        elif effect.status is EffectStatus.SUCCEEDED:
            action_past = _("banned") if action == "ban" else _("kicked")
            description = _("{mention} ({id}) took the bait role and was {action}").format(
                mention=after.mention,
                id=after.id,
                action=action_past,
            )
        else:
            description = _("{mention} ({id}) took the bait role").format(
                mention=after.mention,
                id=after.id,
            )
        bait_channel = cog._get_text_channel_or_thread(after.guild, guild_settings.baitrole_channel)
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
                log.debug(
                    "Failed to send bait role log for user %s in guild %s", after.id, after.guild.id
                )
                await cog._record_operational_failure(
                    after.guild.id,
                    "bait_role_alert",
                    f"Could not publish bait-role alert for user {after.id}: {exc}",
                    terminal=True,
                )
