"""Shared contracts for durable detection-case operation handlers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol

from ..detection_cases import CaseSnapshot, OperationRecord

if TYPE_CHECKING:
    from ..honeypot import Honeypot


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
