"""Review publication operation handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..settings import GuildSettings
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot


async def review_publish_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    raw_config = await cog.config.guild_from_id(
        context.snapshot.case.guild_id
    ).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    guild = cog.bot.get_guild(context.snapshot.case.guild_id)
    logs_channel = context.publication_channel or (
        cog._get_text_channel_or_thread(guild, guild_settings.logs_channel)
        if guild is not None
        else None
    )
    await cog._publish_detection_case(
        context.operation.case_id,
        guild_settings.review_channel,
        logs_channel,
        message_sequence=context.operation.message_sequence,
    )
    return OperationOutcome()
