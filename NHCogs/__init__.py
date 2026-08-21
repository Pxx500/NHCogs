from redbot.core.bot import Red
from redbot.core.utils import get_end_user_data_statement

from .custom_commands import CustomCommands, build_custom_commands_component
from .honeypot import Honeypot
from .nhmisc import NHMisc

__red_end_user_data_statement__ = get_end_user_data_statement(file=__file__)

__all__ = ("CustomCommands", "Honeypot", "NHMisc")


async def setup(bot: Red) -> None:
    nhmisc = NHMisc(bot)
    cogs = (nhmisc, Honeypot(bot))
    attempted = []
    try:
        for cog in cogs:
            attempted.append(cog)
            await bot.add_cog(cog)
        custom_commands = await build_custom_commands_component(bot, nhmisc)
        attempted.append(custom_commands)
        if bot.get_cog(custom_commands.qualified_name) is not custom_commands:
            await bot.add_cog(custom_commands)
    except BaseException:
        for cog in reversed(attempted):
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
