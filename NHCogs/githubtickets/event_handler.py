from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from NHCogs.operational_errors import report_operational_error

from . import events
from .coordinator import TicketCoordinator
from .models import GitHubDelivery
from .runtime import DeliveryDisposition
from .store import GitHubTicketsStore

_TICKET_LABEL = "discord-ticket"


class _AmbiguousGitHubMapping(RuntimeError):
    def __init__(self, login: str, count: int) -> None:
        super().__init__(f"GitHub login {login} maps to {count} cached members")


class GitHubEventHandler:
    def __init__(
        self,
        store: GitHubTicketsStore,
        coordinator: TicketCoordinator,
        *,
        bot: Any,
        guild_id: int,
        participant_role_ids: Iterable[int],
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self._bot = bot
        self._guild_id = guild_id
        self._participant_role_ids = frozenset(participant_role_ids)

    async def __call__(self, delivery: GitHubDelivery) -> DeliveryDisposition:
        parsed = events.parse_delivery(delivery)
        if parsed is None:
            return DeliveryDisposition.IGNORED
        await self._store.observe_pull_request(parsed.pull_request)
        if isinstance(parsed, events.PullRequestEvent):
            await self._handle_pull_request(parsed)
        else:
            await self._handle_review(parsed)
        return DeliveryDisposition.PROCESSED

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
            await self._coordinator.create_ticket_from_github(
                self._guild_id,
                pull_request,
                author_id=int(author.id) if author is not None else None,
            )
            return
        if event.action == "closed":
            await self._coordinator.finish_ticket_from_github(
                pull_request.repository_id,
                pull_request.pr_number,
            )
            return
        if event.action == "assigned":
            candidate, ambiguous = await self._first_eligible_assignee(
                self._assignee_logins(event),
                author_login=pull_request.github_author_login,
            )
            if ambiguous or candidate is None:
                return
            await self._coordinator.claim_ticket_from_github(
                pull_request.repository_id,
                pull_request.pr_number,
                user_id=int(candidate.id),
                ensure_assigned_login=None,
            )
            return
        if event.action == "unassigned":
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
        await self._coordinator.claim_ticket_from_github(
            event.pull_request.repository_id,
            event.pull_request.pr_number,
            user_id=int(reviewer.id),
            ensure_assigned_login=None if already_assigned else reviewer_login,
        )

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
        candidate, ambiguous = await self._first_eligible_assignee(
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
            if not result.success:
                return
        if candidate is not None:
            await self._coordinator.claim_ticket_from_github(
                pull_request.repository_id,
                pull_request.pr_number,
                user_id=int(candidate.id),
                ensure_assigned_login=None,
            )

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
    ) -> tuple[Any | None, bool]:
        first = None
        for login in logins:
            if login.casefold() == author_login.casefold():
                continue
            member, ambiguous = await self._resolve_member(
                login,
                require_eligible=True,
            )
            if ambiguous:
                return None, True
            if first is None and member is not None:
                first = member
        return first, False

    @staticmethod
    def _assignee_logins(event: events.PullRequestEvent) -> tuple[str, ...]:
        logins = list(event.assignee_logins)
        if event.assignee_login is not None and all(
            login.casefold() != event.assignee_login.casefold() for login in logins
        ):
            logins.append(event.assignee_login)
        return tuple(dict.fromkeys(login.casefold() for login in logins))

    def _eligible(self, member: Any) -> bool:
        permissions = getattr(member, "guild_permissions", None)
        if permissions is not None and bool(permissions.manage_messages):
            return True
        role_ids = {int(role.id) for role in getattr(member, "roles", ())}
        return bool(self._participant_role_ids.intersection(role_ids))
