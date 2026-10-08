"""Check whether missing Gateway data prevents pending attachment capture."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from ..gateway_capabilities import available
from . import detection_runtime
from .detection_cases import OperationType

if TYPE_CHECKING:
    from .operations.context import OperationContext


def capture_needs_unavailable_content(bot: Any, context: OperationContext) -> bool:
    if context.operation.operation_type != OperationType.MESSAGE_PROCESS:
        return False
    if available(bot, "message_content"):
        return False
    terminal_statuses = {status.value for status in detection_runtime.CaptureStatus}
    pending = tuple(
        attachment for attachment in context.snapshot.attachments
        if attachment.message_sequence == context.operation.message_sequence
        and attachment.capture_status not in terminal_statuses
    )
    if not pending:
        return False
    if context.live_message is None:
        return True
    supplied = Counter(
        (str(attachment.filename), int(attachment.size))
        for attachment in getattr(context.live_message, "attachments", ())
    )
    needed = Counter((attachment.filename, attachment.size) for attachment in pending)
    return any(supplied[key] < count for key, count in needed.items())
