from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import (
    NewTicket,
    NextAction,
    PingReservation,
    PresenceTier,
    RoutingMode,
    Ticket,
    TicketState,
)
from .projection import ProjectionNotFound, TicketProjection
from .routing import CandidateFacts, select_reviewer
from .settings import GuildSettings
from .store import GitHubTicketsStore

PERMISSION_DENIED = "You cannot use this action"
INACTIVE_TICKET = "This ticket is no longer active"
CLAIM_RACE_LOST = "This ticket has already been claimed"
MISSING_TICKET_CHANNEL = "Ticket channel is not configured"
MISSING_AUTOMATIC_CATEGORIES = "Select at least one category for automatic pings"
MISSING_DIRECT_REVIEWER = "Select a reviewer for direct pings"
CREATE_FAILED = "Could not create the ticket"
ACTION_FAILED = "Could not complete this action"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class TicketActor:
    user_id: int
    is_participant: bool
    can_manage_messages: bool

    @property
    def can_participate(self) -> bool:
        return self.is_participant or self.can_manage_messages


@dataclass(frozen=True, slots=True)
class TicketRequest:
    guild_id: int
    pr_title: str
    pr_url: str
    category_display: str
    routing_mode: RoutingMode
    direct_target_id: int | None
    category_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TicketResult:
    success: bool
    response: str | None = None


SettingsGetter = Callable[[int], Awaitable[GuildSettings]]
CandidatesGetter = Callable[[Ticket], Awaitable[Sequence[CandidateFacts]]]


class TicketCoordinator:
    def __init__(
        self,
        store: GitHubTicketsStore,
        projection: TicketProjection,
        *,
        get_settings: SettingsGetter,
        get_candidates: CandidatesGetter,
        wake_deadlines: Callable[[], None],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._projection = projection
        self._get_settings = get_settings
        self._get_candidates = get_candidates
        self._wake_deadlines = wake_deadlines
        self._clock = clock
        self._locks: dict[int, asyncio.Lock] = {}

    async def create_ticket(
        self,
        request: TicketRequest,
        actor: TicketActor,
    ) -> TicketResult:
        if not actor.can_participate:
            return TicketResult(False, PERMISSION_DENIED)
        settings = await self._get_settings(request.guild_id)
        if settings.ticket_channel_id is None:
            return TicketResult(False, MISSING_TICKET_CHANNEL)
        validation_error = self._validate_request(request)
        if validation_error is not None:
            return TicketResult(False, validation_error)

        now = self._clock()
        try:
            ticket = await self._store.create_ticket(
                NewTicket(
                    guild_id=request.guild_id,
                    channel_id=settings.ticket_channel_id,
                    author_id=actor.user_id,
                    pr_title=request.pr_title,
                    pr_url=request.pr_url,
                    category_display=request.category_display,
                    routing_mode=request.routing_mode,
                    direct_target_id=request.direct_target_id,
                    category_ids=request.category_ids,
                    created_at=now,
                )
            )
        except Exception:
            return TicketResult(False, CREATE_FAILED)
        try:
            message_id = await self._projection.send_ticket(ticket)
            if not await self._store.record_ticket_message(
                ticket.ticket_id,
                message_id,
                now,
            ):
                raise RuntimeError("ticket message reservation lost its creating state")
            thread_id = await self._projection.create_thread(ticket, message_id)
            if not await self._store.record_ticket_thread(
                ticket.ticket_id,
                thread_id,
                now,
            ):
                raise RuntimeError("ticket thread reservation lost its creating state")
            protection_until, next_action, next_action_at = self._creation_schedule(
                request.routing_mode,
                settings,
                now,
            )
            activated = await self._store.activate_ticket(
                ticket.ticket_id,
                message_id=message_id,
                thread_id=thread_id,
                protection_until=protection_until,
                next_action=next_action,
                next_action_at=next_action_at,
                updated_at=now,
            )
            if not activated:
                raise RuntimeError("ticket activation lost its creating state")
        except Exception:
            await self._cleanup_failed_creation(ticket, locals().get("message_id"), locals().get("thread_id"))
            return TicketResult(False, CREATE_FAILED)

        if next_action_at is not None:
            self._wake_deadlines()
        return TicketResult(True)

    async def claim(self, ticket_id: int, actor: TicketActor) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state not in (TicketState.OPEN, TicketState.CLAIMED):
                return TicketResult(False, INACTIVE_TICKET)
            if ticket.state is TicketState.CLAIMED:
                return TicketResult(False, CLAIM_RACE_LOST)
            if not (
                actor.can_participate
                or actor.user_id == ticket.direct_target_id
            ):
                return TicketResult(False, PERMISSION_DENIED)
            return await self._claim_open(ticket, actor)

    async def _claim_open(self, ticket: Ticket, actor: TicketActor) -> TicketResult:
        settings = await self._get_settings(ticket.guild_id)
        now = self._clock()
        protection_until = now + timedelta(seconds=settings.protection_seconds)
        if not await self._store.claim(
            ticket.ticket_id,
            actor.user_id,
            protection_until,
            now,
        ):
            current = await self._store.get_ticket(ticket.ticket_id)
            if current is not None and current.state is TicketState.CLAIMED:
                return TicketResult(False, CLAIM_RACE_LOST)
            return TicketResult(False, INACTIVE_TICKET)

        claimed = await self._store.get_ticket(ticket.ticket_id)
        if claimed is None:
            return TicketResult(False, INACTIVE_TICKET)
        return await self._edit_after_transition(claimed)

    async def decline(self, ticket_id: int, actor: TicketActor) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state is not TicketState.OPEN:
                return TicketResult(False, INACTIVE_TICKET)
            if not (
                actor.can_participate
                or actor.user_id == ticket.direct_target_id
            ):
                return TicketResult(False, PERMISSION_DENIED)
            return await self._decline_open(ticket, actor)

    async def _decline_open(self, ticket: Ticket, actor: TicketActor) -> TicketResult:
        settings = await self._get_settings(ticket.guild_id)
        now = self._clock()
        protection_until = now + timedelta(seconds=settings.protection_seconds)
        is_current_target = actor.user_id in (
            ticket.current_target_id,
            ticket.pending_target_id,
        )
        next_action, next_action_at = (
            self._release_schedule(ticket.routing_mode, protection_until)
            if is_current_target
            else (None, None)
        )
        changed = await self._store.decline(
            ticket.ticket_id,
            actor.user_id,
            now,
            protection_until=protection_until,
            next_action=next_action,
            next_action_at=next_action_at,
        )
        if not changed:
            return await self._repeated_decline_result(ticket.ticket_id, actor.user_id)
        if not is_current_target:
            return TicketResult(True)
        current = await self._store.get_ticket(ticket.ticket_id)
        if current is None:
            return TicketResult(False, INACTIVE_TICKET)
        result = await self._edit_after_transition(current)
        if result.success and next_action_at is not None:
            self._wake_deadlines()
        return result

    async def _repeated_decline_result(
        self,
        ticket_id: int,
        user_id: int,
    ) -> TicketResult:
        current = await self._store.get_ticket(ticket_id)
        if current is None or current.state is not TicketState.OPEN:
            return TicketResult(False, INACTIVE_TICKET)
        exclusions = await self._store.list_exclusions(ticket_id)
        if any(item.user_id == user_id for item in exclusions):
            return TicketResult(True)
        return TicketResult(False, ACTION_FAILED)

    async def unassign(self, ticket_id: int, actor: TicketActor) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state is not TicketState.CLAIMED:
                return TicketResult(False, INACTIVE_TICKET)
            if not (
                actor.can_manage_messages
                or actor.user_id == ticket.assignee_id
            ):
                return TicketResult(False, PERMISSION_DENIED)

            settings = await self._get_settings(ticket.guild_id)
            now = self._clock()
            protection_until = now + timedelta(seconds=settings.protection_seconds)
            next_action, next_action_at = self._release_schedule(
                ticket.routing_mode,
                protection_until,
            )
            former_assignee = await self._store.unassign(
                ticket_id,
                protection_until=protection_until,
                next_action=next_action,
                next_action_at=next_action_at,
                updated_at=now,
            )
            if former_assignee is None:
                return TicketResult(False, INACTIVE_TICKET)
            current = await self._store.get_ticket(ticket_id)
            if current is None:
                return TicketResult(False, INACTIVE_TICKET)
            result = await self._edit_after_transition(current)
            if result.success and next_action_at is not None:
                self._wake_deadlines()
            return result

    async def mark_finished(self, ticket_id: int, actor: TicketActor) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state not in (TicketState.OPEN, TicketState.CLAIMED):
                return TicketResult(False, INACTIVE_TICKET)
            if not (
                actor.can_manage_messages
                or actor.user_id in (ticket.author_id, ticket.assignee_id)
            ):
                return TicketResult(False, PERMISSION_DENIED)
            if not await self._store.begin_finishing(ticket_id, self._clock()):
                return TicketResult(False, INACTIVE_TICKET)
            finishing = await self._store.get_ticket(ticket_id)
            if finishing is None:
                return TicketResult(True)
            try:
                await self._delete_remaining_projection(finishing)
            except Exception:
                return TicketResult(False, ACTION_FAILED)
            self._locks.pop(ticket_id, None)
            return TicketResult(True)

    async def handle_message_deleted(self, message_id: int) -> None:
        ticket = await self._store.get_ticket_by_message_id(message_id)
        if ticket is None:
            return
        async with self._ticket_lock(ticket.ticket_id):
            current = await self._store.get_ticket(ticket.ticket_id)
            if current is None or current.message_id != message_id:
                return
            if current.state in (TicketState.OPEN, TicketState.CLAIMED):
                if not await self._store.begin_finishing(current.ticket_id, self._clock()):
                    return
                current = await self._store.get_ticket(current.ticket_id)
                if current is None:
                    return
            try:
                await self._delete_remaining_projection(current, message_absent=True)
            except Exception:
                return
            self._locks.pop(current.ticket_id, None)

    async def handle_thread_deleted(self, thread_id: int) -> None:
        ticket = await self._store.get_ticket_by_thread_id(thread_id)
        if ticket is None:
            return
        async with self._ticket_lock(ticket.ticket_id):
            current = await self._store.get_ticket(ticket.ticket_id)
            if current is None or current.thread_id != thread_id:
                return
            if current.state in (TicketState.OPEN, TicketState.CLAIMED):
                if not await self._store.begin_finishing(current.ticket_id, self._clock()):
                    return
                current = await self._store.get_ticket(current.ticket_id)
                if current is None:
                    return
            try:
                await self._delete_remaining_projection(current, thread_absent=True)
            except Exception:
                return
            self._locks.pop(current.ticket_id, None)

    async def recover_projection_cleanup(self, ticket_id: int) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None:
                return TicketResult(True)
            if ticket.state not in (TicketState.CREATING, TicketState.FINISHING):
                return TicketResult(False, INACTIVE_TICKET)
            try:
                await self._delete_remaining_projection(ticket)
            except Exception:
                return TicketResult(False, ACTION_FAILED)
            self._locks.pop(ticket_id, None)
            return TicketResult(True)

    async def process_due(self, ticket_id: int) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            now = self._clock()
            if ticket is None or ticket.state is not TicketState.OPEN:
                return TicketResult(False, INACTIVE_TICKET)
            if ticket.next_action is None or ticket.next_action_at is None:
                return TicketResult(True)
            if ticket.next_action_at > now:
                return TicketResult(True)

            settings = await self._get_settings(ticket.guild_id)
            if ticket.next_action is NextAction.TARGET_TIMEOUT:
                return await self._process_target_timeout(ticket, settings, now)
            if ticket.protection_until is not None and ticket.protection_until > now:
                await self._store.defer_due_ping(
                    ticket.ticket_id,
                    ticket.protection_until,
                    now,
                )
                self._wake_deadlines()
                return TicketResult(True)
            return await self._process_due_ping(ticket, settings, now)

    async def _process_due_ping(
        self,
        ticket: Ticket,
        settings: GuildSettings,
        now: datetime,
    ) -> TicketResult:
        next_action = ticket.next_action
        assert next_action is not None
        if ticket.ping_count >= settings.max_pings:
            await self._store.exhaust_due_routing(
                ticket.ticket_id,
                next_action,
                now,
            )
            return TicketResult(True)
        reservation = self._pending_reservation(ticket)
        if reservation is None:
            reservation = await self._reserve_new_ping(ticket, settings, now)
        if reservation is None:
            await self._store.exhaust_due_routing(
                ticket.ticket_id,
                next_action,
                now,
            )
            return TicketResult(True)
        effect_result = await self._send_ping_effect(ticket, reservation, settings, now)
        if effect_result is not None:
            return effect_result

        acknowledged = await self._store.acknowledge_ping(ticket.ticket_id, now)
        if acknowledged is None:
            return TicketResult(False, INACTIVE_TICKET)
        current = await self._store.get_ticket(ticket.ticket_id)
        if current is None:
            return TicketResult(False, INACTIVE_TICKET)
        result = await self._edit_after_transition(current)
        if result.success:
            self._wake_deadlines()
        return result

    async def _send_ping_effect(
        self,
        ticket: Ticket,
        reservation: PingReservation,
        settings: GuildSettings,
        now: datetime,
    ) -> TicketResult | None:
        if ticket.thread_id is None:
            await self._delete_remaining_projection(ticket, thread_absent=True)
            return TicketResult(True)
        try:
            await self._projection.ping_reviewer(
                ticket.thread_id,
                reservation.target_user_id,
                reservation.automatic,
            )
        except ProjectionNotFound:
            await self._delete_remaining_projection(ticket, thread_absent=True)
            return TicketResult(True)
        except Exception:
            retry_at = now + timedelta(seconds=settings.protection_seconds)
            await self._store.defer_due_ping(ticket.ticket_id, retry_at, now)
            self._wake_deadlines()
            return TicketResult(False, ACTION_FAILED)
        return None

    async def _reserve_new_ping(
        self,
        ticket: Ticket,
        settings: GuildSettings,
        now: datetime,
    ) -> PingReservation | None:
        if ticket.ping_count >= settings.max_pings:
            return None
        if ticket.next_action is NextAction.DIRECT_PING:
            if ticket.direct_target_id is None:
                return None
            target_user_id = ticket.direct_target_id
            presence_tier = None
            automatic = False
            response_seconds = settings.direct_response_seconds
        else:
            candidate = select_reviewer(await self._get_candidates(ticket))
            if candidate is None:
                return None
            target_user_id = candidate.user_id
            presence_tier = candidate.presence_tier
            automatic = True
            response_seconds = self._automatic_response_seconds(
                settings,
                candidate.presence_tier,
            )
        return await self._store.reserve_ping(
            ticket.ticket_id,
            target_user_id=target_user_id,
            presence_tier=presence_tier,
            automatic=automatic,
            reserved_at=now,
            response_deadline=now + timedelta(seconds=response_seconds),
            maximum_pings=settings.max_pings,
        )

    @staticmethod
    def _pending_reservation(ticket: Ticket) -> PingReservation | None:
        if (
            ticket.pending_target_id is None
            or ticket.pending_ping_automatic is None
            or ticket.pending_response_deadline is None
        ):
            return None
        return PingReservation(
            ticket_id=ticket.ticket_id,
            target_user_id=ticket.pending_target_id,
            presence_tier=ticket.pending_presence_tier,
            automatic=ticket.pending_ping_automatic,
            response_deadline=ticket.pending_response_deadline,
        )

    async def _process_target_timeout(
        self,
        ticket: Ticket,
        settings: GuildSettings,
        now: datetime,
    ) -> TicketResult:
        if ticket.current_target_id is None:
            await self._store.exhaust_due_routing(
                ticket.ticket_id,
                NextAction.TARGET_TIMEOUT,
                now,
            )
            return TicketResult(True)
        protection_until = now + timedelta(seconds=settings.protection_seconds)
        next_action, next_action_at = self._release_schedule(
            ticket.routing_mode,
            protection_until,
        )
        settled = await self._store.settle_target_timeout(
            ticket.ticket_id,
            target_user_id=ticket.current_target_id,
            protection_until=protection_until,
            next_action=next_action,
            next_action_at=next_action_at,
            updated_at=now,
        )
        if not settled:
            return TicketResult(False, INACTIVE_TICKET)
        current = await self._store.get_ticket(ticket.ticket_id)
        if current is None:
            return TicketResult(False, INACTIVE_TICKET)
        result = await self._edit_after_transition(current)
        if result.success and next_action_at is not None:
            self._wake_deadlines()
        return result

    @staticmethod
    def _automatic_response_seconds(
        settings: GuildSettings,
        presence_tier: PresenceTier,
    ) -> int:
        if presence_tier is PresenceTier.ONLINE:
            return settings.online_response_seconds
        if presence_tier is PresenceTier.IDLE:
            return settings.idle_response_seconds
        if presence_tier is PresenceTier.DO_NOT_DISTURB:
            return settings.dnd_response_seconds
        return settings.offline_response_seconds

    async def _edit_after_transition(self, ticket: Ticket) -> TicketResult:
        try:
            await self._projection.edit_ticket(ticket)
        except ProjectionNotFound:
            await self._delete_remaining_projection(ticket, message_absent=True)
        except Exception:
            return TicketResult(False, ACTION_FAILED)
        return TicketResult(True)

    @staticmethod
    def _release_schedule(
        routing_mode: RoutingMode,
        protection_until: datetime,
    ) -> tuple[NextAction | None, datetime | None]:
        if routing_mode in (RoutingMode.AUTOMATIC, RoutingMode.DIRECT_AUTOMATIC):
            return NextAction.AUTOMATIC_PING, protection_until
        return None, None

    def _ticket_lock(self, ticket_id: int) -> asyncio.Lock:
        return self._locks.setdefault(ticket_id, asyncio.Lock())

    async def _delete_remaining_projection(
        self,
        ticket: Ticket,
        *,
        message_absent: bool = False,
        thread_absent: bool = False,
    ) -> None:
        if ticket.thread_id is not None and not thread_absent:
            try:
                await self._projection.delete_thread(ticket.thread_id)
            except ProjectionNotFound:
                pass
        if ticket.message_id is not None and not message_absent:
            try:
                await self._projection.delete_message(ticket.channel_id, ticket.message_id)
            except ProjectionNotFound:
                pass
        await self._store.delete_ticket(ticket.ticket_id)

    @staticmethod
    def _validate_request(request: TicketRequest) -> str | None:
        if request.routing_mode in (
            RoutingMode.AUTOMATIC,
            RoutingMode.DIRECT_AUTOMATIC,
        ) and not request.category_ids:
            return MISSING_AUTOMATIC_CATEGORIES
        if request.routing_mode in (
            RoutingMode.DIRECT_WAIT,
            RoutingMode.DIRECT_AUTOMATIC,
        ) and request.direct_target_id is None:
            return MISSING_DIRECT_REVIEWER
        return None

    @staticmethod
    def _creation_schedule(
        routing_mode: RoutingMode,
        settings: GuildSettings,
        now: datetime,
    ) -> tuple[datetime, NextAction | None, datetime | None]:
        protection_until = now + timedelta(seconds=settings.protection_seconds)
        if routing_mode is RoutingMode.AUTOMATIC:
            volunteer_until = now + timedelta(seconds=settings.volunteer_seconds)
            deadline = max(protection_until, volunteer_until)
            return protection_until, NextAction.AUTOMATIC_PING, deadline
        if routing_mode in (RoutingMode.DIRECT_WAIT, RoutingMode.DIRECT_AUTOMATIC):
            return protection_until, NextAction.DIRECT_PING, protection_until
        return protection_until, None, None

    async def _cleanup_failed_creation(
        self,
        ticket: Ticket,
        message_id: object,
        thread_id: object,
    ) -> None:
        if isinstance(thread_id, int):
            try:
                await self._projection.delete_thread(thread_id)
            except ProjectionNotFound:
                pass
            except Exception:
                return
        if isinstance(message_id, int):
            try:
                await self._projection.delete_message(ticket.channel_id, message_id)
            except ProjectionNotFound:
                pass
            except Exception:
                return
        await self._store.delete_ticket(ticket.ticket_id)
