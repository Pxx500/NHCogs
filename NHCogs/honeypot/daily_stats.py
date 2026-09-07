"""Daily aggregation publication owned by Honeypot."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

import discord

from .detection_cases import DailyStatsSnapshot
from .settings import GuildSettings

log = logging.getLogger("red.Honeypot")

PUBLICATION_TIME_UTC = time(hour=0, minute=5, tzinfo=timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def publication_is_due(now: datetime) -> bool:
    current = _utc(now)
    return current.timetz() >= PUBLICATION_TIME_UTC


def completed_report_date(now: datetime) -> date:
    return _utc(now).date() - timedelta(days=1)


def next_publication_at(now: datetime) -> datetime:
    current = _utc(now)
    target = datetime.combine(current.date(), PUBLICATION_TIME_UTC)
    if current >= target:
        target += timedelta(days=1)
    return target


def build_embed(report_date: date, stats: DailyStatsSnapshot) -> discord.Embed:
    embed = discord.Embed(
        title=f"Honeypot daily summary - {report_date.isoformat()} UTC",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="Honeypot",
        value=(
            f"Detections: {stats.detections}\n"
            f"Automated bans: {stats.automated_bans}\n"
            f"Manual bans: {stats.manual_bans}"
        ),
        inline=False,
    )
    embed.add_field(
        name="JoinWatch",
        value=f"Shadowbans: {stats.shadowbans}\nBans: {stats.joinwatch_bans}",
        inline=False,
    )
    return embed


async def _publish_guild(
    cog,
    guild: discord.Guild,
    report_date: date,
    published_at: datetime,
) -> None:
    raw_config = await cog.config.guild(guild).all()
    settings = GuildSettings.from_mapping(raw_config)
    if settings.daily_stats_channel is None:
        return
    channel = cog._get_text_channel_or_thread(guild, settings.daily_stats_channel)
    if channel is None:
        raise RuntimeError("configured daily statistics channel is unavailable")
    stats = await asyncio.to_thread(
        cog._case_store.get_daily_stats,
        guild.id,
        report_date,
    )
    if not stats.observed:
        return
    if stats.publication_message_id is not None:
        return
    message = await channel.send(
        embed=build_embed(report_date, stats),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await asyncio.to_thread(
        cog._case_store.record_daily_stats_publication,
        guild.id,
        report_date,
        published_at,
        channel_id=channel.id,
        message_id=message.id,
    )


async def publish_completed_day(cog, now: datetime) -> None:
    if not publication_is_due(now):
        return
    published_at = _utc(now)
    report_date = completed_report_date(published_at)
    for guild in cog.bot.guilds:
        try:
            await _publish_guild(cog, guild, report_date, published_at)
        except Exception as error:
            log.exception(
                "Failed to publish daily statistics for guild %s",
                guild.id,
            )
            try:
                await cog._record_operational_failure(
                    guild.id,
                    "daily_stats_publish",
                    f"Could not publish daily statistics for {report_date.isoformat()}: {error}",
                )
            except Exception:
                log.exception(
                    "Failed to record daily statistics publication error for guild %s",
                    guild.id,
                )


async def observe_current_day(cog, now: datetime) -> None:
    current_date = _utc(now).date()
    for guild in cog.bot.guilds:
        try:
            await asyncio.to_thread(
                cog._case_store.observe_daily_stats_day,
                guild.id,
                current_date,
            )
        except Exception:
            log.exception(
                "Failed to observe daily statistics for guild %s",
                guild.id,
            )


async def publisher_loop(cog) -> None:
    await cog.bot.wait_until_red_ready()
    while True:
        now = datetime.now(timezone.utc)
        await observe_current_day(cog, now)
        await publish_completed_day(cog, now)
        target = next_publication_at(now)
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))
