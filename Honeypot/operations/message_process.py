"""Detection-case message processing operation handler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from time import perf_counter
from typing import TYPE_CHECKING

import discord

from .. import detection_runtime
from ..detection_cases import (
    ActionIntent,
    CaseStatus,
    DeleteStatus,
    OPERATION_RESULT_CASE_TERMINAL,
    OperationType,
    effective_action,
)
from ..settings import GuildSettings
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot


log = logging.getLogger("red.Honeypot")
DETECTION_FAST_RETRY_SECONDS = 10
DETECTION_CAPTURE_START_TIMEOUT_SECONDS = 1.0


async def message_process_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    if context.snapshot.case.status not in {
        CaseStatus.PENDING,
        CaseStatus.RESOLVING,
    }:
        if context.operation.message_sequence is not None:
            await asyncio.to_thread(
                cog._case_store.fail_pending_attachment_captures,
                context.operation.case_id,
                context.operation.message_sequence,
                "case closed before attachment capture completed",
            )
        return OperationOutcome(result=OPERATION_RESULT_CASE_TERMINAL)
    return OperationOutcome(
        result=await _execute_detection_message_process(cog, context)
    )


async def _execute_detection_message_process(
    cog: Honeypot, context: OperationContext
) -> str:
    operation = context.operation
    snapshot = context.snapshot
    now = context.now
    live_message = context.live_message
    publication_channel = context.publication_channel
    timings = context.timings if context.timings is not None else {}
    source = next(
        (
            message
            for message in snapshot.messages
            if message.sequence == operation.message_sequence
        ),
        None,
    )
    if source is None:
        raise RuntimeError("detection case source message is unavailable")
    signals = tuple(
        item.signal
        for item in snapshot.signals
        if item.message_sequence == source.sequence
    )
    containment_required = any(
        signal.action != ActionIntent.NONE
        or (
            signal.detector == "honeypot"
            and not signal.metadata.get("whitelist_bypass")
        )
        or signal.metadata.get("containment_required")
        for signal in signals
    )
    has_forward_purge_signal = any(
        signal.detector == "forward_purge" for signal in signals
    )
    action = effective_action(signals)
    raw_config = await cog.config.guild_from_id(snapshot.case.guild_id).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    guild = (
        live_message.guild
        if live_message is not None
        else cog.bot.get_guild(snapshot.case.guild_id)
    )
    if guild is None:
        raise RuntimeError("detection case guild is unavailable")
    direct_message = live_message is not None
    channel = None
    if live_message is None:
        channel = await cog._fetch_message_channel(guild, source.channel_id)
    if channel is not None:
        fetch_message = getattr(channel, "fetch_message", None)
        if callable(fetch_message):
            try:
                live_message = await fetch_message(source.message_id)
            except discord.NotFound:
                live_message = None

    persisted = cog._persisted_capture_results(snapshot, source.sequence)
    message_attachments = tuple(
        attachment
        for attachment in snapshot.attachments
        if attachment.message_sequence == source.sequence
    )
    evidence_started = perf_counter()
    capture_started = asyncio.Event()
    if live_message is not None and len(persisted) < len(message_attachments):
        capture_task = asyncio.create_task(
            cog._capture_case_attachments(
                live_message,
                operation.case_id,
                source.sequence,
                started_event=capture_started,
            )
        )
    else:
        capture_started.set()
        capture_task = asyncio.create_task(asyncio.sleep(0, result=persisted))

    try:
        containment_started = perf_counter()
        if message_attachments and not persisted:
            try:
                await asyncio.wait_for(
                    capture_started.wait(),
                    timeout=DETECTION_CAPTURE_START_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await asyncio.to_thread(
                    cog._case_store.fail_pending_attachment_captures,
                    operation.case_id,
                    source.sequence,
                    "attachment capture could not start before containment",
                )
                capture_task.cancel()
                await asyncio.gather(capture_task, return_exceptions=True)
                refreshed = await asyncio.to_thread(
                    cog._case_store.get_case, operation.case_id
                )
                persisted = cog._persisted_capture_results(
                    refreshed, source.sequence
                )
                capture_task = asyncio.create_task(
                    asyncio.sleep(0, result=persisted)
                )
        delete_result = detection_runtime.DeleteResult(
            source.delete_status, 0, source.error
        )
        if source.delete_status is DeleteStatus.PENDING and containment_required:
            if guild_settings.dry_run:
                delete_result = detection_runtime.DeleteResult(
                    DeleteStatus.PLANNED, 0, None
                )
            elif live_message is None:
                delete_result = detection_runtime.DeleteResult(
                    DeleteStatus.ALREADY_GONE, 1, None
                )
            else:
                delete_result = await detection_runtime.delete_message(live_message)
            needs_attention = delete_result.status in {
                DeleteStatus.FORBIDDEN,
                DeleteStatus.TRANSIENT_FAILURE,
            }
            await asyncio.to_thread(
                cog._case_store.update_message_delete,
                operation.case_id,
                source.sequence,
                delete_result.status,
                delete_result.error,
                needs_attention,
            )
            if needs_attention:
                retry = await asyncio.to_thread(
                    cog._case_store.ensure_operation,
                    operation.case_id,
                    OperationType.SOURCE_DELETE,
                    (
                        f"source-delete:{operation.case_id}:"
                        f"{source.channel_id}:{source.message_id}"
                    ),
                    source.sequence,
                )
                claimed_retry = await asyncio.to_thread(
                    cog._case_store.claim_operation,
                    retry.operation_id,
                    now,
                )
                if claimed_retry is not None:
                    failed_retry = await asyncio.to_thread(
                        cog._case_store.fail_operation,
                        claimed_retry.operation_id,
                        claimed_retry.claim_token,
                        delete_result.error or delete_result.status.value,
                        now,
                        now + timedelta(seconds=DETECTION_FAST_RETRY_SECONDS),
                        delete_result.status.value,
                    )
                    if failed_retry:
                        await cog._record_operational_failure(
                            snapshot.case.guild_id,
                            OperationType.SOURCE_DELETE,
                            delete_result.error or delete_result.status.value,
                            case_id=operation.case_id,
                            operation_id=claimed_retry.operation_id,
                            attempts=claimed_retry.attempts,
                        )
            if direct_message:
                if delete_result.status is DeleteStatus.DELETED:
                    await cog._increment_stat(guild, "purged_messages")
                    if has_forward_purge_signal:
                        await cog._increment_stat(guild, "forward_purge_deletes")
                elif delete_result.status is DeleteStatus.FORBIDDEN:
                    await cog._increment_stat(guild, "delete_forbidden")
                    if has_forward_purge_signal:
                        await cog._increment_stat(
                            guild, "forward_purge_delete_failures"
                        )
                elif delete_result.status is DeleteStatus.TRANSIENT_FAILURE:
                    await cog._increment_stat(guild, "delete_transient_failures")
                    if has_forward_purge_signal:
                        await cog._increment_stat(
                            guild, "forward_purge_delete_failures"
                        )
        if containment_required and live_message is not None:
            deleted = await cog._purge_detection_case_cached_messages(
                guild,
                snapshot.case.user_id,
                guild_settings,
                operation.case_id,
                source.sequence,
                exclude_message_id=source.message_id,
            )
            if deleted:
                await cog._increment_stat(guild, "purged_messages", deleted)
                await cog._increment_stat(guild, "cached_purge_deletes", deleted)

        refreshed = await asyncio.to_thread(
            cog._case_store.get_case, operation.case_id
        )
        if refreshed is None:
            return "case_deleted"
        if action in {ActionIntent.KICK, ActionIntent.BAN}:
            await cog._execute_detection_message_child(
                refreshed, OperationType.MODERATION_ACTION, source.sequence, now
            )
        elif action is ActionIntent.REVIEW:
            await cog._execute_detection_message_child(
                refreshed, OperationType.ROLE_APPLY, source.sequence, now
            )
        timings["containment_ms"] = (
            perf_counter() - containment_started
        ) * 1000

        first_publish_started = perf_counter()
        logs_channel = publication_channel or cog._get_text_channel_or_thread(
            guild, guild_settings.logs_channel
        )
        review_publication = next(
            (
                item
                for item in refreshed.operations
                if item.operation_type == OperationType.REVIEW_PUBLISH
                and item.message_sequence == source.sequence
            ),
            None,
        )
        has_review_publication = review_publication is not None
        preview_published = False
        if has_review_publication:
            try:
                preview_published = await cog._publish_detection_case(
                    operation.case_id,
                    guild_settings.review_channel,
                    logs_channel,
                    message_sequence=source.sequence,
                    skip_if_done=capture_task,
                )
            except Exception as error:
                await cog._record_operational_failure(
                    snapshot.case.guild_id,
                    OperationType.REVIEW_PUBLISH,
                    f"{type(error).__name__}: {error}",
                    case_id=operation.case_id,
                    operation_id=review_publication.operation_id,
                )
                log.warning(
                    "Detection case preview publication failed case=%s "
                    "message=%s error=%s",
                    operation.case_id,
                    source.sequence,
                    error,
                )
        timings["first_publish_ms"] = (
            perf_counter() - first_publish_started
        ) * 1000

        evidence_wait_started = perf_counter()
        await capture_task
        timings["evidence_wait_ms"] = (
            perf_counter() - evidence_wait_started
        ) * 1000
        timings["evidence_ms"] = (perf_counter() - evidence_started) * 1000
        refreshed = await asyncio.to_thread(
            cog._case_store.get_case, operation.case_id
        )
        if refreshed is None:
            return "case_deleted"
        captures = cog._persisted_capture_results(refreshed, source.sequence)
        if len(captures) < len(message_attachments):
            raise RuntimeError(
                "attachment evidence is not terminal; retry after reservation expiry"
            )
        capture_failures = sum(
            capture.status
            in {
                detection_runtime.CaptureStatus.FAILED,
                detection_runtime.CaptureStatus.TIMEOUT,
                detection_runtime.CaptureStatus.TOO_LARGE,
            }
            for capture in captures
        )
        if capture_failures:
            await cog._increment_stat(
                guild, "evidence_capture_failures", capture_failures
            )

        scan_started = perf_counter()
        if live_message is not None:
            await cog._scan_all_case_message_images(
                live_message,
                guild_settings,
                operation.case_id,
                source.sequence,
                captures,
            )
        else:
            attachments = tuple(
                attachment
                for attachment in refreshed.attachments
                if attachment.message_sequence == source.sequence
            )
            await cog._scan_case_message_images(
                snapshot.case.guild_id,
                attachments,
                guild_settings,
                operation.case_id,
                source.sequence,
                captures,
            )
        timings["scan_ms"] = (perf_counter() - scan_started) * 1000
        refreshed = await asyncio.to_thread(
            cog._case_store.get_case, operation.case_id
        )
        refresh_started = perf_counter()
        review_executed = await cog._execute_detection_message_child(
            refreshed,
            OperationType.REVIEW_PUBLISH,
            source.sequence,
            now,
            publication_channel=publication_channel,
        )
        if has_review_publication and not review_executed:
            await cog._publish_detection_case(
                operation.case_id,
                guild_settings.review_channel,
                logs_channel,
                message_sequence=source.sequence,
            )
        timings["refresh_ms"] = (perf_counter() - refresh_started) * 1000
        await asyncio.to_thread(
            cog._case_store.reconcile_moderator_actions,
            datetime.now(timezone.utc),
        )
        return "processed"
    finally:
        if not capture_task.done():
            capture_task.cancel()
        await asyncio.gather(capture_task, return_exceptions=True)
