"""Automatic detection-case moderation operation handler."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from ..detection_cases import (
    OPERATION_RESULT_KICK_MISSING,
    OPERATION_RESULT_KICK_OUTCOME_UNKNOWN,
    PLANNED_PREFIX,
    ActionIntent,
    effective_action,
)
from ..settings import GuildSettings
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot

log = logging.getLogger("red.Honeypot")


async def _moderation_target(
    cog: Honeypot,
    guild: discord.Guild,
    user_id: int,
    action: ActionIntent,
) -> discord.Member | discord.User | discord.Object | None:
    """Resolve who to action, or None when a kick target already left the guild."""
    member = guild.get_member(user_id)
    if member is None and action is ActionIntent.BAN:
        member = await cog._get_user_or_object(user_id)
    if member is None and action is ActionIntent.KICK:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
    if member is None:
        raise RuntimeError("detection case member is unavailable")
    return member


async def moderation_action_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    source = next(
        (
            message
            for message in context.snapshot.messages
            if message.sequence == context.operation.message_sequence
        ),
        None,
    )
    if source is None:
        raise RuntimeError("detection case source message is unavailable")
    signals = tuple(
        item.signal
        for item in context.snapshot.signals
        if item.message_sequence == source.sequence
    )
    action = effective_action(signals)
    if action not in (ActionIntent.KICK, ActionIntent.BAN):
        raise RuntimeError(
            "detection case moderation action is no longer applicable"
        )
    effect_started = await asyncio.to_thread(
        cog._case_store.operation_effect_started,
        context.operation.operation_id,
    )
    if effect_started and action is ActionIntent.KICK:
        guild = cog.bot.get_guild(context.snapshot.case.guild_id)
        member = (
            guild.get_member(context.snapshot.case.user_id)
            if guild is not None
            else None
        )
        await asyncio.to_thread(
            cog._case_store.mark_case_needs_attention,
            context.snapshot.case.case_id,
        )
        log.warning(
            "Detection case kick outcome is unknown; operation will not be retried "
            "(case_id=%s, operation_id=%s, member_joined_at=%s)",
            context.snapshot.case.case_id,
            context.operation.operation_id,
            getattr(member, "joined_at", None),
        )
        return OperationOutcome(result=OPERATION_RESULT_KICK_OUTCOME_UNKNOWN)
    raw_config = await cog.config.guild_from_id(
        context.snapshot.case.guild_id
    ).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    if guild_settings.dry_run:
        return OperationOutcome(result=f"{PLANNED_PREFIX}{action.value}")
    guild = cog.bot.get_guild(context.snapshot.case.guild_id)
    if guild is None:
        raise RuntimeError("detection case guild is unavailable")
    if effect_started and action is ActionIntent.BAN:
        target = guild.get_member(context.snapshot.case.user_id)
        if target is None:
            target = await cog._get_user_or_object(context.snapshot.case.user_id)
        try:
            await guild.fetch_ban(target)
        except discord.NotFound:
            pass
        else:
            return OperationOutcome(result=action.value)
    member = await _moderation_target(
        cog, guild, context.snapshot.case.user_id, action
    )
    if member is None:
        return OperationOutcome(result=OPERATION_RESULT_KICK_MISSING)
    public_reason = cog._public_moderation_reason(signals, action)
    started = await asyncio.to_thread(
        cog._case_store.start_operation_effect,
        context.operation.operation_id,
        context.operation.claim_token,
        datetime.now(timezone.utc),
    )
    if not started:
        raise RuntimeError("moderation action operation lease was lost")
    result = await cog._execute_action(
        guild,
        member,
        source.created_at,
        guild_settings,
        reason=public_reason,
        action=action.value,
    )
    if result.failed_message is not None:
        raise RuntimeError(result.failed_message)
    return OperationOutcome(result=action.value)
