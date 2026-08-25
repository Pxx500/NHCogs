from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .coordinator import TicketActor, TicketResult

TicketAction = Callable[[int, "TicketActor"], Awaitable["TicketResult"]]
ActorFactory = Callable[[discord.Interaction], "TicketActor"]


class TicketControls(discord.ui.View):
    def __init__(
        self,
        ticket_id: int,
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
        self._actor_factory = actor_factory
        actions = [
            ("Mark Finished", "mark_finished", discord.ButtonStyle.success, mark_finished),
        ]
        if claimed:
            actions.append(("Unassign", "unassign", discord.ButtonStyle.danger, unassign))
        else:
            actions.extend(
                (
                    ("Claim", "claim", discord.ButtonStyle.primary, claim),
                    ("Decline", "decline", discord.ButtonStyle.danger, decline),
                )
            )

        for label, action_name, style, action in actions:
            button = discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"githubtickets:{ticket_id}:{action_name}",
            )

            async def callback(interaction, selected_action=action):
                await interaction.response.defer()
                actor = self._actor_factory(interaction)
                result = await selected_action(self.ticket_id, actor)
                if result.success:
                    return
                await interaction.followup.send(result.response, ephemeral=True)

            button.callback = callback
            self.add_item(button)
