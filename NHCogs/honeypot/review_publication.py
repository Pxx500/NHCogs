"""Detection-case review publication: evidence capture, timeline projection, moderator UI.

Every definition here was a `Honeypot` method. The cog keeps a one-line seam only
where another module or a test must reach the behaviour through `self`; see the
`# Review publication seam` comments in `honeypot.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import shutil
import typing
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import discord
from redbot.core.i18n import Translator

from . import detection_runtime
from .case_review import (
    CaseFeedbackItem,
    bulk_image_confirmation_label,
    case_feedback_items,
    is_persisted_image_attachment,
    render_case,
    render_timeline,
    validate_image_review_action,
)
from .detection_cases import (
    MODERATION_SUPERSEDING_RESULTS,
    MODERATION_SUPERSEDING_TYPES,
    OPERATION_RESULT_KICK_MISSING,
    AttachmentKey,
    NewAttachment,
    NewMessage,
    OperationStatus,
)
from .operations.moderator_decision import apply_moderator_ignore
from .settings import GuildSettings
from .views import (
    DetectionBulkConfirmationView,
    DetectionCaseView,
    DetectionIndividualView,
    DetectionModerationConfirmationView,
)

_ = Translator("Honeypot", __file__)
log = logging.getLogger("red.Honeypot")

DETECTION_CAPTURE_DEADLINE_SECONDS = 20.0
DETECTION_CAPTURE_CONCURRENCY = 4
DETECTION_EVIDENCE_RESERVATION_STALE_SECONDS = 5 * 60
_TIMELINE_VIEW_UNSET = object()


def case_evidence_root(evidence_root: Path, guild_id: int, case_id: str) -> Path:
    """Return the canonical storage root for one guild-scoped detection case."""
    return evidence_root / str(guild_id) / case_id


def _delete_detection_case_evidence(cog, cases: tuple[tuple[int, str], ...]) -> None:
    evidence_root = cog._detection_case_files_path.resolve()
    for guild_id, case_id in cases:
        case_root = case_evidence_root(
            cog._detection_case_files_path, guild_id, case_id
        )
        if not case_root.exists():
            continue
        if not case_root.resolve().is_relative_to(evidence_root):
            raise RuntimeError("detection case evidence path escapes storage root")
        shutil.rmtree(case_root)


def _discard_rejected_detection_case_capture(
    cog, guild_id: int, case_id: str, capture_path: Path
) -> None:
    evidence_root = cog._detection_case_files_path.resolve()
    case_root = case_evidence_root(
        cog._detection_case_files_path, guild_id, case_id
    ).resolve()
    resolved_capture = capture_path.resolve()
    if not case_root.is_relative_to(evidence_root):
        raise RuntimeError("detection case evidence path escapes storage root")
    if not resolved_capture.is_relative_to(case_root):
        raise RuntimeError("detection case evidence path escapes case root")
    capture_path.unlink(missing_ok=True)
    parent = resolved_capture.parent
    while parent != case_root:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent
    try:
        case_root.rmdir()
    except OSError:
        pass


@asynccontextmanager
async def _detection_case_deletion_barrier(cog):
    async with cog._detection_case_evidence_lock:
        acquired_slots = 0
        try:
            for _ in range(DETECTION_CAPTURE_CONCURRENCY):
                await cog._detection_case_capture_slots.acquire()
                acquired_slots += 1
            yield
        finally:
            for _ in range(acquired_slots):
                cog._detection_case_capture_slots.release()


async def _delete_detection_case_scope(
    cog,
    delete_cases: typing.Callable[[int], tuple[tuple[int, str], ...]],
    scope_id: int,
) -> None:
    async with _detection_case_deletion_barrier(cog):
        await asyncio.to_thread(delete_cases, scope_id)
        cases = await asyncio.to_thread(
            cog._case_store.list_planned_case_deletions
        )
        await _finish_detection_case_deletions(cog, cases)


async def _finish_detection_case_deletions(cog, cases: tuple[tuple[int, str], ...]) -> None:
    errors: list[Exception] = []
    for guild_id, case_id in cases:
        job = await asyncio.to_thread(
            cog._case_store.get_case_deletion_job, case_id
        )
        if job is None:
            continue
        if not job.remote_deleted:
            try:
                await _delete_detection_case_publications(cog, guild_id, case_id)
                await asyncio.to_thread(
                    cog._case_store.mark_case_deletion_remote, case_id
                )
            except Exception as error:
                await asyncio.to_thread(
                    cog._case_store.mark_case_deletion_remote,
                    case_id,
                    error=str(error),
                )
                await cog._record_operational_failure(
                    guild_id,
                    "case_publication_deletion",
                    f"{type(error).__name__}: {error}",
                    case_id=case_id,
                )
                errors.append(error)
        local_deleted = job.local_deleted
        if not local_deleted:
            try:
                await asyncio.to_thread(
                    _delete_detection_case_evidence,
                    cog,
                    ((guild_id, case_id),),
                )
                await asyncio.to_thread(
                    cog._case_store.mark_case_deletion_local, case_id
                )
                local_deleted = True
            except Exception as error:
                await cog._record_operational_failure(
                    guild_id,
                    "case_evidence_deletion",
                    f"{type(error).__name__}: {error}",
                    case_id=case_id,
                )
                errors.append(error)
        if not job.rows_deleted and local_deleted:
            inflight = await asyncio.to_thread(
                cog._case_store.case_deletion_has_inflight_publications,
                case_id,
            )
            if inflight:
                errors.append(
                    RuntimeError(
                        f"detection case publications are still in flight: {case_id}"
                    )
                )
            else:
                finalized = await asyncio.to_thread(
                    cog._case_store.finalize_case_deletion,
                    guild_id,
                    case_id,
                )
                if not finalized:
                    errors.append(
                        RuntimeError(
                            f"detection case deletion job disappeared: {case_id}"
                        )
                    )
                else:
                    cog._case_views.pop(case_id, None)
        await asyncio.to_thread(
            cog._case_store.complete_case_deletion_job, case_id
        )
    if errors:
        raise errors[0]


async def _delete_detection_case_publications(cog, guild_id: int, case_id: str) -> None:
    job = await asyncio.to_thread(
        cog._case_store.get_case_deletion_job, case_id
    )
    if job is None:
        raise RuntimeError(f"detection case deletion job disappeared: {case_id}")
    if (
        job.parent_channel_id is None
        and job.summary_message_id is None
        and job.thread_id is None
        and not job.legacy_publications
    ):
        return
    guild = cog.bot.get_guild(guild_id)
    if guild is None:
        raise RuntimeError(
            f"guild {guild_id} is unavailable for detection case deletion"
        )
    parent = await cog._fetch_text_channel_or_thread(
        guild, job.parent_channel_id
    )
    summary = None
    if parent is not None and job.summary_message_id is not None:
        try:
            summary = await parent.fetch_message(job.summary_message_id)
        except discord.NotFound:
            summary = None

    thread = None
    if summary is not None:
        fetch_thread = getattr(summary, "fetch_thread", None)
        if callable(fetch_thread):
            try:
                thread = await fetch_thread()
            except discord.NotFound:
                thread = None
    if thread is None and job.thread_id is not None:
        thread = await cog._fetch_text_channel_or_thread(guild, job.thread_id)

    if thread is not None:
        try:
            await thread.delete(reason="Honeypot user data deletion")
        except discord.NotFound:
            pass
    for channel_id, message_id in job.legacy_publications:
        legacy_channel = await cog._fetch_text_channel_or_thread(
            guild, channel_id
        )
        if legacy_channel is None:
            continue
        try:
            legacy_message = await legacy_channel.fetch_message(message_id)
            await legacy_message.delete()
        except discord.NotFound:
            pass
    if summary is not None:
        try:
            await summary.delete()
        except discord.NotFound:
            pass


async def _retry_detection_case_deletions(cog) -> None:
    async with _detection_case_deletion_barrier(cog):
        cases = await asyncio.to_thread(
            cog._case_store.list_planned_case_deletions
        )
        await _finish_detection_case_deletions(cog, cases)


def _new_case_message(message: discord.Message) -> NewMessage:
    return NewMessage(
        guild_id=message.guild.id,
        user_id=message.author.id,
        channel_id=message.channel.id,
        message_id=message.id,
        content=message.content,
        created_at=message.created_at,
        jump_url=getattr(message, "jump_url", None),
        attachments=tuple(
            NewAttachment(
                position=position,
                filename=attachment.filename,
                size=attachment.size,
                content_type=attachment.content_type,
                width=getattr(attachment, "width", None),
                height=getattr(attachment, "height", None),
                url=attachment.url,
                description=getattr(attachment, "description", None),
                spoiler=attachment.is_spoiler(),
            )
            for position, attachment in enumerate(message.attachments)
        ),
        display_name=getattr(message.author, "display_name", None),
        avatar_url=(
            str(getattr(message.author, "display_avatar", None).url)
            if getattr(getattr(message.author, "display_avatar", None), "url", None)
            else None
        ),
        account_created_at=getattr(message.author, "created_at", None),
        guild_joined_at=getattr(message.author, "joined_at", None),
    )


async def _capture_case_attachments(
    cog,
    message: discord.Message,
    case_id: str,
    sequence: int,
    *,
    started_event: asyncio.Event | None = None,
) -> tuple[detection_runtime.CaptureResult, ...]:
    async with cog._detection_case_evidence_lock:
        await cog._detection_case_capture_slots.acquire()
    try:
        accepts_evidence = await asyncio.to_thread(
            cog._case_store.case_accepts_evidence,
            message.guild.id,
            case_id,
        )
        if not accepts_evidence:
            return tuple(
                detection_runtime.CaptureResult(
                    position,
                    detection_runtime.CaptureStatus.FAILED,
                    None,
                    "detection case deletion is in progress",
                )
                for position, _attachment in enumerate(message.attachments)
            )
        return await _capture_case_attachments_unlocked(cog,
            message,
            case_id,
            sequence,
            started_event=started_event,
            prefetched_scans=cog._initial_image_scan_batches.get(
                (message.guild.id, message.id), {}
            ),
        )
    finally:
        cog._detection_case_capture_slots.release()


async def _capture_case_attachments_unlocked(
    cog,
    message: discord.Message,
    case_id: str,
    sequence: int,
    *,
    started_event: asyncio.Event | None = None,
    prefetched_scans: dict[int, asyncio.Task] | None = None,
) -> tuple[detection_runtime.CaptureResult, ...]:
    target = case_evidence_root(
        cog._detection_case_files_path, message.guild.id, case_id
    ) / str(sequence) / f".attempt-{uuid4().hex}"
    if not message.attachments:
        if started_event is not None:
            started_event.set()
        return ()
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    if snapshot is None:
        return ()
    case_declared_bytes = sum(
        max(0, int(attachment.size)) for attachment in snapshot.attachments
    )
    tasks: dict[int, asyncio.Task] = {}
    claim_tokens: dict[int, str] = {}
    attachment_sizes: dict[int, int] = {}
    captures_by_position = {}
    for position, attachment in enumerate(message.attachments):
        size = max(0, int(getattr(attachment, "size", 0) or 0))
        attachment_sizes[position] = size
        claimed_at = datetime.now(timezone.utc)
        reservation = await asyncio.to_thread(
            cog._case_store.reserve_attachment_capture,
            case_id,
            sequence,
            position,
            size,
            claimed_at,
            stale_before=claimed_at
            - timedelta(seconds=DETECTION_EVIDENCE_RESERVATION_STALE_SECONDS),
            max_attachment_bytes=size,
            max_case_bytes=case_declared_bytes,
        )
        if reservation.status == "too_large":
            captures_by_position[position] = detection_runtime.CaptureResult(
                position,
                detection_runtime.CaptureStatus.TOO_LARGE,
                None,
                reservation.error,
            )
            continue
        if reservation.status != "claimed" or reservation.claim_token is None:
            captures_by_position[position] = detection_runtime.CaptureResult(
                position,
                detection_runtime.CaptureStatus.FAILED,
                None,
                reservation.error or "evidence capture reservation unavailable",
            )
            continue
        claim_tokens[position] = reservation.claim_token
        prefetched_task = (prefetched_scans or {}).get(position)

        async def capture_reader(
            candidate, max_bytes, *, prefetched=prefetched_task
        ):
            if prefetched is None:
                return await detection_runtime.read_attachment_bounded(
                    candidate, max_bytes
                )
            scan = await asyncio.shield(prefetched)
            if scan["error"] is not None:
                return await detection_runtime.read_attachment_bounded(
                    candidate, max_bytes
                )
            data = scan["data"]
            if len(data) > max_bytes:
                raise detection_runtime.AttachmentTooLargeError(
                    f"attachment exceeds the {max_bytes} byte evidence limit"
                )
            return data

        tasks[position] = asyncio.create_task(
            detection_runtime.capture_attachment(
                attachment,
                target,
                position,
                detection_runtime.DETECTION_ATTACHMENT_TIMEOUT_SECONDS,
                max_bytes=size,
                reader=capture_reader,
            )
        )
    if started_event is not None:
        started_event.set()
    try:
        done, pending = await asyncio.wait(
            tuple(tasks.values()), timeout=DETECTION_CAPTURE_DEADLINE_SECONDS
        ) if tasks else (set(), set())
    except BaseException:
        for task in tasks.values():
            task.cancel()
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for result in results:
            if (
                isinstance(result, detection_runtime.CaptureResult)
                and result.path is not None
            ):
                await asyncio.to_thread(
                    _discard_rejected_detection_case_capture,
                    cog,
                    message.guild.id,
                    case_id,
                    result.path,
                )
        for position, claim_token in claim_tokens.items():
            await asyncio.to_thread(
                cog._case_store.release_attachment_capture,
                case_id,
                sequence,
                position,
                claim_token,
                detection_runtime.CaptureStatus.FAILED.value,
                error="attachment capture cancelled",
            )
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for position, task in tasks.items():
        if task in done:
            captures_by_position[position] = task.result()
        else:
            captures_by_position[position] = detection_runtime.CaptureResult(
                position,
                detection_runtime.CaptureStatus.TIMEOUT,
                None,
                "overall attachment capture deadline exceeded",
            )
    captures = tuple(
        captures_by_position[position]
        for position in range(len(message.attachments))
    )
    persisted_captures = []
    for capture in captures:
        claim_token = claim_tokens.get(capture.position)
        if claim_token is None:
            persisted_captures.append(capture)
            continue
        if capture.status is detection_runtime.CaptureStatus.CAPTURED and capture.path is not None:
            actual_bytes = await asyncio.to_thread(lambda path=capture.path: path.stat().st_size)
            completion = await asyncio.to_thread(
                cog._case_store.complete_attachment_capture,
                case_id,
                sequence,
                capture.position,
                claim_token,
                actual_bytes,
                evidence_path=str(capture.path),
                now=datetime.now(timezone.utc),
                max_attachment_bytes=attachment_sizes[capture.position],
                max_case_bytes=case_declared_bytes,
            )
            if completion == "captured":
                persisted_captures.append(capture)
                continue
            if completion == "too_large":
                await asyncio.to_thread(
                    _discard_rejected_detection_case_capture,
                    cog,
                    message.guild.id,
                    case_id,
                    capture.path,
                )
                persisted_captures.append(
                    detection_runtime.CaptureResult(
                        capture.position,
                        detection_runtime.CaptureStatus.TOO_LARGE,
                        None,
                        "captured attachment exceeds its reserved evidence bytes",
                    )
                )
            else:
                await asyncio.to_thread(
                    _discard_rejected_detection_case_capture,
                    cog,
                    message.guild.id,
                    case_id,
                    capture.path,
                )
                persisted_captures.append(
                    detection_runtime.CaptureResult(
                        capture.position,
                        detection_runtime.CaptureStatus.FAILED,
                        None,
                        "evidence capture claim is no longer owned",
                    )
                )
            continue
        released = await asyncio.to_thread(
            cog._case_store.release_attachment_capture,
            case_id,
            sequence,
            capture.position,
            claim_token,
            capture.status.value,
            error=capture.error,
        )
        persisted_captures.append(
            capture
            if released
            else detection_runtime.CaptureResult(
                capture.position,
                detection_runtime.CaptureStatus.FAILED,
                None,
                "evidence capture claim is no longer owned",
            )
        )
    failed_captures = tuple(
        capture
        for capture in persisted_captures
        if capture.status in {
            detection_runtime.CaptureStatus.FAILED,
            detection_runtime.CaptureStatus.TIMEOUT,
        }
        and capture.error not in {
            "detection case deletion is in progress",
            "evidence capture claim is no longer owned",
        }
    )
    if failed_captures:
        details = "; ".join(
            f"attachment {capture.position + 1}: "
            f"{capture.error or capture.status.value}"
            for capture in failed_captures[:3]
        )
        await cog._record_operational_failure(
            message.guild.id,
            "evidence_capture",
            f"Failed to capture {len(failed_captures)} attachment(s): {details}"[:512],
            case_id=case_id,
            attempts=3,
            terminal=True,
        )
    return tuple(persisted_captures)


def _case_timeline_attachment_line(attachment) -> str:
    metadata = attachment.match_metadata
    hash_diff = metadata.get(
        "hash_diff", metadata.get("distance", metadata.get("score"))
    )
    threshold = metadata.get("threshold")
    decisions = {
        "true_positive": "TP",
        "false_positive": "FP",
        "ignored": "IGN",
    }
    decision = decisions.get(attachment.learning_decision, "?")
    if attachment.capture_status in {
        detection_runtime.CaptureStatus.FAILED.value,
        detection_runtime.CaptureStatus.TIMEOUT.value,
        detection_runtime.CaptureStatus.TOO_LARGE.value,
    }:
        return f"{attachment.key.position + 1}·CF"
    if metadata.get("exact_decision") is not None:
        match = "SHA"
    elif hash_diff == 0:
        match = "OH"
    elif hash_diff is not None:
        difference = str(hash_diff)
        if threshold is not None:
            difference += f"/{threshold}"
        match = f"HD {difference}"
    else:
        match = None
    details = f"{attachment.key.position + 1}·{decision}"
    if match:
        details += f"·{match}"
    if attachment.publication_error:
        details += "·CF"
    return details


def _case_timeline_message_content(message) -> str:
    reasons = (
        "\n".join(f"- {reason}" for reason in message.signal_reasons)
        if message.signal_reasons
        else "- Detection signal recorded"
    )
    content = (message.content or "(message with attachments only)").replace(
        "```", "``\u200b`"
    )
    source = message.jump_url or "Source unavailable"
    attachments = (
        "\n\nFiles: "
        + "  ".join(
            _case_timeline_attachment_line(attachment)
            for attachment in message.attachments
        )
        if message.attachments
        else ""
    )
    return (
        f"**M{message.sequence}** • {source} • "
        f"<t:{int(message.created_at.timestamp())}:F>\n"
        f"Status: {message.delete_status}\n"
        f"Signals:\n{reasons}\n```\n{content}\n```{attachments}"
    )


def _case_timeline_message_chunks(message) -> tuple[str, ...]:
    rendered = _case_timeline_message_content(message)
    metadata, opening, fenced = rendered.partition("```\n")
    content, closing, trailing = fenced.partition("\n```")
    if not opening or not closing:
        raise RuntimeError("timeline message content is missing its code fence")

    chunks: list[str] = []
    remaining = content
    while remaining:
        prefix = (
            metadata + opening
            if not chunks
            else f"**M{message.sequence} (continued)**\n```\n"
        )
        suffix = "\n```"
        available = 2000 - len(prefix) - len(suffix)
        if available <= 0:
            raise RuntimeError("timeline message metadata exceeds Discord's limit")
        split_at = min(len(remaining), available)
        if split_at < len(remaining):
            newline = remaining.rfind("\n", 0, split_at + 1)
            if newline > 0:
                split_at = newline + 1
        payload = prefix + remaining[:split_at] + suffix
        remaining = remaining[split_at:]
        if not remaining and trailing and len(payload) + len(trailing) <= 2000:
            payload += trailing
            trailing = ""
        chunks.append(payload)

    while trailing:
        prefix = f"**M{message.sequence} (continued)**\n"
        available = 2000 - len(prefix)
        split_at = min(len(trailing), available)
        if split_at < len(trailing):
            newline = trailing.rfind("\n", 0, split_at + 1)
            if newline > 0:
                split_at = newline + 1
        chunks.append(prefix + trailing[:split_at].lstrip("\n"))
        trailing = trailing[split_at:]

    return tuple(chunks)


def _case_publication_nonce(logical_key: str) -> int:
    digest = hashlib.blake2b(logical_key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


async def _complete_case_timeline_publication(
    cog, publication, sent_message, thread_id: int, *, revision: int = 1
) -> None:
    if publication.claim_token is None:
        raise RuntimeError("timeline publication is not claimed")
    try:
        await asyncio.to_thread(
            cog._case_store.complete_timeline_publication,
            publication.logical_key,
            publication.claim_token,
            channel_id=thread_id,
            message_id=sent_message.id,
            revision=revision,
        )
    except KeyError:
        current = next(
            (
                item
                for item in await asyncio.to_thread(
                    cog._case_store.list_timeline_publications,
                    publication.case_id,
                )
                if item.logical_key == publication.logical_key
            ),
            None,
        )
        if (
            current is not None
            and current.state == "published"
            and current.channel_id == thread_id
            and current.message_id == sent_message.id
        ):
            return
        await _compensate_case_publication(cog,
            publication.case_id, thread_id, sent_message
        )
        raise


async def _compensate_case_publication(cog, case_id: str, channel_id: int, message) -> None:
    delete = getattr(message, "delete", None)
    if callable(delete):
        try:
            await delete()
            return
        except discord.NotFound:
            return
        except discord.HTTPException:
            pass
    recorded = await asyncio.to_thread(
        cog._case_store.add_case_deletion_publication,
        case_id,
        channel_id,
        message.id,
    )
    if not recorded:
        recorded = await asyncio.to_thread(
            cog._case_store.record_orphan_publication,
            case_id,
            channel_id,
            message.id,
        )
    if not recorded:
        raise RuntimeError("failed to retain a late case publication for cleanup")


async def _retry_detection_orphan_publications(cog) -> None:
    publications = await asyncio.to_thread(
        cog._case_store.list_orphan_publications
    )
    for case_id, guild_id, channel_id, message_id in publications:
        guild = cog.bot.get_guild(guild_id)
        if guild is None:
            continue
        channel = await cog._fetch_text_channel_or_thread(guild, channel_id)
        if channel is None:
            continue
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as error:
            await cog._record_operational_failure(
                guild_id,
                "orphan_publication_deletion",
                f"{type(error).__name__}: {error}",
                case_id=case_id,
            )
            continue
        await asyncio.to_thread(
            cog._case_store.complete_orphan_publication,
            case_id,
            channel_id,
            message_id,
        )


async def _acquire_case_timeline_publication(
    cog, publication, *, replace_message_id: int | None = None
):
    for _attempt in range(20):
        claimed = await asyncio.to_thread(
            cog._case_store.claim_timeline_publication,
            publication.logical_key,
            datetime.now(timezone.utc),
            replace_message_id=replace_message_id,
        )
        if claimed is not None:
            return claimed, True
        current = next(
            (
                item
                for item in await asyncio.to_thread(
                    cog._case_store.list_timeline_publications,
                    publication.case_id,
                )
                if item.logical_key == publication.logical_key
            ),
            None,
        )
        if current is None:
            raise KeyError(publication.logical_key)
        if current.state == "published":
            return current, False
        await asyncio.sleep(0)
    raise RuntimeError("timeline publication claim is unavailable")


async def _release_case_timeline_publication(cog, publication) -> None:
    if publication.claim_token is not None:
        await asyncio.to_thread(
            cog._case_store.release_timeline_publication_claim,
            publication.logical_key,
        publication.claim_token,
    )


def _case_timeline_discord_files(evidence_batch) -> list[discord.File]:
    return [
        discord.File(
            Path(attachment.evidence_path),
            filename=attachment.filename,
            spoiler=attachment.spoiler,
            description=attachment.description,
        )
        for attachment in evidence_batch
    ]


async def _upsert_case_timeline_text(
    cog,
    publication,
    thread,
    content: str,
    *,
    view: object = _TIMELINE_VIEW_UNSET,
    evidence_batch: tuple = (),
) -> None:
    edit_kwargs = {"content": content}
    if view is not _TIMELINE_VIEW_UNSET:
        edit_kwargs["view"] = view
    # Revision 1 is text-only; later revisions record the uploaded file count.
    evidence_revision = len(evidence_batch) + 1
    attach_evidence = bool(evidence_batch) and (
        publication.revision < evidence_revision
    )
    replace_message_id = None
    if publication.state == "published" and publication.message_id is not None:
        if attach_evidence:
            edit_kwargs["attachments"] = _case_timeline_discord_files(
                evidence_batch
            )
        try:
            message = thread.get_partial_message(publication.message_id)
            await message.edit(**edit_kwargs)
            if attach_evidence:
                await asyncio.to_thread(
                    cog._case_store.update_timeline_publication_revision,
                    publication.logical_key,
                    message_id=publication.message_id,
                    revision=evidence_revision,
                )
            return
        except discord.NotFound:
            replace_message_id = publication.message_id
    publication, owned = await _acquire_case_timeline_publication(cog,
        publication, replace_message_id=replace_message_id
    )
    if not owned:
        message = await thread.fetch_message(publication.message_id)
        if evidence_batch and publication.revision < evidence_revision:
            edit_kwargs["attachments"] = _case_timeline_discord_files(
                evidence_batch
            )
        else:
            edit_kwargs.pop("attachments", None)
        await message.edit(**edit_kwargs)
        if "attachments" in edit_kwargs:
            await asyncio.to_thread(
                cog._case_store.update_timeline_publication_revision,
                publication.logical_key,
                message_id=publication.message_id,
                revision=evidence_revision,
            )
        return
    send_kwargs = {}
    if view is not _TIMELINE_VIEW_UNSET:
        send_kwargs["view"] = view
    if evidence_batch:
        send_kwargs["files"] = _case_timeline_discord_files(evidence_batch)
    try:
        message = await thread.send(
            content,
            **send_kwargs,
            allowed_mentions=discord.AllowedMentions.none(),
            nonce=_case_publication_nonce(publication.logical_key),
        )
        await _complete_case_timeline_publication(cog,
            publication,
            message,
            thread.id,
            revision=evidence_revision,
        )
    except BaseException:
        await _release_case_timeline_publication(cog, publication)
        raise


def _case_note_chunks(notes: tuple[str, ...]) -> tuple[str, ...]:
    chunks: list[str] = []
    current = "**Case operation notes**"
    for note in notes:
        line = f"\n- {note}"
        if len(current) + len(line) > 2000:
            chunks.append(current)
            current = "**Case operation notes (continued)**" + line
        else:
            current += line
    if notes:
        chunks.append(current)
    else:
        chunks.append("**Case operation notes**\nNo current operation warnings")
    return tuple(chunks)


async def _ensure_detection_case_thread(cog, snapshot, summary_message):
    fetch_thread = getattr(summary_message, "fetch_thread", None)
    thread = None
    if callable(fetch_thread):
        try:
            thread = await fetch_thread()
        except discord.NotFound:
            thread = None
    if thread is None:
        create_thread = getattr(summary_message, "create_thread", None)
        if not callable(create_thread):
            raise RuntimeError("detection case summary cannot create a thread")
        try:
            thread = await create_thread(
                name=f"case-{snapshot.case.user_id}",
                auto_archive_duration=1440,
                reason="Honeypot detection case",
            )
        except discord.HTTPException as create_error:
            if not callable(fetch_thread):
                raise
            try:
                thread = await fetch_thread()
            except discord.NotFound as fetch_error:
                raise create_error from fetch_error
    parent = getattr(summary_message, "channel", None)
    parent_channel_id = getattr(parent, "id", snapshot.case.review_channel_id)
    try:
        await asyncio.to_thread(
            cog._case_store.activate_projection_endpoint,
            snapshot.case.case_id,
            parent_channel_id=parent_channel_id,
            summary_message_id=summary_message.id,
            thread_id=thread.id,
            projected_revision=len(snapshot.messages),
            verified_at=datetime.now(timezone.utc),
        )
    except KeyError:
        delete = getattr(thread, "delete", None)
        if callable(delete):
            try:
                await delete(reason="Honeypot user data deletion")
            except discord.NotFound:
                pass
        raise
    return thread


async def _activate_detection_case_thread(thread):
    if not getattr(thread, "archived", False) and not getattr(
        thread, "locked", False
    ):
        return thread
    return await thread.edit(
        archived=False,
        locked=False,
        reason="Honeypot detection case update",
    )


async def _finalize_detection_case_thread(thread) -> None:
    await thread.edit(
        archived=True,
        locked=True,
        reason="Honeypot detection case resolved",
    )


async def _publish_case_timeline(
    cog,
    snapshot,
    thread,
    *,
    resolved: bool,
    message_sequence: int | None = None,
) -> None:
    timeline = render_timeline(snapshot)
    feedback_items = case_feedback_items(snapshot)
    note_chunks = (
        _case_note_chunks(timeline.case_notes) if timeline.case_notes else ()
    )
    timeline_publications = await asyncio.to_thread(
        cog._case_store.list_timeline_publications,
        snapshot.case.case_id,
    )
    existing_note_count = sum(
        1
        for publication in timeline_publications
        if publication.kind == "case_note"
    )
    for chunk_index in range(max(len(note_chunks), existing_note_count)):
        publication = await asyncio.to_thread(
            cog._case_store.ensure_timeline_publication,
            snapshot.case.case_id,
            kind="case_note",
            chunk_index=chunk_index,
        )
        content = (
            note_chunks[chunk_index]
            if chunk_index < len(note_chunks)
            else "**Case operation notes**\nNo current operation warnings"
        )
        await _upsert_case_timeline_text(cog, publication, thread, content)
    if resolved or message_sequence is None:
        messages = timeline.messages
    else:
        published_message_sequences = {
            publication.message_sequence
            for publication in timeline_publications
            if publication.kind == "message"
            and publication.chunk_index == 0
            and publication.state == "published"
        }
        messages = tuple(
            message
            for message in timeline.messages
            if message.sequence == message_sequence
            or (
                message.sequence < message_sequence
                and message.sequence not in published_message_sequences
            )
        )
    for message in messages:
        batches, oversized, upload_limit = _case_timeline_evidence_batches(
            message, thread
        )
        pending_message_feedback = _pending_feedback_items(
            feedback_items, message.sequence
        )
        has_pending_image_feedback = bool(pending_message_feedback)
        message_chunks = _case_timeline_message_chunks(message)
        legacy_evidence_layout = any(
            publication.kind == "evidence"
            and publication.message_sequence == message.sequence
            and publication.chunk_index == 0
            for publication in timeline_publications
        )
        existing_message_chunks = sum(
            1
            for publication in timeline_publications
            if publication.kind == "message"
            and publication.message_sequence == message.sequence
        )
        for chunk_index in range(
            max(len(message_chunks), existing_message_chunks)
        ):
            publication = await asyncio.to_thread(
                cog._case_store.ensure_timeline_publication,
                snapshot.case.case_id,
                kind="message",
                message_sequence=message.sequence,
                chunk_index=chunk_index,
            )
            content = (
                message_chunks[chunk_index]
                if chunk_index < len(message_chunks)
                else f"**M{message.sequence} (continued)**\nNo additional content"
            )
            view = (
                DetectionCaseView(
                    cog,
                    snapshot.case.case_id,
                    has_image_feedback=has_pending_image_feedback,
                    feedback_items=pending_message_feedback,
                    message_sequence=message.sequence,
                    resolved=resolved,
                    moderation_actions=(),
                )
                if chunk_index == 0
                and (not batches or not legacy_evidence_layout)
                else None
            )
            evidence_batch = ()
            if chunk_index == 0 and batches and not legacy_evidence_layout:
                evidence_batch = batches[0]
            await _upsert_case_timeline_text(
                cog,
                publication,
                thread,
                content,
                view=view,
                evidence_batch=evidence_batch,
            )
        limit_label = f"{upload_limit / (1024 * 1024):g} MiB"
        for attachment in oversized:
            await asyncio.to_thread(
                cog._case_store.update_attachment_publication_error,
                snapshot.case.case_id,
                attachment.key.message_sequence,
                attachment.key.position,
                f"attachment exceeds the {limit_label} review destination upload limit",
            )
        evidence_batches = (
            enumerate(batches)
            if legacy_evidence_layout
            else enumerate(batches[1:], start=1)
        )
        for chunk_index, batch in evidence_batches:
            evidence = await asyncio.to_thread(
                cog._case_store.ensure_timeline_publication,
                snapshot.case.case_id,
                kind="evidence",
                message_sequence=message.sequence,
                chunk_index=chunk_index,
            )
            content = f"Message {message.sequence} attachments"
            view = (
                DetectionCaseView(
                    cog,
                    snapshot.case.case_id,
                    has_image_feedback=has_pending_image_feedback,
                    feedback_items=pending_message_feedback,
                    message_sequence=message.sequence,
                    resolved=resolved,
                    moderation_actions=(),
                )
                if chunk_index == 0
                else None
            )
            replace_message_id = None
            if evidence.state == "published" and evidence.message_id is not None:
                try:
                    published = await thread.fetch_message(evidence.message_id)
                    existing_attachments = getattr(
                        published, "attachments", None
                    )
                    same_batch = (
                        getattr(published, "content", None) == content
                        and existing_attachments is not None
                        and len(existing_attachments) == len(batch)
                    )
                    if same_batch:
                        await published.edit(view=view)
                    else:
                        files = _case_timeline_discord_files(batch)
                        await published.edit(
                            content=content,
                            attachments=files,
                            view=view,
                        )
                    continue
                except discord.NotFound:
                    replace_message_id = evidence.message_id
            evidence, owned = await _acquire_case_timeline_publication(cog,
                evidence, replace_message_id=replace_message_id
            )
            if not owned:
                published = await thread.fetch_message(evidence.message_id)
                await published.edit(view=view)
                continue
            files = _case_timeline_discord_files(batch)
            try:
                published = await thread.send(
                    content,
                    files=files,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                    nonce=_case_publication_nonce(evidence.logical_key),
                )
                await _complete_case_timeline_publication(cog,
                    evidence, published, thread.id
                )
            except BaseException:
                await _release_case_timeline_publication(cog, evidence)
                raise
        existing_evidence = tuple(
            publication
            for publication in await asyncio.to_thread(
                cog._case_store.list_timeline_publications,
                snapshot.case.case_id,
            )
            if publication.kind == "evidence"
            and publication.message_sequence == message.sequence
            and publication.chunk_index >= len(batches)
        )
        for obsolete in existing_evidence:
            if obsolete.state != "published" or obsolete.message_id is None:
                continue
            try:
                published = await thread.fetch_message(obsolete.message_id)
            except discord.NotFound:
                continue
            await published.edit(
                content=(
                    f"Message {message.sequence} attachments: "
                    "No additional attachments"
                ),
                attachments=[],
                view=None,
            )


def _case_timeline_evidence_batches(message, thread):
    upload_limit = getattr(thread, "filesize_limit", None)
    if not isinstance(upload_limit, int) or upload_limit <= 0:
        upload_limit = getattr(getattr(thread, "guild", None), "filesize_limit", None)
    if not isinstance(upload_limit, int) or upload_limit <= 0:
        upload_limit = math.inf
    terminal_statuses = {
        status.value for status in detection_runtime.CaptureStatus
    }
    if any(
        attachment.capture_status not in terminal_statuses
        for attachment in message.attachments
    ):
        return (), (), upload_limit
    batches = []
    batch = []
    oversized = []
    max_batch_files = 10
    for attachment in message.attachments:
        if attachment.capture_status != "captured" or not attachment.evidence_path:
            continue
        path = Path(attachment.evidence_path)
        if not path.is_file():
            continue
        actual_size = path.stat().st_size
        if actual_size > upload_limit:
            oversized.append(attachment)
            continue
        if len(batch) == max_batch_files:
            batches.append(tuple(batch))
            batch = []
        batch.append(attachment)
    if batch:
        batches.append(tuple(batch))
    return tuple(batches), tuple(oversized), upload_limit


async def _publish_detection_case(
    cog,
    case_id: str,
    review_channel_id: int | None,
    *,
    message_sequence: int | None = None,
    skip_if_done: asyncio.Task | None = None,
) -> bool:
    digest = hashlib.blake2b(case_id.encode("utf-8"), digest_size=8).digest()
    lock = cog._detection_publication_locks[
        int.from_bytes(digest, "big") % len(cog._detection_publication_locks)
    ]
    async with lock:
        if skip_if_done is not None and skip_if_done.done():
            return False
        await cog._publish_detection_case_serial(
            case_id,
            review_channel_id,
            message_sequence=message_sequence,
        )
        return True


def _pending_feedback_items(
    feedback_items: tuple[CaseFeedbackItem, ...],
    message_sequence: int | None = None,
) -> tuple[CaseFeedbackItem, ...]:
    return tuple(
        item
        for item in feedback_items
        if item.decision is None
        and (
            message_sequence is None
            or item.message_sequence == message_sequence
        )
    )


async def _publish_detection_case_serial(
    cog,
    case_id: str,
    review_channel_id: int | None,
    *,
    message_sequence: int | None = None,
) -> None:
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    if snapshot is None:
        return
    guild = None
    if (
        review_channel_id is not None
        or snapshot.case.review_channel_id is not None
    ):
        guild = cog.bot.get_guild(snapshot.case.guild_id)
    review_channel = (
        await cog._fetch_text_channel_or_thread(guild, review_channel_id)
        if guild is not None and review_channel_id is not None
        else None
    )
    if review_channel is not None and not isinstance(
        review_channel, discord.TextChannel
    ):
        raise RuntimeError(
            "The configured review destination must be a text channel."
        )
    channel = review_channel
    has_persisted_primary = bool(
        snapshot.case.review_channel_id and snapshot.case.review_message_id
    )
    if channel is None and not has_persisted_primary:
        raise RuntimeError(
            "No configured detection case publication destination is available."
        )
    projection = render_case(snapshot)
    def projection_embed():
        page_embed = discord.Embed(
            title=_(projection.title),
            description=projection.description,
            color=(
                discord.Color.dark_red()
                if projection.needs_attention
                else discord.Color.gold()
            ),
        )
        set_thumbnail = getattr(page_embed, "set_thumbnail", None)
        if projection.thumbnail_url and callable(set_thumbnail):
            set_thumbnail(url=projection.thumbnail_url)
        for field in projection.pages[0]:
            page_embed.add_field(
                name=_(field.name), value=_(field.value), inline=False
            )
        return page_embed

    embed = projection_embed()
    resolved = snapshot.case.status.value in {"resolved", "expired"}
    moderation_actions = projection.moderation_actions
    pending_feedback = _pending_feedback_items(
        projection.feedback_items
    )
    view = DetectionCaseView(
        cog,
        case_id,
        has_image_feedback=bool(pending_feedback),
        feedback_items=pending_feedback,
        resolved=resolved,
        allow_individual=len(pending_feedback) <= 25,
        moderation_actions=moderation_actions,
    )
    cog._case_views[case_id] = view
    existing = None
    if snapshot.case.review_channel_id and snapshot.case.review_message_id and guild is not None:
        old_channel = await cog._fetch_text_channel_or_thread(
            guild, snapshot.case.review_channel_id
        )
        if old_channel is not None:
            try:
                existing = old_channel.get_partial_message(
                    snapshot.case.review_message_id
                )
            except discord.NotFound:
                cleared = await asyncio.to_thread(
                    cog._case_store.clear_review_message,
                    case_id,
                    snapshot.case.review_channel_id,
                    snapshot.case.review_message_id,
                )
                if not cleared:
                    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    if existing is not None:
        try:
            await existing.edit(embed=embed, view=view)
        except discord.NotFound:
            cleared = await asyncio.to_thread(
                cog._case_store.clear_review_message,
                case_id,
                snapshot.case.review_channel_id,
                snapshot.case.review_message_id,
            )
            if not cleared:
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, case_id
                )
        else:
            thread = await _ensure_detection_case_thread(cog, snapshot, existing)
            thread = await _activate_detection_case_thread(thread)
            await _publish_case_timeline(cog,
                snapshot,
                thread,
                resolved=resolved,
                message_sequence=message_sequence,
            )
            if resolved:
                await _finalize_detection_case_thread(thread)
            return
    if channel is None:
        raise RuntimeError(
            "No configured detection case publication destination is available."
        )
    summary_message = None
    token = await asyncio.to_thread(
        cog._case_store.claim_publication, case_id, "primary", datetime.now(timezone.utc)
    )
    if token is not None:
        heartbeat = asyncio.create_task(
            _renew_case_publication_claim(cog, case_id, "primary", token)
        )
        try:
            sent = await channel.send(
                embed=embed,
                view=view,
                nonce=UUID(case_id).int & ((1 << 63) - 1),
            )
            summary_message = sent
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            completed = await asyncio.to_thread(
                cog._case_store.complete_primary_publication,
                case_id, token, channel.id, sent.id,
            )
            if not completed:
                await _compensate_case_publication(cog,
                    case_id, channel.id, sent
                )
                raise RuntimeError("detection case primary publication lease was lost")
            if guild is not None:
                await cog._increment_stat(guild, "reviewed")
        except BaseException:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await asyncio.to_thread(
                cog._case_store.release_publication_claim, case_id, "primary", token
            )
            raise
    else:
        for _attempt in range(20):
            await asyncio.sleep(0)
            snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
            if snapshot is not None and snapshot.case.review_message_id is not None:
                winner_channel = await cog._fetch_text_channel_or_thread(
                    guild, snapshot.case.review_channel_id
                )
                if winner_channel is not None:
                    winner_message = await winner_channel.fetch_message(
                        snapshot.case.review_message_id
                    )
                    await winner_message.edit(embed=embed, view=view)
                    summary_message = winner_message
                break
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    if summary_message is None and snapshot.case.review_message_id is not None:
        destination = await cog._fetch_text_channel_or_thread(
            guild, snapshot.case.review_channel_id
        )
        if destination is not None:
            summary_message = await destination.fetch_message(
                snapshot.case.review_message_id
            )
    if summary_message is None:
        raise RuntimeError("detection case summary publication is unavailable")
    thread = await _ensure_detection_case_thread(cog, snapshot, summary_message)
    thread = await _activate_detection_case_thread(thread)
    await _publish_case_timeline(cog,
        snapshot,
        thread,
        resolved=resolved,
        message_sequence=message_sequence,
    )
    if resolved:
        await _finalize_detection_case_thread(thread)


async def _renew_case_publication_claim(cog, case_id: str, slot: str, token: str) -> None:
    while True:
        await asyncio.sleep(cog._detection_heartbeat_interval_seconds)
        renewed = await asyncio.to_thread(
            cog._case_store.renew_publication_claim,
            case_id,
            slot,
            token,
            datetime.now(timezone.utc),
        )
        if not renewed:
            return


def _case_review_has_permission(interaction: discord.Interaction) -> bool:
    permissions = getattr(getattr(interaction, "user", None), "guild_permissions", None)
    return bool(
        permissions
        and (
            getattr(permissions, "moderate_members", False)
            or getattr(permissions, "manage_messages", False)
            or getattr(permissions, "ban_members", False)
            or getattr(permissions, "kick_members", False)
        )
    )


def _case_review_has_action_permission(
    interaction: discord.Interaction, action: str
) -> bool:
    return action in {"ban", "kick", "ignore"} and _case_review_has_permission(
        interaction
    )


async def _case_review_defer(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer()


async def _dismiss_case_review_prompt(cog, interaction: discord.Interaction) -> None:
    await _case_review_defer(interaction)
    try:
        await interaction.delete_original_response()
    except discord.NotFound:
        pass
    except discord.HTTPException:
        log.warning(
            "Could not dismiss Honeypot ephemeral review prompt",
            exc_info=True,
        )


async def _case_review_error(interaction: discord.Interaction, message: str) -> None:
    response = interaction.response
    if not response.is_done():
        await response.send_message(message, ephemeral=True)
    else:
        await interaction.followup.send(message, ephemeral=True)


async def _case_review_rerender(cog, case_id: str) -> None:
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    if snapshot is None:
        return
    raw_config = await cog.config.guild_from_id(snapshot.case.guild_id).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._publish_detection_case(case_id, guild_settings.review_channel)


async def _case_review_rerender_if_open(cog, case_id: str) -> None:
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    if (
        snapshot is None
        or snapshot.case.status.value in {"resolved", "expired"}
        or snapshot.case.review_message_id is None
    ):
        return
    await cog._case_review_rerender(case_id)


async def _case_review_rerender_safely(cog, case_id: str) -> None:
    try:
        await cog._case_review_rerender_if_open(case_id)
    except Exception as error:
        log.warning(
            "Detection case moderation state could not be published "
            "case=%s error=%s",
            case_id,
            error,
        )


def _schedule_case_review_followup(cog, case_id: str) -> None:
    task = asyncio.create_task(_run_case_review_followup(cog, case_id))
    cog._case_review_tasks.add(task)
    task.add_done_callback(cog._case_review_tasks.discard)


async def _run_case_review_followup(cog, case_id: str) -> None:
    try:
        await cog._execute_case_final_operations(
            case_id,
            datetime.now(timezone.utc),
        )
        await _case_review_rerender_safely(cog, case_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "Detection case review follow-up failed case=%s",
            case_id,
            exc_info=True,
        )


async def _finish_case_review_if_ready(
    cog,
    case_id: str,
    moderator_id: int | None,
    *,
    defer_final_operations: bool = False,
) -> bool:
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    if (
        snapshot is None
        or any(
            attachment.capture_status == "pending"
            for attachment in snapshot.attachments
        )
        or any(item.decision is None for item in case_feedback_items(snapshot))
    ):
        return False
    completed = next(
        (
            operation
            for operation in reversed(snapshot.operations)
            if operation.operation_type
            in MODERATION_SUPERSEDING_TYPES
            and operation.status is OperationStatus.SUCCEEDED
            and operation.result in MODERATION_SUPERSEDING_RESULTS
        ),
        None,
    )
    if completed is None:
        return False
    if snapshot.case.status.value in {"resolving", "resolved"}:
        if defer_final_operations:
            if snapshot.case.status.value == "resolving":
                await asyncio.to_thread(
                    cog._case_store.reconcile_moderator_actions,
                    datetime.now(timezone.utc),
                )
            refreshed = await asyncio.to_thread(
                cog._case_store.get_case,
                case_id,
            )
            return bool(
                refreshed is not None
                and refreshed.case.status.value in {"resolved", "expired"}
            )
        await cog._run_detection_reconciliation()
        refreshed = await asyncio.to_thread(cog._case_store.get_case, case_id)
        return bool(
            refreshed is not None
            and refreshed.case.status.value in {"resolved", "expired"}
        )
    resolution = (
        "kick"
        if completed.result == OPERATION_RESULT_KICK_MISSING
        else completed.result
    )
    if defer_final_operations:
        return await cog.resolve_detection_case(
            case_id,
            resolution,
            completed.actor_id,
            defer_final_operations=True,
        )
    return await cog.resolve_detection_case(
        case_id,
        resolution,
        completed.actor_id,
    )


async def _case_review_bulk_interaction(
    cog,
    interaction: discord.Interaction,
    case_id: str,
    action: str,
    *,
    confirmed: bool = False,
    expected_keys: tuple[AttachmentKey, ...] = (),
) -> bool:
    if not _case_review_has_permission(interaction):
        await _case_review_error(interaction, _("You do not have permission to review this case."))
        return False
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    pending_feedback = _pending_feedback_items(
        case_feedback_items(snapshot) if snapshot is not None else ()
    )
    review_items = (
        tuple(item for item in pending_feedback if item.key in set(expected_keys))
        if confirmed and expected_keys
        else pending_feedback
    )
    try:
        validate_image_review_action(review_items, action)
    except ValueError as error:
        await _case_review_error(
            interaction,
            _(str(error)),
        )
        return False
    if action in {"tp", "fp"} and not confirmed:
        await interaction.response.send_message(
            _("Confirm this bulk image decision."),
            view=DetectionBulkConfirmationView(
                cog,
                case_id,
                action,
                confirm_label=bulk_image_confirmation_label(
                    pending_feedback, action
                ),
                expected_keys=tuple(item.key for item in pending_feedback),
            ),
            ephemeral=True,
        )
        return False
    await _case_review_defer(interaction)
    try:
        await cog._case_review_service.apply_bulk(
            case_id,
            action,
            interaction.user.id,
            expected_keys=expected_keys or None,
        )
        await cog._finish_case_review_if_ready(
            case_id,
            interaction.user.id,
            defer_final_operations=True,
        )
        cog._schedule_case_review_followup(case_id)
        return True
    except (KeyError, ValueError) as error:
        await _case_review_error(interaction, str(error))
        return False


async def _case_review_message_bulk_interaction(
    cog,
    interaction: discord.Interaction,
    case_id: str,
    message_sequence: int,
    action: str,
    *,
    confirmed: bool = False,
    expected_keys: tuple[AttachmentKey, ...] = (),
) -> bool:
    if not _case_review_has_permission(interaction):
        await _case_review_error(
            interaction, _("You do not have permission to review this case.")
        )
        return False
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    pending_feedback = _pending_feedback_items(
        case_feedback_items(snapshot) if snapshot is not None else (),
        message_sequence,
    )
    review_items = (
        tuple(item for item in pending_feedback if item.key in set(expected_keys))
        if confirmed and expected_keys
        else pending_feedback
    )
    try:
        validate_image_review_action(review_items, action)
    except ValueError as error:
        await _case_review_error(
            interaction,
            _(str(error)),
        )
        return False
    if action in {"tp", "fp"} and not confirmed:
        await interaction.response.send_message(
            _("Confirm this message's image decision."),
            view=DetectionBulkConfirmationView(
                cog,
                case_id,
                action,
                message_sequence=message_sequence,
                confirm_label=bulk_image_confirmation_label(
                    pending_feedback, action
                ),
                expected_keys=tuple(item.key for item in pending_feedback),
            ),
            ephemeral=True,
        )
        return False
    await _case_review_defer(interaction)
    try:
        await cog._case_review_service.apply_message(
            case_id,
            message_sequence,
            action,
            interaction.user.id,
            expected_keys=expected_keys or None,
        )
        await cog._finish_case_review_if_ready(
            case_id,
            interaction.user.id,
            defer_final_operations=True,
        )
        cog._schedule_case_review_followup(case_id)
        return True
    except (KeyError, ValueError) as error:
        await _case_review_error(interaction, str(error))
        return False


async def _case_review_moderation_interaction(
    cog,
    interaction: discord.Interaction,
    case_id: str,
    action: str,
    *,
    confirmed: bool = False,
) -> bool:
    if not _case_review_has_action_permission(interaction, action):
        await _case_review_error(
            interaction, _("You do not have permission to review this case.")
        )
        return False
    if action in {"ban", "kick"} and not confirmed:
        snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
        has_unreviewed_images = snapshot is not None and any(
            is_persisted_image_attachment(attachment)
            and (
                attachment.capture_status == "pending"
                or (
                    attachment.capture_status == "captured"
                    and attachment.evidence_path is not None
                    and attachment.learning_decision is None
                )
            )
            for attachment in snapshot.attachments
        )
        if has_unreviewed_images:
            await interaction.response.send_message(
                _(
                    "Some images are still processing or have not been reviewed. "
                    "Continue with moderation now?"
                ),
                view=DetectionModerationConfirmationView(cog, case_id, action),
                ephemeral=True,
            )
            return False
    await _case_review_defer(interaction)
    try:
        if action == "ignore":
            moderated_at = datetime.now(timezone.utc)
            await apply_moderator_ignore(
                cog,
                case_id,
                interaction.user.id,
                moderated_at,
            )
            return True
        if action not in {"ban", "kick"}:
            raise ValueError("unsupported detection case moderation action")
        operation = await asyncio.to_thread(
            cog._case_store.claim_moderator_action,
            case_id,
            action,
            interaction.user.id,
            datetime.now(timezone.utc),
        )
        if operation is None:
            raise ValueError("detection case is already resolving or resolved")
        if operation.operation_type != f"moderator_{action}":
            raise ValueError("another moderator action already owns this case")
        await _case_review_rerender_safely(cog, case_id)
        now = datetime.now(timezone.utc)
        if operation.status.value == "failed" and operation.retry_at is not None:
            now = max(now, operation.retry_at)
        claimed = await asyncio.to_thread(
            cog._case_store.claim_operation, operation.operation_id, now
        )
        if claimed is not None:
            await cog._execute_detection_case_operation(claimed, now)
        snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
        persisted = next(
            (
                item
                for item in snapshot.operations
                if item.operation_id == operation.operation_id
            ),
            None,
        )
        if persisted is None:
            if snapshot.case.status.value in {"resolved", "expired"}:
                return True
            raise ValueError("moderator action result is unavailable")
        if persisted.status not in {
            OperationStatus.PENDING,
            OperationStatus.RUNNING,
            OperationStatus.SUCCEEDED,
        }:
            await _case_review_rerender_safely(cog, case_id)
            raise ValueError("Moderator action failed. Check the maintainer error channel.")
        return True
    except (KeyError, ValueError) as error:
        await _case_review_error(interaction, str(error))
        return False


async def _case_review_attachment_interaction(
    cog, interaction: discord.Interaction, key: AttachmentKey, action: str
) -> None:
    if not _case_review_has_permission(interaction):
        await _case_review_error(interaction, _("You do not have permission to review this case."))
        return
    await _case_review_defer(interaction)
    try:
        await cog._case_review_service.apply_individual(key, action, interaction.user.id)
        await cog._finish_case_review_if_ready(
            key.case_id,
            interaction.user.id,
            defer_final_operations=True,
        )
        cog._schedule_case_review_followup(key.case_id)
    except (KeyError, ValueError) as error:
        await _case_review_error(interaction, str(error))


async def _case_review_individual_prompt(
    cog,
    interaction: discord.Interaction,
    case_id: str,
    *,
    message_sequence: int | None = None,
) -> None:
    if not _case_review_has_permission(interaction):
        await _case_review_error(interaction, _("You do not have permission to review this case."))
        return
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    feedback_items = tuple(
        item
        for item in case_feedback_items(snapshot)
        if item.decision is None
        and (
            message_sequence is None
            or item.message_sequence == message_sequence
        )
    )
    if not feedback_items:
        await _case_review_error(
            interaction, _("No unresolved image evidence remains")
        )
        return
    await interaction.response.send_message(
        _("Choose an image to review."),
        view=DetectionIndividualView(cog, feedback_items),
        ephemeral=True,
    )
