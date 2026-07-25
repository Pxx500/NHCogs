"""Shared execution policy for durable detection-case operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from .detection_cases import CaseSnapshot, OperationRecord, OperationType

if TYPE_CHECKING:
    from .honeypot import Honeypot


class CompletionMode(str, Enum):
    OPERATION = "operation"
    MODERATOR_ACTION = "moderator_action"


class FollowUpKind(str, Enum):
    ROLE_APPLY_RERENDER = "role_apply_rerender"
    COMPACT_TERMINAL_CASE = "compact_terminal_case"
    FINISH_MODERATION = "finish_moderation"
    FINISH_MESSAGE_PROCESS = "finish_message_process"


@dataclass(frozen=True)
class OperationLease:
    operation_id: str
    claim_token: str | None


@dataclass(frozen=True)
class OperationContext:
    operation: OperationRecord
    snapshot: CaseSnapshot
    lease: OperationLease
    now: datetime
    publication_channel: object | None = None
    live_message: object | None = None
    timings: dict[str, float] | None = None


@dataclass(frozen=True)
class OperationFollowUp:
    kind: FollowUpKind
    requires_completion: bool = True


@dataclass(frozen=True)
class OperationOutcome:
    result: str | None = None
    role_was_added: bool = False
    follow_ups: tuple[OperationFollowUp, ...] = ()
    completed: bool = False
    completion_mode: CompletionMode = CompletionMode.OPERATION
    resolve_failure_on_first_attempt: bool = False
    error: Exception | None = None


@dataclass(frozen=True)
class OperationPolicy:
    completion_mode: CompletionMode = CompletionMode.OPERATION
    follow_ups: tuple[OperationFollowUp, ...] = ()
    resolve_failure_on_first_attempt: bool = False


class OperationHandler(Protocol):
    async def __call__(
        self, cog: Honeypot, context: OperationContext
    ) -> OperationOutcome: ...


_ROLE_APPLY_FOLLOW_UP = OperationFollowUp(FollowUpKind.ROLE_APPLY_RERENDER)
_TERMINAL_COMPACTION_FOLLOW_UP = OperationFollowUp(
    FollowUpKind.COMPACT_TERMINAL_CASE,
    requires_completion=False,
)
_MODERATION_FOLLOW_UP = OperationFollowUp(FollowUpKind.FINISH_MODERATION)
_MESSAGE_PROCESS_FOLLOW_UP = OperationFollowUp(
    FollowUpKind.FINISH_MESSAGE_PROCESS
)

EXECUTOR_OPERATION_POLICIES: Mapping[OperationType, OperationPolicy] = (
    MappingProxyType(
        {
            OperationType.MESSAGE_PROCESS: OperationPolicy(
                follow_ups=(_MESSAGE_PROCESS_FOLLOW_UP,)
            ),
            OperationType.ROLE_APPLY: OperationPolicy(
                follow_ups=(_ROLE_APPLY_FOLLOW_UP,)
            ),
            OperationType.ROLE_RELEASE: OperationPolicy(
                follow_ups=(_TERMINAL_COMPACTION_FOLLOW_UP,)
            ),
            OperationType.REVIEW_UPDATE: OperationPolicy(
                follow_ups=(_TERMINAL_COMPACTION_FOLLOW_UP,)
            ),
            OperationType.REVIEW_PUBLISH: OperationPolicy(
                resolve_failure_on_first_attempt=True
            ),
            OperationType.SOURCE_DELETE: OperationPolicy(),
            OperationType.EVIDENCE_CLEANUP: OperationPolicy(
                follow_ups=(_TERMINAL_COMPACTION_FOLLOW_UP,)
            ),
            OperationType.CACHED_PURGE: OperationPolicy(),
            OperationType.MODERATION_ACTION: OperationPolicy(
                follow_ups=(_MODERATION_FOLLOW_UP,)
            ),
            OperationType.MODERATOR_BAN: OperationPolicy(
                completion_mode=CompletionMode.MODERATOR_ACTION,
                follow_ups=(_MODERATION_FOLLOW_UP,),
            ),
            OperationType.MODERATOR_KICK: OperationPolicy(
                completion_mode=CompletionMode.MODERATOR_ACTION,
                follow_ups=(_MODERATION_FOLLOW_UP,),
            ),
        }
    )
)


def executor_operation_policy(
    operation_type: OperationType | str,
) -> OperationPolicy | None:
    try:
        canonical_type = OperationType(operation_type)
    except ValueError:
        return None
    return EXECUTOR_OPERATION_POLICIES.get(canonical_type)


def apply_operation_policy(
    outcome: OperationOutcome, policy: OperationPolicy
) -> OperationOutcome:
    return replace(
        outcome,
        follow_ups=policy.follow_ups,
        completed=False,
        completion_mode=policy.completion_mode,
        resolve_failure_on_first_attempt=policy.resolve_failure_on_first_attempt,
    )


class OperationHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[OperationType, OperationHandler] = {}

    def register(
        self,
        operation_type: OperationType | str,
        handler: OperationHandler,
    ) -> None:
        self._handlers[OperationType(operation_type)] = handler

    def resolve(
        self, operation_type: OperationType | str
    ) -> OperationHandler | None:
        try:
            canonical_type = OperationType(operation_type)
        except ValueError:
            return None
        return self._handlers.get(canonical_type)
