from redbot.core.bot import Red
from redbot.core.utils import get_end_user_data_statement

from .honeypot import Honeypot
from .nhmisc import NHMisc

__red_end_user_data_statement__ = get_end_user_data_statement(file=__file__)

__all__ = ("Honeypot", "NHMisc")


async def setup(bot: Red) -> None:
    cogs = (NHMisc(bot), Honeypot(bot))
    attempted = []
    try:
        for cog in cogs:
            attempted.append(cog)
            await bot.add_cog(cog)
    except BaseException:
        for cog in reversed(attempted):
            try:
                await bot.remove_cog(cog.qualified_name)
            except BaseException:
                pass
        raise
