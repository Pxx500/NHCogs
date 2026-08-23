from __future__ import annotations

import asyncio
import logging
from typing import Any

from .catalog import CustomCommandCatalog
from .cog import CustomCommands
from .migration_state import (
    MigrationApplyError,
    MigrationPhase,
    MigrationStateStore,
)

log = logging.getLogger("red.NHCogs.CustomCommands.Lifecycle")
OFFICIAL_EXTENSION_MODULE = "redbot.cogs.customcom"
OFFICIAL_COG_MODULE = "redbot.cogs.customcom.customcom"


def assert_safe_to_replace(bot: Any) -> None:
    """Reject every unknown owner before the suite performs startup effects."""
    active = bot.get_cog("CustomCommands")
    if active is not None:
        module = type(active).__module__
        if module != OFFICIAL_COG_MODULE and not isinstance(active, CustomCommands):
            raise MigrationApplyError("Another cog owns the CustomCommands name")
    extension = bot.extensions.get("customcom")
    if extension is not None:
        module_name = getattr(extension, "__name__", None)
        spec_name = getattr(getattr(extension, "__spec__", None), "name", None)
        if OFFICIAL_EXTENSION_MODULE not in {module_name, spec_name}:
            raise MigrationApplyError(
                "The active customcom extension is not Red's official package"
            )
    for name in ("customcom", "cc"):
        command = bot.get_command(name)
        if command is not None and command.cog is not active:
            raise MigrationApplyError(f"Another cog owns the {name} command")


class ReplacementActivator:
    """Make the managed Custom Commands cog the permanent runtime owner."""

    def __init__(self, bot: Any, nhmisc: Any, catalog: CustomCommandCatalog):
        self.bot = bot
        self.nhmisc = nhmisc
        self.catalog = catalog
        self._activation_lock = asyncio.Lock()

    async def activate(self) -> CustomCommands:
        async with self._activation_lock:
            assert_safe_to_replace(self.bot)
            await self.bot.add_loaded_package("NHCogs")
            await self.bot.remove_loaded_package("customcom")
            packages = await self.bot._config.packages()
            if "customcom" in packages:
                raise MigrationApplyError("customcom remains in Red's package list")
            await self.remove_official_extension()
            active = self.bot.get_cog("CustomCommands")
            if active is not None:
                module = type(active).__module__
                if module == OFFICIAL_COG_MODULE:
                    raise MigrationApplyError("Official CustomCommands cog is still active")
                if isinstance(active, CustomCommands):
                    self.verify_public_commands(active)
                    return active
                raise MigrationApplyError("Another cog owns the CustomCommands name")
            runtime = CustomCommands(self.bot, self.nhmisc, catalog=self.catalog)
            try:
                await self.bot.add_cog(runtime)
                self.verify_public_commands(runtime)
            except Exception:
                if self.bot.get_cog("CustomCommands") is runtime:
                    try:
                        await self.bot.remove_cog(runtime.qualified_name)
                    except Exception as cleanup_error:
                        raise MigrationApplyError(
                            "Partial Custom Commands replacement could not be removed"
                        ) from cleanup_error
                raise
            return runtime

    async def remove_official_extension(self) -> None:
        extension = self.bot.extensions.get("customcom")
        if extension is None:
            return
        module_name = getattr(extension, "__name__", None)
        spec_name = getattr(getattr(extension, "__spec__", None), "name", None)
        if OFFICIAL_EXTENSION_MODULE not in {module_name, spec_name}:
            raise MigrationApplyError(
                "The active customcom extension is not Red's official package"
            )
        await self.bot.unload_extension("customcom")
        if self.bot.extensions.get("customcom") is not None:
            raise MigrationApplyError("Official customcom extension did not unload")

    def verify_public_commands(self, runtime: CustomCommands) -> None:
        for name in ("customcom", "cc"):
            command = self.bot.get_command(name)
            if command is None or command.cog is not runtime:
                raise MigrationApplyError(
                    f"Replacement does not own the {name} command"
                )


class CutoverController:
    """Own the one-way authority switch from Red Config to the SQLite catalog."""

    def __init__(
        self,
        bot: Any,
        nhmisc: Any,
        catalog: CustomCommandCatalog,
        state_store: MigrationStateStore,
    ):
        self.bot = bot
        self.nhmisc = nhmisc
        self.catalog = catalog
        self.state_store = state_store
        self._activation_lock = asyncio.Lock()
        self._runtime = ReplacementActivator(bot, nhmisc, catalog)

    async def activate_imported(self) -> CustomCommands:
        async with self._activation_lock:
            state = await self.state_store.get()
            if state.phase is MigrationPhase.COMPLETE:
                active = self.bot.get_cog("CustomCommands")
                if isinstance(active, CustomCommands):
                    return active
                raise MigrationApplyError(
                    "Migration is complete but the replacement runtime is inactive"
                )
            if state.phase is not MigrationPhase.IMPORTED_NOT_ACTIVE:
                raise MigrationApplyError("A verified import is required before cutover")
            try:
                runtime = await self._activate_runtime()
                await self.state_store.transition(
                    MigrationPhase.IMPORTED_NOT_ACTIVE,
                    MigrationPhase.COMPLETE,
                    source_digest=state.source_digest,
                    destination_digest=state.destination_digest,
                )
            except Exception as error:
                latest = await self.state_store.get()
                if latest.phase is not MigrationPhase.COMPLETE:
                    await self._restore_official_where_possible()
                raise MigrationApplyError("Custom Commands cutover failed") from error
            return runtime

    async def quiesce_official(self) -> None:
        await self._runtime.remove_official_extension()

    async def restore_official(self) -> None:
        await self._restore_official_where_possible()

    async def activate_completed(self) -> CustomCommands:
        state = await self.state_store.get()
        if state.phase is not MigrationPhase.COMPLETE:
            raise MigrationApplyError("Migration is not complete")
        return await self._activate_runtime()

    async def _activate_runtime(self) -> CustomCommands:
        return await self._runtime.activate()

    async def _remove_official_extension(self) -> None:
        await self._runtime.remove_official_extension()

    def _verify_public_commands(self, runtime: CustomCommands) -> None:
        self._runtime.verify_public_commands(runtime)

    async def _restore_official_where_possible(self) -> None:
        active = self.bot.get_cog("CustomCommands")
        if isinstance(active, CustomCommands):
            try:
                await self.bot.remove_cog(active.qualified_name)
            except Exception:
                log.warning("Failed to remove partial replacement cog", exc_info=True)
        if "customcom" in self.bot.extensions:
            return
        try:
            spec = await self.bot._cog_mgr.find_cog("customcom")
            if spec is None:
                return
            if getattr(spec, "name", None) != OFFICIAL_EXTENSION_MODULE:
                log.error(
                    "Refusing to restore non-official customcom extension %r",
                    getattr(spec, "name", None),
                )
                return
            await self.bot.load_extension(spec)
            await self.bot.add_loaded_package("customcom")
        except Exception:
            log.warning("Failed to restore official customcom after cutover", exc_info=True)
