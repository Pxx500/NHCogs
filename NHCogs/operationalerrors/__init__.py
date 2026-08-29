from ..operational_errors import (
    mark_operational_error_recovered,
    report_operational_error,
)
from .cog import OperationalErrors, OperationalFailure


async def setup(bot) -> None:
    await bot.add_cog(OperationalErrors(bot))


__all__ = (
    "OperationalErrors",
    "OperationalFailure",
    "mark_operational_error_recovered",
    "report_operational_error",
)
