"""Detection-case message processing operation handler."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from time import perf_counter
from typing import TYPE_CHECKING

import discord

from .. import detection_runtime
from ..detection_cases import (
    ActionIntent,
    AttachmentRecord,
    CaseSnapshot,
    CaseStatus,
    DeleteStatus,
    MessageRecord,
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


@dataclass(frozen=True)
class _MessageProcessState:
    source: MessageRecord
    guild_settings: GuildSettings
    guild: object
    live_message: object | None
    direct_message: bool
    containment_required: bool
    has_forward_purge_signal: bool
    action: ActionIntent
    timings: dict[str, float]


@dataclass(frozen=True)
class _PublicationTrigger:
    has_review_publication: bool
    logs_channel: object | None


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
    return OperationOutcome(result=await _process_active_message(cog, context))


async def _reserve_attachment_capture(
    cog: Honeypot,
    context: OperationContext,
    state: _MessageProcessState,
) -> tuple[
    asyncio.Task,
    tuple[AttachmentRecord, ...],
    float,
    float,
]:
    operation = context.operation
    persisted = cog._persisted_capture_results(
        context.snapshot, state.source.sequence
    )
    message_attachments = tuple(
        attachment
        for attachment in context.snapshot.attachments
        if attachment.message_sequence == state.source.sequence
    )
    evidence_started = perf_counter()
    capture_started = asyncio.Event()
    if (
        state.live_message is not None
        and len(persisted) < len(message_attachments)
    ):
        capture_task = asyncio.create_task(
            cog._capture_case_attachments(
                state.live_message,
                operation.case_id,
                state.source.sequence,
                started_event=capture_started,
            )
        )
    else:
        capture_started.set()
        capture_task = asyncio.create_task(asyncio.sleep(0, result=persisted))

    containment_started = perf_counter()
    try:
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
                    state.source.sequence,
                    "attachment capture could not start before containment",
                )
                capture_task.cancel()
                await asyncio.gather(capture_task, return_exceptions=True)
                refreshed = await asyncio.to_thread(
                    cog._case_store.get_case, operation.case_id
                )
                persisted = cog._persisted_capture_results(
                    refreshed, state.source.sequence
                )
                capture_task = asyncio.create_task(
                    asyncio.sleep(0, result=persisted)
                )
        return (
            capture_task,
            message_attachments,
            evidence_started,
            containment_started,
        )
    except BaseException:
        if not capture_task.done():
            capture_task.cancel()
        await asyncio.gather(capture_task, return_exceptions=True)
        raise


async def _complete_attachment_capture(
    cog: Honeypot,
    context: OperationContext,
    state: _MessageProcessState,
    capture_task: asyncio.Task,
    message_attachments: tuple[AttachmentRecord, ...],
    evidence_started: float,
) -> tuple[
    CaseSnapshot | None,
    tuple[detection_runtime.CaptureResult, ...],
]:
    evidence_wait_started = perf_counter()
    await capture_task
    state.timings["evidence_wait_ms"] = (
        perf_counter() - evidence_wait_started
    ) * 1000
    state.timings["evidence_ms"] = (
        perf_counter() - evidence_started
    ) * 1000
    refreshed = await asyncio.to_thread(
        cog._case_store.get_case, context.operation.case_id
    )
    if refreshed is None:
        return None, ()
    captures = cog._persisted_capture_results(
        refreshed, state.source.sequence
    )
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
            state.guild, "evidence_capture_failures", capture_failures
        )
    return refreshed, captures


async def _delete_source_message(
    cog: Honeypot,
    context: OperationContext,
    state: _MessageProcessState,
    containment_started: float,
) -> CaseSnapshot | None:
    operation = context.operation
    source = state.source
    delete_result = detection_runtime.DeleteResult(
        source.delete_status, 0, source.error
    )
    if (
        source.delete_status is DeleteStatus.PENDING
        and state.containment_required
    ):
        if state.guild_settings.dry_run:
            delete_result = detection_runtime.DeleteResult(
                DeleteStatus.PLANNED, 0, None
            )
        elif state.live_message is None:
            delete_result = detection_runtime.DeleteResult(
                DeleteStatus.ALREADY_GONE, 1, None
            )
        else:
            delete_result = await detection_runtime.delete_message(
                state.live_message
            )
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
                context.now,
            )
            if claimed_retry is not None:
                failed_retry = await asyncio.to_thread(
                    cog._case_store.fail_operation,
                    claimed_retry.operation_id,
                    claimed_retry.claim_token,
                    delete_result.error or delete_result.status.value,
                    context.now,
                    context.now
                    + timedelta(seconds=DETECTION_FAST_RETRY_SECONDS),
                    delete_result.status.value,
                )
                if failed_retry:
                    await cog._record_operational_failure(
                        context.snapshot.case.guild_id,
                        OperationType.SOURCE_DELETE,
                        delete_result.error or delete_result.status.value,
                        case_id=operation.case_id,
                        operation_id=claimed_retry.operation_id,
                        attempts=claimed_retry.attempts,
                    )
        if state.direct_message:
            if delete_result.status is DeleteStatus.DELETED:
                await cog._increment_stat(state.guild, "purged_messages")
                if state.has_forward_purge_signal:
                    await cog._increment_stat(
                        state.guild, "forward_purge_deletes"
                    )
            elif delete_result.status is DeleteStatus.FORBIDDEN:
                await cog._increment_stat(state.guild, "delete_forbidden")
                if state.has_forward_purge_signal:
                    await cog._increment_stat(
                        state.guild, "forward_purge_delete_failures"
                    )
            elif delete_result.status is DeleteStatus.TRANSIENT_FAILURE:
                await cog._increment_stat(
                    state.guild, "delete_transient_failures"
                )
                if state.has_forward_purge_signal:
                    await cog._increment_stat(
                        state.guild, "forward_purge_delete_failures"
                    )
    if state.containment_required and state.live_message is not None:
        deleted = await cog._purge_detection_case_cached_messages(
            state.guild,
            context.snapshot.case.user_id,
            state.guild_settings,
            operation.case_id,
            source.sequence,
            exclude_message_id=source.message_id,
        )
        if deleted:
            await cog._increment_stat(
                state.guild, "purged_messages", deleted
            )
            await cog._increment_stat(
                state.guild, "cached_purge_deletes", deleted
            )

    refreshed = await asyncio.to_thread(
        cog._case_store.get_case, operation.case_id
    )
    if refreshed is None:
        return None
    if state.action in {ActionIntent.KICK, ActionIntent.BAN}:
        await cog._execute_detection_message_child(
            refreshed,
            OperationType.MODERATION_ACTION,
            source.sequence,
            context.now,
        )
    elif state.action is ActionIntent.REVIEW:
        await cog._execute_detection_message_child(
            refreshed,
            OperationType.ROLE_APPLY,
            source.sequence,
            context.now,
        )
    state.timings["containment_ms"] = (
        perf_counter() - containment_started
    ) * 1000
    return refreshed


async def _trigger_image_scan(
    cog: Honeypot,
    context: OperationContext,
    state: _MessageProcessState,
    refreshed: CaseSnapshot,
    captures: tuple[detection_runtime.CaptureResult, ...],
) -> CaseSnapshot | None:
    scan_started = perf_counter()
    if state.live_message is not None:
        await cog._scan_all_case_message_images(
            state.live_message,
            state.guild_settings,
            context.operation.case_id,
            state.source.sequence,
            captures,
        )
    else:
        attachments = tuple(
            attachment
            for attachment in refreshed.attachments
            if attachment.message_sequence == state.source.sequence
        )
        await cog._scan_case_message_images(
            context.snapshot.case.guild_id,
            attachments,
            state.guild_settings,
            context.operation.case_id,
            state.source.sequence,
            captures,
        )
    state.timings["scan_ms"] = (perf_counter() - scan_started) * 1000
    return await asyncio.to_thread(
        cog._case_store.get_case, context.operation.case_id
    )


async def _trigger_publication_preview(
    cog: Honeypot,
    context: OperationContext,
    state: _MessageProcessState,
    refreshed: CaseSnapshot,
    capture_task: asyncio.Task,
) -> _PublicationTrigger:
    first_publish_started = perf_counter()
    logs_channel = (
        context.publication_channel
        or cog._get_text_channel_or_thread(
            state.guild, state.guild_settings.logs_channel
        )
    )
    review_publication = next(
        (
            item
            for item in refreshed.operations
            if item.operation_type == OperationType.REVIEW_PUBLISH
            and item.message_sequence == state.source.sequence
        ),
        None,
    )
    has_review_publication = review_publication is not None
    if has_review_publication:
        try:
            await cog._publish_detection_case(
                context.operation.case_id,
                state.guild_settings.review_channel,
                logs_channel,
                message_sequence=state.source.sequence,
                skip_if_done=capture_task,
            )
        except Exception as error:
            await cog._record_operational_failure(
                context.snapshot.case.guild_id,
                OperationType.REVIEW_PUBLISH,
                f"{type(error).__name__}: {error}",
                case_id=context.operation.case_id,
                operation_id=review_publication.operation_id,
            )
            log.warning(
                "Detection case preview publication failed case=%s "
                "message=%s error=%s",
                context.operation.case_id,
                state.source.sequence,
                error,
            )
    state.timings["first_publish_ms"] = (
        perf_counter() - first_publish_started
    ) * 1000
    return _PublicationTrigger(
        has_review_publication=has_review_publication,
        logs_channel=logs_channel,
    )


async def _trigger_publication_completion(
    cog: Honeypot,
    context: OperationContext,
    state: _MessageProcessState,
    refreshed: CaseSnapshot | None,
    trigger: _PublicationTrigger,
) -> None:
    refresh_started = perf_counter()
    review_executed = await cog._execute_detection_message_child(
        refreshed,
        OperationType.REVIEW_PUBLISH,
        state.source.sequence,
        context.now,
        publication_channel=context.publication_channel,
    )
    if trigger.has_review_publication and not review_executed:
        await cog._publish_detection_case(
            context.operation.case_id,
            state.guild_settings.review_channel,
            trigger.logs_channel,
            message_sequence=state.source.sequence,
        )
    state.timings["refresh_ms"] = (
        perf_counter() - refresh_started
    ) * 1000


async def _process_active_message(
    cog: Honeypot, context: OperationContext
) -> str:
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
    containment_required = any(
        signal.action != ActionIntent.NONE
        or (
            signal.detector == "honeypot"
            and not signal.metadata.get("whitelist_bypass")
        )
        or signal.metadata.get("containment_required")
        for signal in signals
    )
    raw_config = await cog.config.guild_from_id(
        context.snapshot.case.guild_id
    ).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    live_message = context.live_message
    guild = (
        live_message.guild
        if live_message is not None
        else cog.bot.get_guild(context.snapshot.case.guild_id)
    )
    if guild is None:
        raise RuntimeError("detection case guild is unavailable")
    direct_message = live_message is not None
    if live_message is None:
        channel = await cog._fetch_message_channel(guild, source.channel_id)
        if channel is not None:
            fetch_message = getattr(channel, "fetch_message", None)
            if callable(fetch_message):
                try:
                    live_message = await fetch_message(source.message_id)
                except discord.NotFound:
                    live_message = None
    state = _MessageProcessState(
        source=source,
        guild_settings=guild_settings,
        guild=guild,
        live_message=live_message,
        direct_message=direct_message,
        containment_required=containment_required,
        has_forward_purge_signal=any(
            signal.detector == "forward_purge" for signal in signals
        ),
        action=effective_action(signals),
        timings=(
            context.timings if context.timings is not None else {}
        ),
    )
    (
        capture_task,
        message_attachments,
        evidence_started,
        containment_started,
    ) = await _reserve_attachment_capture(cog, context, state)

    try:
        refreshed = await _delete_source_message(
            cog, context, state, containment_started
        )
        if refreshed is None:
            return "case_deleted"
        publication = await _trigger_publication_preview(
            cog, context, state, refreshed, capture_task
        )
        refreshed, captures = await _complete_attachment_capture(
            cog,
            context,
            state,
            capture_task,
            message_attachments,
            evidence_started,
        )
        if refreshed is None:
            return "case_deleted"
        refreshed = await _trigger_image_scan(
            cog, context, state, refreshed, captures
        )
        await _trigger_publication_completion(
            cog, context, state, refreshed, publication
        )
        await asyncio.to_thread(
            cog._case_store.reconcile_moderator_actions,
            datetime.now(timezone.utc),
        )
        return "processed"
    finally:
        if not capture_task.done():
            capture_task.cancel()
        await asyncio.gather(capture_task, return_exceptions=True)
