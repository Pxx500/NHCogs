from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import Ticket


class ProjectionNotFound(Exception):
    """A required Discord projection object is already absent."""


class ProjectionUnavailable(RuntimeError):
    """A required Discord projection object is not available from cache."""


class TicketProjection(Protocol):
    async def send_ticket(
        self,
        ticket: Ticket,
        *,
        reviewer_github: str | None = None,
    ) -> int: ...

    async def find_ticket_message(self, ticket: Ticket) -> int | None: ...

    async def find_ticket_thread(self, ticket: Ticket) -> int | None: ...

    async def create_thread(self, ticket: Ticket, message_id: int) -> int: ...

    async def prompt_categories(self, ticket: Ticket, thread_id: int) -> None: ...

    async def prompt_draft_decision(self, ticket: Ticket) -> None: ...

    async def edit_ticket(
        self,
        ticket: Ticket,
        *,
        reviewer_github: str | None = None,
    ) -> None: ...

    async def ping_reviewer(
        self,
        thread_id: int,
        target_user_id: int,
        automatic: bool,
    ) -> None: ...

    async def find_ping(
        self,
        thread_id: int,
        target_user_id: int,
        automatic: bool,
        reserved_at: datetime,
    ) -> datetime | None: ...

    async def delete_message(self, channel_id: int, message_id: int) -> None: ...

    async def delete_thread(self, thread_id: int) -> None: ...
