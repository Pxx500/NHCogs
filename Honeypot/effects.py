"""Shared policy and result contracts for moderation effects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import discord

from .settings import GuildSettings


class EffectStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    PLANNED = "planned"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True)
class ModerationEffectResult:
    label: str | None
    failed_message: str | None
    status: EffectStatus
    modlog_failed: bool = False


async def punitive_effect_allowed(cog, guild: discord.Guild) -> bool:
    raw = await cog.config.guild(guild).all()
    if not GuildSettings.from_mapping(raw).dry_run:
        return True
    await cog._increment_stat(guild, "dry_run_actions")
    return False
