from .history import NHModerationHistory
from .models import BanChartData, BanChartQuery, BanChartRow, ModerationObservation

__all__ = (
    "BanChartData",
    "BanChartQuery",
    "BanChartRow",
    "ModerationObservation",
    "NHModeration",
    "NHModerationHistory",
)


def __getattr__(name: str):
    if name != "NHModeration":
        raise AttributeError(name)
    from .nhmoderation import NHModeration

    return NHModeration


async def setup(bot) -> None:
    from ..operational_support import ensure_operational_support

    await bot.add_cog(__getattr__("NHModeration")(bot, await ensure_operational_support(bot)))
