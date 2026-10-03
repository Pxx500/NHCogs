"""Moderator CAPTCHA setup, with effects delegated to JoinWatch."""

from __future__ import annotations

import discord
from redbot.core import commands

from .captcha_views import PANEL_TEXT, VERIFY_CUSTOM_ID, CaptchaPracticeView, VerifyPanelView
from .settings import GuildSettings


def _private(cog, ctx) -> bool:
    return cog._channel_is_private(ctx.guild, ctx.channel)


async def config_captcha(cog, ctx) -> None:
    configured = GuildSettings.from_mapping(await cog.config.guild(ctx.guild).all())
    await cog._send_config_dump(ctx, "CAPTCHA", [
        ("Configuration", "\n".join((
            f"New joins: {str(configured.joinwatch_captcha_enabled).lower()}",
            f"Role (JoinWatch): {cog._format_role_setting(ctx.guild, configured.joinwatch_auto_role_id)}",
            f"Channel: {cog._format_channel_setting(ctx.guild, configured.captcha_channel)}",
            f"Moderator log: {cog._format_channel_setting(ctx.guild, configured.captcha_log_channel)}",
            f"Panel: {'Published' if configured.captcha_panel_message_id else 'Not configured'}",
            "Two full attempts, two questions per attempt. Existing deadlines still apply.",
        )))
    ])


async def check_configuration(cog, guild) -> None:
    try:
        await cog._joinwatch_verification.check_configuration(guild)
    except ValueError as error:
        raise commands.UserFeedbackCheckFailure(str(error)) from error


async def panel(cog, ctx) -> None:
    if not _private(cog, ctx):
        await ctx.send("Run this command in a private moderator channel")
        return
    config = cog.config.guild(ctx.guild)
    channel_id = await config.captcha_channel()
    channel = cog._get_text_channel_or_thread(ctx.guild, channel_id)
    if channel is None:
        raise commands.UserFeedbackCheckFailure("Configure a CAPTCHA channel first")
    missing = cog._missing_channel_permissions(ctx.guild, channel, send_messages=True, read_history=True)
    if missing:
        raise commands.UserFeedbackCheckFailure(missing)
    old_channel_id = await config.captcha_panel_channel_id()
    old_message_id = await config.captcha_panel_message_id()
    view = VerifyPanelView(cog)
    if old_message_id:
        old_channel = cog._get_text_channel_or_thread(ctx.guild, old_channel_id)
        try:
            old_message = await old_channel.fetch_message(old_message_id) if old_channel else None
        except discord.NotFound:
            old_message = None
        if old_message is not None:
            if old_channel_id != channel_id:
                raise commands.UserFeedbackCheckFailure("The existing panel is in another channel. Move it deliberately before publishing another panel")
            await old_message.edit(content=PANEL_TEXT, view=view, allowed_mentions=discord.AllowedMentions.none())
            await ctx.send("The existing CAPTCHA panel is ready", allowed_mentions=discord.AllowedMentions.none())
            return
    message = await channel.send(PANEL_TEXT, view=view, allowed_mentions=discord.AllowedMentions.none())
    await config.captcha_panel_channel_id.set(channel.id)
    await config.captcha_panel_message_id.set(message.id)
    await ctx.send("CAPTCHA panel published", allowed_mentions=discord.AllowedMentions.none())


async def restore_panels(cog) -> None:
    # Message-bound handlers win over the global handler, even after a cog reload.
    for view in cog.bot.persistent_views:
        if any(getattr(item, "custom_id", None) == VERIFY_CUSTOM_ID for item in view.children):
            view.stop()
    # A global persistent handler also restores invitation buttons after restart.
    cog.bot.add_view(VerifyPanelView(cog))


async def test(cog, ctx, member) -> None:
    if not _private(cog, ctx):
        await ctx.send("Run this command in a private moderator channel")
        return
    if member.bot:
        await ctx.send("Bots can't take the CAPTCHA test", allowed_mentions=discord.AllowedMentions.none())
        return
    if await cog._is_protected_member(member):
        await ctx.send(
            "This member is protected from restrictions, but can still try the CAPTCHA",
            view=CaptchaPracticeView(cog, ctx.guild.id, member.id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    result = await cog._joinwatch_verification.enroll_test(member, moderator_id=ctx.author.id)
    descriptions = {
        "active": "This member already has an active check. It hasn't been reset",
        "pending": "This member has a pending role assignment. It hasn't been reset",
        "ambiguous": "This member already has a restriction outside this check. It hasn't been changed",
        "unavailable": "The test can't start. Check the configured JoinWatch role and the bot's role permissions",
        "protected": "This member is now protected from restrictions. Run the command again to try the CAPTCHA without role changes",
        "preparing": "Test check registered. The member can use Verify while the questions prepare",
        "enrolled": "Test check registered. The member can use Verify",
        "question": "Test check ready. The member can use Verify",
        "complete": "Test check ready. The member can use Verify",
        "dry_run": "Test is in dry-run mode. No role changes were made",
    }
    await ctx.send(descriptions.get(result.status, "The test couldn't be started"), allowed_mentions=discord.AllowedMentions.none())


async def status(cog, ctx, member) -> None:
    if not _private(cog, ctx):
        await ctx.send("Run this command in a private moderator channel")
        return
    result = await cog._joinwatch_verification.inspect(member)
    if not result:
        await ctx.send("No active check for this member", allowed_mentions=discord.AllowedMentions.none())
        return
    rows = [f"{key.replace('_', ' ').capitalize()}: {value}" for key, value in result.items()]
    await cog._send_config_dump(ctx, "Member check", [("Current state", "\n".join(rows))])


async def resolve(cog, ctx, member, reason: str) -> None:
    if not _private(cog, ctx):
        await ctx.send("Run this command in a private moderator channel")
        return
    result = await cog._joinwatch_verification.release(member, outcome="manual", moderator_id=ctx.author.id, reason=reason)
    await ctx.send(
        "The member's check is resolved" if result.status == "complete" else "The check is resolved, but an independent restriction remains" if result.status == "complete_restricted" else "The check couldn't be resolved. The existing restriction remains",
        allowed_mentions=discord.AllowedMentions.none(),
    )
