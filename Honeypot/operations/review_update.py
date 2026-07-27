"""Review projection update operation handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .context import OperationContext, OperationOutcome

if TYPE_CHECKING:
    from ..honeypot import Honeypot


async def review_update_handler(
    cog: Honeypot, context: OperationContext
) -> OperationOutcome:
    await cog._case_review_rerender(context.operation.case_id)
    return OperationOutcome()
