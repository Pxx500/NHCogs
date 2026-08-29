"""Detection: signal collection, the durable operation dispatcher, purge and actions.

The first-observed-message (firstpost) domain is folded in here: it is one of the
detectors on this path and is far under the size that would justify its own module.

Every definition here was a `Honeypot` method. The cog keeps a one-line seam only
where another module or a test must reach the behaviour through `self`; see the
`# Detection seam` comments in `honeypot.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import typing
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

import discord
from redbot.core import commands, modlog
from redbot.core.i18n import Translator
from redbot.core.utils.chat_formatting import box

from ..operational_errors import mark_operational_error_recovered
from . import detection_runtime, imagescan, review_publication
from .case_review import case_feedback_items
from .detection_cases import (
    OPERATION_RESULT_CHANNEL_UNAVAILABLE,
    OPERATION_RESULT_MEMBER_UNAVAILABLE,
    OPERATION_RESULT_SUPERSEDED_BY_MODERATION,
    OPERATION_RESULT_UNSUPPORTED_CHANNEL,
    ActionIntent,
    CaseStatus,
    DeleteStatus,
    DetectionSignal,
    OperationType,
    effective_action,
)
from .effects import (
    EffectRetryDisposition,
    EffectStatus,
    ModerationEffectResult,
    ModerationOrigin,
)
from .message_registry import MESSAGE_REGISTRY_RETENTION_DAYS, MessageRecord
from .operations import executor_operation_policy
from .operations.context import (
    DETECTION_CACHED_PURGE_ATTEMPT_LIMIT,
    DETECTION_FAST_RETRY_LIMIT,
    DETECTION_FAST_RETRY_SECONDS,
    DETECTION_SLOW_RETRY_MINUTES,
    CompletionMode,
    FollowUpKind,
    OperationContext,
    OperationLease,
    OperationOutcome,
    apply_operation_policy,
)
from .settings import (
    BAIT_ACTION_OPTIONS,
    BOOL_OPTIONS,
    CORE_ACTION_OPTIONS,
    DEFAULT_ATTACHMENT_PATTERNS,
    DEFAULT_STATS,
    FALLBACK_ACTION_OPTIONS,
    REVIEW_KICK_FAIL_WARNING_MODES,
    SCAM_KEYWORDS,
    WHITELIST_MODE_OPTIONS,
    GuildSettings,
    WhitelistModeOption,
)

_ = Translator("Honeypot", __file__)
log = logging.getLogger("red.Honeypot")

PURGE_PERMISSION_REQUIREMENTS = (
    ("View Channel", "view_channel"),
    ("Read Message History", "read_message_history"),
    ("Manage Messages", "manage_messages"),
)
POST_BAN_SWEEP_DELAY_SECONDS = 5
PURGE_MIN_RETENTION_SECONDS = 60
PURGE_BACKWARD_MAX_SECONDS = 3600
PURGE_FORWARD_MAX_SECONDS = 300
SPAM_WINDOW_MIN_SECONDS = 3
SPAM_WINDOW_MAX_SECONDS = 60
SPAM_CHANNEL_MIN = 2
SPAM_CHANNEL_MAX = 10

GENERIC_ATTACHMENT_NAME_RE = re.compile(r"^(?:image(?: ?\(\d+\))?|\d+)$", re.IGNORECASE)
ATTACHMENT_ONLY_SCAM_KEYWORDS = {"bro"}
WORD_KEYWORD_RE = re.compile(r"^[\w ]+$")


def missing_purge_permissions(permissions: object) -> list[str]:
    if not bool(getattr(permissions, "view_channel", False)):
        return ["View Channel"]
    return [
        name
        for name, attribute in PURGE_PERMISSION_REQUIREMENTS
        if not bool(getattr(permissions, attribute, False))
    ]


def is_purgeable_message_channel(channel: object) -> bool:
    return callable(getattr(channel, "purge", None))


def keyword_matches_content(keyword: str, content: str) -> bool:
    keyword = keyword.strip().lower()
    if not keyword:
        return False
    if WORD_KEYWORD_RE.fullmatch(keyword):
        return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", content) is not None
    return keyword in content


def matched_scam_keywords(
    keywords: typing.Iterable[str],
    content: str,
    *,
    include_attachment_only: bool = False,
) -> list[str]:
    return [
        keyword
        for keyword in keywords
        if (
            include_attachment_only
            or keyword.strip().lower() not in ATTACHMENT_ONLY_SCAM_KEYWORDS
        )
        and keyword_matches_content(keyword, content)
    ]


def message_spam_fingerprint(message: discord.Message) -> str:
    content = re.sub(r"\s+", " ", message.content.strip().lower())
    attachments = tuple(
        (
            attachment.filename.lower(),
            attachment.size,
            (attachment.content_type or "").lower(),
        )
        for attachment in message.attachments
    )
    raw = repr((content, attachments))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _observe_message(cog, message: discord.Message) -> None:
    if message.webhook_id is not None:
        author_kind = "webhook"
        fingerprint = None
    elif message.author.bot:
        author_kind = "bot"
        fingerprint = None
    else:
        author_kind = "member"
        fingerprint = message_spam_fingerprint(message)
    await cog._message_registry.observe(
        MessageRecord(
            message_id=message.id,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            created_at=message.created_at,
            pinned=bool(getattr(message, "pinned", False)),
            author_kind=author_kind,
            fingerprint=fingerprint,
        )
    )


async def _record_detection_stats(
    cog,
    guild: discord.Guild,
    signals: tuple[DetectionSignal, ...],
    occurred_at: datetime,
) -> None:
    signals = tuple(
        signal
        for signal in signals
        if not signal.metadata.get("whitelist_bypass")
    )
    if not signals:
        return
    await cog._record_daily_stat(guild, occurred_at, "detections")
    await cog._increment_stat(guild, "detections")
    if any(signal.decisive for signal in signals):
        await cog._increment_stat(guild, "suspicious")
    for detector, prefix, catch_key in (
        ("honeypot", "honeypot", "honeypot_catches"),
        ("firstpost", "firstpost", "early_catches"),
        ("spam", "spam", "spam_catches"),
        ("image", "image", "image_catches"),
    ):
        detector_signals = tuple(
            signal for signal in signals if signal.detector == detector
        )
        if not detector_signals:
            continue
        await cog._increment_stat(guild, f"{prefix}_hits")
        if any(signal.decisive for signal in detector_signals):
            await cog._increment_stat(guild, catch_key)
        intents = {signal.action for signal in detector_signals}
        for intent, suffix in (
            (ActionIntent.REVIEW, "reviews"),
            (ActionIntent.KICK, "kicks"),
            (ActionIntent.BAN, "bans"),
        ):
            if intent in intents:
                await cog._increment_stat(guild, f"{prefix}_{suffix}")


async def _init_firstpost_seen_store(cog) -> None:
    await asyncio.to_thread(cog._firstpost_store.initialize)


async def _count_firstpost_seen_authors(cog, guild_id: int) -> int:
    return await asyncio.to_thread(cog._firstpost_store.count, guild_id)


async def _ensure_firstpost_seen_loaded(cog, guild_id: int) -> None:
    if guild_id in cog._firstpost_loaded_guilds:
        return
    async with cog._firstpost_db_lock:
        if guild_id in cog._firstpost_loaded_guilds:
            return
        seen = await asyncio.to_thread(cog._firstpost_store.load_guild, guild_id)
        cog._firstpost_seen_authors[guild_id].update(seen)
        cog._firstpost_loaded_guilds.add(guild_id)


async def _flush_firstpost_seen_authors(cog) -> None:
    async with cog._firstpost_db_lock:
        dirty = {
            guild_id: set(user_ids)
            for guild_id, user_ids in cog._firstpost_dirty_seen_authors.items()
            if user_ids
        }
    if not dirty:
        return
    for guild_id, user_ids in dirty.items():
        await asyncio.to_thread(cog._firstpost_store.flush, guild_id, user_ids)
    async with cog._firstpost_db_lock:
        for guild_id, user_ids in dirty.items():
            remaining = cog._firstpost_dirty_seen_authors.get(guild_id)
            if remaining is None:
                continue
            remaining.difference_update(user_ids)
            if not remaining:
                cog._firstpost_dirty_seen_authors.pop(guild_id, None)


async def _remove_review_mute_role(
    cog,
    member: discord.Member,
    role: discord.Role,
    reason: str,
) -> bool:
    if await cog._is_joinwatch_active_role(member.guild, member.id, role.id):
        return True
    try:
        await member.remove_roles(role, reason=reason)
    except discord.NotFound:
        return True
    except discord.HTTPException:
        return False
    return True


def _format_honeypot_channel_list(cog, guild: discord.Guild, channel_ids: list[int]) -> str:
    if not channel_ids:
        return _("not set")
    return "\n".join(
        f"{index}. {cog._format_channel_setting(guild, channel_id)}"
        for index, channel_id in enumerate(channel_ids, 1)
    )


async def _send_config_dump(
    cog,
    ctx: commands.Context,
    title: str,
    entries: list[tuple[str, typing.Any]],
) -> None:
    lines = [f"{label}: {value}" for label, value in entries]
    await ctx.send(_("{title}:\n").format(title=title) + box("\n".join(lines)))


def _dry_run_label(cog, action: str) -> str:
    if action == "ban":
        return _("Dry run: I would ban this member.")
    if action == "kick":
        return _("Dry run: I would kick this member.")
    return _("Dry run: I would not take action.")


def _ban_delete_message_seconds() -> int:
    return 0


def _missing_action_permission(cog, guild: discord.Guild, action: str) -> str | None:
    me = guild.me
    if me is None:
        return _("**Failed:** I couldn't find my server member.")
    permissions = me.guild_permissions
    if action == "kick" and not permissions.kick_members:
        return _("**Failed:** I do not have the `Kick Members` permission.")
    if action == "ban" and not permissions.ban_members:
        return _("**Failed:** I do not have the `Ban Members` permission.")
    return None


def _missing_role_assignment_permission(cog, guild: discord.Guild, role: discord.Role) -> str | None:
    me = guild.me
    if me is None:
        return _("I couldn't find my server member.")
    if not me.guild_permissions.manage_roles:
        return _("I need `Manage Roles` permission to apply the joinwatch auto-role.")
    if me.top_role <= role:
        return _("My top role must be above the joinwatch auto-role.")
    return None


async def purge_cache_cleanup_loop(cog) -> None:
    try:
        await cog._message_registry.prune(
            datetime.now(timezone.utc) - timedelta(days=MESSAGE_REGISTRY_RETENTION_DAYS)
        )
    except sqlite3.Error:
        log.exception("Message registry retention prune failed")
    _prune_purge_cache(cog)


async def detection_case_loop(cog) -> None:
    await _run_detection_case_expiry(cog)


async def _run_detection_case_expiry(cog) -> None:
    now = datetime.now(timezone.utc)
    due_cases = await asyncio.to_thread(cog._case_store.list_due_cases, now)
    for case in due_cases:
        await cog.resolve_detection_case(case.case_id, "expired", now=now)


async def _run_detection_reconciliation(
    cog, *, now: datetime | None = None
) -> None:
    try:
        await review_publication._retry_detection_orphan_publications(cog)
    except Exception:
        log.warning("Detection orphan publication retry failed", exc_info=True)
    try:
        await review_publication._retry_detection_case_deletions(cog)
    except Exception:
        log.warning("Detection case deletion retry failed", exc_info=True)
    current_time = now or datetime.now(timezone.utc)
    stale_before = current_time - timedelta(minutes=5)
    await asyncio.to_thread(
        cog._case_store.reconcile_moderator_actions, current_time
    )
    operations = await asyncio.to_thread(
        cog._case_store.claim_due_operations,
        current_time,
        50,
        stale_before,
    )
    for operation in operations:
        await cog._execute_detection_case_operation(operation, current_time)
    cases = await asyncio.to_thread(
        cog._case_store.list_reconcilable_cases, current_time, stale_before
    )
    for case in cases:
        await cog.resolve_detection_case(
            case.case_id, "expired", now=current_time
        )


async def resolve_detection_case(
    cog,
    case_id: str,
    resolution: str,
    moderator_id: int | None = None,
    *,
    now: datetime | None = None,
    defer_final_operations: bool = False,
) -> bool:
    resolved_at = now or datetime.now(timezone.utc)
    lease = await asyncio.to_thread(
        cog._case_store.claim_resolution,
        case_id,
        resolved_at,
        resolved_at - timedelta(minutes=5),
        require_terminal_captures=resolution == "ignore",
    )
    if lease is None:
        return False
    try:
        status = CaseStatus.EXPIRED if resolution == "expired" else CaseStatus.RESOLVED
        decision = {
            "tp": "true_positive",
            "fp": "false_positive",
            "ignore": "ignored",
        }.get(resolution.removeprefix("images:"))
        snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
        decisions = (
            {item.key: decision for item in case_feedback_items(snapshot)}
            if snapshot is not None and decision is not None
            else None
        )
        owned_role_ids = await asyncio.to_thread(
            cog._case_store.owned_role_ids, case_id
        )
        final_operations = [
            (OperationType.REVIEW_UPDATE, f"review-update:{case_id}"),
            (OperationType.EVIDENCE_CLEANUP, f"evidence-cleanup:{case_id}"),
        ]
        for role_id in owned_role_ids:
            final_operations.append(
                (
                    OperationType.ROLE_RELEASE,
                    f"role-release:{case_id}:{int(role_id)}",
                )
            )
        finished = await asyncio.to_thread(
            cog._case_store.finish_resolution,
            lease,
            status,
            resolution,
            moderator_id,
            resolved_at,
            decisions=decisions,
            final_operations=tuple(final_operations),
        )
    except BaseException:
        await asyncio.to_thread(cog._case_store.release_resolution, lease)
        raise
    if not finished:
        return False
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    guild = cog.bot.get_guild(snapshot.case.guild_id)
    if guild is not None and resolution == "expired":
        await cog._increment_stat(guild, "review_expired")
    elif guild is not None and resolution == "ignore":
        await cog._increment_stat(guild, "ignored")
    if defer_final_operations:
        return True
    await cog._execute_case_final_operations(case_id, resolved_at)
    return True


async def _execute_case_final_operations(
    cog,
    case_id: str,
    now: datetime,
) -> None:
    snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
    if snapshot is None:
        return
    final_operation_priority: dict[OperationType | str, int] = {
        OperationType.REVIEW_UPDATE: 0,
        OperationType.ROLE_RELEASE: 1,
        OperationType.EVIDENCE_CLEANUP: 2,
    }
    for operation in sorted(
        snapshot.operations,
        key=lambda item: (
            final_operation_priority.get(item.operation_type, 99),
            item.operation_id,
        ),
    ):
        if operation.operation_type not in {
            OperationType.REVIEW_UPDATE,
            OperationType.ROLE_RELEASE,
            OperationType.EVIDENCE_CLEANUP,
        }:
            continue
        claimed = await asyncio.to_thread(
            cog._case_store.claim_operation, operation.operation_id, now
        )
        if claimed is not None:
            await cog._execute_detection_case_operation(claimed, now)


async def _execute_detection_message_child(
    cog,
    snapshot,
    operation_type: OperationType,
    sequence: int,
    now: datetime,
) -> bool:
    operation = next(
        (
            item
            for item in snapshot.operations
            if item.operation_type == operation_type
            and item.message_sequence == sequence
        ),
        None,
    )
    if operation is None:
        return False
    claim_time = now
    if operation.status.value == "failed" and operation.retry_at is not None:
        claim_time = max(claim_time, operation.retry_at)
    claimed = await asyncio.to_thread(
        cog._case_store.claim_operation, operation.operation_id, claim_time
    )
    if claimed is not None:
        await cog._execute_detection_case_operation(
            claimed,
            claim_time,
        )
        return True
    return False


async def _release_detection_case_roles(
    cog, case_id: str, now: datetime
) -> None:
    role_ids = await asyncio.to_thread(cog._case_store.owned_role_ids, case_id)
    for role_id in role_ids:
        operation = await asyncio.to_thread(
            cog._case_store.ensure_operation,
            case_id,
            OperationType.ROLE_RELEASE,
            f"role-release:{case_id}:{int(role_id)}",
        )
        claimed = await asyncio.to_thread(
            cog._case_store.claim_operation, operation.operation_id, now
        )
        if claimed is not None:
            await cog._execute_detection_case_operation(claimed, now)


def _persisted_capture_results(snapshot, sequence: int):
    terminal_statuses = {
        status.value for status in detection_runtime.CaptureStatus
    }
    return tuple(
        detection_runtime.CaptureResult(
            attachment.position,
            detection_runtime.CaptureStatus(attachment.capture_status),
            Path(attachment.evidence_path)
            if attachment.evidence_path is not None
            else None,
            attachment.error,
        )
        for attachment in snapshot.attachments
        if attachment.message_sequence == sequence
        and attachment.capture_status in terminal_statuses
    )


@asynccontextmanager
async def _operation_lease(
    cog, operation
) -> typing.AsyncIterator[OperationLease]:
    heartbeat = asyncio.create_task(cog._renew_detection_operation(operation))
    try:
        yield OperationLease(
            operation_id=operation.operation_id,
            claim_token=operation.claim_token,
        )
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def _settle_detection_operation_failure(
    cog,
    operation,
    lease: OperationLease,
    now: datetime,
    snapshot,
    *,
    outcome: OperationOutcome,
    error: Exception,
    operation_type_value: str,
) -> None:
    retry_at = (
        None
        if outcome.terminal_failure
        else now
        + (
            timedelta(seconds=DETECTION_FAST_RETRY_SECONDS)
            if operation.attempts <= DETECTION_FAST_RETRY_LIMIT
            else timedelta(minutes=DETECTION_SLOW_RETRY_MINUTES)
        )
    )
    if (
        operation.operation_type == OperationType.SOURCE_DELETE
        and not outcome.terminal_failure
    ):
        retry_at = now + timedelta(seconds=DETECTION_FAST_RETRY_SECONDS)
    cached_purge_exhausted = (
        operation.operation_type == OperationType.CACHED_PURGE
        and outcome.result == DeleteStatus.TRANSIENT_FAILURE.value
        and operation.attempts >= DETECTION_CACHED_PURGE_ATTEMPT_LIMIT
    )
    if operation.operation_type == OperationType.CACHED_PURGE and (
        outcome.result
        in (
            DeleteStatus.FORBIDDEN.value,
            OPERATION_RESULT_CHANNEL_UNAVAILABLE,
            OPERATION_RESULT_UNSUPPORTED_CHANNEL,
        )
        or cached_purge_exhausted
    ):
        retry_at = None
        await asyncio.to_thread(
            cog._case_store.mark_case_needs_attention, operation.case_id
        )
    failure = await asyncio.to_thread(
        cog._case_store.fail_operation,
        lease.operation_id,
        lease.claim_token,
        f"{type(error).__name__}: {error}",
        now,
        retry_at,
        result=outcome.result,
        terminal_failure=outcome.terminal_failure,
    )
    if failure and snapshot is not None:
        await cog._record_operational_failure(
            snapshot.case.guild_id,
            operation.operation_type,
            f"{type(error).__name__}: {error}",
            case_id=operation.case_id,
            operation_id=operation.operation_id,
            attempts=operation.attempts,
            terminal=retry_at is None,
        )
    if operation.operation_type == OperationType.ROLE_APPLY:
        await review_publication._case_review_rerender_safely(cog, operation.case_id)
    if operation.operation_type == OperationType.ROLE_APPLY and snapshot is not None:
        failed_guild = cog.bot.get_guild(snapshot.case.guild_id)
        if failed_guild is not None:
            await cog._increment_stat(failed_guild, "pending_mute_failures")
    log.warning(
        "Detection case operation failed case=%s operation=%s kind=%s error=%s",
        operation.case_id,
        operation.operation_id,
        operation_type_value,
        error,
    )


async def _settle_detection_operation_success(
    cog,
    context: OperationContext,
    outcome: OperationOutcome,
) -> OperationOutcome:
    operation = context.operation
    if outcome.completion_mode is CompletionMode.MODERATOR_ACTION:
        completed = await asyncio.to_thread(
            cog._case_store.complete_moderator_action,
            context.lease.operation_id,
            context.lease.claim_token,
            context.now,
            outcome.result,
        )
    else:
        completed = await asyncio.to_thread(
            cog._case_store.complete_operation,
            context.lease.operation_id,
            context.lease.claim_token,
            context.now,
            outcome.result,
        )
    if not completed:
        current_case = await asyncio.to_thread(
            cog._case_store.get_case, operation.case_id
        )
        if current_case is not None:
            raise RuntimeError(
                "detection case operation lease was lost before completion"
            )
    elif context.snapshot is not None and (
        operation.attempts > 1 or outcome.resolve_failure_on_first_attempt
    ):
        await mark_operational_error_recovered(
            cog.bot,
            guild_id=context.snapshot.case.guild_id,
            source="Honeypot",
            action=operation.operation_type.value,
            correlation_key=operation.operation_id,
        )
    elif outcome.role_was_added and context.snapshot is not None:
        guild = cog.bot.get_guild(context.snapshot.case.guild_id)
        if guild is not None:
            await cog._increment_stat(guild, "pending_mutes")
    return replace(outcome, completed=completed)


async def _run_detection_operation_follow_ups(
    cog,
    context: OperationContext,
    outcome: OperationOutcome,
) -> None:
    operation = context.operation
    for follow_up in outcome.follow_ups:
        if follow_up.requires_completion and not outcome.completed:
            continue
        if follow_up.kind is FollowUpKind.ROLE_APPLY_RERENDER:
            if operation.attempts > 1 or outcome.result in {
                OPERATION_RESULT_SUPERSEDED_BY_MODERATION,
                OPERATION_RESULT_MEMBER_UNAVAILABLE,
            }:
                await review_publication._case_review_rerender_safely(cog, operation.case_id)
        elif follow_up.kind is FollowUpKind.COMPACT_TERMINAL_CASE:
            await asyncio.to_thread(
                cog._case_store.compact_terminal_case, operation.case_id
            )
        elif follow_up.kind is FollowUpKind.FINISH_MODERATION:
            await cog._finish_case_review_if_ready(
                operation.case_id,
                operation.actor_id,
            )
            await review_publication._case_review_rerender_safely(cog, operation.case_id)
        elif follow_up.kind is FollowUpKind.FINISH_MESSAGE_PROCESS:
            await cog._finish_case_review_if_ready(operation.case_id, None)


async def _execute_detection_case_operation(
    cog,
    operation,
    now: datetime,
    *,
    live_message=None,
    timings: dict[str, float] | None = None,
) -> None:
    lease_context = _operation_lease(cog, operation)
    lease = await lease_context.__aenter__()
    operation_type_value = (
        operation.operation_type.value
        if isinstance(operation.operation_type, OperationType)
        else operation.operation_type
    )
    snapshot = None
    context = None
    operation_outcome = OperationOutcome()
    operation_error = None
    cancellation = None
    try:
        snapshot = await asyncio.to_thread(cog._case_store.get_case, operation.case_id)
        if snapshot is None:
            return
        context = OperationContext(
            operation=operation,
            snapshot=snapshot,
            lease=lease,
            now=now,
            live_message=live_message,
            timings=timings,
        )
        operation_policy = executor_operation_policy(operation.operation_type)
        handler = (
            cog._detection_operation_handlers.resolve(operation.operation_type)
            if operation_policy is not None
            else None
        )
        if handler is None:
            raise RuntimeError(
                "unsupported detection case operation: "
                f"{operation_type_value}"
            )
        operation_outcome = apply_operation_policy(
            await handler(cog, context), operation_policy
        )
        if operation_outcome.error is not None:
            raise operation_outcome.error
    except asyncio.CancelledError as error:
        cancellation = error
    except Exception as error:
        operation_error = error
    finally:
        await lease_context.__aexit__(None, None, None)
    if cancellation is not None:
        raise cancellation
    if operation_error is not None:
        await _settle_detection_operation_failure(
            cog,
            operation,
            lease,
            now,
            snapshot,
            outcome=replace(operation_outcome, error=operation_error),
            error=operation_error,
            operation_type_value=operation_type_value,
        )
        return
    operation_outcome = await _settle_detection_operation_success(
        cog,
        context,
        operation_outcome,
    )
    await _run_detection_operation_follow_ups(cog, context, operation_outcome)


async def _renew_detection_operation(cog, operation) -> None:
    while True:
        await asyncio.sleep(cog._detection_heartbeat_interval_seconds)
        renewed = await asyncio.to_thread(
            cog._case_store.renew_operation_claim,
            operation.operation_id,
            operation.claim_token,
            datetime.now(timezone.utc),
        )
        if not renewed:
            return


def _forward_purge_signal(cog, message: discord.Message) -> DetectionSignal | None:
    if not cog._is_forward_purge_active(message.guild.id, message.author.id):
        return None
    return DetectionSignal(
        detector="forward_purge",
        reason="Active forward-purge containment window",
        action=ActionIntent.REVIEW,
        decisive=True,
        metadata={"containment_required": True},
    )


def _signal_action(value: object, valid_actions: tuple[str, ...]) -> ActionIntent:
    action = value if value in valid_actions else "review"
    return ActionIntent(typing.cast(str, action))


async def _spam_signal(
    cog, message: discord.Message, guild_settings: GuildSettings
) -> DetectionSignal | None:
    if not guild_settings.spam_enabled:
        return None
    reasons = await cog._spam_suspicion_reasons(message, guild_settings)
    if not reasons:
        return None
    return DetectionSignal(
        detector="spam",
        reason="\n".join(reasons),
        action=_signal_action(
            guild_settings.spam_action.value, CORE_ACTION_OPTIONS
        ),
        decisive=True,
        metadata={"reasons": tuple(reasons)},
    )


async def _firstpost_signal(
    cog, message: discord.Message, guild_settings: GuildSettings
) -> DetectionSignal | None:
    firstpost_enabled = guild_settings.firstpost_enabled
    collect_enabled = guild_settings.firstpost_collect_enabled
    if not firstpost_enabled and not collect_enabled:
        return None
    await _ensure_firstpost_seen_loaded(cog, message.guild.id)
    if message.author.id in cog._firstpost_seen_authors[message.guild.id]:
        return None
    if not firstpost_enabled:
        return None
    reasons = _firstpost_suspicion_reasons(message, guild_settings)
    if not reasons:
        return None
    return DetectionSignal(
        detector="firstpost",
        reason="\n".join(reasons),
        action=_signal_action(
            guild_settings.firstpost_action.value, CORE_ACTION_OPTIONS
        ),
        decisive=True,
        metadata={"reasons": tuple(reasons)},
    )


def _firstpost_candidate(
    cog, message: discord.Message, guild_settings: GuildSettings
) -> DetectionSignal | None:
    if not guild_settings.firstpost_enabled:
        return None
    reasons = _firstpost_suspicion_reasons(message, guild_settings)
    if not reasons:
        return None
    return DetectionSignal(
        detector="firstpost",
        reason="\n".join(reasons),
        action=_signal_action(
            guild_settings.firstpost_action.value, CORE_ACTION_OPTIONS
        ),
        decisive=True,
        metadata={"reasons": tuple(reasons)},
    )


async def _honeypot_signals(
    cog,
    message: discord.Message,
    guild_settings: GuildSettings,
    *,
    image_evidence: DetectionSignal | None = None,
) -> tuple[DetectionSignal, ...]:
    if message.channel.id not in guild_settings.honeypot_channels:
        return ()
    whitelisted_role_ids = set(guild_settings.whitelisted_roles)
    has_whitelist_role = any(
        role.id in whitelisted_role_ids for role in message.author.roles
    )
    whitelist_mode = guild_settings.whitelist_mode if has_whitelist_role else None
    if whitelist_mode is WhitelistModeOption.BYPASS:
        return (
            DetectionSignal(
                detector="honeypot",
                reason="Message posted in a configured honeypot channel",
                action=ActionIntent.NONE,
                decisive=True,
                metadata={"whitelist_bypass": True},
            ),
        )
    reasons = await cog._suspicion_reasons(message, guild_settings)
    if image_evidence is not None:
        reasons.append(_("Known suspicious image match"))
    second_strike_role_ids = {
        role_id
        for role_id in (
            guild_settings.mute_role,
            guild_settings.joinwatch_auto_role_id,
        )
        if role_id
    }
    second_strike = bool(second_strike_role_ids) and any(
        role.id in second_strike_role_ids for role in message.author.roles
    )
    if second_strike:
        reasons.append(_("Repeat honeypot activity"))
    force_review = whitelist_mode is WhitelistModeOption.REVIEW
    force_fallback = whitelist_mode is WhitelistModeOption.FALLBACK
    if second_strike and not force_review and not force_fallback:
        action = ActionIntent.BAN
    elif force_review:
        action = ActionIntent.REVIEW
    elif force_fallback or not reasons:
        action = _signal_action(
            guild_settings.fallback_action.value, FALLBACK_ACTION_OPTIONS
        )
    else:
        action = _signal_action(
            (
                guild_settings.action.value
                if guild_settings.action is not None
                else None
            ),
            CORE_ACTION_OPTIONS,
        )
    return (
        DetectionSignal(
            detector="honeypot",
            reason="\n".join(reasons) if reasons else "Message posted in a configured honeypot channel",
            action=action,
            decisive=True,
            metadata={
                "reasons": tuple(reasons),
                "second_strike": second_strike,
                "force_review": force_review,
                "force_fallback": force_fallback,
            },
        ),
    )


async def _collect_detection_signals(
    cog, message: discord.Message, guild_settings: GuildSettings
) -> tuple[DetectionSignal, ...]:
    forward = _forward_purge_signal(cog, message)
    signals: list[DetectionSignal] = []
    if forward is not None:
        signals.append(forward)
    in_honeypot = (
        message.channel.id
        in guild_settings.honeypot_channels
    )
    if in_honeypot:
        image = None
        if not any(signal.decisive for signal in signals):
            image = await cog._initial_image_signal(
                message,
                guild_settings,
                action_override=ActionIntent.NONE,
            )
        signals.extend(
            await _honeypot_signals(
                cog,
                message,
                guild_settings,
                image_evidence=image,
            )
        )
        if image is not None:
            signals.append(image)
    else:
        spam = await _spam_signal(cog, message, guild_settings)
        if spam is not None:
            signals.append(spam)
        firstpost = await _firstpost_signal(cog, message, guild_settings)
        if firstpost is not None:
            signals.append(firstpost)
        if not any(signal.decisive for signal in signals):
            image = await cog._initial_image_signal(message, guild_settings)
            if image is not None:
                signals.append(image)
    return tuple(signals)


def _public_moderation_reason(
    signals: tuple[DetectionSignal, ...], action: ActionIntent
) -> str:
    owning_signal = next(
        (signal for signal in signals if signal.action is action),
        next((signal for signal in signals if signal.decisive), None),
    )
    if owning_signal is None:
        return "Honeypot"
    if owning_signal.detector == "spam":
        return "Same message in multiple channels"
    if owning_signal.detector == "firstpost":
        return "Suspicious first observed message."
    if owning_signal.detector == "image":
        return "Honeypot"
    if owning_signal.detector == "honeypot":
        if owning_signal.metadata.get("review_fallback"):
            return "Message in the honeypot channel without a matching scam pattern."
        if owning_signal.metadata.get("second_strike"):
            return "Suspicious Activity"
        if owning_signal.metadata.get("reasons") and not owning_signal.metadata.get(
            "force_fallback"
        ):
            return "Suspicious message in the honeypot channel."
        return "Message in the honeypot channel without a matching scam pattern."
    return "Honeypot"


def _resolve_unavailable_review_signals(
    guild_settings: GuildSettings,
    signals: tuple[DetectionSignal, ...],
) -> tuple[DetectionSignal, ...]:
    review_available = bool(
        guild_settings.review_enabled
        and guild_settings.review_channel is not None
    )
    if review_available:
        return signals
    fallback = _signal_action(
        guild_settings.fallback_action.value, FALLBACK_ACTION_OPTIONS
    )
    if fallback is ActionIntent.REVIEW:
        fallback = ActionIntent.NONE
    return tuple(
        DetectionSignal(
            signal.detector,
            signal.reason,
            (
                fallback
                if signal.detector == "honeypot"
                and signal.action is ActionIntent.REVIEW
                and not signal.metadata.get("containment_required")
                else signal.action
            ),
            signal.decisive,
            (
                {**signal.metadata, "review_fallback": True}
                if signal.detector == "honeypot"
                and signal.action is ActionIntent.REVIEW
                else signal.metadata
            ),
        )
        for signal in signals
    )


async def _process_detected_message(
    cog,
    message: discord.Message,
    guild_settings: GuildSettings,
    signals: tuple[DetectionSignal, ...],
    *,
    timings: dict[str, float] | None = None,
    admission_lock: asyncio.Lock | None = None,
) -> None:
    timings = timings if timings is not None else {}
    signals = _resolve_unavailable_review_signals(guild_settings, signals)
    role_id = guild_settings.mute_role

    def initial_operations(owned_signals):
        action = effective_action(owned_signals)
        whitelist_bypass = bool(owned_signals) and all(
            signal.metadata.get("whitelist_bypass") for signal in owned_signals
        )
        publish_review = guild_settings.review_enabled and not whitelist_bypass
        containment = any(
            signal.action != ActionIntent.NONE
            or (
                signal.detector == "honeypot"
                and not signal.metadata.get("whitelist_bypass")
            )
            or signal.metadata.get("containment_required")
            for signal in owned_signals
        )
        operations = []
        if publish_review:
            operations.append(
                (
                    OperationType.REVIEW_PUBLISH,
                    "review_publish:{case_id}:{sequence}",
                )
            )
        if containment or message.attachments:
            operations.append(
                (
                    OperationType.MESSAGE_PROCESS,
                    "message-process:{case_id}:{sequence}",
                )
            )
        if action in {ActionIntent.KICK, ActionIntent.BAN}:
            operations.append(
                (
                    OperationType.MODERATION_ACTION,
                    f"moderation_action:{{case_id}}:{{sequence}}:{action.value}",
                )
            )
        if (
            role_id is not None
            and action is ActionIntent.REVIEW
            and not guild_settings.dry_run
        ):
            operations.append(
                (
                    OperationType.ROLE_APPLY,
                    f"role-apply:{{case_id}}:{int(role_id)}",
                )
            )
        return tuple(operations)

    tracking_firstpost = (
        guild_settings.firstpost_enabled
        or guild_settings.firstpost_collect_enabled
    )
    admission_started = perf_counter()
    try:
        append = await asyncio.to_thread(
            cog._case_store.append_message,
            review_publication._new_case_message(message),
            signals,
            initial_operations,
            claim_firstpost=tracking_firstpost,
        )
    finally:
        if admission_lock is not None:
            admission_lock.release()
    timings["admission_ms"] = (perf_counter() - admission_started) * 1000
    if append is None:
        cog._firstpost_seen_authors[message.guild.id].add(message.author.id)
        return
    if tracking_firstpost:
        cog._firstpost_seen_authors[message.guild.id].add(message.author.id)
        if append.firstpost_claimed:
            cog._firstpost_dirty_seen_authors[message.guild.id].add(
                message.author.id
            )
            await cog._increment_stat(message.guild, "firstpost_seen")
    admitted_snapshot = await asyncio.to_thread(
        cog._case_store.get_case, append.case.case_id
    )
    persisted_signals = tuple(
        item.signal
        for item in admitted_snapshot.signals
        if item.message_sequence == append.message.sequence
    )
    if append.message_created:
        await _record_detection_stats(
            cog,
            message.guild,
            persisted_signals,
            message.created_at,
        )
    if append.message_created and any(
        signal.metadata.get("whitelist_bypass") for signal in persisted_signals
    ):
        await cog._increment_stat(message.guild, "whitelisted")
    durable_operations = initial_operations(persisted_signals)
    if not append.message_created:
        for operation_type, idempotency_key in durable_operations:
            await asyncio.to_thread(
                cog._case_store.ensure_operation,
                append.case.case_id,
                operation_type,
                idempotency_key.format(
                    case_id=append.case.case_id,
                    sequence=append.message.sequence,
                ),
                append.message.sequence,
            )
        admitted_snapshot = await asyncio.to_thread(
            cog._case_store.get_case, append.case.case_id
        )
    pipeline_operation = next(
        (
            operation
            for operation in admitted_snapshot.operations
            if operation.operation_type == OperationType.MESSAGE_PROCESS
            and operation.message_sequence == append.message.sequence
        ),
        None,
    )
    pipeline_claim = (
        await asyncio.to_thread(
            cog._case_store.claim_operation,
            pipeline_operation.operation_id,
            datetime.now(timezone.utc),
        )
        if pipeline_operation is not None
        else None
    )
    if pipeline_operation is not None:
        if pipeline_claim is None:
            if (
                not append.message_created
                and pipeline_operation.status.value == "succeeded"
            ):
                for child_type in (
                    OperationType.MODERATION_ACTION,
                    OperationType.ROLE_APPLY,
                    OperationType.REVIEW_PUBLISH,
                ):
                    await cog._execute_detection_message_child(
                        admitted_snapshot,
                        child_type,
                        append.message.sequence,
                        datetime.now(timezone.utc),
                    )
                if any(
                    operation.operation_type == OperationType.REVIEW_PUBLISH
                    and operation.message_sequence == append.message.sequence
                    for operation in admitted_snapshot.operations
                ):
                    await cog._publish_detection_case(
                        append.case.case_id,
                        guild_settings.review_channel,
                    )
            return
        await cog._execute_detection_case_operation(
            pipeline_claim,
            datetime.now(timezone.utc),
            live_message=message,
            timings=timings,
        )
        return

    review_operation = next(
        (
            operation
            for operation in admitted_snapshot.operations
            if operation.operation_type == OperationType.REVIEW_PUBLISH
            and operation.message_sequence == append.message.sequence
        ),
        None,
    )
    if review_operation is not None:
        review_claim = await asyncio.to_thread(
            cog._case_store.claim_operation,
            review_operation.operation_id,
            datetime.now(timezone.utc),
        )
        if review_claim is not None:
            await cog._execute_detection_case_operation(
                review_claim,
                datetime.now(timezone.utc),
            )
        elif (
            not append.message_created
            and review_operation.status.value == "succeeded"
        ):
            await cog._publish_detection_case(
                append.case.case_id,
                guild_settings.review_channel,
            )
    return


async def _suspicion_reasons(
    cog, message: discord.Message, guild_settings: GuildSettings
) -> list[str]:
    reasons: list[str] = []
    content = message.content.lower()
    if message.author.created_at > datetime.now(timezone.utc) - timedelta(days=7):
        reasons.append(_("Account is under 7 days old"))
    scam_keywords = guild_settings.scam_keywords
    matched_keywords = matched_scam_keywords(scam_keywords, content)
    if matched_keywords:
        reasons.append(_("Matched keywords: {keywords}").format(keywords=", ".join(matched_keywords[:5])))
    if message.attachments and message.author.created_at > datetime.now(timezone.utc) - timedelta(days=14):
        reasons.append(_("Attachment from an account under 14 days old"))
    image_attachment_count = sum(1 for attachment in message.attachments if imagescan.is_image_attachment(attachment))
    if image_attachment_count >= 4:
        reasons.append(_("Multiple image attachments: {count}").format(count=image_attachment_count))
    attachment_patterns = guild_settings.attachment_patterns
    filename_bases = [attachment.filename.rsplit(".", 1)[0].lower() for attachment in message.attachments]
    generic_attachment_count = sum(1 for filename_base in filename_bases if GENERIC_ATTACHMENT_NAME_RE.fullmatch(filename_base))
    if generic_attachment_count >= 2:
        reasons.append(_("Multiple generic attachment names: {count}").format(count=generic_attachment_count))
    matched_patterns: list[str] = []
    matched_attachment_indexes: set[int] = set()
    for pattern in attachment_patterns:
        try:
            matches = [
                index
                for index, filename_base in enumerate(filename_bases)
                if re.fullmatch(pattern, filename_base, flags=re.IGNORECASE)
            ]
        except re.error:
            continue
        if matches:
            matched_attachment_indexes.update(matches)
            matched_patterns.append(pattern)
    if len(matched_attachment_indexes) >= 2 and matched_patterns:
        reasons.append(_("Matched attachment rules: {patterns}").format(patterns=", ".join(matched_patterns[:3])))
    return reasons


def _purge_backward_seconds(value: int) -> int:
    return max(PURGE_MIN_RETENTION_SECONDS, min(value, PURGE_BACKWARD_MAX_SECONDS))


def _purge_forward_seconds(value: int) -> int:
    return max(0, min(value, PURGE_FORWARD_MAX_SECONDS))


def _purge_retention_seconds(purge_backward_seconds: int | None = None) -> int:
    if purge_backward_seconds is None:
        return PURGE_MIN_RETENTION_SECONDS
    return max(
        PURGE_MIN_RETENTION_SECONDS,
        _purge_backward_seconds(purge_backward_seconds),
    )


def _prune_purge_cache(cog) -> None:
    now = datetime.now(timezone.utc)
    for guild_id, users in list(cog._hot_purge_users.items()):
        for user_id, expires_at in list(users.items()):
            if expires_at <= now:
                users.pop(user_id, None)
        if not users:
            cog._hot_purge_users.pop(guild_id, None)


def _activate_forward_purge(
    cog, guild_id: int, user_id: int, purge_forward_seconds: int
) -> None:
    forward_seconds = _purge_forward_seconds(purge_forward_seconds)
    if forward_seconds <= 0:
        cog._deactivate_forward_purge(guild_id, user_id)
        return
    cog._hot_purge_users[guild_id][user_id] = datetime.now(timezone.utc) + timedelta(
        seconds=forward_seconds
    )


def _deactivate_forward_purge(cog, guild_id: int, user_id: int) -> None:
    users = cog._hot_purge_users.get(guild_id)
    if users is not None:
        users.pop(user_id, None)


def _is_forward_purge_active(cog, guild_id: int, user_id: int) -> bool:
    expires_at = cog._hot_purge_users.get(guild_id, {}).get(user_id)
    if expires_at is None:
        return False
    if expires_at <= datetime.now(timezone.utc):
        cog._hot_purge_users[guild_id].pop(user_id, None)
        return False
    return True


def _get_cached_message_channel(
    cog, guild: discord.Guild, channel_id: int
) -> typing.Any | None:
    return guild.get_channel(channel_id) or guild.get_thread(channel_id)


async def _delete_cached_message_ref(cog, guild: discord.Guild, user_id: int, ref) -> bool:
    channel = cog._get_cached_message_channel(guild, ref.channel_id)
    if channel is None:
        return False
    get_partial_message = getattr(channel, "get_partial_message", None)
    if not callable(get_partial_message):
        return False
    try:
        await get_partial_message(ref.message_id).delete()
        await cog._message_registry.forget(ref.message_id)
        return True
    except discord.NotFound:
        await cog._message_registry.forget(ref.message_id)
        return False
    except (discord.Forbidden, discord.HTTPException) as exc:
        await cog._record_operational_failure(
            guild.id,
            "cached_message_deletion",
            f"{type(exc).__name__}: {exc}",
        )
        log.debug(
            "Failed to delete cached message %s for user %s in channel %s: %r",
            ref.message_id,
            user_id,
            ref.channel_id,
            exc,
        )
        return False


async def _delete_recent_cached_user_messages(
    cog,
    guild: discord.Guild,
    user_id: int,
    *,
    exclude_message_id: int | None = None,
    retention_seconds: int = PURGE_MIN_RETENTION_SECONDS,
) -> int:
    refs = await cog._message_registry.recent_by_author(
        guild.id,
        user_id,
        since_utc=datetime.now(timezone.utc) - timedelta(seconds=retention_seconds),
        exclude_message_id=exclude_message_id,
    )
    deleted = 0
    for ref in refs:
        if await _delete_cached_message_ref(cog, guild, user_id, ref):
            deleted += 1
    return deleted


async def _cached_purge_user_messages(
    cog,
    guild: discord.Guild,
    user_id: int,
    guild_settings: GuildSettings,
    *,
    exclude_message_id: int | None = None,
) -> int:
    deleted = await _delete_recent_cached_user_messages(
        cog,
        guild,
        user_id,
        exclude_message_id=exclude_message_id,
        retention_seconds=_purge_retention_seconds(
            guild_settings.purge_backward_seconds
        ),
    )
    _activate_forward_purge(
        cog,
        guild.id,
        user_id,
        guild_settings.purge_forward_seconds,
    )
    return deleted


def _schedule_post_ban_sweep(cog, guild: discord.Guild, user_id: int) -> None:
    """After a ban, delete recent cached messages that Discord may have missed."""
    task = cog.bot.loop.create_task(
        _post_ban_message_sweep(cog, guild.id, user_id),
        name=f"honeypot-post-ban-sweep-{guild.id}-{user_id}",
    )
    cog._post_ban_sweep_tasks.add(task)
    task.add_done_callback(cog._post_ban_sweep_tasks.discard)


async def _post_ban_message_sweep(cog, guild_id: int, user_id: int) -> None:
    try:
        await asyncio.sleep(POST_BAN_SWEEP_DELAY_SECONDS)
        guild = cog.bot.get_guild(guild_id)
        if guild is None:
            return
        raw_config = await cog.config.guild(guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        deleted = await cog._cached_purge_user_messages(
            guild, user_id, guild_settings
        )
        if deleted:
            await cog._increment_stat(guild, "purged_messages", deleted)
            await cog._increment_stat(guild, "cached_purge_deletes", deleted)
    except Exception as error:
        await cog._record_operational_failure(
            guild_id,
            "post_ban_cached_purge",
            f"{type(error).__name__}: {error}",
        )
        log.exception(
            "Post-ban cached message purge failed for user %s in guild %s",
            user_id,
            guild_id,
        )


async def _purge_detection_case_cached_messages(
    cog,
    guild: discord.Guild,
    user_id: int,
    guild_settings: GuildSettings,
    *,
    case_id: str,
    message_sequence: int,
    exclude_message_id: int | None = None,
) -> int:
    retention_seconds = _purge_retention_seconds(
        guild_settings.purge_backward_seconds
    )
    refs = await cog._message_registry.recent_by_author(
        guild.id,
        user_id,
        since_utc=datetime.now(timezone.utc) - timedelta(seconds=retention_seconds),
        exclude_message_id=exclude_message_id,
    )
    deleted = 0
    for ref in refs:
        operation = await asyncio.to_thread(
            cog._case_store.ensure_operation,
            case_id,
            OperationType.CACHED_PURGE,
            f"cached_purge:{case_id}:{ref.channel_id}:{ref.message_id}",
            message_sequence,
        )
        was_deleted = (
            operation.status.value == "succeeded"
            and operation.result == DeleteStatus.DELETED.value
        )
        now = datetime.now(timezone.utc)
        if operation.status.value == "failed" and operation.retry_at is not None:
            now = max(now, operation.retry_at)
        claimed = await asyncio.to_thread(
            cog._case_store.claim_operation, operation.operation_id, now
        )
        if claimed is not None:
            if guild_settings.dry_run:
                await asyncio.to_thread(
                    cog._case_store.complete_operation,
                    claimed.operation_id,
                    claimed.claim_token,
                    now,
                    "planned",
                )
            else:
                await cog._execute_detection_case_operation(claimed, now)
        snapshot = await asyncio.to_thread(cog._case_store.get_case, case_id)
        persisted = next(
            item
            for item in snapshot.operations
            if item.operation_id == operation.operation_id
        )
        if persisted.result == DeleteStatus.DELETED.value and not was_deleted:
            deleted += 1
    if not guild_settings.dry_run:
        _activate_forward_purge(
            cog,
            guild.id,
            user_id,
            guild_settings.purge_forward_seconds,
        )
    return deleted


def _firstpost_suspicion_reasons(
    message: discord.Message, guild_settings: GuildSettings
) -> list[str]:
    attachment_count = len(message.attachments)
    reasons: list[str] = []
    content = message.content.strip().lower()
    if attachment_count == 4:
        reasons.append(_("First post with four attachments"))
    elif attachment_count == 2:
        scam_keywords = guild_settings.scam_keywords
        matched_keywords = matched_scam_keywords(
            scam_keywords,
            content,
            include_attachment_only=True,
        )
        if matched_keywords:
            reasons.append(
                _("First post with two attachments and keywords: {keywords}").format(
                    keywords=", ".join(matched_keywords[:5])
                )
            )
    return reasons


async def _spam_suspicion_reasons(
    cog, message: discord.Message, guild_settings: GuildSettings
) -> list[str]:
    window_seconds = guild_settings.spam_window_seconds or 10
    window_seconds = max(SPAM_WINDOW_MIN_SECONDS, min(window_seconds, SPAM_WINDOW_MAX_SECONDS))
    min_channels = guild_settings.spam_min_channels or 2
    min_channels = max(SPAM_CHANNEL_MIN, min(min_channels, SPAM_CHANNEL_MAX))
    content = message.content.strip().lower()
    scam_keywords = guild_settings.scam_keywords
    has_signal = bool(message.attachments) or bool(matched_scam_keywords(scam_keywords, content))
    if not has_signal:
        return []
    current_fingerprint = message_spam_fingerprint(message)
    cutoff = message.created_at - timedelta(seconds=window_seconds)
    try:
        channel_count = await cog._message_registry.matching_channel_count(
            message.guild.id,
            message.author.id,
            current_fingerprint,
            since_utc=cutoff,
        )
    except Exception as error:
        log.exception("Message registry spam lookup failed")
        try:
            await cog._record_operational_failure(
                message.guild.id,
                "message_registry_spam_lookup",
                f"{type(error).__name__}: {error}",
            )
        except Exception:
            log.exception("Failed to record message registry spam lookup error")
        return []
    if channel_count < min_channels:
        return []
    return [
        _("Same message in {count} channels within {seconds}s").format(
            count=channel_count,
            seconds=window_seconds,
        )
    ]


async def _execute_action(
    cog,
    guild: discord.Guild,
    member: discord.Member | discord.User | discord.Object,
    created_at: datetime,
    settings: GuildSettings,
    *,
    reason: str,
    origin: ModerationOrigin,
    action: str | None = None,
    moderator: discord.Member | discord.User | discord.Object | None = None,
) -> ModerationEffectResult:
    """Execute the configured action (kick/ban) against a guild member.
    Return the effect outcome, including whether the punishment itself succeeded.
    """
    action = action or (
        settings.action.value if settings.action is not None else None
    )
    if action not in ("kick", "ban"):
        return ModerationEffectResult(
            _("No action configured"),
            None,
            EffectStatus.NOT_CONFIGURED,
        )
    if not await cog._punitive_effect_allowed(guild):
        return ModerationEffectResult(
            cog._dry_run_label(action),
            None,
            EffectStatus.PLANNED,
        )
    missing_permission = cog._missing_action_permission(guild, action)
    if missing_permission is not None:
        await cog._increment_stat(guild, "failed_actions")
        return ModerationEffectResult(
            None,
            missing_permission,
            EffectStatus.FAILED,
        )
    try:
        if action == "kick":
            _activate_forward_purge(
                cog,
                guild.id,
                member.id,
                settings.purge_forward_seconds,
            )
            try:
                await member.kick(reason=reason)
            except discord.NotFound:
                if cog._automated_kick_fail_warning_enabled(
                    settings.automated_kick_fail_warning
                ):
                    cog._deactivate_forward_purge(guild.id, member.id)
                    label, failed_message = await cog._create_kick_fail_warning(
                        guild, member.id
                    )
                    return ModerationEffectResult(
                        label,
                        failed_message,
                        EffectStatus.FAILED,
                        retry_disposition=EffectRetryDisposition.TERMINAL,
                    )
                raise
            await cog._increment_stat(guild, "kicked")
        elif action == "ban":
            _activate_forward_purge(
                cog,
                guild.id,
                member.id,
                settings.purge_forward_seconds,
            )
            delete_message_seconds = cog._ban_delete_message_seconds()
            member_ban = getattr(member, "ban", None)
            if callable(member_ban):
                await member_ban(
                    reason=reason,
                    delete_message_seconds=delete_message_seconds,
                )
            else:
                await guild.ban(
                    member,
                    reason=reason,
                    delete_message_seconds=delete_message_seconds,
                )
            cog._schedule_post_ban_sweep(guild, member.id)
            daily_metric = (
                "automated_bans"
                if origin is ModerationOrigin.AUTOMATIC
                else "manual_bans"
            )
            await cog._record_daily_stat(
                guild,
                datetime.now(timezone.utc),
                daily_metric,
            )
            await cog._increment_stat(guild, "banned")
    except discord.HTTPException as e:
        cog._deactivate_forward_purge(guild.id, member.id)
        await cog._increment_stat(guild, "failed_actions")
        return ModerationEffectResult(
            None,
            _("**Action failed:**\n") + box(str(e), lang="py"),
            EffectStatus.FAILED,
        )
    modlog_failed = False
    try:
        await modlog.create_case(
            cog.bot,
            guild,
            created_at,
            action_type=action,
            user=member,
            moderator=moderator or guild.me,
            reason=reason,
        )
    except Exception:
        log.exception("Failed to create modlog case in _execute_action")
        modlog_failed = True
    label = _("The member has been kicked") if action == "kick" else _("The member has been banned")
    return ModerationEffectResult(
        label,
        None,
        EffectStatus.SUCCEEDED,
        modlog_failed=modlog_failed,
    )


async def on_message(cog, message: discord.Message) -> None:
    if message.guild is None:
        return
    if await cog.bot.cog_disabled_in_guild(cog, message.guild):
        return
    try:
        await cog._observe_message(message)
    except Exception as error:
        log.exception("Message registry observation failed")
        try:
            await cog._record_operational_failure(
                message.guild.id,
                "message_registry_observation",
                f"{type(error).__name__}: {error}",
            )
        except Exception:
            log.exception("Failed to record message registry observation error")
    if message.author.bot:
        return
    if message.webhook_id is not None:
        return
    lock_index = (
        message.guild.id * 31 + message.author.id
    ) % len(cog._detection_admission_locks)
    batch_key = (message.guild.id, message.id)
    pipeline_started = perf_counter()
    admission_lock = cog._detection_admission_locks[lock_index]
    admission_lock_owned = False
    try:
        await admission_lock.acquire()
        admission_lock_owned = True
        try:
            queue_wait_ms = (perf_counter() - pipeline_started) * 1000
            raw_config = await cog.config.guild(message.guild).all()
            guild_settings = GuildSettings.from_mapping(raw_config)
            if not guild_settings.enabled:
                return
            if await cog._is_protected_member(message.author, message.guild):
                return
            signals_started = perf_counter()
            signals = await cog._collect_detection_signals(
                message, guild_settings
            )
            timings = {
                "queue_wait_ms": queue_wait_ms,
                "signals_ms": (perf_counter() - signals_started) * 1000,
            }
            if not signals:
                return
            admission_lock_owned = False
            await cog._process_detected_message(
                message,
                guild_settings,
                signals,
                timings=timings,
                admission_lock=admission_lock,
            )
        finally:
            if admission_lock_owned:
                admission_lock.release()
    finally:
        cog._initial_image_scan_batches.pop(batch_key, None)
    return


async def honeypot_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).enabled.set(value)
        await ctx.send(_("✅ Enabled set to {value}").format(value=value))


async def action(cog, ctx: commands.Context, value: str = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).action()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=v or _("not set"),
                options=cog._format_options(CORE_ACTION_OPTIONS),
            )
        )
    elif value not in CORE_ACTION_OPTIONS:
        await ctx.send(_("Choose one of: {options}").format(options=cog._format_options(CORE_ACTION_OPTIONS)))
    else:
        await cog.config.guild(ctx.guild).action.set(value)
        await ctx.send(_("✅ Action set to {value}").format(value=value))


async def fallback_action(cog, ctx: commands.Context, value: str = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).fallback_action()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=v,
                options=cog._format_options(FALLBACK_ACTION_OPTIONS),
            )
        )
    elif value not in FALLBACK_ACTION_OPTIONS:
        await ctx.send(_("Choose one of: {options}").format(options=cog._format_options(FALLBACK_ACTION_OPTIONS)))
    else:
        await cog.config.guild(ctx.guild).fallback_action.set(value)
        await ctx.send(_("✅ Fallback action set to {value}").format(value=value))


async def dry_run(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).dry_run()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).dry_run.set(value)
        await ctx.send(_("✅ Dry run set to {value}").format(value=value))


async def whitelist_mode(cog, ctx: commands.Context, value: str = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).whitelist_mode()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=v,
                options=cog._format_options(WHITELIST_MODE_OPTIONS),
            )
        )
    elif value not in WHITELIST_MODE_OPTIONS:
        await ctx.send(_("Choose one of: {options}").format(options=cog._format_options(WHITELIST_MODE_OPTIONS)))
    else:
        await cog.config.guild(ctx.guild).whitelist_mode.set(value)
        await ctx.send(_("✅ Whitelist mode set to {value}").format(value=value))


async def automated_kick_fail_warn(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).automated_kick_fail_warning()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).automated_kick_fail_warning.set(value)
        await ctx.send(_("✅ Warn on automated kick fail set to {value}").format(value=value))


async def create(cog, ctx: commands.Context) -> None:
    me = ctx.guild.me
    if me is None:
        raise commands.UserFeedbackCheckFailure(_("I couldn't find my server member."))
    honeypot_channel = await ctx.guild.create_text_channel(
        name="honeypot",
        position=0,
        overwrites={
            me: discord.PermissionOverwrite(
                view_channel=True, read_messages=True, send_messages=True,
                manage_messages=True, manage_channels=True,
            ),
            ctx.guild.default_role: discord.PermissionOverwrite(
                view_channel=True, read_messages=True, send_messages=True,
            ),
        },
        reason=_("Honeypot channel requested by {author}.").format(author=ctx.author),
    )
    async with cog.config.guild(ctx.guild).honeypot_channels() as channel_ids:
        if honeypot_channel.id not in channel_ids:
            channel_ids.append(honeypot_channel.id)
    await ctx.send(_("✅ Honeypot channel added: {channel.mention}").format(channel=honeypot_channel))


async def punishment_mute_role(cog, ctx: commands.Context, role: discord.Role = None) -> None:
    if role is None:
        v = await cog.config.guild(ctx.guild).mute_role()
        r = ctx.guild.get_role(v) if v else None
        await ctx.send(_("Mute role: {role}").format(role=r.mention if r else _("not set")))
    else:
        await cog.config.guild(ctx.guild).mute_role.set(role.id)
        await ctx.send(_("✅ Mute role set to {role.mention}").format(role=role))


async def purge_backward(cog, ctx: commands.Context, seconds: int = None) -> None:
    if seconds is None:
        raw_config = await cog.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await ctx.send(
            _("Backward purge window: {seconds}s").format(
                seconds=_purge_backward_seconds(
                    guild_settings.purge_backward_seconds
                ),
            )
        )
    elif seconds < PURGE_MIN_RETENTION_SECONDS or seconds > PURGE_BACKWARD_MAX_SECONDS:
        await ctx.send(
            _("Backward purge must be between {minimum} and {maximum} seconds.").format(
                minimum=PURGE_MIN_RETENTION_SECONDS,
                maximum=PURGE_BACKWARD_MAX_SECONDS,
            )
        )
    else:
        await cog.config.guild(ctx.guild).purge_backward_seconds.set(seconds)
        await ctx.send(_("✅ Backward purge window set to {seconds}s").format(seconds=seconds))


async def purge_forward(cog, ctx: commands.Context, seconds: int = None) -> None:
    if seconds is None:
        raw_config = await cog.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await ctx.send(
            _("Forward purge window: {seconds}s").format(
                seconds=_purge_forward_seconds(
                    guild_settings.purge_forward_seconds
                ),
            )
        )
    elif seconds < 0 or seconds > PURGE_FORWARD_MAX_SECONDS:
        await ctx.send(
            _("Forward purge must be between 0 and {maximum} seconds.").format(
                maximum=PURGE_FORWARD_MAX_SECONDS,
            )
        )
    else:
        await cog.config.guild(ctx.guild).purge_forward_seconds.set(seconds)
        await ctx.send(_("✅ Forward purge window set to {seconds}s").format(seconds=seconds))


async def spam_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).spam_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).spam_enabled.set(value)
        await ctx.send(_("✅ Spam detection set to {value}").format(value=value))


async def spam_action(cog, ctx: commands.Context, value: str = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).spam_action()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=v,
                options=cog._format_options(CORE_ACTION_OPTIONS),
            )
        )
    elif value not in CORE_ACTION_OPTIONS:
        await ctx.send(_("Choose one of: {options}").format(options=cog._format_options(CORE_ACTION_OPTIONS)))
    else:
        await cog.config.guild(ctx.guild).spam_action.set(value)
        await ctx.send(_("✅ Spam action set to {value}").format(value=value))


async def spam_window(cog, ctx: commands.Context, seconds: int = None) -> None:
    if seconds is None:
        v = await cog.config.guild(ctx.guild).spam_window_seconds()
        await ctx.send(_("Spam window: {seconds}s").format(seconds=v))
    elif seconds < SPAM_WINDOW_MIN_SECONDS or seconds > SPAM_WINDOW_MAX_SECONDS:
        await ctx.send(
            _("Seconds must be between {minimum} and {maximum}.").format(
                minimum=SPAM_WINDOW_MIN_SECONDS,
                maximum=SPAM_WINDOW_MAX_SECONDS,
            )
        )
    else:
        await cog.config.guild(ctx.guild).spam_window_seconds.set(seconds)
        await ctx.send(_("✅ Spam window set to {seconds}s").format(seconds=seconds))


async def spam_channels(cog, ctx: commands.Context, count: int = None) -> None:
    if count is None:
        v = await cog.config.guild(ctx.guild).spam_min_channels()
        await ctx.send(_("Spam channel threshold: {count}").format(count=v))
    elif count < SPAM_CHANNEL_MIN or count > SPAM_CHANNEL_MAX:
        await ctx.send(
            _("Channel count must be between {minimum} and {maximum}.").format(
                minimum=SPAM_CHANNEL_MIN,
                maximum=SPAM_CHANNEL_MAX,
            )
        )
    else:
        await cog.config.guild(ctx.guild).spam_min_channels.set(count)
        await ctx.send(_("✅ Spam channel threshold set to {count}").format(count=count))


async def firstpost_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).firstpost_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).firstpost_enabled.set(value)
        if value:
            await cog.config.guild(ctx.guild).firstpost_collect_enabled.set(False)
        await ctx.send(_("✅ Firstpost enabled set to {value}").format(value=value))


async def firstpost_collect(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).firstpost_collect_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).firstpost_collect_enabled.set(value)
        if value:
            await cog.config.guild(ctx.guild).firstpost_enabled.set(False)
        await ctx.send(_("✅ Firstpost warmup set to {value}").format(value=value))


async def firstpost_action(cog, ctx: commands.Context, value: str = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).firstpost_action()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=v,
                options=cog._format_options(CORE_ACTION_OPTIONS),
            )
        )
    elif value not in CORE_ACTION_OPTIONS:
        await ctx.send(_("Choose one of: {options}").format(options=cog._format_options(CORE_ACTION_OPTIONS)))
    else:
        await cog.config.guild(ctx.guild).firstpost_action.set(value)
        await ctx.send(_("✅ Firstpost action set to {value}").format(value=value))


async def review_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).review_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).review_enabled.set(value)
        await ctx.send(_("✅ Review enabled set to {value}").format(value=value))


async def review_kick_fail_warn(cog, ctx: commands.Context, value: str = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).review_kick_fail_warning()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=v,
                options=cog._format_options(REVIEW_KICK_FAIL_WARNING_MODES),
            )
        )
        return
    value = value.lower()
    if value not in REVIEW_KICK_FAIL_WARNING_MODES:
        await ctx.send(_("Choose one of: {options}").format(options=cog._format_options(REVIEW_KICK_FAIL_WARNING_MODES)))
        return
    await cog.config.guild(ctx.guild).review_kick_fail_warning.set(value)
    await ctx.send(_("✅ Kick-fail warning set to {value}").format(value=value))


async def roles_add(cog, ctx: commands.Context, role: discord.Role) -> None:
    async with cog.config.guild(ctx.guild).whitelisted_roles() as roles:
        if role.id in roles:
            raise commands.UserFeedbackCheckFailure(_("That role is already whitelisted."))
        roles.append(role.id)
    await ctx.send(_("✅ {role} added to the whitelist").format(role=role.mention))


async def roles_remove(cog, ctx: commands.Context, role: discord.Role) -> None:
    async with cog.config.guild(ctx.guild).whitelisted_roles() as roles:
        if role.id not in roles:
            raise commands.UserFeedbackCheckFailure(_("That role is not in the whitelist."))
        roles.remove(role.id)
    await ctx.send(_("✅ {role} removed from the whitelist").format(role=role.mention))


async def roles_list(cog, ctx: commands.Context) -> None:
    role_ids = await cog.config.guild(ctx.guild).whitelisted_roles()
    if not role_ids:
        await ctx.send(_("No whitelisted roles"))
        return
    roles = [ctx.guild.get_role(rid) for rid in role_ids if ctx.guild.get_role(rid) is not None]
    if not roles:
        await ctx.send(_("No valid roles found (deleted?)"))
        return
    await ctx.send(_("**Whitelisted roles:**\n{lines}").format(lines="\n".join(f"- {r.mention}" for r in roles)))


async def keywords_add(cog, ctx: commands.Context, *, keyword: str) -> None:
    keyword = keyword.strip().lower()
    if not keyword:
        raise commands.UserFeedbackCheckFailure(_("Keyword cannot be empty."))
    async with cog.config.guild(ctx.guild).scam_keywords() as keywords:
        if keyword in [kw.lower() for kw in keywords]:
            raise commands.UserFeedbackCheckFailure(_("Keyword already exists."))
        keywords.append(keyword)
    await ctx.send(_("✅ Keyword added: `{keyword}`").format(keyword=keyword))


async def keywords_remove(cog, ctx: commands.Context, *, keyword: str) -> None:
    keyword = keyword.strip().lower()
    async with cog.config.guild(ctx.guild).scam_keywords() as keywords:
        for existing in list(keywords):
            if existing.lower() == keyword:
                keywords.remove(existing)
                await ctx.send(_("✅ Keyword removed: `{keyword}`").format(keyword=existing))
                return
    raise commands.UserFeedbackCheckFailure(_("Keyword not found."))


async def keywords_list(cog, ctx: commands.Context) -> None:
    keywords = await cog.config.guild(ctx.guild).scam_keywords()
    if not keywords:
        await ctx.send(_("No keywords configured"))
        return
    await ctx.send(_("**Scam keywords:**\n{lines}").format(lines="\n".join(f"`{i}.` {kw}" for i, kw in enumerate(keywords, 1))))


async def keywords_reset(cog, ctx: commands.Context) -> None:
    await cog.config.guild(ctx.guild).scam_keywords.set(SCAM_KEYWORDS.copy())
    await ctx.send(_("✅ Keywords reset to defaults"))


async def keyword_attachments_add(cog, ctx: commands.Context, *, pattern: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise commands.UserFeedbackCheckFailure(_("Invalid regex: {error}").format(error=exc)) from exc
    async with cog.config.guild(ctx.guild).attachment_patterns() as patterns:
        if pattern in patterns:
            raise commands.UserFeedbackCheckFailure(_("Pattern already exists."))
        patterns.append(pattern)
    await ctx.send(_("✅ Attachment pattern added: `{pattern}`").format(pattern=pattern))


async def keyword_attachments_remove(cog, ctx: commands.Context, *, pattern: str) -> None:
    async with cog.config.guild(ctx.guild).attachment_patterns() as patterns:
        if pattern not in patterns:
            raise commands.UserFeedbackCheckFailure(_("Pattern not found."))
        patterns.remove(pattern)
    await ctx.send(_("✅ Attachment pattern removed: `{pattern}`").format(pattern=pattern))


async def keyword_attachments_list(cog, ctx: commands.Context) -> None:
    patterns = await cog.config.guild(ctx.guild).attachment_patterns()
    if not patterns:
        await ctx.send(_("No attachment patterns configured"))
        return
    await ctx.send(_("**Attachment patterns:**\n{lines}").format(lines="\n".join(f"`{i}.` {pattern}" for i, pattern in enumerate(patterns, 1))))


async def keyword_attachments_reset(cog, ctx: commands.Context) -> None:
    await cog.config.guild(ctx.guild).attachment_patterns.set(DEFAULT_ATTACHMENT_PATTERNS.copy())
    await ctx.send(_("✅ Attachment patterns reset to defaults"))


async def bait_toggle(cog, ctx: commands.Context, value: bool = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).baitrole_enabled()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=str(v).lower(),
                options=cog._format_options(BOOL_OPTIONS),
            )
        )
    else:
        await cog.config.guild(ctx.guild).baitrole_enabled.set(value)
        await ctx.send(_("✅ Bait role trap set to {value}").format(value=value))


async def role(cog, ctx: commands.Context, role: discord.Role = None) -> None:
    if role is None:
        v = await cog.config.guild(ctx.guild).baitrole_id()
        r = ctx.guild.get_role(v) if v else None
        await ctx.send(_("Bait role: {role}").format(role=r.mention if r else _("not set")))
    else:
        await cog.config.guild(ctx.guild).baitrole_id.set(role.id)
        await ctx.send(_("✅ Bait role set to {role.mention}").format(role=role))


async def bait_action(cog, ctx: commands.Context, value: str = None) -> None:
    if value is None:
        v = await cog.config.guild(ctx.guild).baitrole_action()
        await ctx.send(
            _("Current: {value}. Choices: {options}").format(
                value=v,
                options=cog._format_options(BAIT_ACTION_OPTIONS),
            )
        )
    elif value not in BAIT_ACTION_OPTIONS:
        await ctx.send(_("Choose one of: {options}").format(options=cog._format_options(BAIT_ACTION_OPTIONS)))
    else:
        await cog.config.guild(ctx.guild).baitrole_action.set(value)
        await ctx.send(_("✅ Bait action set to {value}").format(value=value))


async def config_honeypot(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._send_config_dump(
        ctx,
        _("Honeypot config"),
        [
            (_("Enabled"), cog._format_bool_setting(guild_settings.enabled)),
            (
                _("Action"),
                guild_settings.action.value
                if guild_settings.action is not None
                else _("not set"),
            ),
            (_("Fallback action"), guild_settings.fallback_action.value),
            (_("Dry run"), cog._format_bool_setting(guild_settings.dry_run)),
            (_("Whitelist mode"), guild_settings.whitelist_mode.value),
            (
                _("Warn on automated kick fail"),
                cog._format_bool_setting(
                    guild_settings.automated_kick_fail_warning
                ),
            ),
        ],
    )


async def config_channel(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._send_config_dump(
        ctx,
        _("Channel config"),
        [
            (
                _("Honeypot channels"),
                _format_honeypot_channel_list(
                    cog,
                    ctx.guild,
                    guild_settings.honeypot_channels,
                ),
            ),
        ],
    )


async def config_punishment(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._send_config_dump(
        ctx,
        _("Punishment config"),
        [
            (
                _("Mute role"),
                cog._format_role_setting(ctx.guild, guild_settings.mute_role),
            ),
        ],
    )


async def config_purge(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._send_config_dump(
        ctx,
        _("Purge config"),
        [
            (_("Mode"), _("Event registry purge")),
            (
                _("Backward window"),
                _("{seconds}s").format(
                    seconds=_purge_backward_seconds(
                        guild_settings.purge_backward_seconds
                    )
                ),
            ),
            (
                _("Forward window"),
                _("{seconds}s").format(
                    seconds=_purge_forward_seconds(
                        guild_settings.purge_forward_seconds
                    )
                ),
            ),
            (_("Minimum retention"), _("{seconds}s").format(seconds=PURGE_MIN_RETENTION_SECONDS)),
        ],
    )


async def config_firstpost(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    seen_count = await _count_firstpost_seen_authors(cog, ctx.guild.id)
    await cog._send_config_dump(
        ctx,
        _("Firstpost config"),
        [
            (
                _("Enabled"),
                cog._format_bool_setting(guild_settings.firstpost_enabled),
            ),
            (
                _("Warmup"),
                cog._format_bool_setting(
                    guild_settings.firstpost_collect_enabled
                ),
            ),
            (_("Action"), guild_settings.firstpost_action.value),
            (_("Seen authors"), seen_count),
        ],
    )


async def config_spam(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._send_config_dump(
        ctx,
        _("Spam config"),
        [
            (_("Enabled"), cog._format_bool_setting(guild_settings.spam_enabled)),
            (_("Action"), guild_settings.spam_action.value),
            (
                _("Window"),
                _("{seconds}s").format(
                    seconds=guild_settings.spam_window_seconds
                ),
            ),
            (_("Channels"), guild_settings.spam_min_channels),
        ],
    )


async def config_review(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._send_config_dump(
        ctx,
        _("Review config"),
        [
            (_("Enabled"), cog._format_bool_setting(guild_settings.review_enabled)),
            (
                _("Channel"),
                cog._format_channel_setting(
                    ctx.guild, guild_settings.review_channel
                ),
            ),
            (_("Case lifetime"), _("24 hours (fixed)")),
            (
                _("Kick fail warning"),
                guild_settings.review_kick_fail_warning.value,
            ),
        ],
    )


async def config_roles(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    roles = [
        cog._format_role_setting(ctx.guild, role_id)
        for role_id in guild_settings.whitelisted_roles
    ]
    await cog._send_config_dump(
        ctx,
        _("Roles config"),
        [
            (_("Whitelist mode"), guild_settings.whitelist_mode.value),
            (_("Whitelisted roles"), ", ".join(roles) if roles else _("none")),
        ],
    )


async def config_keywords(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._send_config_dump(
        ctx,
        _("Keywords config"),
        [
            (_("Scam keywords"), len(guild_settings.scam_keywords)),
            (
                _("Attachment patterns"),
                len(guild_settings.attachment_patterns),
            ),
        ],
    )


async def config_bait(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._send_config_dump(
        ctx,
        _("Bait config"),
        [
            (
                _("Enabled"),
                cog._format_bool_setting(guild_settings.baitrole_enabled),
            ),
            (
                _("Role"),
                cog._format_role_setting(ctx.guild, guild_settings.baitrole_id),
            ),
            (_("Action"), guild_settings.baitrole_action.value),
        ],
    )


async def config_stats(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    stats = DEFAULT_STATS.copy()
    stats.update(guild_settings.stats)
    now = datetime.now(timezone.utc)
    case_counts = await asyncio.to_thread(
        cog._case_store.operational_counts,
        ctx.guild.id,
        now,
        now - timedelta(minutes=5),
    )
    await cog._send_config_dump(
        ctx,
        _("Stats config"),
        [
            (_("Stored stats"), len(stats)),
            (
                _("Pending joinwatch role applications"),
                len(guild_settings.joinwatch_pending_role_assignments),
            ),
            (
                _("Active joinwatch auto-role timers"),
                len(guild_settings.joinwatch_pending_roles),
            ),
            (_("Active detection cases"), case_counts["active_cases"]),
            (_("Due detection cases"), case_counts["due_cases"]),
            (_("Stale resolving cases"), case_counts["stale_resolving_cases"]),
            (_("Failed containment cases"), case_counts["failed_containment"]),
            (_("Forbidden message deletes"), case_counts["forbidden_deletes"]),
            (_("Outstanding durable operations"), case_counts["outstanding_operations"]),
            (_("Queued privacy deletions"), case_counts["privacy_deletion_jobs"]),
        ],
    )


async def config_all(cog, ctx: commands.Context) -> None:
    raw_config = await cog.config.guild(ctx.guild).all()
    guild_settings = GuildSettings.from_mapping(raw_config)
    await cog._send_config_dump(
        ctx,
        _("Honeypot config summary"),
        [
            (_("Honeypot"), cog._format_bool_setting(guild_settings.enabled)),
            (
                _("Honeypot channels"),
                _format_honeypot_channel_list(
                    cog,
                    ctx.guild,
                    guild_settings.honeypot_channels,
                ),
            ),
            (_("Review"), cog._format_bool_setting(guild_settings.review_enabled)),
            (_("Spam"), cog._format_bool_setting(guild_settings.spam_enabled)),
            (
                _("Image scan"),
                cog._format_bool_setting(
                    guild_settings.imagescan_detector_enabled
                ),
            ),
            (
                _("GIF detector"),
                cog._format_bool_setting(guild_settings.gif_detector_enabled),
            ),
            (
                _("Joinwatch"),
                cog._format_bool_setting(guild_settings.joinwatch_enabled),
            ),
            (
                _("Joinwatch auto-role"),
                cog._format_bool_setting(
                    guild_settings.joinwatch_auto_role_enabled
                ),
            ),
            (
                _("Bait role"),
                cog._format_bool_setting(guild_settings.baitrole_enabled),
            ),
            (
                _("Pending joinwatch role applications"),
                len(guild_settings.joinwatch_pending_role_assignments),
            ),
            (
                _("Active joinwatch auto-role timers"),
                len(guild_settings.joinwatch_pending_roles),
            ),
        ],
    )
