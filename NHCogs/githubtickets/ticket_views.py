from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

import discord

from . import presentation

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .coordinator import TicketActor, TicketResult
    from .models import Category

TicketAction = Callable[[str, "TicketActor"], Awaitable["TicketResult"]]
AddCategoriesAction = Callable[
    [str, tuple[int, ...], "TicketActor"],
    Awaitable["TicketResult"],
]
CategoriesGetter = Callable[[int], Awaitable[Sequence["Category"]]]
ActorFactory = Callable[[discord.Interaction], "TicketActor"]


class TicketControls(discord.ui.View):
    def __init__(
        self,
        ticket_id: int,
        public_token: str,
        *,
        claimed: bool,
        actor_factory: ActorFactory,
        claim: TicketAction,
        decline: TicketAction,
        unassign: TicketAction,
        mark_finished: TicketAction,
    ) -> None:
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.public_token = public_token
        self._actor_factory = actor_factory
        actions = [
            (presentation.MARK_FINISHED, "mark_finished", discord.ButtonStyle.success, mark_finished),
        ]
        if claimed:
            actions.append((presentation.UNASSIGN, "unassign", discord.ButtonStyle.danger, unassign))
        else:
            actions.extend(
                (
                    (presentation.CLAIM, "claim", discord.ButtonStyle.primary, claim),
                    (presentation.DECLINE, "decline", discord.ButtonStyle.danger, decline),
                )
            )

        for label, action_name, style, action in actions:
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"githubtickets:{public_token}:{action_name}",
            )

            async def callback(interaction, selected_action=action):
                await interaction.response.defer()
                try:
                    actor = self._actor_factory(interaction)
                    result = await selected_action(self.public_token, actor)
                except Exception:
                    log.exception("GitHub Tickets control callback failed")
                    await interaction.followup.send(
                        presentation.COULD_NOT_COMPLETE_ACTION,
                        ephemeral=True,
                    )
                    return
                if result.success:
                    return
                await interaction.followup.send(result.response, ephemeral=True)

            button.callback = callback
            self.add_item(button)


class GitHubCategoryPrompt(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        public_token: str,
        *,
        actor_factory: ActorFactory,
        get_categories: CategoriesGetter,
        add_categories: AddCategoriesAction,
    ) -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.public_token = public_token
        self._actor_factory = actor_factory
        self._get_categories = get_categories
        self._add_categories = add_categories

        button = discord.ui.Button(
            label=presentation.ADD_CATEGORIES,
            style=discord.ButtonStyle.primary,
            custom_id=f"githubtickets:{public_token}:add_categories",
        )
        button.callback = self._open_categories
        self.add_item(button)

    async def _open_categories(self, interaction: discord.Interaction) -> None:
        try:
            actor = self._actor_factory(interaction)
            if not actor.can_participate:
                await interaction.response.send_message(
                    presentation.CANNOT_USE_ACTION,
                    ephemeral=True,
                )
                return
            categories = tuple(await self._get_categories(self.guild_id))
        except Exception:
            log.exception("GitHub Tickets category prompt failed")
            await interaction.response.send_message(
                presentation.COULD_NOT_COMPLETE_ACTION,
                ephemeral=True,
            )
            return
        if not categories:
            await interaction.response.send_message(
                presentation.NO_CATEGORIES_CONFIGURED,
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            presentation.SELECT_CATEGORIES,
            view=TicketCategorySelection(
                self.guild_id,
                self.public_token,
                categories,
                actor_factory=self._actor_factory,
                get_categories=self._get_categories,
                add_categories=self._add_categories,
            ),
            ephemeral=True,
        )


class TicketCategorySelection(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        public_token: str,
        categories: Sequence[Category],
        *,
        actor_factory: ActorFactory,
        get_categories: CategoriesGetter,
        add_categories: AddCategoriesAction,
    ) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.public_token = public_token
        self._actor_factory = actor_factory
        self._get_categories = get_categories
        self._add_categories = add_categories
        self.categories = discord.ui.Select(
            placeholder=presentation.SELECT_CATEGORIES,
            options=[
                discord.SelectOption(
                    label=category.name,
                    value=str(category.category_id),
                )
                for category in categories
            ],
            min_values=1,
            max_values=len(categories),
            required=True,
        )
        self.add_item(self.categories)

        confirm = discord.ui.Button(
            label=presentation.CONFIRM_CATEGORIES,
            style=discord.ButtonStyle.primary,
        )
        confirm.callback = self._confirm
        self.add_item(confirm)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            actor = self._actor_factory(interaction)
            if not actor.can_participate:
                await interaction.followup.send(
                    presentation.CANNOT_USE_ACTION,
                    ephemeral=True,
                )
                return
            selected_ids = tuple(int(value) for value in self.categories.values)
            current_ids = {
                category.category_id
                for category in await self._get_categories(self.guild_id)
            }
            if any(category_id not in current_ids for category_id in selected_ids):
                await interaction.followup.send(
                    presentation.CATEGORY_NO_LONGER_EXISTS,
                    ephemeral=True,
                )
                return
            result = await self._add_categories(
                self.public_token,
                selected_ids,
                actor,
            )
        except Exception:
            log.exception("GitHub Tickets category selection failed")
            await interaction.followup.send(
                presentation.COULD_NOT_COMPLETE_ACTION,
                ephemeral=True,
            )
            return
        if not result.success:
            await interaction.followup.send(result.response, ephemeral=True)
            return
        await interaction.edit_original_response(
            content=presentation.CATEGORIES_ADDED,
            view=None,
        )


class DraftTicketControls(discord.ui.View):
    def __init__(
        self,
        public_token: str,
        *,
        actor_factory: ActorFactory,
        keep_ticket: TicketAction,
        remove_ticket: TicketAction,
    ) -> None:
        super().__init__(timeout=None)
        self.public_token = public_token
        self._actor_factory = actor_factory
        self._keep_ticket = keep_ticket
        self._remove_ticket = remove_ticket

        keep = discord.ui.Button(
            label=presentation.KEEP_TICKET,
            style=discord.ButtonStyle.primary,
            custom_id=f"githubtickets:{public_token}:keep_draft_ticket",
        )
        keep.callback = self._keep
        self.add_item(keep)
        self._add_remove_button()

    def _add_remove_button(self) -> None:
        remove = discord.ui.Button(
            label=presentation.REMOVE_TICKET,
            style=discord.ButtonStyle.danger,
            custom_id=f"githubtickets:{self.public_token}:remove_draft_ticket",
        )
        remove.callback = self._remove
        self.add_item(remove)

    async def _keep(self, interaction: discord.Interaction) -> None:
        try:
            actor = self._actor_factory(interaction)
            result = await self._keep_ticket(self.public_token, actor)
        except Exception:
            log.exception("GitHub Tickets keep-draft callback failed")
            await interaction.response.send_message(
                presentation.COULD_NOT_COMPLETE_ACTION,
                ephemeral=True,
            )
            return
        if not result.success:
            await interaction.response.send_message(result.response, ephemeral=True)
            return
        await interaction.response.edit_message(
            view=RetainedDraftTicketControls(
                self.public_token,
                actor_factory=self._actor_factory,
                remove_ticket=self._remove_ticket,
            )
        )

    async def _remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            actor = self._actor_factory(interaction)
            result = await self._remove_ticket(self.public_token, actor)
        except Exception:
            log.exception("GitHub Tickets remove-draft callback failed")
            await interaction.followup.send(
                presentation.COULD_NOT_COMPLETE_ACTION,
                ephemeral=True,
            )
            return
        if not result.success:
            await interaction.followup.send(result.response, ephemeral=True)


class RetainedDraftTicketControls(DraftTicketControls):
    def __init__(
        self,
        public_token: str,
        *,
        actor_factory: ActorFactory,
        remove_ticket: TicketAction,
    ) -> None:
        discord.ui.View.__init__(self, timeout=None)
        self.public_token = public_token
        self._actor_factory = actor_factory
        self._remove_ticket = remove_ticket
        self._add_remove_button()
