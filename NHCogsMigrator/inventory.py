from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SuiteInventory:
    prefix_commands: tuple[str, ...]
    listeners: tuple[str, ...]
    application_commands: tuple[str, ...]
    persistent_view_custom_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "prefix_commands": list(self.prefix_commands),
            "listeners": list(self.listeners),
            "application_commands": list(self.application_commands),
            "persistent_view_custom_ids": list(self.persistent_view_custom_ids),
        }


def snapshot_suite_inventory(
    bot: Any,
    cog_names: tuple[str, ...],
) -> SuiteInventory:
    prefix_commands: list[str] = []
    listeners: list[str] = []
    for cog_name in cog_names:
        cog = bot.get_cog(cog_name)
        if cog is None:
            raise RuntimeError(f"required cog {cog_name} is not loaded")
        for command in cog.walk_commands():
            aliases = ",".join(sorted(str(alias) for alias in command.aliases))
            prefix_commands.append(
                f"{cog_name}:{command.qualified_name}:{aliases}"
            )
        for event, callback in cog.get_listeners():
            callback_name = getattr(callback, "__name__", type(callback).__name__)
            listeners.append(f"{cog_name}:{event}:{callback_name}")

    application_commands = tuple(
        sorted(
            f"{_command_type(command)}:{command.name}"
            for command in bot.tree.get_commands()
        )
    )
    persistent_ids = []
    for view in getattr(bot, "persistent_views", ()):
        for item in getattr(view, "children", ()):
            custom_id = getattr(item, "custom_id", None)
            if custom_id is not None:
                persistent_ids.append(str(custom_id))

    return SuiteInventory(
        prefix_commands=tuple(sorted(prefix_commands)),
        listeners=tuple(sorted(listeners)),
        application_commands=application_commands,
        persistent_view_custom_ids=tuple(sorted(persistent_ids)),
    )


def _command_type(command: Any) -> str:
    command_type = getattr(command, "type", "unknown")
    return str(getattr(command_type, "value", command_type))
