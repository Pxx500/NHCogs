import logging
from importlib import import_module

from redbot.core.bot import Red
from redbot.core.utils import get_end_user_data_statement

__red_end_user_data_statement__ = get_end_user_data_statement(file=__file__)
log = logging.getLogger("red.NHCogs")


async def _remove_partial_subcog(bot: Red, cog, name: str) -> None:
    if cog is None or bot.get_cog(cog.qualified_name) is not cog:
        return
    try:
        await bot.remove_cog(cog.qualified_name)
    except Exception:
        log.exception("NHCogs subcog %s cleanup failed", name)


async def _load_subcog(bot: Red, module_name: str, class_name: str):
    cog = None
    try:
        module = import_module(module_name, __name__)
        cog = getattr(module, class_name)(bot)
        await bot.add_cog(cog)
    except Exception:
        log.exception("NHCogs subcog %s failed during startup", class_name)
        await _remove_partial_subcog(bot, cog, class_name)
        return None
    return cog


async def _load_custom_commands(bot: Red, nhmisc) -> None:
    if nhmisc is None:
        log.error("NHCogs subcog CustomCommands skipped because NHMisc is unavailable")
        return
    custom_commands = None
    try:
        module = import_module(".custom_commands", __name__)
        module.assert_safe_to_replace(bot)
        custom_commands = await module.build_custom_commands_component(bot, nhmisc)
        if bot.get_cog(custom_commands.qualified_name) is not custom_commands:
            await bot.add_cog(custom_commands)
    except Exception:
        log.exception("NHCogs subcog CustomCommands failed during startup")
        await _remove_partial_subcog(bot, custom_commands, "CustomCommands")


async def setup(bot: Red) -> None:
    await _load_subcog(bot, ".consoledump", "ConsoleDump")
    nhmisc = await _load_subcog(bot, ".nhmisc", "NHMisc")
    await _load_subcog(bot, ".honeypot", "Honeypot")
    await _load_subcog(bot, ".githubtickets", "GitHubTickets")
    await _load_custom_commands(bot, nhmisc)


async def teardown(bot: Red) -> None:
    for name in ("CustomCommands", "CustomCommandsMigration"):
        cog = bot.get_cog(name)
        if cog is None:
            continue
        if not type(cog).__module__.startswith("NHCogs.custom_commands"):
            continue
        await bot.remove_cog(name)
