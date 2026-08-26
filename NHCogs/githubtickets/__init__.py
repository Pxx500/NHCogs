from redbot.core.bot import Red
from redbot.core.utils import get_end_user_data_statement

from .githubtickets import GitHubTickets

__red_end_user_data_statement__ = get_end_user_data_statement(file=__file__)
__all__ = ("GitHubTickets",)


async def setup(bot: Red) -> None:
    cog = GitHubTickets(bot)
    await bot.add_cog(cog)
