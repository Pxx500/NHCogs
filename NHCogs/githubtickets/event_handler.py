from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from NHCogs.operational_errors import report_operational_error

from . import events
from .coordinator import TicketCoordinator
from .models import GitHubDelivery, GitHubPullRequest, PullRequestObservationState, Ticket
from .runtime import DeliveryDisposition
from .store import GitHubTicketsStore

_TICKET_LABEL = "discord-ticket"
RefreshPullRequest = Callable[[GitHubPullRequest], Awaitable[GitHubPullRequest]]
TicketFinished = Callable[[Ticket], Awaitable[None]]
log = logging.getLogger(__name__)


class _AmbiguousGitHubMapping(RuntimeError):
    def __init__(self, login: str, count: int) -> None:
        super().__init__(f"GitHub login {login} maps to {count} cached members")


class GitHubEventTransitionDeferred(RuntimeError):
    pass


class GitHubEventHandler:
    def __init__(
        self,
        store: GitHubTicketsStore,
        coordinator: TicketCoordinator,
        *,
        bot: Any,
        guild_id: int,
        member_is_eligible: Callable[[Any], bool],
        refresh_pull_request: RefreshPullRequest | None = None,
        ticket_finished: TicketFinished | None = None,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._bot = bot
        self._guild_id = guild_id
        self._member_is_eligible = member_is_eligible
        self._refresh_pull_request = refresh_pull_request
        self._ticket_finished = ticket_finished

    async def __call__(self, delivery: GitHubDelivery) -> DeliveryDisposition:
        parsed = events.parse_delivery(delivery)
        if parsed is None:
            return DeliveryDisposition.IGNORED
        if isinstance(parsed, events.GitHubAppLifecycleEvent):
            return DeliveryDisposition.STOPPED
        observation = await self._store.observe_pull_request(parsed.pull_request)
        if observation.state is PullRequestObservationState.CONFLICT:
            if self._refresh_pull_request is None:
                raise GitHubEventTransitionDeferred(
                    "GitHub pull request conflict could not be refreshed"
                )
            authoritative = await self._refresh_pull_request(parsed.pull_request)
            observation = await self._store.observe_pull_request(
                authoritative,
                authoritative=True,
            )
            if observation.state is not PullRequestObservationState.APPLIED:
                raise GitHubEventTransitionDeferred(
                    "GitHub pull request conflict did not settle"
                )
        if not observation.pull_request.title.strip():
            return DeliveryDisposition.PROCESSED
        if isinstance(parsed, events.PullRequestEvent):
            parsed = replace(
                parsed,
                action=self._reconciled_action(parsed, observation.pull_request),
                pull_request=observation.pull_request,
                assignee_logins=observation.pull_request.assignees,
            )
            await self._handle_pull_request(parsed)
        else:
            parsed = replace(
                parsed,
                pull_request=observation.pull_request,
                assignee_logins=observation.pull_request.assignees,
            )
            await self._handle_review(parsed)
        return DeliveryDisposition.PROCESSED

    @staticmethod
    def _reconciled_action(
        event: events.PullRequestEvent,
        pull_request: GitHubPullRequest,
    ) -> events.PullRequestAction:
        if event.action in {"assigned", "unassigned"} and event.assignee_login is not None:
            assigned = any(
                login.casefold() == event.assignee_login.casefold()
                for login in pull_request.assignees
            )
            return "assigned" if assigned else "unassigned"
        if event.action in {"labeled", "unlabeled"} and event.label is not None:
            labeled = any(
                label.casefold() == event.label.casefold() for label in pull_request.labels
            )
            return "labeled" if labeled else "unlabeled"
        if event.action in {"closed", "reopened"}:
            return "reopened" if pull_request.open else "closed"
        if event.action in {"converted_to_draft", "ready_for_review"}:
            return "converted_to_draft" if pull_request.draft else "ready_for_review"
        return event.action

    async def _handle_pull_request(self, event: events.PullRequestEvent) -> None:
        pull_request = event.pull_request
        labeled_for_ticket = (
            event.action == "labeled"
            and event.label is not None
            and event.label.casefold() == _TICKET_LABEL
        )
        became_ready_with_label = event.action == "ready_for_review" and any(
            label.casefold() == _TICKET_LABEL for label in pull_request.labels
        )
        if (
            (labeled_for_ticket or became_ready_with_label)
            and pull_request.open
            and not pull_request.draft
        ):
            author, ambiguous = await self._resolve_member(
                pull_request.github_author_login,
                require_eligible=True,
            )
            if ambiguous:
                return
            result = await self._coordinator.create_ticket_from_github(
                self._guild_id,
                pull_request,
                author_id=int(author.id) if author is not None else None,
            )
            self._require_settled(result)
        elif event.action == "closed":
            result = await self._coordinator.finish_ticket_from_github(
                pull_request.repository_id,
                pull_request.pr_number,
            )
            self._require_settled(result)
            if result.finished_ticket is not None and self._ticket_finished is not None:
                try:
                    await self._ticket_finished(result.finished_ticket)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("GitHub Tickets finished ticket log failed")
        elif event.action == "converted_to_draft":
            result = await self._coordinator.prompt_draft_decision_from_github(
                pull_request.repository_id,
                pull_request.pr_number,
            )
            self._require_settled(result)
        elif event.action == "edited" and event.title_changed:
            result = await self._coordinator.update_title_from_github(
                pull_request.repository_id,
                pull_request.pr_number,
                title=pull_request.title,
            )
            self._require_settled(result)
        elif event.action == "assigned":
            candidate, candidate_login, ambiguous = await self._first_eligible_assignee(
                self._assignee_logins(event),
                author_login=pull_request.github_author_login,
            )
            if ambiguous or candidate is None or candidate_login is None:
                return
            result = await self._coordinator.claim_ticket_from_github(
                pull_request.repository_id,
                pull_request.pr_number,
                user_id=int(candidate.id),
                github_login=candidate_login,
                github_write_required=False,
            )
            self._require_settled(result)
        elif event.action == "unassigned":
            await self._handle_unassigned(event)

    async def _handle_review(self, event: events.PullRequestReviewEvent) -> None:
        reviewer_login = event.reviewer_login
        if (
            event.state not in {"approved", "changes_requested"}
            or reviewer_login is None
            or reviewer_login.casefold() == event.pull_request.github_author_login.casefold()
        ):
            return
        reviewer, ambiguous = await self._resolve_member(
            reviewer_login,
            require_eligible=True,
        )
        if ambiguous or reviewer is None:
            return
        already_assigned = any(
            login.casefold() == reviewer_login.casefold() for login in event.assignee_logins
        )
        result = await self._coordinator.claim_ticket_from_github(
            event.pull_request.repository_id,
            event.pull_request.pr_number,
            user_id=int(reviewer.id),
            github_login=reviewer_login,
            github_write_required=not already_assigned,
        )
        self._require_settled(result)

    async def _handle_unassigned(self, event: events.PullRequestEvent) -> None:
        pull_request = event.pull_request
        removed = None
        if event.assignee_login is not None:
            removed, ambiguous = await self._resolve_member(
                event.assignee_login,
                require_eligible=False,
            )
            if ambiguous:
                return
        remaining_logins = tuple(
            login
            for login in event.assignee_logins
            if event.assignee_login is None or login.casefold() != event.assignee_login.casefold()
        )
        candidate, candidate_login, ambiguous = await self._first_eligible_assignee(
            remaining_logins,
            author_login=pull_request.github_author_login,
        )
        if ambiguous:
            return
        if removed is not None:
            result = await self._coordinator.unassign_ticket_from_github(
                pull_request.repository_id,
                pull_request.pr_number,
                user_id=int(removed.id),
            )
            self._require_settled(result)
        if candidate is not None and candidate_login is not None:
            result = await self._coordinator.claim_ticket_from_github(
                pull_request.repository_id,
                pull_request.pr_number,
                user_id=int(candidate.id),
                github_login=candidate_login,
                github_write_required=False,
            )
            self._require_settled(result)

    async def _resolve_member(
        self,
        login: str,
        *,
        require_eligible: bool,
    ) -> tuple[Any | None, bool]:
        profiles = await self._store.list_profiles_by_github_username(
            self._guild_id,
            login,
        )
        guild = self._bot.get_guild(self._guild_id)
        if guild is None:
            return None, False
        members: dict[int, Any] = {}
        for profile in profiles:
            member = guild.get_member(int(profile.user_id))
            if member is not None:
                members[int(member.id)] = member
        if len(members) > 1:
            await report_operational_error(
                self._bot,
                guild_id=self._guild_id,
                source="GitHubTickets",
                action="resolve GitHub identity",
                error=_AmbiguousGitHubMapping(login, len(members)),
            )
            return None, True
        member = next(iter(members.values()), None)
        if require_eligible and member is not None and not self._eligible(member):
            return None, False
        return member, False

    async def _first_eligible_assignee(
        self,
        logins: tuple[str, ...],
        *,
        author_login: str,
    ) -> tuple[Any | None, str | None, bool]:
        first = None
        first_login = None
        for login in logins:
            if login.casefold() == author_login.casefold():
                continue
            member, ambiguous = await self._resolve_member(
                login,
                require_eligible=True,
            )
            if ambiguous:
                return None, None, True
            if first is None and member is not None:
                first = member
                first_login = login
        return first, first_login, False

    @staticmethod
    def _assignee_logins(event: events.PullRequestEvent) -> tuple[str, ...]:
        return tuple(dict.fromkeys(login.casefold() for login in event.assignee_logins))

    def _eligible(self, member: Any) -> bool:
        return self._member_is_eligible(member)

    @staticmethod
    def _require_settled(result: Any) -> None:
        if not result.success:
            raise GitHubEventTransitionDeferred("GitHub event transition did not settle")
