from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from . import presentation
from .models import (
    ActivePullRequestTicketExists,
    Category,
    GitHubPullRequest,
    NewTicket,
    NextAction,
    PingReservation,
    PresenceTier,
    RoutingMode,
    Ticket,
    TicketOrigin,
    TicketState,
)
from .projection import ProjectionNotFound, TicketProjection
from .routing import CandidateFacts, select_reviewer
from .settings import GuildSettings
from .store import GitHubTicketsStore

PERMISSION_DENIED = presentation.CANNOT_USE_ACTION
INACTIVE_TICKET = presentation.TICKET_NOT_ACTIVE
CLAIM_RACE_LOST = presentation.TICKET_ALREADY_CLAIMED
MISSING_TICKET_CHANNEL = presentation.TICKET_CHANNEL_NOT_CONFIGURED
MISSING_AUTOMATIC_CATEGORIES = presentation.AUTOMATIC_REQUIRES_CATEGORY
MISSING_DIRECT_REVIEWER = presentation.DIRECT_REQUIRES_REVIEWER
SELF_REVIEW_DENIED = "You cannot select yourself as the reviewer"
CREATE_FAILED = presentation.COULD_NOT_CREATE_TICKET
ACTION_FAILED = presentation.COULD_NOT_COMPLETE_ACTION
PROJECTION_RETRY_SECONDS = 5
PING_RETRY_FLOOR_SECONDS = 5
log = logging.getLogger(__name__)


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
    finished_ticket: Ticket | None = None


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
        self._lifecycle_lock = asyncio.Lock()
        self._sent_ping_settlements: dict[
            int,
            tuple[PingReservation, datetime],
        ] = {}
        self._locally_reserved_pings: set[int] = set()

    async def create_ticket(
        self,
        request: TicketRequest,
        actor: TicketActor,
    ) -> TicketResult:
        async with self._lifecycle_lock:
            return await self._create_ticket_locked(request, actor)

    async def create_ticket_for_pull_request(
        self,
        request: TicketRequest,
        actor: TicketActor,
        pull_request: GitHubPullRequest,
    ) -> TicketResult:
        async with self._lifecycle_lock:
            return await self._create_ticket_locked(
                request,
                actor,
                pull_request=pull_request,
            )

    async def create_ticket_from_github(
        self,
        guild_id: int,
        pull_request: GitHubPullRequest,
        *,
        author_id: int | None,
    ) -> TicketResult:
        async with self._lifecycle_lock:
            settings = await self._get_settings(guild_id)
            if settings.ticket_channel_id is None:
                return TicketResult(False, MISSING_TICKET_CHANNEL)
            now = self._clock()
            try:
                ticket = await self._store.create_ticket_for_pull_request(
                    NewTicket(
                        guild_id=guild_id,
                        channel_id=settings.ticket_channel_id,
                        author_id=author_id,
                        pr_title=pull_request.title,
                        pr_url=pull_request.url,
                        category_display="",
                        routing_mode=RoutingMode.NONE,
                        direct_target_id=None,
                        category_ids=(),
                        created_at=now,
                        origin=TicketOrigin.GITHUB,
                    ),
                    pull_request,
                )
            except ActivePullRequestTicketExists:
                return TicketResult(True)
            except Exception:
                return TicketResult(False, CREATE_FAILED)
            return await self._project_created_ticket(
                ticket,
                routing_mode=RoutingMode.NONE,
                settings=settings,
                now=now,
            )

    async def _create_ticket_locked(
        self,
        request: TicketRequest,
        actor: TicketActor,
        *,
        pull_request: GitHubPullRequest | None = None,
    ) -> TicketResult:
        direct_target_id = (
            request.direct_target_id
            if request.routing_mode in (RoutingMode.DIRECT_WAIT, RoutingMode.DIRECT_AUTOMATIC)
            else None
        )
        actor_error = None
        if not actor.can_participate:
            actor_error = PERMISSION_DENIED
        elif direct_target_id == actor.user_id:
            actor_error = SELF_REVIEW_DENIED
        if actor_error is not None:
            return TicketResult(False, actor_error)
        settings = await self._get_settings(request.guild_id)
        if settings.ticket_channel_id is None:
            return TicketResult(False, MISSING_TICKET_CHANNEL)
        effective_request = (
            request
            if pull_request is None
            else replace(
                request,
                pr_title=pull_request.title,
                pr_url=pull_request.url,
            )
        )
        validation_error = self._validate_request(effective_request)
        if validation_error is not None:
            return TicketResult(False, validation_error)

        now = self._clock()
        try:
            new_ticket = NewTicket(
                guild_id=request.guild_id,
                channel_id=settings.ticket_channel_id,
                author_id=actor.user_id,
                pr_title=effective_request.pr_title,
                pr_url=effective_request.pr_url,
                category_display=request.category_display,
                routing_mode=request.routing_mode,
                direct_target_id=direct_target_id,
                category_ids=request.category_ids,
                created_at=now,
            )
            ticket = (
                await self._store.create_ticket(new_ticket)
                if pull_request is None
                else await self._store.create_ticket_for_pull_request(
                    new_ticket,
                    pull_request,
                )
            )
        except Exception:
            return TicketResult(False, CREATE_FAILED)
        return await self._project_created_ticket(
            ticket,
            routing_mode=request.routing_mode,
            settings=settings,
            now=now,
        )

    async def _project_created_ticket(
        self,
        ticket: Ticket,
        *,
        routing_mode: RoutingMode,
        settings: GuildSettings,
        now: datetime,
    ) -> TicketResult:
        message_id: int | None = None
        thread_id: int | None = None
        try:
            message_id = await self._projection.send_ticket(
                ticket,
                reviewer_github=await self._reviewer_github(ticket),
            )
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
                routing_mode,
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
            await self._cleanup_failed_creation(ticket, message_id, thread_id)
            if await self._store.get_ticket(ticket.ticket_id) is not None:
                await self._defer_cleanup_retry(ticket.ticket_id)
            return TicketResult(False, CREATE_FAILED)

        activated_ticket = await self._store.get_ticket(ticket.ticket_id)
        if activated_ticket is not None:
            await self._send_pending_category_prompt(activated_ticket)
        if next_action_at is not None:
            self._wake_deadlines()
        return TicketResult(True)

    async def _send_pending_category_prompt(self, ticket: Ticket) -> bool:
        if ticket.category_prompt_retry_at is None:
            return True
        if ticket.thread_id is None:
            return False
        try:
            await self._projection.prompt_categories(ticket, ticket.thread_id)
        except Exception:
            log.exception(
                "GitHub Tickets category prompt failed for ticket %s",
                ticket.ticket_id,
            )
            retry_at = self._clock() + timedelta(seconds=PROJECTION_RETRY_SECONDS)
            if await self._store.defer_category_prompt(ticket.ticket_id, retry_at):
                self._wake_deadlines()
            return False
        await self._store.acknowledge_category_prompt(ticket.ticket_id)
        return True

    async def claim(self, ticket_id: int, actor: TicketActor) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state not in (TicketState.OPEN, TicketState.CLAIMED):
                return TicketResult(False, INACTIVE_TICKET)
            if ticket.state is TicketState.CLAIMED:
                return TicketResult(False, CLAIM_RACE_LOST)
            if not (actor.can_participate or actor.user_id == ticket.direct_target_id):
                return TicketResult(False, PERMISSION_DENIED)
            return await self._claim_open(ticket, actor)

    async def claim_ticket_from_github(
        self,
        repository_id: int,
        pr_number: int,
        *,
        user_id: int,
        github_login: str,
        github_write_required: bool,
    ) -> TicketResult:
        ticket_id = await self._bound_ticket_id(repository_id, pr_number)
        if ticket_id is None:
            return TicketResult(False, INACTIVE_TICKET)
        async with self._ticket_lock(ticket_id):
            return await self._claim_ticket_from_github_locked(
                ticket_id,
                user_id=user_id,
                github_login=github_login,
                github_write_required=github_write_required,
            )

    async def _claim_ticket_from_github_locked(
        self,
        ticket_id: int,
        *,
        user_id: int,
        github_login: str,
        github_write_required: bool,
    ) -> TicketResult:
        ticket = await self._store.get_ticket(ticket_id)
        if ticket is None or ticket.state is not TicketState.OPEN:
            if ticket is not None and ticket.state is TicketState.CLAIMED:
                return TicketResult(True)
            return TicketResult(False, INACTIVE_TICKET)
        if ticket.author_id == user_id:
            return TicketResult(False, SELF_REVIEW_DENIED)
        settings = await self._get_settings(ticket.guild_id)
        now = self._clock()
        protection_until = now + timedelta(seconds=settings.protection_seconds)
        claimed = await self._store.claim_with_github_assignment(
            ticket_id,
            assignee_id=user_id,
            github_login=github_login,
            github_write_required=github_write_required,
            protection_until=protection_until,
            updated_at=now,
        )
        if not claimed:
            return TicketResult(False, CLAIM_RACE_LOST)
        current = await self._store.get_ticket(ticket_id)
        if current is None:
            return TicketResult(False, INACTIVE_TICKET)
        return await self._edit_after_transition(current)

    async def _claim_open(self, ticket: Ticket, actor: TicketActor) -> TicketResult:
        settings = await self._get_settings(ticket.guild_id)
        now = self._clock()
        protection_until = now + timedelta(seconds=settings.protection_seconds)
        github_login = await self._bound_unique_github_login(ticket, actor.user_id)
        transitioned = (
            await self._store.claim_with_github_assignment(
                ticket.ticket_id,
                assignee_id=actor.user_id,
                github_login=github_login,
                github_write_required=True,
                protection_until=protection_until,
                updated_at=now,
            )
            if github_login is not None
            else await self._store.claim(
                ticket.ticket_id,
                actor.user_id,
                protection_until,
                now,
            )
        )
        if not transitioned:
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
            if not (actor.can_participate or actor.user_id == ticket.direct_target_id):
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
            if not (actor.can_manage_messages or actor.user_id == ticket.assignee_id):
                return TicketResult(False, PERMISSION_DENIED)

            settings = await self._get_settings(ticket.guild_id)
            now = self._clock()
            protection_until = now + timedelta(seconds=settings.protection_seconds)
            next_action, next_action_at = self._release_schedule(
                ticket.routing_mode,
                protection_until,
            )
            former_assignee = await self._store.unassign_with_github_outbox(
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

    async def _bound_unique_github_login(
        self,
        ticket: Ticket,
        user_id: int | None,
    ) -> str | None:
        if user_id is None:
            return None
        pull_request = await self._store.get_pull_request_for_ticket(ticket.ticket_id)
        if pull_request is None:
            return None
        profile = await self._store.get_profile(ticket.guild_id, user_id)
        if profile is None or not profile.github_username:
            return None
        matching_profiles = await self._store.list_profiles_by_github_username(
            ticket.guild_id,
            profile.github_username,
        )
        if len(matching_profiles) != 1 or matching_profiles[0].user_id != user_id:
            return None
        return profile.github_username

    async def unassign_ticket_from_github(
        self,
        repository_id: int,
        pr_number: int,
        *,
        user_id: int,
    ) -> TicketResult:
        ticket_id = await self._bound_ticket_id(repository_id, pr_number)
        if ticket_id is None:
            return TicketResult(True)
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if (
                ticket is None
                or ticket.state is not TicketState.CLAIMED
                or ticket.assignee_id != user_id
            ):
                return TicketResult(True)
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

    async def _bound_ticket_id(
        self,
        repository_id: int,
        pr_number: int,
    ) -> int | None:
        pull_request = await self._store.get_pull_request(repository_id, pr_number)
        return pull_request.current_ticket_id if pull_request is not None else None

    async def mark_finished(self, ticket_id: int, actor: TicketActor) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state not in (TicketState.OPEN, TicketState.CLAIMED):
                return TicketResult(False, INACTIVE_TICKET)
            if not (
                actor.can_manage_messages or actor.user_id in (ticket.author_id, ticket.assignee_id)
            ):
                return TicketResult(False, PERMISSION_DENIED)
            return await self._finish_ticket_locked(ticket)

    async def finish_ticket_from_github(
        self,
        repository_id: int,
        pr_number: int,
    ) -> TicketResult:
        ticket_id = await self._bound_ticket_id(repository_id, pr_number)
        if ticket_id is None:
            return TicketResult(True)
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state not in (
                TicketState.OPEN,
                TicketState.CLAIMED,
            ):
                return TicketResult(True)
            return await self._finish_ticket_locked(ticket)

    async def prompt_draft_decision_from_github(
        self,
        repository_id: int,
        pr_number: int,
    ) -> TicketResult:
        ticket_id = await self._bound_ticket_id(repository_id, pr_number)
        if ticket_id is None:
            return TicketResult(True)
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state not in (
                TicketState.OPEN,
                TicketState.CLAIMED,
            ):
                return TicketResult(True)
            pull_request = await self._store.get_pull_request_for_ticket(ticket_id)
            if pull_request is None or not pull_request.draft:
                return TicketResult(True)
            try:
                await self._projection.prompt_draft_decision(ticket)
            except Exception:
                return TicketResult(False, ACTION_FAILED)
            return TicketResult(True)

    async def add_ticket_categories(
        self,
        ticket_id: int,
        category_ids: tuple[int, ...],
        actor: TicketActor,
    ) -> TicketResult:
        selected_ids = tuple(dict.fromkeys(category_ids))
        if not selected_ids:
            return TicketResult(False, MISSING_AUTOMATIC_CATEGORIES)
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state not in (
                TicketState.OPEN,
                TicketState.CLAIMED,
            ):
                return TicketResult(False, INACTIVE_TICKET)
            if not actor.can_participate:
                return TicketResult(False, PERMISSION_DENIED)
            categories = await self._store.list_categories(ticket.guild_id)
            selected = set(selected_ids)
            ordered_categories = tuple(
                category for category in categories if category.category_id in selected
            )
            if len(ordered_categories) != len(selected_ids):
                return TicketResult(False, presentation.CATEGORY_NO_LONGER_EXISTS)
            ordered_ids = tuple(category.category_id for category in ordered_categories)
            settled = self._settled_category_result(ticket, ordered_ids)
            if settled is not None:
                return settled
            return await self._start_ticket_routing(ticket, ordered_categories)

    @staticmethod
    def _settled_category_result(
        ticket: Ticket,
        category_ids: tuple[int, ...],
    ) -> TicketResult | None:
        if ticket.routing_mode is RoutingMode.AUTOMATIC:
            if ticket.category_ids == category_ids:
                return TicketResult(True)
            return TicketResult(False, INACTIVE_TICKET)
        if (
            ticket.origin is not TicketOrigin.GITHUB
            or ticket.routing_mode is not RoutingMode.NONE
        ):
            return TicketResult(False, INACTIVE_TICKET)
        return None

    async def _start_ticket_routing(
        self,
        ticket: Ticket,
        categories: tuple[Category, ...],
    ) -> TicketResult:
        settings = await self._get_settings(ticket.guild_id)
        now = self._clock()
        _, _, next_action_at = self._creation_schedule(
            RoutingMode.AUTOMATIC,
            settings,
            now,
        )
        if next_action_at is None:
            raise RuntimeError("automatic routing must have a deadline")
        try:
            current = await self._store.start_automatic_routing(
                ticket.ticket_id,
                category_ids=tuple(category.category_id for category in categories),
                category_display=", ".join(category.name for category in categories),
                next_action_at=next_action_at,
                updated_at=now,
            )
        except ValueError:
            return TicketResult(False, presentation.CATEGORY_NO_LONGER_EXISTS)
        if current is None:
            return TicketResult(False, INACTIVE_TICKET)
        result = await self._edit_after_transition(current)
        if result.success and current.next_action_at is not None:
            self._wake_deadlines()
        return result

    async def keep_draft_ticket(
        self,
        ticket_id: int,
        actor: TicketActor,
    ) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket, error = await self._authorized_draft_ticket(ticket_id, actor)
            if ticket is None:
                return TicketResult(False, error)
            return TicketResult(True)

    async def remove_draft_ticket(
        self,
        ticket_id: int,
        actor: TicketActor,
    ) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket, error = await self._authorized_draft_ticket(ticket_id, actor)
            if ticket is None:
                return TicketResult(False, error)
            return await self._finish_ticket_locked(ticket)

    async def _authorized_draft_ticket(
        self,
        ticket_id: int,
        actor: TicketActor,
    ) -> tuple[Ticket | None, str]:
        ticket = await self._store.get_ticket(ticket_id)
        if ticket is None or ticket.state not in (TicketState.OPEN, TicketState.CLAIMED):
            return None, INACTIVE_TICKET
        pull_request = await self._store.get_pull_request_for_ticket(ticket_id)
        if pull_request is None or not pull_request.draft:
            return None, INACTIVE_TICKET
        if actor.can_manage_messages or actor.user_id == ticket.author_id:
            return ticket, ""
        profiles = await self._store.list_profiles_by_github_username(
            ticket.guild_id,
            pull_request.github_author_login,
        )
        if len(profiles) == 1 and profiles[0].user_id == actor.user_id:
            return ticket, ""
        return None, PERMISSION_DENIED

    async def update_title_from_github(
        self,
        repository_id: int,
        pr_number: int,
        *,
        title: str,
    ) -> TicketResult:
        ticket_id = await self._bound_ticket_id(repository_id, pr_number)
        if ticket_id is None:
            return TicketResult(True)
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state not in (
                TicketState.OPEN,
                TicketState.CLAIMED,
            ):
                return TicketResult(True)
            normalized_title = title.strip()
            if not normalized_title:
                return TicketResult(True)
            if ticket.pr_title == normalized_title:
                return TicketResult(True)
            updated = await self._store.update_ticket_title(
                ticket_id,
                normalized_title,
                self._clock(),
            )
            if updated is None:
                return TicketResult(False, INACTIVE_TICKET)
            return await self._edit_after_transition(updated)

    async def _finish_ticket_locked(self, ticket: Ticket) -> TicketResult:
        if not await self._store.begin_finishing(ticket.ticket_id, self._clock()):
            return TicketResult(False, INACTIVE_TICKET)
        finishing = await self._store.get_ticket(ticket.ticket_id)
        if finishing is None:
            return TicketResult(True, finished_ticket=ticket)
        try:
            await self._delete_remaining_projection(finishing)
        except Exception:
            await self._defer_cleanup_retry(ticket.ticket_id)
            return TicketResult(False, ACTION_FAILED)
        self._locks.pop(ticket.ticket_id, None)
        return TicketResult(True, finished_ticket=ticket)

    async def handle_message_deleted(self, message_id: int) -> None:
        ticket = await self._store.get_ticket_by_message_id(message_id)
        if ticket is None:
            return
        async with self._ticket_lock(ticket.ticket_id):
            current = await self._store.get_ticket(ticket.ticket_id)
            if current is None or current.message_id != message_id:
                return
            if current.state in (
                TicketState.CREATING,
                TicketState.OPEN,
                TicketState.CLAIMED,
                TicketState.FINISHING,
            ):
                if not await self._store.begin_finishing(
                    current.ticket_id,
                    self._clock(),
                    message_absent=True,
                ):
                    return
                current = await self._store.get_ticket(current.ticket_id)
                if current is None:
                    return
            try:
                await self._delete_remaining_projection(current, message_absent=True)
            except Exception:
                await self._defer_cleanup_retry(current.ticket_id)
                raise
            self._locks.pop(current.ticket_id, None)

    async def handle_thread_deleted(self, thread_id: int) -> None:
        ticket = await self._store.get_ticket_by_thread_id(thread_id)
        if ticket is None:
            return
        async with self._ticket_lock(ticket.ticket_id):
            current = await self._store.get_ticket(ticket.ticket_id)
            if current is None or current.thread_id != thread_id:
                return
            if current.state in (
                TicketState.CREATING,
                TicketState.OPEN,
                TicketState.CLAIMED,
                TicketState.FINISHING,
            ):
                if not await self._store.begin_finishing(
                    current.ticket_id,
                    self._clock(),
                    thread_absent=True,
                ):
                    return
                current = await self._store.get_ticket(current.ticket_id)
                if current is None:
                    return
            try:
                await self._delete_remaining_projection(current, thread_absent=True)
            except Exception:
                await self._defer_cleanup_retry(current.ticket_id)
                raise
            self._locks.pop(current.ticket_id, None)

    async def recover_projection_cleanup(self, ticket_id: int) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            return await self._recover_projection_cleanup_locked(ticket_id)

    async def _recover_projection_cleanup_locked(
        self,
        ticket_id: int,
        *,
        thread_absent: bool = False,
    ) -> TicketResult:
        ticket = await self._store.get_ticket(ticket_id)
        if ticket is None:
            return TicketResult(True)
        if ticket.state not in (TicketState.CREATING, TicketState.FINISHING):
            return TicketResult(False, INACTIVE_TICKET)
        if ticket.state is TicketState.CREATING:
            result = await self._recover_creation(ticket)
        else:
            try:
                await self._delete_remaining_projection(
                    ticket,
                    thread_absent=thread_absent,
                )
            except Exception:
                result = TicketResult(False, ACTION_FAILED)
            else:
                self._locks.pop(ticket_id, None)
                return TicketResult(True)
        if not result.success:
            await self._defer_cleanup_retry(ticket.ticket_id)
        return result

    async def sync_projection(self, ticket_id: int) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.state not in (TicketState.OPEN, TicketState.CLAIMED):
                return TicketResult(False, INACTIVE_TICKET)
            return await self._edit_after_transition(ticket)

    async def begin_authored_ticket_cleanup(
        self,
        ticket_id: int,
        *,
        author_id: int,
        updated_at: datetime,
    ) -> Ticket | None:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is None or ticket.author_id != author_id:
                return None
            return await self._store.begin_authored_ticket_cleanup(
                ticket_id,
                author_id=author_id,
                updated_at=updated_at,
            )

    async def redact_user(
        self,
        user_id: int,
        *,
        updated_at: datetime,
    ) -> tuple[Ticket, ...]:
        async with self._lifecycle_lock:
            return await self._redact_user_locked(
                user_id,
                updated_at=updated_at,
            )

    async def _redact_user_locked(
        self,
        user_id: int,
        *,
        updated_at: datetime,
    ) -> tuple[Ticket, ...]:
        authored = await self._store.list_authored_tickets(user_id)
        active_ids = {ticket.ticket_id for ticket in await self._store.list_active_tickets()}
        ticket_ids = tuple(
            sorted(
                active_ids.union(ticket.ticket_id for ticket in authored).union(
                    await self._store.user_reference_ticket_ids(user_id)
                )
            )
        )
        async with AsyncExitStack() as stack:
            for ticket_id in ticket_ids:
                await stack.enter_async_context(self._ticket_lock(ticket_id))
            for ticket_id in ticket_ids:
                await self._store.get_ticket(ticket_id)
            for ticket in authored:
                current = await self._store.get_ticket(ticket.ticket_id)
                if current is None or current.author_id != user_id:
                    continue
                cleanup = await self._store.begin_authored_ticket_cleanup(
                    current.ticket_id,
                    author_id=user_id,
                    updated_at=updated_at,
                )
                if cleanup is not None:
                    await self._recover_projection_cleanup_locked(cleanup.ticket_id)
            protection_until_by_guild = {}
            for guild_id in await self._store.user_reference_guild_ids(user_id):
                settings = await self._get_settings(guild_id)
                protection_until_by_guild[guild_id] = updated_at + timedelta(
                    seconds=settings.protection_seconds
                )
            return await self._store.redact_user(
                user_id,
                protection_until_by_guild=protection_until_by_guild,
                updated_at=updated_at,
            )

    async def _recover_creation(self, ticket: Ticket) -> TicketResult:
        now = self._clock()
        try:
            message_id = ticket.message_id
            if message_id is None:
                message_id = await self._projection.find_ticket_message(ticket)
                if message_id is None:
                    message_id = await self._projection.send_ticket(
                        ticket,
                        reviewer_github=await self._reviewer_github(ticket),
                    )
                if not await self._store.record_ticket_message(
                    ticket.ticket_id,
                    message_id,
                    now,
                ):
                    raise RuntimeError("creating ticket lost its message reservation")

            current = await self._store.get_ticket(ticket.ticket_id)
            if current is None or current.state is not TicketState.CREATING:
                return TicketResult(False, INACTIVE_TICKET)
            thread_id = current.thread_id
            if thread_id is None:
                try:
                    thread_id = await self._projection.find_ticket_thread(current)
                except ProjectionNotFound:
                    return await self._finish_missing_creation_message(current, now)
                if thread_id is None:
                    thread_id = await self._projection.create_thread(current, message_id)
                if not await self._store.record_ticket_thread(
                    current.ticket_id,
                    thread_id,
                    now,
                ):
                    raise RuntimeError("creating ticket lost its thread reservation")

            settings = await self._get_settings(current.guild_id)
            protection_until, next_action, next_action_at = self._creation_schedule(
                current.routing_mode,
                settings,
                now,
            )
            activated = await self._store.activate_ticket(
                current.ticket_id,
                message_id=message_id,
                thread_id=thread_id,
                protection_until=protection_until,
                next_action=next_action,
                next_action_at=next_action_at,
                updated_at=now,
            )
        except Exception:
            return TicketResult(False, ACTION_FAILED)
        if not activated:
            return TicketResult(False, ACTION_FAILED)
        if next_action_at is not None:
            self._wake_deadlines()
        return TicketResult(True)

    async def _finish_missing_creation_message(
        self,
        ticket: Ticket,
        now: datetime,
    ) -> TicketResult:
        if not await self._store.begin_finishing(
            ticket.ticket_id,
            now,
            message_absent=True,
        ):
            return TicketResult(False, INACTIVE_TICKET)
        finishing = await self._store.get_ticket(ticket.ticket_id)
        if finishing is None:
            return TicketResult(True)
        await self._delete_remaining_projection(finishing)
        return TicketResult(True)

    async def process_due(self, ticket_id: int) -> TicketResult:
        async with self._ticket_lock(ticket_id):
            ticket = await self._store.get_ticket(ticket_id)
            if ticket is not None and ticket.state in (
                TicketState.CREATING,
                TicketState.FINISHING,
            ):
                return await self._recover_projection_cleanup_locked(ticket_id)
            if (
                ticket is not None
                and ticket.category_prompt_retry_at is not None
                and ticket.category_prompt_retry_at <= self._clock()
            ):
                if not await self._send_pending_category_prompt(ticket):
                    return TicketResult(True)
                ticket = await self._store.get_ticket(ticket_id)
                if ticket is None:
                    return TicketResult(False, INACTIVE_TICKET)
            projection_sync = await self._store.get_projection_sync_ticket(ticket_id)
            if projection_sync is not None:
                result = await self._edit_after_transition(projection_sync)
                if result.success:
                    self._wake_deadlines()
                return result
            return await self._process_due_routing(ticket_id)

    async def _process_due_routing(self, ticket_id: int) -> TicketResult:
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
        reservation = None
        if ticket.ping_count < settings.max_pings:
            reservation = self._pending_reservation(ticket)
            if reservation is None:
                reservation = await self._reserve_new_ping(ticket, settings, now)
                if reservation is not None:
                    self._locally_reserved_pings.add(ticket.ticket_id)
        if reservation is None:
            await self._store.exhaust_due_routing(
                ticket.ticket_id,
                next_action,
                now,
            )
            return TicketResult(True)
        return await self._settle_due_ping(ticket, reservation, settings, now)

    async def _settle_due_ping(
        self,
        ticket: Ticket,
        reservation: PingReservation,
        settings: GuildSettings,
        now: datetime,
    ) -> TicketResult:
        pending_settlement = self._sent_ping_settlements.get(ticket.ticket_id)
        if (
            pending_settlement is None
            and ticket.ticket_id not in self._locally_reserved_pings
            and ticket.thread_id is not None
        ):
            try:
                recovered_sent_at = await self._projection.find_ping(
                    ticket.thread_id,
                    reservation.target_user_id,
                    reservation.automatic,
                    reservation.reserved_at,
                )
            except ProjectionNotFound:
                if not await self._store.begin_finishing(
                    ticket.ticket_id,
                    now,
                    thread_absent=True,
                ):
                    return TicketResult(False, INACTIVE_TICKET)
                return await self._recover_projection_cleanup_locked(
                    ticket.ticket_id,
                    thread_absent=True,
                )
            if recovered_sent_at is not None:
                pending_settlement = (reservation, recovered_sent_at)
                self._sent_ping_settlements[ticket.ticket_id] = pending_settlement
        if pending_settlement is None or pending_settlement[0] != reservation:
            effect_result = await self._send_ping_effect(
                ticket,
                reservation,
                settings,
                now,
            )
            if effect_result is not None:
                return effect_result
            pending_settlement = (reservation, now)
            self._sent_ping_settlements[ticket.ticket_id] = pending_settlement

        acknowledged = await self._store.acknowledge_ping(
            ticket.ticket_id,
            pending_settlement[1],
        )
        self._sent_ping_settlements.pop(ticket.ticket_id, None)
        self._locally_reserved_pings.discard(ticket.ticket_id)
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
            log.exception("GitHub Tickets reviewer ping failed for ticket %s", ticket.ticket_id)
            retry_at = now + timedelta(
                seconds=max(settings.protection_seconds, PING_RETRY_FLOOR_SECONDS)
            )
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
            or ticket.pending_ping_reserved_at is None
            or ticket.pending_response_deadline is None
        ):
            return None
        return PingReservation(
            ticket_id=ticket.ticket_id,
            target_user_id=ticket.pending_target_id,
            presence_tier=ticket.pending_presence_tier,
            automatic=ticket.pending_ping_automatic,
            reserved_at=ticket.pending_ping_reserved_at,
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
            await self._projection.edit_ticket(
                ticket,
                reviewer_github=await self._reviewer_github(ticket),
            )
        except ProjectionNotFound:
            await self._delete_remaining_projection(ticket, message_absent=True)
        except Exception:
            retry_at = self._clock() + timedelta(seconds=PROJECTION_RETRY_SECONDS)
            if await self._store.defer_projection_sync(
                ticket.ticket_id,
                ticket.transition_version,
                retry_at,
            ):
                self._wake_deadlines()
            return TicketResult(False, ACTION_FAILED)
        await self._store.acknowledge_projection_sync(
            ticket.ticket_id,
            ticket.transition_version,
        )
        return TicketResult(True)

    async def _reviewer_github(self, ticket: Ticket) -> str | None:
        reviewer_id = ticket.assignee_id
        if reviewer_id is None:
            return None
        profile = await self._store.get_profile(ticket.guild_id, reviewer_id)
        return profile.github_username if profile is not None else None

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
        if (
            request.routing_mode
            in (
                RoutingMode.AUTOMATIC,
                RoutingMode.DIRECT_AUTOMATIC,
            )
            and not request.category_ids
        ):
            return MISSING_AUTOMATIC_CATEGORIES
        if (
            request.routing_mode
            in (
                RoutingMode.DIRECT_WAIT,
                RoutingMode.DIRECT_AUTOMATIC,
            )
            and request.direct_target_id is None
        ):
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

    async def _defer_cleanup_retry(self, ticket_id: int) -> None:
        current = await self._store.get_ticket(ticket_id)
        if current is None:
            return
        await self._store.defer_projection_sync(
            current.ticket_id,
            current.transition_version,
            self._clock() + timedelta(seconds=PROJECTION_RETRY_SECONDS),
        )
        self._wake_deadlines()
