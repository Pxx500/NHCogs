"""Channel-scoped Discord GIF embed interception for Honeypot."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import Any

import discord
from redbot.core import commands
from redbot.core.i18n import Translator

from . import settings
from .settings import GuildSettings

SEEN_MESSAGE_LIMIT = 4096
MAX_SECONDARY_MESSAGE_LENGTH = 1900
_ = Translator("Honeypot", __file__)


def has_gifv_embed(embeds: Iterable[Any]) -> bool:
    """Return whether Discord classified any embed as an animated GIF."""

    return any(
        (embed.get("type") if isinstance(embed, Mapping) else getattr(embed, "type", None))
        == "gifv"
        for embed in embeds
    )


def channel_scope_id(channel: Any) -> int:
    """Return the configured parent-channel scope for a channel or thread."""

    return getattr(channel, "parent_id", None) or channel.id


def render_icbm_frame(author_mention: str, *, track_lines: int) -> str:
    """Render one ICBM frame with all remaining track above the rocket."""

    header = f"ICBM detected targeting {author_mention}'s GIF!"
    return "\n".join((header, *("│" for _ in range(track_lines)), "🚀"))


async def _record_http_failure(cog: Any, message: Any, action: str, error: Exception) -> None:
    await cog._record_operational_failure(
        message.guild.id,
        "gif_detector",
        f"{action}: {type(error).__name__}: {error}",
    )


async def _delete_message(cog: Any, message: Any, action: str) -> None:
    try:
        await message.delete()
    except discord.NotFound:
        return
    except discord.HTTPException as error:
        await _record_http_failure(cog, message, action, error)


async def _run_secondary(cog: Any, message: Any, text: str) -> None:
    await _delete_message(cog, message, "Could not delete GIF message")
    warning = None
    try:
        warning = await message.channel.send(
            f"{message.author.mention} {text}",
            allowed_mentions=_author_mentions(message),
        )
    except discord.HTTPException as error:
        await _record_http_failure(cog, message, "Could not send GIF warning", error)
    if warning is None:
        return
    await asyncio.sleep(3)
    await _delete_message(cog, warning, "Could not delete GIF warning")


def _author_mentions(message: Any) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[message.author],
        replied_user=False,
    )


async def _run_animated(cog: Any, message: Any) -> None:
    guild_id = message.guild.id
    warning = None
    try:
        try:
            warning = await message.channel.send(
                render_icbm_frame(message.author.mention, track_lines=10),
                allowed_mentions=_author_mentions(message),
            )
        except discord.HTTPException as error:
            await _record_http_failure(
                cog, message, "Could not send animated GIF warning", error
            )

        if warning is None:
            await asyncio.sleep(10)
        else:
            for track_lines in range(9, 0, -1):
                await asyncio.sleep(1)
                try:
                    await warning.edit(
                        content=render_icbm_frame(
                            message.author.mention,
                            track_lines=track_lines,
                        ),
                        allowed_mentions=_author_mentions(message),
                    )
                except discord.NotFound:
                    warning = None
                    break
                except discord.HTTPException as error:
                    await _record_http_failure(
                        cog, message, "Could not edit animated GIF warning", error
                    )
            if warning is None:
                await asyncio.sleep(track_lines)
            else:
                await asyncio.sleep(1)

        await _delete_message(cog, message, "Could not delete GIF message")
        if warning is not None:
            await _delete_message(cog, warning, "Could not delete animated GIF warning")
    finally:
        cog._gif_detector_animated_guilds.discard(guild_id)


def _remember_message(cog: Any, key: tuple[int, int]) -> bool:
    if key in cog._gif_detector_seen_messages:
        return False
    cog._gif_detector_seen_messages[key] = None
    if len(cog._gif_detector_seen_messages) > SEEN_MESSAGE_LIMIT:
        oldest = next(iter(cog._gif_detector_seen_messages))
        del cog._gif_detector_seen_messages[oldest]
    return True


def _spawn(cog: Any, coroutine: Any) -> None:
    task = asyncio.create_task(coroutine)
    cog._gif_detector_tasks.add(task)

    def settled(done: asyncio.Task) -> None:
        cog._gif_detector_tasks.discard(done)
        cog._observe_background_task(done, "GIF detector task")

    task.add_done_callback(settled)


async def _admit_message(cog: Any, message: Any) -> None:
    guild = message.guild
    if guild is None or message.author.bot or message.webhook_id is not None:
        return
    if await cog.bot.cog_disabled_in_guild(cog, guild):
        return
    configured = GuildSettings.from_mapping(await cog.config.guild(guild).all())
    if not configured.gif_detector_enabled:
        return
    if channel_scope_id(message.channel) not in configured.gif_detector_channels:
        return
    if await cog._is_protected_member(message.author, guild):
        return
    if not _remember_message(cog, (guild.id, message.id)):
        return
    if (
        configured.gif_detector_animation_enabled
        and guild.id not in cog._gif_detector_animated_guilds
    ):
        cog._gif_detector_animated_guilds.add(guild.id)
        _spawn(cog, _run_animated(cog, message))
    else:
        _spawn(
            cog,
            _run_secondary(cog, message, configured.gif_detector_secondary_message),
        )


async def on_message(cog: Any, message: Any) -> None:
    if has_gifv_embed(getattr(message, "embeds", ())):
        await _admit_message(cog, message)


async def on_raw_message_edit(cog: Any, payload: Any) -> None:
    raw_embeds = payload.data.get("embeds")
    cached_message = getattr(payload, "cached_message", None)
    if cached_message is None or raw_embeds is None:
        return
    if has_gifv_embed(raw_embeds):
        await _admit_message(cog, cached_message)


def _format_channels(cog: Any, guild: Any, channel_ids: list[int]) -> str:
    if not channel_ids:
        return _("Not configured")
    labels = []
    for channel_id in channel_ids[:20]:
        channel = cog._get_text_channel_or_thread(guild, channel_id)
        labels.append(channel.mention if channel is not None else _("Unknown channel"))
    remaining = len(channel_ids) - len(labels)
    if remaining:
        labels.append(_("… and {count} more").format(count=remaining))
    return "\n".join(labels)


async def config_gif_detector(cog: Any, ctx: commands.Context) -> None:
    configured = GuildSettings.from_mapping(await cog.config.guild(ctx.guild).all())
    await cog._send_config_dump(
        ctx,
        _("GIF detector config"),
        [
            (_("Enabled"), cog._format_bool_setting(configured.gif_detector_enabled)),
            (
                _("Animation"),
                cog._format_bool_setting(configured.gif_detector_animation_enabled),
            ),
            (
                _("Channels"),
                _format_channels(cog, ctx.guild, configured.gif_detector_channels),
            ),
            (_("Secondary message"), configured.gif_detector_secondary_message),
        ],
    )


async def gif_detector_toggle(cog: Any, ctx: commands.Context, value: bool) -> None:
    await cog.config.guild(ctx.guild).gif_detector_enabled.set(value)
    await ctx.send(_("✅ GIF detector enabled: {value}").format(value=str(value).lower()))


async def gif_detector_animation(cog: Any, ctx: commands.Context, value: bool) -> None:
    await cog.config.guild(ctx.guild).gif_detector_animation_enabled.set(value)
    await ctx.send(_("✅ GIF detector animation enabled: {value}").format(value=str(value).lower()))


def _target_scope(target: Any) -> tuple[int, Any]:
    scope_id = channel_scope_id(target)
    parent = getattr(target, "parent", None)
    return scope_id, parent or target


async def gif_detector_channel_add(
    cog: Any, ctx: commands.Context, channel: Any = None
) -> None:
    target = channel or ctx.channel
    scope_id, scope_channel = _target_scope(target)
    async with cog.config.guild(ctx.guild).gif_detector_channels() as channel_ids:
        if scope_id in channel_ids:
            raise commands.UserFeedbackCheckFailure(
                _("That channel is already monitored for GIFs.")
            )
        channel_ids.append(scope_id)
    await ctx.send(
        _("✅ GIF detector channel added: {channel}").format(
            channel=scope_channel.mention
        )
    )


async def gif_detector_channel_remove(
    cog: Any, ctx: commands.Context, channel: Any = None
) -> None:
    target = channel or ctx.channel
    scope_id, scope_channel = _target_scope(target)
    async with cog.config.guild(ctx.guild).gif_detector_channels() as channel_ids:
        if scope_id not in channel_ids:
            raise commands.UserFeedbackCheckFailure(
                _("That channel is not monitored for GIFs.")
            )
        channel_ids.remove(scope_id)
    await ctx.send(
        _("✅ GIF detector channel removed: {channel}").format(
            channel=scope_channel.mention
        )
    )


async def gif_detector_message_set(
    cog: Any, ctx: commands.Context, *, text: str
) -> None:
    text = text.strip()
    if not text or len(text) > MAX_SECONDARY_MESSAGE_LENGTH:
        raise commands.UserFeedbackCheckFailure(
            _("Message must contain between 1 and {limit} characters.").format(
                limit=MAX_SECONDARY_MESSAGE_LENGTH
            )
        )
    await cog.config.guild(ctx.guild).gif_detector_secondary_message.set(text)
    await ctx.send(_("✅ Secondary GIF warning updated."))


async def gif_detector_message_reset(cog: Any, ctx: commands.Context) -> None:
    default = str(settings.DEFAULTS["gif_detector_secondary_message"])
    await cog.config.guild(ctx.guild).gif_detector_secondary_message.set(default)
    await ctx.send(_("✅ Secondary GIF warning reset to `{message}`").format(message=default))
