"""Registry-backed Discord message deletion without history fetches."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from .message_registry import MESSAGE_REGISTRY_RETENTION_DAYS, MessageRecord

DELETE_REASON = "NHCogs managed cleanup"
BULK_DELETE_LIMIT = 100
HTTP_BAD_REQUEST = 400
MIN_CLEANUP_COUNT = 1
MAX_CLEANUP_COUNT = 1000
RANGE_QUERY_LIMIT = MAX_CLEANUP_COUNT + 1


@dataclass(frozen=True, slots=True)
class CleanupResult:
    requested: int
    selected: int
    deleted: int
    already_missing: int
    failed: int
    public_message: str


def _result(
    requested: int,
    selected: int,
    deleted: int,
    missing: int,
    failed: int,
) -> CleanupResult:
    message = (
        f"Cleanup complete: requested {requested}, selected {selected}, "
        f"deleted {deleted}, already missing {missing}, failed {failed}"
    )
    return CleanupResult(requested, selected, deleted, missing, failed, message)


def _can_delete(channel, guild) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    permissions = permissions_for(guild.me)
    return bool(
        getattr(permissions, "view_channel", False)
        and getattr(permissions, "manage_messages", False)
    )


def _is_stale_batch_error(error: discord.HTTPException) -> bool:
    return getattr(error, "status", None) == HTTP_BAD_REQUEST


async def _delete_individually(cog, messages) -> tuple[int, int, int]:
    deleted = missing = failed = 0
    for record, partial in messages:
        try:
            await partial.delete()
        except discord.NotFound:
            missing += 1
            await cog._message_registry.forget(record.message_id)
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
        else:
            deleted += 1
            await cog._message_registry.forget(record.message_id)
    return deleted, missing, failed


async def _delete_channel_records(
    cog,
    channel,
    records: Iterable[MessageRecord],
) -> tuple[int, int, int]:
    records = tuple(records)
    if not records:
        return 0, 0, 0
    if not _can_delete(channel, channel.guild):
        return 0, 0, len(records)
    get_partial_message = getattr(channel, "get_partial_message", None)
    delete_messages = getattr(channel, "delete_messages", None)
    if not callable(get_partial_message) or not callable(delete_messages):
        return 0, 0, len(records)

    totals = [0, 0, 0]
    for offset in range(0, len(records), BULK_DELETE_LIMIT):
        batch = records[offset : offset + BULK_DELETE_LIMIT]
        messages = tuple((record, get_partial_message(record.message_id)) for record in batch)
        try:
            await delete_messages(
                tuple(partial for _, partial in messages),
                reason=DELETE_REASON,
            )
        except discord.NotFound:
            await cog._message_registry.forget_many(tuple(record.message_id for record in batch))
            outcomes = (0, len(batch), 0)
        except discord.Forbidden:
            outcomes = (0, 0, len(batch))
        except discord.HTTPException as error:
            outcomes = (
                await _delete_individually(cog, messages)
                if _is_stale_batch_error(error)
                else (0, 0, len(batch))
            )
        else:
            await cog._message_registry.forget_many(tuple(record.message_id for record in batch))
            outcomes = (len(batch), 0, 0)
        for index, value in enumerate(outcomes):
            totals[index] += value
    return totals[0], totals[1], totals[2]


async def _delete_invocation(cog, message) -> None:
    try:
        await message.delete()
    except discord.NotFound:
        await cog._message_registry.forget(message.id)
    except (discord.Forbidden, discord.HTTPException):
        return
    else:
        await cog._message_registry.forget(message.id)


def _validate_count(count: int) -> None:
    if not MIN_CLEANUP_COUNT <= count <= MAX_CLEANUP_COUNT:
        raise ValueError("count must be between 1 and 1000")


def _retention_cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=MESSAGE_REGISTRY_RETENTION_DAYS)


async def _require_channel_boundary(cog, ctx, message_id: int) -> MessageRecord:
    if message_id >= ctx.message.id:
        raise ValueError("boundary must be older than the cleanup command")
    record = await cog._message_registry.get_in_channel(
        ctx.guild.id,
        ctx.channel.id,
        message_id,
        since_utc=_retention_cutoff(),
    )
    if record is None:
        raise ValueError("boundary is not retained in this channel")
    return record


def _reject_oversized_range(records: tuple[MessageRecord, ...]) -> None:
    if len(records) > MAX_CLEANUP_COUNT:
        raise ValueError("cleanup range contains more than 1000 messages")


async def _finish_channel_cleanup(
    cog,
    ctx,
    records: tuple[MessageRecord, ...],
    requested: int,
) -> CleanupResult:
    deleted, missing, failed = await _delete_channel_records(
        cog,
        ctx.channel,
        records,
    )
    await _delete_invocation(cog, ctx.message)
    return _result(requested, len(records), deleted, missing, failed)


async def cleanup_channel(cog, ctx, count: int) -> CleanupResult:
    _validate_count(count)
    records = await cog._message_registry.recent_in_channel(
        ctx.guild.id,
        ctx.channel.id,
        limit=count,
        before_message_id=ctx.message.id,
        since_utc=_retention_cutoff(),
    )
    return await _finish_channel_cleanup(cog, ctx, records, count)


async def cleanup_user(cog, ctx, user_id: int, count: int) -> CleanupResult:
    _validate_count(count)
    records = await cog._message_registry.recent_by_author(
        ctx.guild.id,
        user_id,
        limit=count,
        since_utc=_retention_cutoff(),
        before_message_id=ctx.message.id,
    )
    by_channel: dict[int, list[MessageRecord]] = defaultdict(list)
    for record in records:
        by_channel[record.channel_id].append(record)

    deleted = missing = failed = 0
    for channel_id, channel_records in by_channel.items():
        channel = ctx.guild.get_channel(channel_id) or ctx.guild.get_thread(channel_id)
        if channel is None:
            failed += len(channel_records)
            continue
        outcomes = await _delete_channel_records(cog, channel, channel_records)
        deleted += outcomes[0]
        missing += outcomes[1]
        failed += outcomes[2]
    await _delete_invocation(cog, ctx.message)
    return _result(count, len(records), deleted, missing, failed)


async def cleanup_after(
    cog,
    ctx,
    boundary_id: int,
    *,
    delete_pinned: bool = False,
) -> CleanupResult:
    await _require_channel_boundary(cog, ctx, boundary_id)
    records = await cog._message_registry.recent_in_channel(
        ctx.guild.id,
        ctx.channel.id,
        limit=RANGE_QUERY_LIMIT,
        before_message_id=ctx.message.id,
        after_message_id=boundary_id,
        since_utc=_retention_cutoff(),
        exclude_pinned=not delete_pinned,
    )
    _reject_oversized_range(records)
    return await _finish_channel_cleanup(cog, ctx, records, len(records))


async def cleanup_before(
    cog,
    ctx,
    boundary_id: int,
    count: int,
    *,
    delete_pinned: bool = False,
) -> CleanupResult:
    _validate_count(count)
    await _require_channel_boundary(cog, ctx, boundary_id)
    records = await cog._message_registry.recent_in_channel(
        ctx.guild.id,
        ctx.channel.id,
        limit=count,
        before_message_id=boundary_id,
        since_utc=_retention_cutoff(),
        exclude_pinned=not delete_pinned,
    )
    return await _finish_channel_cleanup(cog, ctx, records, count)


async def cleanup_between(
    cog,
    ctx,
    older_id: int,
    newer_id: int,
    *,
    delete_pinned: bool = False,
) -> CleanupResult:
    if older_id >= newer_id:
        raise ValueError("older boundary must precede newer boundary")
    await _require_channel_boundary(cog, ctx, older_id)
    await _require_channel_boundary(cog, ctx, newer_id)
    records = await cog._message_registry.recent_in_channel(
        ctx.guild.id,
        ctx.channel.id,
        limit=RANGE_QUERY_LIMIT,
        before_message_id=newer_id,
        after_message_id=older_id,
        since_utc=_retention_cutoff(),
        exclude_pinned=not delete_pinned,
    )
    _reject_oversized_range(records)
    return await _finish_channel_cleanup(cog, ctx, records, len(records))
