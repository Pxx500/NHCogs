import asyncio
import logging
from importlib import import_module

from redbot.core.bot import Red
from redbot.core.utils import get_end_user_data_statement

__red_end_user_data_statement__ = get_end_user_data_statement(file=__file__)
log = logging.getLogger("red.NHCogs")


class _LifecycleTracker:
    def __init__(self, cog) -> None:
        self.cog = cog
        self.load_started = False
        self.unload_started = False
        self._original_load = cog.cog_load
        self._original_unload = cog.cog_unload

    async def load(self) -> None:
        self.load_started = True
        await self._original_load()

    async def unload(self) -> None:
        self.unload_started = True
        await self._original_unload()

    def install(self) -> None:
        self.cog.cog_load = self.load
        self.cog.cog_unload = self.unload

    def restore(self) -> None:
        self.cog.cog_load = self._original_load
        self.cog.cog_unload = self._original_unload


async def _cleanup_failed_subcog(
    bot: Red,
    cog,
    name: str,
    lifecycle: _LifecycleTracker,
) -> None:
    try:
        if bot.get_cog(cog.qualified_name) is cog:
            await bot.remove_cog(cog.qualified_name)
        elif lifecycle.load_started and not lifecycle.unload_started:
            await lifecycle.unload()
    except Exception:
        log.exception("NHCogs subcog %s cleanup failed", name)


async def _add_subcog(bot: Red, cog, name: str) -> bool:
    lifecycle = _LifecycleTracker(cog)
    lifecycle.install()
    try:
        await bot.add_cog(cog)
    except asyncio.CancelledError:
        await _cleanup_failed_subcog(bot, cog, name, lifecycle)
        raise
    except Exception:
        log.exception("NHCogs subcog %s failed during startup", name)
        await _cleanup_failed_subcog(bot, cog, name, lifecycle)
        return False
    finally:
        lifecycle.restore()
    return True


async def _load_subcog(bot: Red, module_name: str, class_name: str):
    try:
        module = import_module(module_name, __name__)
        cog = getattr(module, class_name)(bot)
    except Exception:
        log.exception("NHCogs subcog %s failed during startup", class_name)
        return None
    if not await _add_subcog(bot, cog, class_name):
        return None
    return cog


async def _load_custom_commands(bot: Red, nhmisc):
    if nhmisc is None:
        log.error("NHCogs subcog CustomCommands skipped because NHMisc is unavailable")
        return None
    custom_commands = None
    try:
        module = import_module(".custom_commands", __name__)
        module.assert_safe_to_replace(bot)
        custom_commands = await module.build_custom_commands_component(bot, nhmisc)
    except Exception:
        log.exception("NHCogs subcog CustomCommands failed during startup")
        return None
    if bot.get_cog(custom_commands.qualified_name) is custom_commands:
        return custom_commands
    if not await _add_subcog(bot, custom_commands, "CustomCommands"):
        return None
    return custom_commands


async def _load_cleanup(bot: Red, nhmisc, honeypot):
    if nhmisc is None or honeypot is None:
        log.error("NHCogs subcog Cleanup skipped because its dependencies are unavailable")
        return None
    cleanup = None
    try:
        module = import_module(".cleanup", __name__)
        module.assert_safe_to_replace(bot)
        cleanup = await module.build_cleanup_component(bot, nhmisc, honeypot)
    except Exception:
        log.exception("NHCogs subcog Cleanup failed during startup")
        return None
    if bot.get_cog(cleanup.qualified_name) is cleanup:
        return cleanup
    if not await _add_subcog(bot, cleanup, "Cleanup"):
        return None
    return cleanup


async def _cleanup_cancelled_setup(bot: Red, loaded: list) -> None:
    for cog in reversed(loaded):
        if bot.get_cog(cog.qualified_name) is not cog:
            continue
        try:
            await bot.remove_cog(cog.qualified_name)
        except Exception:
            log.exception("NHCogs subcog %s cancellation cleanup failed", cog.qualified_name)


async def setup(bot: Red) -> None:
    loaded = []
    try:
        if consoledump := await _load_subcog(bot, ".consoledump", "ConsoleDump"):
            loaded.append(consoledump)
        if operational_errors := await _load_subcog(
            bot, ".operationalerrors", "OperationalErrors"
        ):
            loaded.append(operational_errors)
        nhmisc = await _load_subcog(bot, ".nhmisc", "NHMisc")
        if nhmisc is not None:
            loaded.append(nhmisc)
        honeypot = await _load_subcog(bot, ".honeypot", "Honeypot")
        if honeypot is not None:
            loaded.append(honeypot)
        if cleanup := await _load_cleanup(bot, nhmisc, honeypot):
            loaded.append(cleanup)
        if githubtickets := await _load_subcog(bot, ".githubtickets", "GitHubTickets"):
            loaded.append(githubtickets)
        if nhmoderation := await _load_subcog(bot, ".nhmoderation", "NHModeration"):
            loaded.append(nhmoderation)
        if custom_commands := await _load_custom_commands(bot, nhmisc):
            loaded.append(custom_commands)
    except asyncio.CancelledError:
        await _cleanup_cancelled_setup(bot, loaded)
        raise


async def teardown(bot: Red) -> None:
    for name in ("Cleanup", "CustomCommands", "CustomCommandsMigration"):
        cog = bot.get_cog(name)
        if cog is None:
            continue
        if not type(cog).__module__.startswith(("NHCogs.cleanup", "NHCogs.custom_commands")):
            continue
        await bot.remove_cog(name)
