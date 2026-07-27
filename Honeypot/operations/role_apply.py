"""Detection-case role application operation handler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

import discord

from ..detection_cases import (
    MODERATION_SUPERSEDING_RESULTS,
    MODERATION_SUPERSEDING_TYPES,
    OPERATION_RESULT_AMBIGUOUS_ROLE_OWNERSHIP,
    OPERATION_RESULT_CASE_TERMINAL,
    OPERATION_RESULT_MEMBER_UNAVAILABLE,
    OPERATION_RESULT_PREEXISTING_ROLE,
    OPERATION_RESULT_ROLE_ALREADY_OWNED,
    OPERATION_RESULT_SUPERSEDED_BY_MODERATION,
    OPERATION_RESULT_TRANSFERRED_ROLE_OWNERSHIP,
    CaseSnapshot,
    CaseStatus,
    OperationStatus,
    OperationType,
)
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot


def _is_superseded_by_moderation(snapshot: CaseSnapshot) -> bool:
    return any(
        operation.operation_type in MODERATION_SUPERSEDING_TYPES
        and operation.status is OperationStatus.SUCCEEDED
        and operation.result in MODERATION_SUPERSEDING_RESULTS
        for operation in snapshot.operations
    )


async def _run_pending_role_release(
    cog: Honeypot, context: OperationContext, role_id: int
) -> None:
    """Drive the queued release of the role's previous detection-case owner."""
    terminal_snapshot = cast(
        CaseSnapshot,
        await asyncio.to_thread(
            cog._case_store.get_case, context.operation.case_id
        ),
    )
    release = next(
        (
            item
            for item in terminal_snapshot.operations
            if item.operation_type == OperationType.ROLE_RELEASE
            and item.idempotency_key
            == f"role-release:{context.operation.case_id}:{role_id}"
        ),
        None,
    )
    if release is not None:
        claimed_release = await asyncio.to_thread(
            cog._case_store.claim_operation,
            release.operation_id,
            datetime.now(timezone.utc),
        )
        if claimed_release is not None:
            await cog._execute_detection_case_operation(
                claimed_release, datetime.now(timezone.utc)
            )


async def _add_case_role(
    cog: Honeypot,
    context: OperationContext,
    *,
    member: discord.Member,
    role: discord.Role,
    role_id: int,
) -> OperationOutcome:
    """Add the review role to the member and record this case as its owner."""
    started = await asyncio.to_thread(
        cog._case_store.start_role_apply_effect,
        context.operation.operation_id,
        cast(str, context.operation.claim_token),
        datetime.now(timezone.utc),
    )
    if not started:
        raise RuntimeError("detection operation lease was lost")
    await member.add_roles(
        role, reason="Detection case pending moderator review."
    )
    result = None
    try:
        ownership_result = await asyncio.to_thread(
            cog._case_store.record_operation_role_ownership,
            context.operation.operation_id,
            cast(str, context.operation.claim_token),
            context.operation.case_id,
            context.snapshot.case.guild_id,
            context.snapshot.case.user_id,
            role_id=role_id,
            now=datetime.now(timezone.utc),
        )
        if ownership_result is None:
            result = OPERATION_RESULT_AMBIGUOUS_ROLE_OWNERSHIP
            await asyncio.to_thread(
                cog._case_store.mark_case_needs_attention,
                context.operation.case_id,
            )
        elif ownership_result == "release_required":
            await _run_pending_role_release(cog, context, role_id)
    except Exception as error:
        return OperationOutcome(
            result=result,
            role_was_added=True,
            error=error,
        )
    return OperationOutcome(result=result, role_was_added=True)


async def _mark_ambiguous_role_ownership(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    """Flag a case whose role was added by an attempt that never recorded ownership."""
    result = OPERATION_RESULT_AMBIGUOUS_ROLE_OWNERSHIP
    try:
        await asyncio.to_thread(
            cog._case_store.mark_case_needs_attention,
            context.operation.case_id,
        )
    except Exception as error:
        return OperationOutcome(result=result, error=error)
    return OperationOutcome(result=result)


async def _reconcile_preexisting_role(
    cog: Honeypot, context: OperationContext, role_id: int
) -> OperationOutcome:
    """Decide what a role the member already carries means for this case."""
    owner_case_id = await asyncio.to_thread(
        cog._case_store.role_owner_case,
        context.snapshot.case.guild_id,
        context.snapshot.case.user_id,
        role_id,
    )
    transferred = await asyncio.to_thread(
        cog._case_store.transfer_terminal_role_ownership,
        context.operation.operation_id,
        cast(str, context.operation.claim_token),
        context.operation.case_id,
        context.snapshot.case.guild_id,
        context.snapshot.case.user_id,
        role_id=role_id,
        now=datetime.now(timezone.utc),
    )
    if transferred:
        return OperationOutcome(result=OPERATION_RESULT_TRANSFERRED_ROLE_OWNERSHIP)
    if owner_case_id is not None and owner_case_id != context.operation.case_id:
        raise RuntimeError(
            "previous detection case role release is still in progress"
        )
    if owner_case_id == context.operation.case_id:
        return OperationOutcome(result=OPERATION_RESULT_ROLE_ALREADY_OWNED)
    return OperationOutcome(result=OPERATION_RESULT_PREEXISTING_ROLE)


async def role_apply_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    if _is_superseded_by_moderation(context.snapshot):
        return OperationOutcome(result=OPERATION_RESULT_SUPERSEDED_BY_MODERATION)
    if context.snapshot.case.status is not CaseStatus.PENDING:
        return OperationOutcome(result=OPERATION_RESULT_CASE_TERMINAL)
    guild = cog.bot.get_guild(context.snapshot.case.guild_id)
    if guild is None:
        raise RuntimeError("detection case guild is unavailable")
    role_id = int(context.operation.idempotency_key.rsplit(":", 1)[1])
    member = guild.get_member(context.snapshot.case.user_id)
    role = guild.get_role(role_id)
    if member is None:
        fetch_member = getattr(guild, "fetch_member", None)
        if not callable(fetch_member):
            raise RuntimeError("detection case member lookup is unavailable")
        try:
            member = await fetch_member(context.snapshot.case.user_id)
        except discord.NotFound:
            return OperationOutcome(result=OPERATION_RESULT_MEMBER_UNAVAILABLE)
        except discord.HTTPException as error:
            raise RuntimeError("detection case member lookup failed") from error
    if role is None:
        raise RuntimeError("detection case role is unavailable")
    effect_started = await asyncio.to_thread(
        cog._case_store.operation_effect_started,
        context.operation.operation_id,
    )
    if role not in member.roles:
        return await _add_case_role(
            cog, context, member=member, role=role, role_id=role_id
        )
    if effect_started:
        return await _mark_ambiguous_role_ownership(cog, context)
    return await _reconcile_preexisting_role(cog, context, role_id)
