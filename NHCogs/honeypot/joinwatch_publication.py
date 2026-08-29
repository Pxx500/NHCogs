"""Discord message lifecycle for JoinWatch incidents."""

from __future__ import annotations

import logging
import typing
from datetime import datetime, timezone

import discord
from redbot.core.i18n import Translator

from . import joinwatch_state

_ = Translator("Honeypot", __file__)
log = logging.getLogger("red.Honeypot")


def _incident_embed(
    member: discord.Member | None,
    incident: typing.Mapping[str, typing.Any],
    status: str | None,
) -> discord.Embed:
    member_id = member.id if member is not None else incident.get("member_id")
    member_mention = (
        member.mention
        if member is not None
        else incident.get("member_mention") or f"<@{member_id}>"
    )
    member_display_name = (
        member.display_name
        if member is not None
        else incident.get("member_display_name") or str(member_id)
    )
    member_avatar = (
        member.display_avatar
        if member is not None
        else incident.get("member_avatar_url")
    )
    try:
        timestamp = datetime.fromisoformat(
            typing.cast(str, incident["first_joined_at"])
        )
    except (KeyError, TypeError, ValueError):
        timestamp = (
            member.joined_at if member is not None else None
        ) or datetime.now(timezone.utc)
    embed = discord.Embed(
        title=_("New account joined"),
        description=_(
            "**{member}**\nMention: {mention}\nID: `{id}`\n"
            "Account is ~{hours} hours old."
        ).format(
            member=incident.get("member_label") or member_display_name,
            mention=member_mention,
            id=member_id,
            hours=incident.get("account_age_hours", 1),
        ),
        color=discord.Color.orange(),
        timestamp=timestamp,
    )
    embed.set_author(
        name=f"{incident.get('member_display_name') or member_display_name} ({member_id})",
        icon_url=incident.get("member_avatar_url") or member_avatar,
    )
    embed.set_thumbnail(
        url=incident.get("member_avatar_url") or member_avatar
    )
    try:
        join_count = max(1, int(incident.get("join_count", 1)))
    except (TypeError, ValueError):
        join_count = 1
    if join_count > 1:
        latest_join = incident.get("last_joined_at")
        deadline = incident.get("expires_at")
        try:
            latest_join = discord.utils.format_dt(
                datetime.fromisoformat(typing.cast(str, latest_join)),
                style="R",
            )
        except (TypeError, ValueError):
            latest_join = _("unknown")
        try:
            deadline = discord.utils.format_dt(
                datetime.fromisoformat(typing.cast(str, deadline)),
                style="R",
            )
        except (TypeError, ValueError):
            deadline = _("unknown")
        embed.add_field(
            name=_("Join activity:"),
            value=_(
                "Joins: {joins} ({rejoins} rejoins)\n"
                "Latest join: {latest}\nOriginal deadline: {deadline}"
            ).format(
                joins=join_count,
                rejoins=join_count - 1,
                latest=latest_join,
                deadline=deadline,
            ),
            inline=False,
        )
    if status is not None:
        embed.add_field(
            name=_("Auto-role:"),
            value=status,
            inline=False,
        )
    return embed


async def _record_update_failure(
    cog,
    guild: discord.Guild,
    message_id: typing.Any,
    error: BaseException,
) -> None:
    await cog._record_operational_failure(
        guild.id,
        "joinwatch_alert_update",
        f"Could not update joinwatch alert {message_id}: {error}",
        error=error,
    )


async def _update_current_incident(
    cog,
    guild: discord.Guild,
    incident: typing.Mapping[str, typing.Any],
    embed: discord.Embed,
) -> None:
    if incident.get("alert_updates_disabled"):
        return
    channel_id = incident.get("alert_channel_id")
    message_id = incident.get("alert_message_id")
    if channel_id is None or message_id is None:
        return
    channel = cog._get_text_channel_or_thread(guild, channel_id)
    if channel is None:
        member_id = incident.get("member_id")
        if member_id is not None:
            await joinwatch_state.disable_alert_updates(
                cog,
                guild,
                int(member_id),
                incident=incident if isinstance(incident, dict) else None,
            )
        await _record_update_failure(
            cog,
            guild,
            message_id,
            LookupError(f"Alert channel {channel_id} is unavailable"),
        )
        return
    try:
        message = channel.get_partial_message(int(message_id))
        await message.edit(embed=embed)
    except (discord.NotFound, discord.Forbidden, TypeError, ValueError) as exc:
        member_id = incident.get("member_id")
        if member_id is not None:
            await joinwatch_state.disable_alert_updates(
                cog,
                guild,
                int(member_id),
                incident=incident if isinstance(incident, dict) else None,
            )
        await _record_update_failure(cog, guild, message_id, exc)
    except discord.HTTPException as exc:
        log.debug(
            "Failed to edit joinwatch alert message %s in guild %s",
            message_id,
            guild.id,
        )
        await _record_update_failure(cog, guild, message_id, exc)


async def _update_legacy_incident(
    cog,
    guild: discord.Guild,
    incident: typing.Mapping[str, typing.Any],
    status: str,
) -> None:
    channel_id = incident.get("alert_channel_id")
    message_id = incident.get("alert_message_id")
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
        await _record_update_failure(cog, guild, message_id, exc)
        return
    if not message.embeds:
        return
    embed = discord.Embed.from_dict(message.embeds[0].to_dict())
    field_name = _("Auto-role:")
    old_field_name = _("Auto role:")
    for index, field in enumerate(embed.fields):
        if field.name in (field_name, old_field_name):
            embed.set_field_at(
                index,
                name=field_name,
                value=status,
                inline=field.inline,
            )
            break
    else:
        embed.add_field(name=field_name, value=status, inline=False)
    try:
        await message.edit(embed=embed)
    except discord.HTTPException as exc:
        log.debug(
            "Failed to edit joinwatch alert message %s in guild %s",
            message_id,
            guild.id,
        )
        await _record_update_failure(cog, guild, message_id, exc)


async def publish_joinwatch_incident(
    cog,
    guild: discord.Guild,
    incident: dict[str, typing.Any],
    status: str | None = None,
    *,
    destination: discord.TextChannel | discord.Thread | None = None,
    member: discord.Member | None = None,
) -> None:
    """Create or update one current incident, with bounded legacy support."""
    if incident.get("member_id") is None:
        if status is not None:
            await _update_legacy_incident(cog, guild, incident, status)
        return

    embed = _incident_embed(member, incident, status)
    if incident.get("alert_message_id") is not None:
        await _update_current_incident(cog, guild, incident, embed)
        return
    if destination is None:
        return
    try:
        alert_message = await destination.send(embed=embed)
        await joinwatch_state.store_alert_reference(
            cog,
            guild,
            int(typing.cast(typing.Any, incident["member_id"])),
            alert_message.channel.id,
            alert_message.id,
        )
    except discord.HTTPException as exc:
        member_id = incident.get("member_id")
        log.debug(
            "Failed to send joinwatch alert for user %s in guild %s",
            member_id,
            guild.id,
        )
        await cog._record_operational_failure(
            guild.id,
            "joinwatch_alert_publish",
            f"Could not publish joinwatch alert for user {member_id}: {exc}",
            terminal=True,
            error=exc,
        )


async def publish_legacy_timer_result(
    cog,
    guild: discord.Guild,
    destination: discord.TextChannel | discord.Thread | None,
    *,
    member_id: int,
    title: str,
    description: str,
    action: str | None,
    failed: bool,
    occurred_at: datetime,
) -> None:
    """Preserve the additional result message used by pre-incident timers."""
    if destination is None:
        return
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.dark_red() if failed else discord.Color.orange(),
        timestamp=occurred_at,
    )
    embed.add_field(
        name=_("Action:"),
        value=action,
        inline=False,
    )
    try:
        await destination.send(embed=embed)
    except discord.HTTPException as exc:
        log.debug(
            "Failed to send legacy joinwatch timer result for user %s in guild %s",
            member_id,
            guild.id,
        )
        await cog._record_operational_failure(
            guild.id,
            "joinwatch_timer_alert",
            f"Could not publish timer result for user {member_id}: {exc}",
            error=exc,
        )
