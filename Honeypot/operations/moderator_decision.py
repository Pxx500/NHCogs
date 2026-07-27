"""Moderator-owned detection-case decision operations."""

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
)
from ..settings import GuildSettings
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot

log = logging.getLogger("red.Honeypot")


async def apply_moderator_ignore(
    cog: Honeypot,
    case_id: str,
    actor_id: int,
    now: datetime,
) -> None:
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    operation = await asyncio.to_thread(
        cog._case_store.record_moderator_ignore,
        case_id,
        actor_id,
        now,
    )
    if operation is None:
        raise ValueError("detection case is already resolving or resolved")
    if snapshot is not None:
        cog._deactivate_forward_purge(
            snapshot.case.guild_id, snapshot.case.user_id
        )
        guild = cog.bot.get_guild(snapshot.case.guild_id)
        if guild is not None:
            await cog._increment_stat(guild, "ignored")
    await cog._release_detection_case_roles(case_id, now)
    await cog._finish_case_review_if_ready(case_id, actor_id)
    await cog._case_review_rerender_if_open(case_id)


async def _ban_already_applied(
    cog: Honeypot, guild: discord.Guild, user_id: int
) -> bool:
    """Report whether the guild already carries the ban this operation would place."""
    target = guild.get_member(user_id)
    if target is None:
        target = await cog._get_user_or_object(user_id)
    try:
        await guild.fetch_ban(target)
    except discord.NotFound:
        return False
    else:
        return True


async def _decision_target(
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


async def _execute_moderator_action(
    cog: Honeypot,
    context: OperationContext,
    guild: discord.Guild,
    member: discord.Member | discord.User | discord.Object,
    *,
    action: ActionIntent,
    guild_settings: GuildSettings,
) -> None:
    """Take the moderator's lease and carry the decision out on Discord."""
    operation = context.operation
    moderator = guild.get_member(operation.actor_id)
    if moderator is None:
        moderator = await cog._get_user_or_object(operation.actor_id)
    effect_started_at = datetime.now(timezone.utc)
    started = await asyncio.to_thread(
        cog._case_store.start_operation_effect,
        operation.operation_id,
        operation.claim_token,
        effect_started_at,
    )
    if not started:
        raise RuntimeError("moderator action operation lease was lost")
    result = await cog._execute_action(
        guild,
        member,
        effect_started_at,
        guild_settings,
        reason=f"Honeypot review: {action.value.title()}",
        action=action.value,
        moderator=moderator,
    )
    if result.failed_message is not None:
        raise RuntimeError(result.failed_message)


async def _apply_moderator_decision(
    cog: Honeypot,
    context: OperationContext,
    *,
    action: ActionIntent,
    effect_started: bool,
    guild_settings: GuildSettings,
) -> str:
    """Apply a live (non dry-run) moderator decision and report its result."""
    snapshot = context.snapshot
    guild = cog.bot.get_guild(snapshot.case.guild_id)
    if guild is None:
        raise RuntimeError("detection case guild is unavailable")
    if (
        effect_started
        and action is ActionIntent.BAN
        and await _ban_already_applied(cog, guild, snapshot.case.user_id)
    ):
        return action.value
    member = await _decision_target(cog, guild, snapshot.case.user_id, action)
    if member is None:
        return OPERATION_RESULT_KICK_MISSING
    await _execute_moderator_action(
        cog,
        context,
        guild,
        member,
        action=action,
        guild_settings=guild_settings,
    )
    return action.value


async def moderator_decision_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    operation = context.operation
    snapshot = context.snapshot
    action = ActionIntent(
        operation.operation_type.value.removeprefix("moderator_")
    )
    effect_started = await asyncio.to_thread(
        cog._case_store.operation_effect_started, operation.operation_id
    )
    if effect_started and action is ActionIntent.KICK:
        guild = cog.bot.get_guild(snapshot.case.guild_id)
        member = guild.get_member(snapshot.case.user_id) if guild is not None else None
        await asyncio.to_thread(
            cog._case_store.mark_case_needs_attention,
            snapshot.case.case_id,
        )
        log.warning(
            "Detection case moderator kick outcome is unknown; operation will not "
            "be retried (case_id=%s, operation_id=%s, member_joined_at=%s)",
            snapshot.case.case_id,
            operation.operation_id,
            getattr(member, "joined_at", None),
        )
        return OperationOutcome(result=OPERATION_RESULT_KICK_OUTCOME_UNKNOWN)
    raw_config = await cog.config.guild_from_id(snapshot.case.guild_id).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    if guild_settings.dry_run:
        operation_result = f"{PLANNED_PREFIX}{action.value}"
    else:
        operation_result = await _apply_moderator_decision(
            cog,
            context,
            action=action,
            effect_started=effect_started,
            guild_settings=guild_settings,
        )
    return OperationOutcome(result=operation_result)
