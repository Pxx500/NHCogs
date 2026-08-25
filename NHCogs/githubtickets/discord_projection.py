from __future__ import annotations

from collections.abc import Callable
from typing import Any

import discord

from .models import Ticket, TicketState
from .presentation import (
    automatic_review_notification,
    direct_review_notification,
    thread_name,
    ticket_message,
)
from .projection import ProjectionNotFound, ProjectionUnavailable


class DiscordTicketProjection:
    def __init__(
        self,
        bot,
        view_factory: Callable[[int, bool], discord.ui.View],
    ) -> None:
        self._bot = bot
        self._view_factory = view_factory
        self._sent_messages: dict[int, Any] = {}

    async def send_ticket(self, ticket: Ticket) -> int:
        channel = self._cached_channel(ticket.channel_id)
        try:
            message = await channel.send(
                self._ticket_content(ticket),
                view=self._ticket_view(ticket),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound as error:
            raise ProjectionNotFound from error
        self._sent_messages[message.id] = message
        return message.id

    async def create_thread(self, ticket: Ticket, message_id: int) -> int:
        message = self._sent_messages.get(message_id)
        if message is None:
            channel = self._cached_channel(ticket.channel_id)
            partial_message = getattr(channel, "get_partial_message", None)
            if not callable(partial_message):
                raise ProjectionUnavailable(
                    "Discord channel cannot create partial messages"
                )
            message = partial_message(message_id)
        try:
            thread = await message.create_thread(name=thread_name(ticket.pr_title))
        except discord.NotFound as error:
            raise ProjectionNotFound from error
        self._sent_messages.pop(message_id, None)
        return thread.id

    async def edit_ticket(self, ticket: Ticket) -> None:
        if ticket.message_id is None:
            raise ProjectionNotFound
        message = self._partial_message(ticket.channel_id, ticket.message_id)
        try:
            await message.edit(
                content=self._ticket_content(ticket),
                view=self._ticket_view(ticket),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound as error:
            raise ProjectionNotFound from error

    async def ping_reviewer(
        self,
        thread_id: int,
        target_user_id: int,
        automatic: bool,
    ) -> None:
        thread = self._cached_channel(thread_id)
        target_mention = f"<@{target_user_id}>"
        content = (
            automatic_review_notification(target_mention)
            if automatic
            else direct_review_notification(target_mention)
        )
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=[discord.Object(id=target_user_id)],
            roles=False,
            replied_user=False,
        )
        try:
            await thread.send(content, allowed_mentions=allowed_mentions)
        except discord.NotFound as error:
            raise ProjectionNotFound from error

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        self._sent_messages.pop(message_id, None)
        message = self._partial_message(channel_id, message_id)
        try:
            await message.delete()
        except discord.NotFound as error:
            raise ProjectionNotFound from error

    async def delete_thread(self, thread_id: int) -> None:
        thread = self._cached_channel(thread_id)
        try:
            await thread.delete()
        except discord.NotFound as error:
            raise ProjectionNotFound from error

    def _cached_channel(self, channel_id: int):
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            raise ProjectionUnavailable("Discord channel is not cached")
        return channel

    def _partial_message(self, channel_id: int, message_id: int):
        channel = self._cached_channel(channel_id)
        get_partial_message = getattr(channel, "get_partial_message", None)
        if not callable(get_partial_message):
            raise ProjectionUnavailable("Discord channel cannot create partial messages")
        return get_partial_message(message_id)

    def _ticket_view(self, ticket: Ticket) -> discord.ui.View:
        return self._view_factory(ticket.ticket_id, ticket.state is TicketState.CLAIMED)

    @staticmethod
    def _ticket_content(ticket: Ticket) -> str:
        categories = (ticket.category_display,) if ticket.category_display else ()
        reviewer_mention = (
            f"<@{ticket.assignee_id}>" if ticket.assignee_id is not None else None
        )
        return ticket_message(
            title=ticket.pr_title,
            url=ticket.pr_url,
            author_mention=f"<@{ticket.author_id}>",
            categories=categories,
            reviewer_mention=reviewer_mention,
        )
