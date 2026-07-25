"""Source-message delete operation handler."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord

from .. import detection_runtime
from ..detection_cases import DeleteStatus
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot


async def source_delete_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    operation = context.operation
    guild = cog.bot.get_guild(context.snapshot.case.guild_id)
    if guild is None:
        raise RuntimeError("detection case guild is unavailable")
    _, case_id, channel_id, message_id = operation.idempotency_key.split(":")
    if case_id != operation.case_id:
        raise RuntimeError("source delete operation case identity does not match")
    if operation.message_sequence is None:
        raise RuntimeError("source delete operation has no message identity")
    channel = await cog._fetch_message_channel(guild, int(channel_id))
    if channel is None:
        result = detection_runtime.DeleteResult(
            DeleteStatus.ALREADY_GONE, 1, None
        )
    else:
        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            result = detection_runtime.DeleteResult(
                DeleteStatus.ALREADY_GONE, 1, None
            )
        else:
            result = await detection_runtime.delete_message(message)
    result_value = result.status.value
    # Shared settlement learns partial results from returned outcomes, so keep
    # the observed status for every ordinary failure after it becomes available.
    try:
        if result.status not in {
            DeleteStatus.DELETED,
            DeleteStatus.ALREADY_GONE,
        }:
            raise RuntimeError(result.error or result_value)
        completed_delete = await asyncio.to_thread(
            cog._case_store.complete_message_delete_retry,
            operation.case_id,
            operation.message_sequence,
            result.status,
        )
        if completed_delete and result.status is DeleteStatus.DELETED:
            await cog._increment_stat(guild, "purged_messages")
            source_signals = tuple(
                item.signal
                for item in context.snapshot.signals
                if item.message_sequence == operation.message_sequence
            )
            if any(
                signal.detector == "forward_purge"
                for signal in source_signals
            ):
                await cog._increment_stat(guild, "forward_purge_deletes")
    except Exception as error:
        return OperationOutcome(
            result=result_value,
            error=error,
        )
    return OperationOutcome(result=result_value)
