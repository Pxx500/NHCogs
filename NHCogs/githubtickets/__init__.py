from redbot.core.bot import Red
from redbot.core.utils import get_end_user_data_statement

from ..operational_support import ensure_operational_support
from .githubtickets import GitHubTickets

__red_end_user_data_statement__ = get_end_user_data_statement(file=__file__)
__all__ = ("GitHubTickets",)


async def setup(bot: Red) -> None:
    support = await ensure_operational_support(bot)
    cog = GitHubTickets(bot, support)
    await bot.add_cog(cog)
