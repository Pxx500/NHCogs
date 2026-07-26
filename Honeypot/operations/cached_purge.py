"""Cached-message purge operation handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import detection_runtime
from ..detection_cases import (
    OPERATION_RESULT_CHANNEL_UNAVAILABLE,
    OPERATION_RESULT_UNSUPPORTED_CHANNEL,
    DeleteStatus,
)
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot


async def cached_purge_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    guild = cog.bot.get_guild(context.snapshot.case.guild_id)
    if guild is None:
        raise RuntimeError("detection case guild is unavailable")
    _, case_id, channel_id, message_id = context.operation.idempotency_key.split(":")
    if case_id != context.operation.case_id:
        raise RuntimeError("cached purge operation case identity does not match")
    channel = cog._get_cached_message_channel(guild, int(channel_id))
    if channel is None:
        return OperationOutcome(
            result=OPERATION_RESULT_CHANNEL_UNAVAILABLE,
            error=RuntimeError("cached purge channel is unavailable"),
        )
    get_partial_message = getattr(channel, "get_partial_message", None)
    if not callable(get_partial_message):
        return OperationOutcome(
            result=OPERATION_RESULT_UNSUPPORTED_CHANNEL,
            error=RuntimeError("cached purge channel cannot resolve messages"),
        )
    result = await detection_runtime.delete_message(
        get_partial_message(int(message_id))
    )
    if result.status not in (
        DeleteStatus.DELETED,
        DeleteStatus.ALREADY_GONE,
    ):
        return OperationOutcome(
            result=result.status.value,
            error=RuntimeError(result.error or result.status.value),
        )
    return OperationOutcome(result=result.status.value)
