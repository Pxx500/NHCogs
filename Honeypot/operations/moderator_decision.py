"""Moderator-owned detection-case decision operations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from ..detection_cases import (
    OPERATION_RESULT_KICK_MISSING,
    PLANNED_PREFIX,
    ActionIntent,
)
from ..settings import GuildSettings
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot


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


async def moderator_decision_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    operation = context.operation
    snapshot = context.snapshot
    action = ActionIntent(
        operation.operation_type.value.removeprefix("moderator_")
    )
    operation_result = None
    raw_config = await cog.config.guild_from_id(snapshot.case.guild_id).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    if guild_settings.dry_run:
        operation_result = f"{PLANNED_PREFIX}{action.value}"
    else:
        guild = cog.bot.get_guild(snapshot.case.guild_id)
        if guild is None:
            raise RuntimeError("detection case guild is unavailable")
        effect_started = await asyncio.to_thread(
            cog._case_store.operation_effect_started, operation.operation_id
        )
        effect_confirmed = False
        if effect_started and action is ActionIntent.BAN:
            target = guild.get_member(snapshot.case.user_id)
            if target is None:
                target = await cog._get_user_or_object(snapshot.case.user_id)
            try:
                await guild.fetch_ban(target)
            except discord.NotFound:
                pass
            else:
                effect_confirmed = True
        if not effect_confirmed:
            member = guild.get_member(snapshot.case.user_id)
            if member is None and action is ActionIntent.BAN:
                member = await cog._get_user_or_object(snapshot.case.user_id)
            if member is None and action is ActionIntent.KICK:
                try:
                    member = await guild.fetch_member(snapshot.case.user_id)
                except discord.NotFound:
                    operation_result = OPERATION_RESULT_KICK_MISSING
                    effect_confirmed = True
        if not effect_confirmed:
            if member is None:
                raise RuntimeError("detection case member is unavailable")
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
            _, failed = await cog._execute_action(
                guild,
                member,
                effect_started_at,
                guild_settings,
                reason=f"Honeypot review: {action.value.title()}",
                action=action.value,
                moderator=moderator,
            )
            if failed is not None:
                raise RuntimeError(failed)
        if operation_result is None:
            operation_result = action.value
    return OperationOutcome(result=operation_result)
