from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("red.OperationalErrors")

COG_NAME = "OperationalErrors"


def _log_report_failure(message: str, *args: Any) -> None:
    try:
        log.exception(message, *args)
    except BaseException:
        pass


async def report_operational_error(
    bot: Any,
    *,
    guild_id: int,
    source: str,
    action: str,
    error: BaseException,
    channel_id: int | None = None,
    thread_id: int | None = None,
    message_id: int | None = None,
    correlation_key: str | None = None,
) -> Any | None:
    """Report one operational failure without allowing reporting to escape."""
    try:
        reporter = bot.get_cog(COG_NAME)
        report = getattr(reporter, "report", None)
        if not callable(report):
            try:
                log.error(
                    "OperationalErrors is unavailable while reporting %s during %s",
                    source,
                    action,
                    exc_info=(type(error), error, error.__traceback__),
                )
            except BaseException:
                pass
            return None
        return await report(
            guild_id=guild_id,
            source=source,
            action=action,
            error=error,
            channel_id=channel_id,
            thread_id=thread_id,
            message_id=message_id,
            correlation_key=correlation_key,
        )
    except BaseException:
        _log_report_failure(
            "OperationalErrors failed while reporting %s during %s",
            source,
            action,
        )
        return None


async def mark_operational_error_recovered(
    bot: Any,
    *,
    guild_id: int,
    source: str,
    action: str,
    correlation_key: str | None = None,
) -> int:
    """Mark matching reports recovered when the process-wide reporter is available."""
    try:
        reporter = bot.get_cog(COG_NAME)
        mark_recovered = getattr(reporter, "mark_action_recovered", None)
        if not callable(mark_recovered):
            return 0
        return await mark_recovered(
            guild_id=guild_id,
            source=source,
            action=action,
            correlation_key=correlation_key,
        )
    except BaseException:
        _log_report_failure(
            "OperationalErrors failed to mark %s during %s recovered",
            source,
            action,
        )
        return 0


__all__ = (
    "mark_operational_error_recovered",
    "report_operational_error",
)
