from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

from . import presentation

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .coordinator import TicketActor, TicketResult

TicketAction = Callable[[str, "TicketActor"], Awaitable["TicketResult"]]
ActorFactory = Callable[[discord.Interaction], "TicketActor"]


class TicketControls(discord.ui.View):
    def __init__(
        self,
        ticket_id: int,
        public_token: str,
        *,
        support,
        guild_id: int,
        claimed: bool,
        actor_factory: ActorFactory,
        claim: TicketAction,
        decline: TicketAction,
        unassign: TicketAction,
        mark_finished: TicketAction,
    ) -> None:
        super().__init__(timeout=None)
        self._support = support
        self._guild_id = guild_id
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
                except Exception as error:
                    await self._support.report_operational_error(
                        guild_id=self._guild_id, source="GitHubTickets", action="ticket control", error=error,
                        channel_id=getattr(interaction, "channel_id", None),
                        message_id=getattr(getattr(interaction, "message", None), "id", None),
                    )
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


    async def on_error(self, interaction: discord.Interaction, error: Exception, _item) -> None:
        await self._support.report_operational_error(
            guild_id=self._guild_id, source="GitHubTickets", action="ticket control feedback", error=error,
            channel_id=getattr(interaction, "channel_id", None),
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
