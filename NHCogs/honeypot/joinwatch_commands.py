"""JoinWatch configuration commands and moderator-facing timer views."""

from __future__ import annotations

import typing
from datetime import datetime, timezone

import discord
from redbot.core import commands
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import box, pagify

from . import joinwatch_publication, joinwatch_state
from .settings import (
    BOOL_OPTIONS,
    JOINWATCH_AUTO_ROLE_ACTION_OPTIONS,
    GuildSettings,
)

_ = Translator("Honeypot", __file__)

JOINWATCH_MAX_ACCOUNT_AGE_HOURS = 1_000_000
JOINWATCH_MAX_TIMER_MINUTES = 10_080


async def _reschedule_pending_roles(
    cog,
    guild: discord.Guild,
    old_timer_minutes: int,
    new_timer_minutes: int,
) -> int:
    alert_updates = await joinwatch_state.reschedule_pending_roles(
        cog,
        guild,
        old_timer_minutes,
        new_timer_minutes,
    )
    for data, role_id, expires_at in alert_updates:
        role = guild.get_role(role_id)
        if role is None:
            continue
        await joinwatch_publication.publish_joinwatch_incident(
            cog,
            guild,
            data,
            _("{role} applied until {time}").format(
                role=role.mention,
                time=discord.utils.format_dt(expires_at, style="R"),
            ),
        )
    return len(alert_updates)


async def joinwatch_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        current = await cog.config.guild(ctx.guild).joinwatch_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(current).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_enabled.set(value)
        await ctx.send(_("✅ Joinwatch enabled set to {value}").format(value=value))


async def joinwatch_alert_toggle(
    cog, ctx: commands.Context, value: bool = None
) -> None:
    if value is None:
        current = await cog.config.guild(ctx.guild).joinwatch_alert_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(current).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_alert_enabled.set(value)
        await ctx.send(_("✅ Joinwatch alerts set to {value}").format(value=value))


async def max_age(cog, ctx: commands.Context, hours: int = None) -> None:
    if hours is None:
        current = await cog.config.guild(ctx.guild).joinwatch_min_age_hours()
        await ctx.send(_("Joinwatch max age: {value} hours").format(value=current))
    elif hours < 1 or hours > JOINWATCH_MAX_ACCOUNT_AGE_HOURS:
        await ctx.send(
            _("Hours must be between 1 and {maximum}.").format(
                maximum=JOINWATCH_MAX_ACCOUNT_AGE_HOURS
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_min_age_hours.set(hours)
        await ctx.send(_("✅ Joinwatch max age set to {value} hours").format(value=hours))


async def joinwatch_autorole_toggle(
    cog, ctx: commands.Context, value: bool = None
) -> None:
    if value is None:
        current = await cog.config.guild(ctx.guild).joinwatch_auto_role_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(current).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_auto_role_enabled.set(value)
        await ctx.send(_("✅ Joinwatch auto-role set to {value}").format(value=value))


async def joinwatch_autorole_role(
    cog, ctx: commands.Context, role: discord.Role = None
) -> None:
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


async def joinwatch_autorole_timer(
    cog, ctx: commands.Context, minutes: int = None
) -> None:
    if minutes is None:
        current = await cog.config.guild(ctx.guild).joinwatch_auto_role_timer_minutes()
        await ctx.send(
            _("Joinwatch auto-role timer: {value} minutes").format(value=current)
        )
    elif minutes < 1 or minutes > JOINWATCH_MAX_TIMER_MINUTES:
        await ctx.send(_("Timer must be between 1 and 10080 minutes."))
    else:
        old_minutes = await cog.config.guild(ctx.guild).joinwatch_auto_role_timer_minutes()
        await cog.config.guild(ctx.guild).joinwatch_auto_role_timer_minutes.set(minutes)
        updated = await _reschedule_pending_roles(
            cog,
            ctx.guild,
            old_minutes,
            minutes,
        )
        await ctx.send(
            _(
                "✅ Joinwatch auto-role timer set to {value} minutes. Updated {count} active timer(s)"
            ).format(
                value=minutes,
                count=updated,
            )
        )


async def joinwatch_autorole_action(
    cog, ctx: commands.Context, value: str = None
) -> None:
    if value is None:
        current = await cog.config.guild(ctx.guild).joinwatch_auto_role_action()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=current,
                options=cog._format_options(JOINWATCH_AUTO_ROLE_ACTION_OPTIONS),
            )
        )
    elif value not in JOINWATCH_AUTO_ROLE_ACTION_OPTIONS:
        await ctx.send(
            _("Choose one of: {options}").format(
                options=cog._format_options(JOINWATCH_AUTO_ROLE_ACTION_OPTIONS)
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_auto_role_action.set(value)
        await ctx.send(
            _("✅ Joinwatch auto-role action set to {value}").format(value=value)
        )


async def joinwatch_autorole_bantimers(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    pending_roles = guild_settings.joinwatch_pending_roles
    if not pending_roles:
        await ctx.send(_("No active joinwatch punishment timers"))
        return

    now = datetime.now(timezone.utc)
    invalid = 0
    entries: list[tuple[datetime, str]] = []
    for member_id_str, data in pending_roles.items():
        try:
            member_id = int(member_id_str)
            expires_at = datetime.fromisoformat(typing.cast(str, data["expires_at"]))
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
                _("{member} | deadline: {deadline} | applied: {applied}").format(
                    member=member_label,
                    deadline=deadline,
                    applied=applied,
                ),
            )
        )

    if not entries:
        await ctx.send(_("No readable joinwatch punishment timers"))
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
        current = (
            await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_enabled()
        )
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(current).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_enabled.set(
            value
        )
        await ctx.send(
            _("✅ Joinwatch auto-role randomized delay set to {value}").format(
                value=value
            )
        )


async def joinwatch_autorole_randomize_min_time(
    cog, ctx: commands.Context, minutes: int = None
) -> None:
    if minutes is None:
        current = (
            await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_min_minutes()
        )
        await ctx.send(
            _("Joinwatch auto-role randomized minimum: {value} minutes").format(
                value=current
            )
        )
    elif minutes < 1 or minutes > JOINWATCH_MAX_TIMER_MINUTES:
        await ctx.send(_("Minimum delay must be between 1 and 10080 minutes."))
    else:
        current_max = (
            await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_max_minutes()
        )
        await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_min_minutes.set(
            minutes
        )
        if minutes > current_max:
            await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_max_minutes.set(
                minutes
            )
            await ctx.send(
                _(
                    "✅ Joinwatch randomized delay minimum and maximum set to {value} minutes"
                ).format(value=minutes)
            )
        else:
            await ctx.send(
                _(
                    "✅ Joinwatch randomized delay minimum set to {value} minutes"
                ).format(value=minutes)
            )


async def joinwatch_autorole_randomize_max_time(
    cog, ctx: commands.Context, minutes: int = None
) -> None:
    if minutes is None:
        current = (
            await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_max_minutes()
        )
        await ctx.send(
            _("Joinwatch auto-role randomized maximum: {value} minutes").format(
                value=current
            )
        )
    elif minutes < 1 or minutes > JOINWATCH_MAX_TIMER_MINUTES:
        await ctx.send(_("Maximum delay must be between 1 and 10080 minutes."))
    else:
        current_min = (
            await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_min_minutes()
        )
        if minutes < current_min:
            await ctx.send(
                _(
                    "Maximum delay must be greater than or equal to the current minimum ({value} minutes)."
                ).format(value=current_min)
            )
            return
        await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_max_minutes.set(
            minutes
        )
        await ctx.send(
            _("✅ Joinwatch randomized delay maximum set to {value} minutes").format(
                value=minutes
            )
        )


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
