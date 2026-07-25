"""Canonical handlers and execution policy for detection-case operations."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..detection_cases import OperationType
from .cached_purge import cached_purge_handler
from .context import (
    CompletionMode,
    FollowUpKind,
    OperationFollowUp,
    OperationHandler,
    OperationPolicy,
)
from .review_publish import review_publish_handler
from .review_update import review_update_handler
from .source_delete import source_delete_handler


HANDLERS: Mapping[OperationType, OperationHandler] = MappingProxyType(
    {
        OperationType.REVIEW_UPDATE: review_update_handler,
        OperationType.REVIEW_PUBLISH: review_publish_handler,
        OperationType.CACHED_PURGE: cached_purge_handler,
        OperationType.SOURCE_DELETE: source_delete_handler,
    }
)

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


class OperationHandlerRegistry:
    def __init__(
        self, handlers: Mapping[OperationType, OperationHandler] = HANDLERS
    ) -> None:
        self._handlers = dict(handlers)

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
