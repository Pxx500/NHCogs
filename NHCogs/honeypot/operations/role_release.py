"""Detection-case role release operation handler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

import discord

from ..detection_cases import OPERATION_RESULT_OWNERSHIP_TRANSFERRED
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot


async def role_release_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    operation = context.operation
    guild = cog.bot.get_guild(context.snapshot.case.guild_id)
    if guild is None:
        raise RuntimeError("detection case guild is unavailable")
    role_id = int(operation.idempotency_key.rsplit(":", 1)[1])
    owned_role_ids = await asyncio.to_thread(
        cog._case_store.owned_role_ids, operation.case_id
    )
    if role_id not in owned_role_ids:
        return OperationOutcome(result=OPERATION_RESULT_OWNERSHIP_TRANSFERRED)
    started = await asyncio.to_thread(
        cog._case_store.start_role_release_effect,
        operation.operation_id,
        cast(str, operation.claim_token),
        operation.case_id,
        role_id,
        datetime.now(timezone.utc),
    )
    if not started:
        owner_case_id = await asyncio.to_thread(
            cog._case_store.role_owner_case,
            context.snapshot.case.guild_id,
            context.snapshot.case.user_id,
            role_id,
        )
        if owner_case_id != operation.case_id:
            return OperationOutcome(
                result=OPERATION_RESULT_OWNERSHIP_TRANSFERRED
            )
        raise RuntimeError("detection operation lease was lost")
    role = guild.get_role(role_id)
    member = guild.get_member(context.snapshot.case.user_id) if role is not None else None
    if role is not None and member is None:
        fetch_member = getattr(guild, "fetch_member", None)
        if not callable(fetch_member):
            raise RuntimeError("detection case member lookup is unavailable")
        try:
            member = await fetch_member(context.snapshot.case.user_id)
        except discord.NotFound:
            member = None
        except discord.HTTPException as error:
            raise RuntimeError("detection case member lookup failed") from error
    if member is not None and role in member.roles:
        removed = await cog._remove_review_mute_role(
            member,
            role,
            "Detection case resolved; removing pending mute.",
        )
        if not removed:
            raise RuntimeError("failed to release detection case role")
    await asyncio.to_thread(
        cog._case_store.release_role_ownership,
        operation.case_id,
        role_id,
    )
    return OperationOutcome()
