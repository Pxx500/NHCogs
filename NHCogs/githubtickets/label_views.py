from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from .coordinator import TicketActor
from .models import Category, CategoryLimitReached
from .store import GitHubTicketsStore

ClassifyLabel = Callable[[int, str, str], Awaitable[None]]
ActorFactory = Callable[[discord.Interaction], TicketActor]


class LabelReviewLauncher(discord.ui.View):
    """Persistent entry point for the maintainer's label decisions."""

    def __init__(
        self,
        store: GitHubTicketsStore,
        guild_id: int,
        classify: ClassifyLabel,
        actor_factory: ActorFactory,
        support,
    ) -> None:
        super().__init__(timeout=None)
        self.store = store
        self.guild_id = guild_id
        self.classify = classify
        self.actor_factory = actor_factory
        self.support = support
        button = discord.ui.Button(
            label="Review labels",
            style=discord.ButtonStyle.primary,
            custom_id=f"githubtickets:labels:{guild_id}",
        )
        button.callback = self._open
        self.add_item(button)

    async def _open(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild_id != self.guild_id
            or not self.actor_factory(interaction).can_manage_messages
        ):
            await interaction.response.send_message("You cannot manage labels", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        labels = await self.store.list_labels(self.guild_id)
        if not labels:
            await interaction.followup.send("No GitHub labels discovered", ephemeral=True)
            return
        await interaction.followup.send(
            "Choose how GitHub labels are used",
            ephemeral=True,
            view=LabelReviewPanel(self, labels),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, _item) -> None:
        await self.support.report_operational_error(
            guild_id=self.guild_id,
            source="GitHubTickets",
            action="review GitHub labels",
            error=error,
        )


class LabelReviewPanel(discord.ui.View):
    def __init__(self, launcher: LabelReviewLauncher, labels: tuple[Category, ...]) -> None:
        super().__init__(timeout=300)
        self.launcher = launcher
        self.labels = labels
        self.page = 0
        self._render()

    def _render(self) -> None:
        self.clear_items()
        ordered = sorted(
            self.labels, key=lambda label: (label.classification != "pending", label.name)
        )
        self.page = min(self.page, max(0, (len(ordered) - 1) // 25))
        self.visible = ordered[self.page * 25 : (self.page + 1) * 25]
        status = {
            "pending": "Awaiting decision",
            "reviewer": "Reviewer category",
            "pr_only": "PR label only",
        }
        self.select = discord.ui.Select(
            placeholder="Select a GitHub label",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=label.name,
                    value=str(label.category_id),
                    description=status[label.classification],
                )
                for label in self.visible
            ],
        )
        self.select.callback = self._selected
        self.add_item(self.select)
        for label, callback in (
            ("Reviewer category", self._reviewer),
            ("PR label only", self._pr_only),
            ("Previous", self._previous),
            ("Next", self._next),
        ):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            button.callback = callback
            button.disabled = (label == "Previous" and self.page == 0) or (
                label == "Next" and (self.page + 1) * 25 >= len(ordered)
            )
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = (
            interaction.guild_id == self.launcher.guild_id
            and self.launcher.actor_factory(interaction).can_manage_messages
        )
        if not allowed:
            await interaction.response.send_message("You cannot manage labels", ephemeral=True)
        return allowed

    async def _selected(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._render()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page += 1
        self._render()
        await interaction.response.edit_message(view=self)

    async def _reviewer(self, interaction: discord.Interaction) -> None:
        await self._classify(interaction, "reviewer")

    async def _pr_only(self, interaction: discord.Interaction) -> None:
        await self._classify(interaction, "pr_only")

    async def _classify(self, interaction: discord.Interaction, classification: str) -> None:
        selected = set(self.select.values)
        label = next((label for label in self.visible if str(label.category_id) in selected), None)
        if label is None:
            await interaction.response.send_message("Select a label first", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await self.launcher.classify(self.launcher.guild_id, label.name, classification)
        except CategoryLimitReached:
            await interaction.followup.send(
                "At most 50 reviewer categories can be enabled", ephemeral=True
            )
            return
        except ValueError:
            await interaction.followup.send(
                "This label cannot be used as a reviewer category", ephemeral=True
            )
            return
        self.labels = await self.launcher.store.list_labels(self.launcher.guild_id)
        self._render()
        pending = sum(label.classification == "pending" for label in self.labels)
        await interaction.edit_original_response(
            content=f"Labels awaiting decision: {pending}", view=self
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await self.launcher.on_error(interaction, error, item)
