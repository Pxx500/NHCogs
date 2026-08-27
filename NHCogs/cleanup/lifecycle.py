from __future__ import annotations

import asyncio
from typing import Any

from .cog import Cleanup

OFFICIAL_EXTENSION_MODULE = "redbot.cogs.cleanup"
OFFICIAL_COG_MODULE = "redbot.cogs.cleanup.cleanup"


class CleanupReplacementError(RuntimeError):
    pass


def assert_safe_to_replace(bot: Any) -> None:
    active = bot.get_cog("Cleanup")
    if active is not None:
        module = type(active).__module__
        if module != OFFICIAL_COG_MODULE and not isinstance(active, Cleanup):
            raise CleanupReplacementError("Another cog owns the Cleanup name")
    extension = bot.extensions.get("cleanup")
    if extension is not None:
        module_name = getattr(extension, "__name__", None)
        spec_name = getattr(getattr(extension, "__spec__", None), "name", None)
        if OFFICIAL_EXTENSION_MODULE not in {module_name, spec_name}:
            raise CleanupReplacementError(
                "The active cleanup extension is not Red's official package"
            )
    command = bot.get_command("cleanup")
    if command is not None and command.cog is not active:
        raise CleanupReplacementError("Another cog owns the cleanup command")


class ReplacementActivator:
    def __init__(self, bot: Any, nhmisc: Any, honeypot: Any) -> None:
        self.bot = bot
        self.nhmisc = nhmisc
        self.honeypot = honeypot
        self._activation_lock = asyncio.Lock()

    async def activate(self) -> Cleanup:
        async with self._activation_lock:
            assert_safe_to_replace(self.bot)
            packages = await self.bot._config.packages()
            official_package_was_loaded = "cleanup" in packages
            official_extension_was_loaded = self.bot.extensions.get("cleanup") is not None
            runtime = None
            try:
                await self.bot.add_loaded_package("NHCogs")
                await self.bot.remove_loaded_package("cleanup")
                packages = await self.bot._config.packages()
                if "cleanup" in packages:
                    raise CleanupReplacementError("cleanup remains in Red's package list")
                await self._remove_official_extension()
                active = self.bot.get_cog("Cleanup")
                if isinstance(active, Cleanup):
                    self._verify_command(active)
                    return active
                if active is not None:
                    raise CleanupReplacementError("Another cog owns the Cleanup name")
                runtime = Cleanup(self.bot, self.nhmisc, self.honeypot)
                await self.bot.add_cog(runtime)
                self._verify_command(runtime)
            except Exception:
                try:
                    if runtime is not None and self.bot.get_cog("Cleanup") is runtime:
                        await self.bot.remove_cog(runtime.qualified_name)
                    await self._restore_official_cleanup(
                        package_was_loaded=official_package_was_loaded,
                        extension_was_loaded=official_extension_was_loaded,
                    )
                except Exception as rollback_error:
                    raise CleanupReplacementError(
                        "Cleanup replacement rollback failed"
                    ) from rollback_error
                raise
            if runtime is None:
                raise CleanupReplacementError("Cleanup replacement was not constructed")
            return runtime

    async def _restore_official_cleanup(
        self,
        *,
        package_was_loaded: bool,
        extension_was_loaded: bool,
    ) -> None:
        if extension_was_loaded and self.bot.extensions.get("cleanup") is None:
            await self.bot.load_extension("cleanup")
        if package_was_loaded:
            packages = await self.bot._config.packages()
            if "cleanup" not in packages:
                await self.bot.add_loaded_package("cleanup")

    async def _remove_official_extension(self) -> None:
        extension = self.bot.extensions.get("cleanup")
        if extension is None:
            return
        module_name = getattr(extension, "__name__", None)
        spec_name = getattr(getattr(extension, "__spec__", None), "name", None)
        if OFFICIAL_EXTENSION_MODULE not in {module_name, spec_name}:
            raise CleanupReplacementError(
                "The active cleanup extension is not Red's official package"
            )
        await self.bot.unload_extension("cleanup")
        if self.bot.extensions.get("cleanup") is not None:
            raise CleanupReplacementError("Official cleanup extension did not unload")

    def _verify_command(self, runtime: Cleanup) -> None:
        command = self.bot.get_command("cleanup")
        if command is None or command.cog is not runtime:
            raise CleanupReplacementError("Replacement does not own the cleanup command")


async def build_cleanup_component(bot: Any, nhmisc: Any, honeypot: Any) -> Cleanup:
    return await ReplacementActivator(bot, nhmisc, honeypot).activate()
