"""Detection-case evidence cleanup operation handler."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..case_review import is_persisted_image_attachment
from ..detection_cases import OperationType
from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot


async def evidence_cleanup_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    from ..honeypot import case_evidence_root

    operation = context.operation
    snapshot = context.snapshot
    review_update = next(
        (
            item
            for item in snapshot.operations
            if item.operation_type == OperationType.REVIEW_UPDATE
        ),
        None,
    )
    if (
        snapshot.case.review_message_id is not None
        and review_update is not None
        and review_update.status.value != "succeeded"
    ):
        raise RuntimeError("terminal review projection is not complete")
    case_root = case_evidence_root(
        cog._detection_case_files_path,
        snapshot.case.guild_id,
        operation.case_id,
    ).resolve()
    evidence_root = cog._detection_case_files_path.resolve()
    if not case_root.is_relative_to(evidence_root):
        raise RuntimeError("detection case evidence path escapes storage root")
    for attachment in snapshot.attachments:
        if attachment.evidence_path is None:
            continue
        path = Path(attachment.evidence_path).resolve()
        if not path.is_relative_to(case_root):
            raise RuntimeError("detection case evidence path escapes case root")
    for attachment in snapshot.attachments:
        if (
            attachment.evidence_path is None
            or not is_persisted_image_attachment(attachment)
            or attachment.learning_decision
            not in {"true_positive", "false_positive"}
        ):
            continue
        evidence_path = Path(attachment.evidence_path)
        if not evidence_path.exists():
            continue
        result, _sample = await cog._imagescan_add_file_sample(
            snapshot.case.guild_id,
            evidence_path,
            attachment.learning_decision,
            snapshot.case.moderator_id,
        )
        if result not in {"inserted", "duplicate"}:
            raise RuntimeError(
                f"failed to copy detection evidence into learning samples: {result}"
            )
    if case_root.exists():
        for path in sorted(case_root.rglob("*"), reverse=True):
            resolved = path.resolve()
            if not resolved.is_relative_to(case_root):
                raise RuntimeError("detection case evidence path escapes case root")
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
    if case_root.exists():
        case_root.rmdir()
    return OperationOutcome()
