"""JoinWatch configuration commands and moderator-facing timer views."""

from __future__ import annotations

import io
import secrets
import time
import typing
from collections import Counter
from datetime import datetime, timezone

import discord
from redbot.core import commands
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import box, pagify

from . import joinwatch_publication, joinwatch_state
from .captcha_commands import check_configuration
from .joinwatch_groups import MAX_HISTORY_BYTES, GroupCriteria
from .settings import (
    BOOL_OPTIONS,
    JOINWATCH_AUTO_ROLE_ACTION_OPTIONS,
    GuildSettings,
)

_ = Translator("Honeypot", __file__)

JOINWATCH_MAX_ACCOUNT_AGE_HOURS = 1_000_000
JOINWATCH_MAX_TIMER_MINUTES = 10_080
WAVE_PROGRESS_INTERVAL_SECONDS = 10
MAX_GROUP_ADMISSION_LIMIT = 10_000


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
            _("Hours must be between 1 and {maximum}").format(
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
    cog, ctx: commands.Context, role: discord.Role | str | None = None
) -> None:
    if isinstance(role, str):
        if role.casefold() != "clear":
            raise commands.UserFeedbackCheckFailure(_("Provide a role or use clear"))
        await cog.config.guild(ctx.guild).joinwatch_auto_role_id.set(None)
        await ctx.send(_("Joinwatch auto-role cleared"))
        return
    if role is None:
        if not cog._channel_is_private(ctx.guild, ctx.channel):
            await ctx.send(_("Run this command in a private moderator channel"))
            return
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
        await ctx.send(_("Timer must be between 1 and 10080 minutes"))
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


async def joinwatch_bantimers(cog, ctx: commands.Context) -> None:
    if not cog._group_overview_is_private(ctx):
        await ctx.send(_("Use this command in a private moderator channel"))
        return
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    pending_roles = guild_settings.joinwatch_pending_roles
    role = ctx.guild.get_role(guild_settings.joinwatch_auto_role_id)
    role_members = role.members if role is not None else []
    without_timer = sorted(
        (member for member in role_members if str(member.id) not in pending_roles),
        key=lambda member: (member.display_name.casefold(), member.id),
    )

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

        member = ctx.guild.get_member(member_id)
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

    entries.sort(key=lambda item: item[0])
    lines = [_("JoinWatch shadowbans")]
    if role is None:
        lines.append(_("Shadowban role: Not configured or unavailable"))
    else:
        lines.append(_("Shadowban role holders: {count}").format(count=len(role_members)))
    if not ctx.guild.chunked:
        lines.append(_("Member cache is incomplete: role holders may be missing from this list"))
    if invalid:
        lines.append(_("Skipped invalid timers: {count}").format(count=invalid))
    lines.extend(["", _("With timer: {count}").format(count=len(entries))])
    lines.extend(f"{index}. {entry}" for index, (_, entry) in enumerate(entries, 1))
    lines.extend(["", _("Without JoinWatch timer: {count}").format(count=len(without_timer))])
    lines.extend(
        f"{index}. {member.display_name} ({member.id})"
        for index, member in enumerate(without_timer, 1)
    )
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
        await ctx.send(_("Minimum delay must be between 1 and 10080 minutes"))
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
        await ctx.send(_("Maximum delay must be between 1 and 10080 minutes"))
    else:
        current_min = (
            await cog.config.guild(ctx.guild).joinwatch_auto_role_random_delay_min_minutes()
        )
        if minutes < current_min:
            await ctx.send(
                _(
                    "Maximum delay must be greater than or equal to the current minimum ({value} minutes)"
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
    if not cog._channel_is_private(ctx.guild, ctx.channel):
        await ctx.send("Run this command in a private moderator channel")
        return
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
        f"  CAPTCHA for new joins: {str(guild_settings.joinwatch_captcha_enabled).lower()}",
        f"  Group rule: {str(guild_settings.joinwatch_groups_enabled).lower()}",
        f"  Group criteria: {guild_settings.joinwatch_groups_minimum_accounts} accounts / {guild_settings.joinwatch_groups_join_window_minutes} minutes / {guild_settings.joinwatch_groups_creation_distance_hours} hours",
    ]
    await ctx.send(_("Joinwatch config:\n") + box("\n".join(lines)))


async def _require_private(cog, ctx) -> bool:
    if cog._channel_is_private(ctx.guild, ctx.channel):
        return True
    await ctx.send("Run this command in a private moderator channel")
    return False


async def captcha_toggle(cog, ctx, value: bool) -> None:
    if not await _require_private(cog, ctx):
        return
    if value:
        await check_configuration(cog, ctx.guild)
    await cog.config.guild(ctx.guild).joinwatch_captcha_enabled.set(value)
    adopted = 0
    if value:
        adopted = await cog._joinwatch_verification.enable_existing(ctx.guild)
    await ctx.send(f"CAPTCHA for new age-based JoinWatch entries: {str(value).lower()}. Existing timers admitted: {adopted}. Questions prepare in the background. Existing attempts and deadlines are unchanged. Auto-role protection remains controlled by autorole settings", allowed_mentions=discord.AllowedMentions.none())


async def groups_toggle(cog, ctx, value: bool) -> None:
    if not await _require_private(cog, ctx):
        return
    if value:
        await check_configuration(cog, ctx.guild)
    await cog.config.guild(ctx.guild).joinwatch_groups_enabled.set(value)
    await ctx.send(f"Group rule for new joins: {str(value).lower()}. No historical wave was started", allowed_mentions=discord.AllowedMentions.none())


async def config_groups(cog, ctx) -> None:
    configured = GuildSettings.from_mapping(await cog.config.guild(ctx.guild).all())
    await cog._send_config_dump(ctx, "JoinWatch groups", [("Configuration", "\n".join((
        f"Enabled: {str(configured.joinwatch_groups_enabled).lower()}",
        f"Minimum accounts: {configured.joinwatch_groups_minimum_accounts}",
        f"Join window: {configured.joinwatch_groups_join_window_minutes} minutes",
        f"Creation distance: {configured.joinwatch_groups_creation_distance_hours} hours",
        f"Maximum active group-only checks: {configured.joinwatch_groups_max_active}",
        f"New group-only checks per rolling minute: {configured.joinwatch_groups_per_minute}",
        "Criteria changes affect future joins only. Historical waves require separate confirmation.",
    )))])


async def groups_limits(cog, ctx, max_active, per_minute):
    if not await _require_private(cog, ctx):
        return
    if not 1 <= max_active <= MAX_GROUP_ADMISSION_LIMIT or not 1 <= per_minute <= MAX_GROUP_ADMISSION_LIMIT:
        raise commands.UserFeedbackCheckFailure("Both limits must be between 1 and 10000")
    config = cog.config.guild(ctx.guild)
    await config.set_raw("joinwatch_groups_max_active", value=max_active)
    await config.set_raw("joinwatch_groups_per_minute", value=per_minute)
    await ctx.send(f"Group-only limits: {max_active} active or reserved checks, {per_minute} new checks per rolling minute. No accounts were enrolled", allowed_mentions=discord.AllowedMentions.none())


def _criteria(minimum_accounts, join_window_minutes, creation_distance_hours):
    try:
        return GroupCriteria(minimum_accounts, join_window_minutes, creation_distance_hours)
    except (ValueError, TypeError) as error:
        raise commands.UserFeedbackCheckFailure(str(error)) from error


def _criteria_text(values) -> str:
    return f"{values['minimum_accounts']} accounts / {values['join_window_minutes']} minutes / {values['creation_distance_hours']} hours"


def wave_embed(record, *, criteria_change=False):
    title = "Group criteria preview" if criteria_change else "Historical wave"
    embed = discord.Embed(title=title, description=f"New accounts: **{record.get('new', 0)}**\nState: {record.get('status', 'preview')}")
    rows = [
        f"Criteria: {_criteria_text(record['criteria'])}",
        f"Matching present: {record.get('matching_present', 0)}",
        f"Already verified or enrolled: {record.get('already', 0)}",
        f"Excluded: {record.get('excluded', 0)}",
        f"Calculated: {record.get('created_at', 'Not available')}",
        f"Data: {'Complete' if record.get('complete') else 'Incomplete or approximate'}",
        f"Source: {record.get('source', 'Observed first joins')}",
    ]
    if criteria_change:
        rows.insert(0, f"Previous: {_criteria_text(record['previous'])}")
        rows.append("Confirm changes future-join criteria only. It doesn't enroll current members.")
    else:
        notification = record.get('notifications', {})
        rows.append(f"Invitations: {notification.get('batch_size', 5)} people every {notification.get('interval_seconds', 15)} seconds, about {notification.get('estimated_seconds', 0)} seconds")
        rows.append("Start applies restrictions to this saved candidate list only.")
        rows.append("Pause stops new restrictions and invitations. Existing deadlines continue. Rollback settles only this wave's restrictions.")
    embed.add_field(name="Scope", value="\n".join(rows)[:1024], inline=False)
    if record.get("entries"):
        counts = Counter(entry["status"] for entry in record["entries"].values())
        applied = sum(counts[status] for status in ("enrolled", "notifying", "notified", "notification_unknown", "released"))
        rows = [f"Prepared: {applied}", f"Restrictions applied: {applied}", f"Invitations sent: {counts['notified']}", f"Pending: {sum(counts[status] for status in ('queued', 'preparing', 'enrolling', 'enrolled'))}", f"Errors or uncertain: {sum(counts[status] for status in ('error', 'uncertain', 'notification_unknown'))}", f"Skipped: {counts['skipped']}"]
        if counts['notification_unknown']:
            rows.append("Some invitations may or may not have been delivered. Resume acknowledges this and won't resend them.")
        if record.get("error"):
            rows.append(f"Attention: {record['error']}")
        embed.add_field(name="Progress", value="\n".join(rows)[:1024], inline=False)
    return embed


def _can_manage(interaction, owner_id, cog) -> bool:
    return (
        interaction.guild is not None
        and interaction.user.id == owner_id
        and interaction.permissions.manage_messages
        and cog._channel_is_private(interaction.guild, interaction.channel)
    )


class ModeratorConfirmationView(discord.ui.View):
    async def on_error(self, interaction, error, item) -> None:
        self.cog._support.schedule_error(source="Honeypot", action="JoinWatch confirmation", error=error)
        await interaction.edit_original_response(content="The operation couldn't be completed. Check the moderator log before retrying", view=None, allowed_mentions=discord.AllowedMentions.none())


class CriteriaConfirmationView(ModeratorConfirmationView):
    def __init__(self, cog, preview, owner_id):
        super().__init__(timeout=600)
        self.cog, self.preview, self.owner_id = cog, preview, owner_id
        self._used = False
        for label in ("Confirm", "Cancel"):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.success if label == "Confirm" else discord.ButtonStyle.secondary, custom_id=secrets.token_urlsafe(24))

            async def callback(interaction, selected=label):
                await interaction.response.defer(ephemeral=True, thinking=True)
                if not _can_manage(interaction, self.owner_id, self.cog):
                    await interaction.edit_original_response(content="Only the moderator who opened this preview can use it, with Manage Messages in a private channel.")
                    return
                if self._used:
                    await interaction.edit_original_response(content="This preview is already closed.")
                    return
                if selected == "Confirm":
                    try:
                        await self.cog._joinwatch_groups.confirm_criteria(interaction.guild, self.preview, interaction.user.id, True)
                    except ValueError as error:
                        await interaction.edit_original_response(content=str(error), allowed_mentions=discord.AllowedMentions.none())
                        return
                self._used = True
                await interaction.message.edit(view=None)
                await interaction.edit_original_response(content="Criteria updated. No historical wave was started." if selected == "Confirm" else "Criteria change cancelled.")

            button.callback = callback
            self.add_item(button)


class WaveControlView(ModeratorConfirmationView):
    """Persistent, owner-bound controls resolving the saved wave each time."""

    def __init__(self, cog, record):
        super().__init__(timeout=None)
        self.cog, self.wave_id = cog, record["id"]
        self.owner_id = record["moderator_id"]
        labels = {
            "preview": [(f"Start wave ({record.get('new', 0)})", "confirm"), ("Cancel", "cancel")],
            "running": [("Pause", "pause"), ("Rollback", "rollback")],
            "paused": [("Resume", "resume"), ("Rollback", "rollback")],
            "completed": [("Rollback", "rollback"), ("Mark finished", "finish")],
        }.get(record.get("status"), [])
        for label, action in labels:
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.danger if action == "rollback" else discord.ButtonStyle.secondary, custom_id=f"honeypot:wave:{self.wave_id}:{action}")

            async def callback(interaction, selected=action):
                await self.control(interaction, selected)

            button.callback = callback
            self.add_item(button)

    async def control(self, interaction, action):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not _can_manage(interaction, self.owner_id, self.cog):
            await interaction.edit_original_response(content="Only the moderator who opened this wave can control it, with Manage Messages in a private channel.")
            return
        owner = self.cog._joinwatch_waves
        try:
            if action == "rollback":
                preview = await owner.rollback_preview(interaction.guild, self.wave_id, interaction.user.id, True)
                await interaction.edit_original_response(content=f"Rollback will settle this wave's own restrictions for {preview['rollback_count']} accounts. Independent moderation restrictions remain.", view=WaveRollbackView(self.cog, self.wave_id, self.owner_id, preview))
                return
            record = await getattr(owner, action)(interaction.guild, self.wave_id, interaction.user.id, True)
        except ValueError as error:
            await interaction.edit_original_response(content=str(error), allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.message.edit(embed=wave_embed(record), view=WaveControlView(self.cog, record), allowed_mentions=discord.AllowedMentions.none())
        await interaction.edit_original_response(content=f"Wave state: {record['status']}")


class WaveRollbackView(ModeratorConfirmationView):
    def __init__(self, cog, wave_id, owner_id, preview):
        super().__init__(timeout=300)
        self.cog, self.wave_id, self.owner_id, self.preview = cog, wave_id, owner_id, preview
        for label in ("Confirm rollback", "Cancel"):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.danger if label == "Confirm rollback" else discord.ButtonStyle.secondary, custom_id=secrets.token_urlsafe(24))

            async def callback(interaction, selected=label):
                await interaction.response.defer(ephemeral=True)
                if not _can_manage(interaction, self.owner_id, self.cog):
                    return
                if selected == "Cancel":
                    await interaction.edit_original_response(content="Rollback cancelled", view=None)
                    return
                try:
                    record = await self.cog._joinwatch_waves.rollback(interaction.guild, self.wave_id, interaction.user.id, True, confirmation_token=self.preview["confirmation_token"])
                except ValueError as error:
                    await interaction.edit_original_response(content=str(error), view=None, allowed_mentions=discord.AllowedMentions.none())
                    return
                await interaction.edit_original_response(content=f"Wave state: {record['status']}", view=None)

            button.callback = callback
            self.add_item(button)


async def groups_criteria(cog, ctx, minimum_accounts, join_window_minutes, creation_distance_hours):
    if not await _require_private(cog, ctx):
        return
    criteria = _criteria(minimum_accounts, join_window_minutes, creation_distance_hours)
    preview = await cog._joinwatch_groups.criteria_preview(ctx.guild, criteria, ctx.author.id)
    await ctx.send(embed=wave_embed(preview, criteria_change=True), view=CriteriaConfirmationView(cog, preview, ctx.author.id), allowed_mentions=discord.AllowedMentions.none())


async def wave(cog, ctx, minimum_accounts, join_window_minutes, creation_distance_hours):
    if not await _require_private(cog, ctx):
        return
    criteria = _criteria(minimum_accounts, join_window_minutes, creation_distance_hours)
    preview = await cog._joinwatch_waves.preview(ctx.guild, criteria, ctx.author.id)
    candidates = "\n".join(f"{uid}\t{getattr(ctx.guild.get_member(int(uid)), 'display_name', 'Not cached')}" for uid in preview['targets'])
    message = await ctx.send(embed=wave_embed(preview), view=WaveControlView(cog, preview), file=discord.File(io.BytesIO(candidates.encode('utf-8')), filename="wave-candidates.txt"), allowed_mentions=discord.AllowedMentions.none())
    await cog._joinwatch_waves.attach_message(ctx.guild, preview['id'], ctx.channel.id, message.id)


async def history_import(cog, ctx):
    if not await _require_private(cog, ctx):
        return
    attachments = ctx.message.attachments
    if len(attachments) != 1 or not attachments[0].filename.lower().endswith('.json'):
        raise commands.UserFeedbackCheckFailure("Attach one normalized history JSON file")
    if attachments[0].size > MAX_HISTORY_BYTES:
        raise commands.UserFeedbackCheckFailure("History files must be no larger than 20 MiB")
    payload = await attachments[0].read()
    try:
        result = await cog._joinwatch_groups.import_history(ctx.guild, payload)
    except (ValueError, TypeError) as error:
        raise commands.UserFeedbackCheckFailure(str(error)) from error
    await ctx.send(f"History imported: {result['imported']} observations, {result['total']} total. No roles were changed", allowed_mentions=discord.AllowedMentions.none())


async def update_wave_message(cog, guild, record):
    """Keep the saved private progress message current at most every ten seconds."""
    if not record.get("message_id"):
        return
    key = guild.id, record["id"]
    observed = time.monotonic()
    last_time, last_status = cog._wave_render_state.get(key, (0.0, ""))
    if observed - last_time < WAVE_PROGRESS_INTERVAL_SECONDS and record["status"] == last_status:
        return
    channel = cog._get_text_channel_or_thread(guild, record.get("channel_id"))
    if channel is None or not cog._channel_is_private(guild, channel):
        return
    message = await channel.fetch_message(record["message_id"])
    await message.edit(embed=wave_embed(record), view=WaveControlView(cog, record), allowed_mentions=discord.AllowedMentions.none())
    cog._wave_render_state[key] = observed, record["status"]
