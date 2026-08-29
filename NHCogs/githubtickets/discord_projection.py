from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import discord

from .models import Ticket
from .presentation import (
    add_categories_notification,
    automatic_review_notification,
    direct_review_notification,
    draft_ticket_notification,
    thread_name,
    ticket_message,
)
from .projection import ProjectionNotFound, ProjectionUnavailable

RECOVERY_LOOKBACK = timedelta(seconds=5)


class DiscordTicketProjection:
    def __init__(
        self,
        bot,
        view_factory: Callable[[Ticket], discord.ui.View],
        *,
        category_prompt_view_factory: Callable[[Ticket], discord.ui.View],
        draft_prompt_view_factory: Callable[[Ticket], discord.ui.View],
    ) -> None:
        self._bot = bot
        self._view_factory = view_factory
        self._category_prompt_view_factory = category_prompt_view_factory
        self._draft_prompt_view_factory = draft_prompt_view_factory
        self._sent_messages: dict[int, Any] = {}

    async def send_ticket(
        self,
        ticket: Ticket,
        *,
        reviewer_github: str | None = None,
    ) -> int:
        channel = self._cached_channel(ticket.channel_id)
        try:
            message = await channel.send(
                self._ticket_content(ticket, reviewer_github=reviewer_github),
                view=self._ticket_view(ticket),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound as error:
            raise ProjectionNotFound from error
        self._sent_messages[message.id] = message
        return message.id

    async def find_ticket_message(self, ticket: Ticket) -> int | None:
        channel = await self._recovery_channel(ticket.channel_id)
        history = getattr(channel, "history", None)
        if not callable(history):
            raise ProjectionUnavailable("Discord channel history is unavailable")
        prefix = f"githubtickets:{ticket.public_token}:"
        async for message in history(
            after=ticket.created_at - RECOVERY_LOOKBACK,
            oldest_first=True,
            limit=100,
        ):
            if self._is_bot_authored(message) and self._has_custom_id(message, prefix):
                return int(message.id)
        return None

    async def find_ticket_thread(self, ticket: Ticket) -> int | None:
        if ticket.message_id is None:
            return None
        channel = await self._recovery_channel(ticket.channel_id)
        try:
            message = await channel.fetch_message(ticket.message_id)
        except discord.NotFound as error:
            raise ProjectionNotFound from error
        thread = getattr(message, "thread", None)
        if thread is not None:
            return int(thread.id)
        try:
            thread = await self._bot.fetch_channel(ticket.message_id)
        except discord.NotFound:
            return None
        return int(thread.id)

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

    async def prompt_categories(self, ticket: Ticket, thread_id: int) -> None:
        author_mention = (
            f"<@{ticket.author_id}>" if ticket.author_id is not None else None
        )
        content = (
            add_categories_notification(author_mention)
            if author_mention is not None
            else "Add categories to start automatic routing"
        )
        await self._send_thread_prompt(
            thread_id,
            content,
            view=self._category_prompt_view_factory(ticket),
            user_id=ticket.author_id,
        )

    async def prompt_draft_decision(self, ticket: Ticket) -> None:
        if ticket.thread_id is None:
            raise ProjectionNotFound
        author_mention = (
            f"<@{ticket.author_id}>" if ticket.author_id is not None else None
        )
        await self._send_thread_prompt(
            ticket.thread_id,
            draft_ticket_notification(author_mention),
            view=self._draft_prompt_view_factory(ticket),
            user_id=ticket.author_id,
        )

    async def edit_ticket(
        self,
        ticket: Ticket,
        *,
        reviewer_github: str | None = None,
    ) -> None:
        if ticket.message_id is None:
            raise ProjectionNotFound
        message = self._partial_message(ticket.channel_id, ticket.message_id)
        try:
            await message.edit(
                content=self._ticket_content(
                    ticket,
                    reviewer_github=reviewer_github,
                ),
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
        partial_messageable = getattr(self._bot, "get_partial_messageable", None)
        if not callable(partial_messageable):
            raise ProjectionUnavailable("Discord partial messageable is unavailable")
        thread = partial_messageable(thread_id)
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

    async def _send_thread_prompt(
        self,
        thread_id: int,
        content: str,
        *,
        view: discord.ui.View,
        user_id: int | None,
    ) -> None:
        thread = self._cached_channel(thread_id)
        allowed_mentions = (
            discord.AllowedMentions.none()
            if user_id is None
            else discord.AllowedMentions(
                everyone=False,
                users=[discord.Object(id=user_id)],
                roles=False,
                replied_user=False,
            )
        )
        try:
            await thread.send(
                content,
                view=view,
                allowed_mentions=allowed_mentions,
            )
        except discord.NotFound as error:
            raise ProjectionNotFound from error

    async def find_ping(
        self,
        thread_id: int,
        target_user_id: int,
        automatic: bool,
        reserved_at,
    ):
        thread = await self._recovery_channel(thread_id)
        history = getattr(thread, "history", None)
        if not callable(history):
            raise ProjectionUnavailable("Discord thread history is unavailable")
        target_mention = f"<@{target_user_id}>"
        expected = (
            automatic_review_notification(target_mention)
            if automatic
            else direct_review_notification(target_mention)
        )
        async for message in history(
            after=reserved_at - RECOVERY_LOOKBACK,
            oldest_first=True,
            limit=100,
        ):
            if self._is_bot_authored(message) and message.content == expected:
                return message.created_at
        return None

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        self._sent_messages.pop(message_id, None)
        message = self._partial_message(channel_id, message_id)
        try:
            await message.delete()
        except discord.NotFound as error:
            raise ProjectionNotFound from error

    async def delete_thread(self, thread_id: int) -> None:
        thread = self._bot.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self._bot.fetch_channel(thread_id)
            except discord.NotFound as error:
                raise ProjectionNotFound from error
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

    async def _recovery_channel(self, channel_id: int):
        channel = self._bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self._bot.fetch_channel(channel_id)
        except discord.NotFound as error:
            raise ProjectionNotFound from error

    def _is_bot_authored(self, message) -> bool:
        bot_user = getattr(self._bot, "user", None)
        author = getattr(message, "author", None)
        return bot_user is not None and author is not None and author.id == bot_user.id

    @staticmethod
    def _has_custom_id(message, prefix: str) -> bool:
        return any(
            getattr(component, "custom_id", "").startswith(prefix)
            for row in getattr(message, "components", ())
            for component in getattr(row, "children", ())
        )

    def _ticket_view(self, ticket: Ticket) -> discord.ui.View:
        return self._view_factory(ticket)

    @staticmethod
    def _ticket_content(
        ticket: Ticket,
        *,
        reviewer_github: str | None = None,
    ) -> str:
        categories = (ticket.category_display,) if ticket.category_display else ()
        reviewer_id = ticket.assignee_id
        reviewer_mention = f"<@{reviewer_id}>" if reviewer_id is not None else None
        return ticket_message(
            title=ticket.pr_title,
            url=ticket.pr_url,
            author_mention=f"<@{ticket.author_id}>",
            categories=categories,
            reviewer_mention=reviewer_mention,
            reviewer_github=reviewer_github,
        )
