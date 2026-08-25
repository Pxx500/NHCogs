from __future__ import annotations

from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from . import settings
from .store import GitHubTicketsStore


class GitHubTickets(commands.Cog):
    """Configure GitHub Tickets"""

    CONFIG_IDENTIFIER = 228724500916148494760637198509440112622

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=self.CONFIG_IDENTIFIER,
            force_registration=True,
        )
        self.config.register_guild(**settings.DEFAULTS)
        self.store = GitHubTicketsStore(cog_data_path(self) / "githubtickets.sqlite")

    async def cog_load(self) -> None:
        await self.store.initialize()
