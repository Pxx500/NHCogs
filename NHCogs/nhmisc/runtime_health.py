from __future__ import annotations

import asyncio


def task_health_issue(
    task: asyncio.Task[object] | None,
    label: str,
) -> str | None:
    if task is None:
        return f"{label} was not created"
    if not task.done():
        return None
    if task.cancelled():
        return f"{label} was cancelled"
    error = task.exception()
    if error is None:
        return f"{label} stopped unexpectedly"
    return f"{label} failed with {type(error).__name__}: {error}"
