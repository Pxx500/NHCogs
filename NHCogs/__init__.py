from redbot.core.bot import Red
from redbot.core.utils import get_end_user_data_statement

from .custom_commands import (
    CustomCommands,
    assert_safe_to_replace,
    build_custom_commands_component,
)
from .githubtickets import GitHubTickets
from .honeypot import Honeypot
from .nhmisc import NHMisc

__red_end_user_data_statement__ = get_end_user_data_statement(file=__file__)

__all__ = ("CustomCommands", "GitHubTickets", "Honeypot", "NHMisc")


async def setup(bot: Red) -> None:
    assert_safe_to_replace(bot)
    nhmisc = NHMisc(bot)
    cogs = (nhmisc, Honeypot(bot), GitHubTickets(bot))
    owned = []
    try:
        for cog in cogs:
            try:
                await bot.add_cog(cog)
            finally:
                if bot.get_cog(cog.qualified_name) is cog:
                    owned.append(cog)
        custom_commands = await build_custom_commands_component(bot, nhmisc)
        if bot.get_cog(custom_commands.qualified_name) is not custom_commands:
            try:
                await bot.add_cog(custom_commands)
            finally:
                if bot.get_cog(custom_commands.qualified_name) is custom_commands:
                    owned.append(custom_commands)
    except BaseException:
        for cog in reversed(owned):
            if bot.get_cog(cog.qualified_name) is not cog:
                continue
            try:
                await bot.remove_cog(cog.qualified_name)
            except BaseException:
                pass
        raise


async def teardown(bot: Red) -> None:
    for name in ("CustomCommands", "CustomCommandsMigration"):
        cog = bot.get_cog(name)
        if cog is None:
            continue
        if not type(cog).__module__.startswith("NHCogs.custom_commands"):
            continue
        await bot.remove_cog(name)
