from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone

import discord

from . import presentation
from .coordinator import SELF_REVIEW_DENIED, TicketActor, TicketRequest, TicketResult
from .github_app import PullRequestSnapshot, pull_request_from_snapshot
from .models import Category, GitHubPullRequest, Profile, RoutingMode
from .store import GitHubTicketsStore

CreateTicket = Callable[
    [TicketRequest, TicketActor, GitHubPullRequest],
    Awaitable[TicketResult],
]
FetchPullRequest = Callable[[str, str, int], Awaitable[PullRequestSnapshot]]
CountAutomaticCandidates = Callable[
    [int, tuple[int, ...], frozenset[int]],
    Awaitable[int],
]
ActorFactory = Callable[[discord.Interaction], TicketActor]
MemberLookup = Callable[[int], discord.Member | None]
MemberActorFactory = Callable[[discord.Member], TicketActor]
log = logging.getLogger("red.NHCogs.GitHubTickets")
GITHUB_INTEGRATION_UNAVAILABLE = "GitHub integration is unavailable"
_PULL_REQUEST_LINK = re.compile(
    r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)"
)
_GITHUB_PROFILE_LINK = re.compile(
    r"https://github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/?"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_pull_request_link(value: str) -> tuple[str, str, int] | None:
    match = _PULL_REQUEST_LINK.fullmatch(value)
    if match is None:
        return None
    owner, repository, number = match.groups()
    return owner, repository, int(number)


def _parse_github_profile_link(value: str) -> str | None:
    match = _GITHUB_PROFILE_LINK.fullmatch(value.strip())
    return match.group(1) if match is not None else None


def _github_profile_link(github_username: str) -> str:
    return f"https://github.com/{github_username}"


def _validated_pull_request(
    snapshot: PullRequestSnapshot,
    *,
    owner: str,
    repository: str,
    number: int,
    expected_organization: str,
) -> GitHubPullRequest | None:
    expected_full_name = f"{owner}/{repository}"
    repository_full_name = snapshot.repository_full_name.strip()
    repository_owner, separator, _ = repository_full_name.partition("/")
    if (
        not separator
        or repository_full_name.casefold() != expected_full_name.casefold()
        or repository_owner.casefold() != expected_organization.casefold()
        or snapshot.number != number
        or snapshot.state.casefold() != "open"
        or snapshot.draft
        or snapshot.merged
    ):
        return None
    return pull_request_from_snapshot(snapshot)


async def _check_participant(
    interaction: discord.Interaction,
    actor_factory: ActorFactory,
) -> bool:
    if actor_factory(interaction).can_participate:
        return True
    await interaction.response.send_message(
        presentation.CANNOT_USE_ACTION,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return False


async def _send_interaction_failure(
    interaction: discord.Interaction,
    error: Exception,
) -> None:
    log.error(
        "GitHub Tickets dashboard interaction failed",
        exc_info=(type(error), error, error.__traceback__),
    )
    kwargs = {
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                presentation.COULD_NOT_COMPLETE_ACTION,
                **kwargs,
            )
        else:
            await interaction.response.send_message(
                presentation.COULD_NOT_COMPLETE_ACTION,
                **kwargs,
            )
    except Exception:
        log.exception("Failed to send GitHub Tickets interaction error feedback")


class _DashboardView(discord.ui.View):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item,
    ) -> None:
        await _send_interaction_failure(interaction, error)


class _DashboardModal(discord.ui.Modal):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await _send_interaction_failure(interaction, error)


def _category_options(categories: Sequence[Category]) -> list[discord.SelectOption]:
    if categories:
        return [
            discord.SelectOption(label=category.name, value=str(category.category_id))
            for category in categories
        ]
    return [
        discord.SelectOption(
            label=presentation.NO_CATEGORIES_CONFIGURED,
            value="none",
        )
    ]


def _visible_profiles(
    profiles: Sequence[Profile],
    member_lookup: MemberLookup,
    member_actor_factory: MemberActorFactory,
) -> tuple[tuple[Profile, discord.Member], ...]:
    visible: list[tuple[Profile, discord.Member]] = []
    for profile in profiles:
        member = member_lookup(profile.user_id)
        if member is None or not member_actor_factory(member).can_participate:
            continue
        visible.append((profile, member))
    return tuple(visible)


def _profile_line(profile: Profile, member: discord.Member) -> str:
    discord_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(member.name)
    )
    return (
        f"<@{profile.user_id}> | {discord_name}"
        + (f" | {profile.github_username}" if profile.github_username else "")
    )


async def send_developer_profile(
    interaction: discord.Interaction,
    store: GitHubTicketsStore,
    *,
    guild_id: int,
    user_id: int,
) -> None:
    profile = await store.get_profile(guild_id, user_id)
    category_names: tuple[str, ...] = ()
    if profile is not None:
        categories = {
            category.category_id: category.name
            for category in await store.list_categories(guild_id)
        }
        category_names = tuple(
            categories[category_id]
            for category_id in profile.category_ids
            if category_id in categories
        )
    content = presentation.developer_profile(
        mention=f"<@{user_id}>",
        has_profile=profile is not None,
        github_username=profile.github_username if profile is not None else None,
        categories=category_names,
    )
    kwargs = {
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if len(content) <= presentation.DISCORD_MESSAGE_LIMIT:
        await interaction.response.send_message(content, **kwargs)
        return
    await interaction.response.send_message(
        embed=discord.Embed(description=content),
        **kwargs,
    )


class EditProfileModal(_DashboardModal):
    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        categories: Sequence[Category],
        profile: Profile | None,
        actor_factory: ActorFactory,
        clock: Callable[[], datetime],
    ) -> None:
        super().__init__(title=presentation.EDIT_PROFILE)
        self._store = store
        self._guild_id = guild_id
        self._actor_factory = actor_factory
        self._clock = clock
        visible_categories = tuple(categories[:25])

        self.github_profile_link = discord.ui.TextInput(
            default=(
                _github_profile_link(profile.github_username)
                if profile is not None and profile.github_username
                else None
            ),
            required=False,
            max_length=presentation.MAX_GITHUB_PROFILE_LINK_LENGTH,
        )
        self.categories = discord.ui.Select(
            placeholder=presentation.SELECT_YOUR_CATEGORIES,
            options=_category_options(visible_categories),
            min_values=0,
            max_values=max(1, len(visible_categories)),
            required=False,
            disabled=not visible_categories,
        )
        self.automatic_pings = discord.ui.Checkbox(
            default=profile.automatic_pings if profile is not None else False,
        )
        if profile is not None:
            selected_ids = set(profile.category_ids)
            for option in self.categories.options:
                option.default = int(option.value) in selected_ids if option.value != "none" else False
        self.add_item(
            discord.ui.Label(
                text=presentation.GITHUB_PROFILE_LINK,
                description=presentation.GITHUB_PROFILE_LINK_DESCRIPTION,
                component=self.github_profile_link,
            )
        )
        self.add_item(
            discord.ui.Label(
                text=presentation.CATEGORIES,
                component=self.categories,
            )
        )
        self.add_item(
            discord.ui.Label(
                text=presentation.ALLOW_AUTOMATIC_PINGS,
                component=self.automatic_pings,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _check_participant(interaction, self._actor_factory):
            return
        profile_link = str(self.github_profile_link.value).strip()
        github_username = (
            _parse_github_profile_link(profile_link) if profile_link else None
        )
        if profile_link and github_username is None:
            await interaction.response.send_message(
                presentation.INVALID_GITHUB_PROFILE_LINK,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        category_ids = tuple(int(value) for value in self.categories.values)
        if self.automatic_pings.value and not category_ids:
            await interaction.response.send_message(
                presentation.AUTOMATIC_REQUIRES_CATEGORY,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if category_ids:
            current_ids = {
                category.category_id
                for category in await self._store.list_categories(self._guild_id)
            }
            if any(category_id not in current_ids for category_id in category_ids):
                await interaction.response.send_message(
                    presentation.CATEGORY_NO_LONGER_EXISTS,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
        actor = self._actor_factory(interaction)
        await self._store.save_profile(
            guild_id=self._guild_id,
            user_id=actor.user_id,
            github_username=github_username,
            category_ids=category_ids,
            automatic_pings=self.automatic_pings.value,
            updated_at=self._clock(),
        )
        await interaction.response.defer()


class NewTicketModal(_DashboardModal):
    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        categories: Sequence[Category],
        create_ticket: CreateTicket,
        fetch_pull_request: FetchPullRequest,
        expected_organization: str,
        actor_factory: ActorFactory,
        count_automatic_candidates: CountAutomaticCandidates,
        draft: TicketRequest | None = None,
    ) -> None:
        super().__init__(title=presentation.NEW_TICKET)
        self._store = store
        self._guild_id = guild_id
        self._create_ticket = create_ticket
        self._fetch_pull_request = fetch_pull_request
        self._expected_organization = expected_organization
        self._actor_factory = actor_factory
        self._count_automatic_candidates = count_automatic_candidates
        visible_categories = tuple(categories[:25])

        self.pr_link = discord.ui.TextInput(
            placeholder=presentation.ENTER_PR_LINK,
            default=draft.pr_url if draft is not None else None,
            required=True,
            max_length=presentation.MAX_PR_URL_LENGTH,
        )
        self.categories = discord.ui.Select(
            placeholder=presentation.SELECT_CATEGORIES,
            options=_category_options(visible_categories),
            min_values=0,
            max_values=min(
                max(1, len(visible_categories)),
                presentation.ticket_category_selection_limit(
                    tuple(category.name for category in visible_categories)
                ),
            ),
            required=False,
            disabled=not visible_categories,
        )
        self.ping_behavior = discord.ui.RadioGroup(
            options=[
                discord.RadioGroupOption(
                    label=presentation.NO_PING,
                    value=RoutingMode.NONE.value,
                    default=draft is not None and draft.routing_mode is RoutingMode.NONE,
                ),
                discord.RadioGroupOption(
                    label=presentation.AUTOMATIC,
                    value=RoutingMode.AUTOMATIC.value,
                    default=draft is not None and draft.routing_mode is RoutingMode.AUTOMATIC,
                ),
                discord.RadioGroupOption(
                    label=presentation.DIRECT_THEN_WAIT,
                    value=RoutingMode.DIRECT_WAIT.value,
                    default=draft is not None and draft.routing_mode is RoutingMode.DIRECT_WAIT,
                ),
                discord.RadioGroupOption(
                    label=presentation.DIRECT_THEN_AUTOMATIC,
                    value=RoutingMode.DIRECT_AUTOMATIC.value,
                    default=draft is not None
                    and draft.routing_mode is RoutingMode.DIRECT_AUTOMATIC,
                ),
            ],
            required=True,
        )
        self.direct_reviewer = discord.ui.UserSelect(
            placeholder=presentation.SELECT_A_REVIEWER,
            min_values=0,
            max_values=1,
            required=False,
            default_values=(
                [discord.Object(id=draft.direct_target_id)]
                if draft is not None and draft.direct_target_id is not None
                else None
            ),
        )
        if draft is not None:
            selected_ids = set(draft.category_ids)
            for option in self.categories.options:
                option.default = (
                    int(option.value) in selected_ids if option.value != "none" else False
                )
        for label, component, description in (
            (presentation.PR_LINK, self.pr_link, None),
            (presentation.CATEGORIES, self.categories, None),
            (
                presentation.PING_BEHAVIOR,
                self.ping_behavior,
                presentation.SELECT_PING_BEHAVIOR,
            ),
            (
                presentation.DIRECT_REVIEWER,
                self.direct_reviewer,
                presentation.DIRECT_REVIEWER_DESCRIPTION,
            ),
        ):
            self.add_item(
                discord.ui.Label(
                    text=label,
                    description=description,
                    component=component,
                )
            )

    async def _linked_pull_request(
        self,
        interaction: discord.Interaction,
    ) -> GitHubPullRequest | None:
        identity = _parse_pull_request_link(str(self.pr_link.value).strip())
        if identity is None:
            await self._send_error(interaction, presentation.COULD_NOT_CREATE_TICKET)
            return None
        owner, repository, number = identity
        if owner.casefold() != self._expected_organization.casefold():
            await self._send_error(interaction, presentation.COULD_NOT_CREATE_TICKET)
            return None
        await interaction.response.defer(ephemeral=True)
        try:
            snapshot = await self._fetch_pull_request(owner, repository, number)
        except Exception:
            log.exception("GitHub Tickets pull request lookup failed")
            await self._send_error(interaction, presentation.COULD_NOT_CREATE_TICKET)
            return None
        pull_request = _validated_pull_request(
            snapshot,
            owner=owner,
            repository=repository,
            number=number,
            expected_organization=self._expected_organization,
        )
        if pull_request is None:
            await self._send_error(interaction, presentation.COULD_NOT_CREATE_TICKET)
        return pull_request

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _check_participant(interaction, self._actor_factory):
            return
        actor = self._actor_factory(interaction)
        routing_mode = RoutingMode(self.ping_behavior.value)
        category_ids = tuple(int(value) for value in self.categories.values)
        if routing_mode in (RoutingMode.AUTOMATIC, RoutingMode.DIRECT_AUTOMATIC) and not category_ids:
            await self._send_error(interaction, presentation.AUTOMATIC_REQUIRES_CATEGORY)
            return
        selected_direct_target_id = (
            self.direct_reviewer.values[0].id if self.direct_reviewer.values else None
        )
        direct_routing = routing_mode in (
            RoutingMode.DIRECT_WAIT,
            RoutingMode.DIRECT_AUTOMATIC,
        )
        if direct_routing and selected_direct_target_id is None:
            await self._send_error(interaction, presentation.DIRECT_REQUIRES_REVIEWER)
            return
        direct_target_id = selected_direct_target_id if direct_routing else None
        if direct_target_id == actor.user_id:
            await self._send_error(interaction, SELF_REVIEW_DENIED)
            return

        current_categories = {
            category.category_id: category
            for category in await self._store.list_categories(self._guild_id)
        }
        if any(category_id not in current_categories for category_id in category_ids):
            await self._send_error(interaction, presentation.CATEGORY_NO_LONGER_EXISTS)
            return
        pull_request = await self._linked_pull_request(interaction)
        if pull_request is None:
            return
        request = TicketRequest(
            guild_id=self._guild_id,
            pr_title=pull_request.title,
            pr_url=pull_request.url,
            category_display=", ".join(
                current_categories[category_id].name for category_id in category_ids
            ),
            routing_mode=routing_mode,
            direct_target_id=direct_target_id,
            category_ids=category_ids,
        )
        if (
            len(category_ids) > 1
            and routing_mode in (RoutingMode.AUTOMATIC, RoutingMode.DIRECT_AUTOMATIC)
        ):
            excluded_user_ids = {actor.user_id}
            if direct_target_id is not None:
                excluded_user_ids.add(direct_target_id)
            candidate_count = await self._count_automatic_candidates(
                self._guild_id,
                category_ids,
                frozenset(excluded_user_ids),
            )
            await interaction.followup.send(
                presentation.confirm_categories(candidate_count),
                view=ConfirmCategoriesView(
                    self._store,
                    guild_id=self._guild_id,
                    categories=tuple(
                        current_categories[category_id] for category_id in category_ids
                    ),
                    request=request,
                    pull_request=pull_request,
                    create_ticket=self._create_ticket,
                    fetch_pull_request=self._fetch_pull_request,
                    expected_organization=self._expected_organization,
                    actor_factory=self._actor_factory,
                    count_automatic_candidates=self._count_automatic_candidates,
                    automatic_candidate_exclusions=frozenset(excluded_user_ids),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await _create_ticket_request(
                interaction,
                request=request,
                pull_request=pull_request,
                create_ticket=self._create_ticket,
                actor_factory=self._actor_factory,
            )

    @staticmethod
    async def _send_error(
        interaction: discord.Interaction,
        message: str | None,
    ) -> None:
        kwargs = {
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if interaction.response.is_done():
            await interaction.followup.send(message, **kwargs)
        else:
            await interaction.response.send_message(message, **kwargs)


async def _create_ticket_request(
    interaction: discord.Interaction,
    *,
    request: TicketRequest,
    pull_request: GitHubPullRequest,
    create_ticket: CreateTicket,
    actor_factory: ActorFactory,
) -> bool:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    result = await create_ticket(
        request,
        actor_factory(interaction),
        pull_request,
    )
    if result.success:
        return True
    await interaction.followup.send(result.response, ephemeral=True)
    return False


class ConfirmCategoriesView(_DashboardView):
    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        categories: Sequence[Category],
        request: TicketRequest,
        pull_request: GitHubPullRequest,
        create_ticket: CreateTicket,
        fetch_pull_request: FetchPullRequest,
        expected_organization: str,
        actor_factory: ActorFactory,
        count_automatic_candidates: CountAutomaticCandidates,
        automatic_candidate_exclusions: frozenset[int],
    ) -> None:
        super().__init__()
        self._store = store
        self._guild_id = guild_id
        self._categories = tuple(categories)
        self._request = request
        self._pull_request = pull_request
        self._create_ticket = create_ticket
        self._fetch_pull_request = fetch_pull_request
        self._expected_organization = expected_organization
        self._actor_factory = actor_factory
        self._count_automatic_candidates = count_automatic_candidates
        self._automatic_candidate_exclusions = automatic_candidate_exclusions
        self._submitted = False
        self._selected_ids = request.category_ids

        self.categories = discord.ui.Select(
            placeholder=presentation.SELECT_CATEGORIES,
            options=[
                discord.SelectOption(
                    label=category.name,
                    value=str(category.category_id),
                    default=True,
                )
                for category in self._categories
            ],
            min_values=1,
            max_values=len(self._categories),
            required=True,
        )
        self.categories.callback = self._categories_changed
        self.add_item(self.categories)

        back = discord.ui.Button(
            label=presentation.BACK,
            style=discord.ButtonStyle.secondary,
        )
        back.callback = self._back
        self.add_item(back)
        create = discord.ui.Button(
            label=presentation.CREATE_TICKET,
            style=discord.ButtonStyle.primary,
        )
        create.callback = self._create
        self.add_item(create)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _check_participant(interaction, self._actor_factory)

    def _component_category_ids(self) -> tuple[int, ...]:
        selected = {int(value) for value in self.categories.values}
        return tuple(
            category.category_id
            for category in self._categories
            if category.category_id in selected
        )

    def _selected_request(self) -> TicketRequest:
        category_ids = self._selected_ids
        categories_by_id = {
            category.category_id: category for category in self._categories
        }
        return replace(
            self._request,
            category_ids=category_ids,
            category_display=", ".join(
                categories_by_id[category_id].name for category_id in category_ids
            ),
        )

    async def _categories_changed(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await interaction.response.defer()
            return
        self._selected_ids = self._component_category_ids()
        selected = set(self._selected_ids)
        for option in self.categories.options:
            option.default = int(option.value) in selected
        current_ids = {
            category.category_id
            for category in await self._store.list_categories(self._guild_id)
        }
        if self._submitted:
            await interaction.response.defer()
            return
        if any(category_id not in current_ids for category_id in self._selected_ids):
            await interaction.response.send_message(
                presentation.CATEGORY_NO_LONGER_EXISTS,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        candidate_count = await self._count_automatic_candidates(
            self._guild_id,
            self._selected_ids,
            self._automatic_candidate_exclusions,
        )
        await interaction.response.edit_message(
            content=presentation.confirm_categories(candidate_count),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await interaction.response.defer()
            return
        self._submitted = True
        categories = await self._store.list_categories(self._guild_id)
        await interaction.response.send_modal(
            NewTicketModal(
                self._store,
                guild_id=self._guild_id,
                categories=categories,
                create_ticket=self._create_ticket,
                fetch_pull_request=self._fetch_pull_request,
                expected_organization=self._expected_organization,
                actor_factory=self._actor_factory,
                count_automatic_candidates=self._count_automatic_candidates,
                draft=self._selected_request(),
            )
        )

    async def _create(self, interaction: discord.Interaction) -> None:
        if self._submitted:
            await interaction.response.defer()
            return
        self._submitted = True
        current_ids = {
            category.category_id
            for category in await self._store.list_categories(self._guild_id)
        }
        request = self._selected_request()
        if any(category_id not in current_ids for category_id in request.category_ids):
            self._submitted = False
            await interaction.response.send_message(
                presentation.CATEGORY_NO_LONGER_EXISTS,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if await _create_ticket_request(
            interaction,
            request=request,
            pull_request=self._pull_request,
            create_ticket=self._create_ticket,
            actor_factory=self._actor_factory,
        ):
            await interaction.delete_original_response()


async def send_new_ticket_modal(
    interaction: discord.Interaction,
    store: GitHubTicketsStore,
    *,
    guild_id: int,
    create_ticket: CreateTicket,
    fetch_pull_request: FetchPullRequest | None,
    expected_organization: str | None,
    actor_factory: ActorFactory,
    count_automatic_candidates: CountAutomaticCandidates,
) -> None:
    if not await _check_participant(interaction, actor_factory):
        return
    if fetch_pull_request is None or not expected_organization:
        await interaction.response.send_message(
            GITHUB_INTEGRATION_UNAVAILABLE,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    categories = await store.list_categories(guild_id)
    await interaction.response.send_modal(
        NewTicketModal(
            store,
            guild_id=guild_id,
            categories=categories,
            create_ticket=create_ticket,
            fetch_pull_request=fetch_pull_request,
            expected_organization=expected_organization,
            actor_factory=actor_factory,
            count_automatic_candidates=count_automatic_candidates,
        )
    )


class ClearProfileConfirmation(_DashboardView):
    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        actor_factory: ActorFactory,
        clock: Callable[[], datetime],
    ) -> None:
        super().__init__()
        self._store = store
        self._guild_id = guild_id
        self._actor_factory = actor_factory
        self._clock = clock
        button = discord.ui.Button(
            label=presentation.CLEAR_PROFILE,
            style=discord.ButtonStyle.danger,
        )
        button.callback = self._clear
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _check_participant(interaction, self._actor_factory)

    async def _clear(self, interaction: discord.Interaction) -> None:
        actor = self._actor_factory(interaction)
        await self._store.save_profile(
            guild_id=self._guild_id,
            user_id=actor.user_id,
            github_username=None,
            category_ids=(),
            automatic_pings=False,
            updated_at=self._clock(),
        )
        await interaction.response.defer()
        await interaction.delete_original_response()


class CategoryBrowser(_DashboardView):
    PAGE_SIZE = 10

    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        categories: Sequence[Category],
        back_view: GitHubTicketsDashboard,
        actor_factory: ActorFactory,
        member_lookup: MemberLookup,
        member_actor_factory: MemberActorFactory,
    ) -> None:
        super().__init__()
        self._store = store
        self._guild_id = guild_id
        self._categories = {category.category_id: category for category in categories[:25]}
        self._back_view = back_view
        self._actor_factory = actor_factory
        self._member_lookup = member_lookup
        self._member_actor_factory = member_actor_factory
        self._selected_category: Category | None = None
        self._profiles: Sequence[tuple[Profile, discord.Member]] = ()
        self._page = 0

        self.category_select = discord.ui.Select(
            placeholder=presentation.SELECT_A_CATEGORY,
            options=[
                discord.SelectOption(label=category.name, value=str(category.category_id))
                for category in categories[:25]
            ],
        )
        self.category_select.callback = self._select_category
        self.add_item(self.category_select)

        self.previous = discord.ui.Button(
            label=presentation.PREVIOUS,
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        self.next = discord.ui.Button(
            label=presentation.NEXT,
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        back = discord.ui.Button(
            label=presentation.BACK,
            style=discord.ButtonStyle.secondary,
        )
        self.previous.callback = self._previous_page
        self.next.callback = self._next_page
        back.callback = self._back
        self.add_item(self.previous)
        self.add_item(self.next)
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _check_participant(interaction, self._actor_factory)

    async def _select_category(self, interaction: discord.Interaction) -> None:
        category_id = int(self.category_select.values[0])
        current_categories = {
            category.category_id: category
            for category in await self._store.list_categories(self._guild_id)
        }
        category = current_categories.get(category_id)
        if category is None:
            await interaction.response.send_message(
                presentation.CATEGORY_NO_LONGER_EXISTS,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self._selected_category = category
        profiles = await self._store.list_profiles_for_category(
            self._guild_id,
            category_id,
        )
        self._profiles = _visible_profiles(
            profiles,
            self._member_lookup,
            self._member_actor_factory,
        )
        self._page = 0
        await self._render(interaction)

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        self._page = max(0, self._page - 1)
        await self._render(interaction)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self._page = min(self._page_count - 1, self._page + 1)
        await self._render(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content=presentation.DEVELOPER_PROFILE_COMMAND,
            view=self._back_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @property
    def _page_count(self) -> int:
        return max(1, (len(self._profiles) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    async def _render(self, interaction: discord.Interaction) -> None:
        if self._selected_category is None:
            return
        page_count = self._page_count
        start = self._page * self.PAGE_SIZE
        visible_profiles = self._profiles[start : start + self.PAGE_SIZE]
        users = [_profile_line(profile, member) for profile, member in visible_profiles]
        self.previous.disabled = self._page == 0
        self.next.disabled = self._page >= page_count - 1
        await interaction.response.edit_message(
            content=presentation.category_page(
                category=self._selected_category.name,
                users=users,
                page=self._page + 1,
                page_count=page_count,
            ),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class GitHubUsernameLookupModal(_DashboardModal):
    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        actor_factory: ActorFactory,
        member_lookup: MemberLookup,
        member_actor_factory: MemberActorFactory,
    ) -> None:
        super().__init__(title=presentation.FIND_BY_GITHUB_USERNAME)
        self._store = store
        self._guild_id = guild_id
        self._actor_factory = actor_factory
        self._member_lookup = member_lookup
        self._member_actor_factory = member_actor_factory
        self.github_username = discord.ui.TextInput(
            placeholder=presentation.ENTER_GITHUB_USERNAME,
            required=True,
            max_length=presentation.MAX_GITHUB_USERNAME_LENGTH,
        )
        self.add_item(
            discord.ui.Label(
                text=presentation.GITHUB_USERNAME,
                component=self.github_username,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _check_participant(interaction, self._actor_factory):
            return
        profiles = await self._store.list_profiles_by_github_username(
            self._guild_id,
            str(self.github_username.value),
        )
        visible = _visible_profiles(
            profiles,
            self._member_lookup,
            self._member_actor_factory,
        )
        content = "\n".join(
            _profile_line(profile, member) for profile, member in visible
        )
        await interaction.response.send_message(
            content or presentation.NO_PROFILE,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class GitHubTicketsDashboard(_DashboardView):
    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        actor_factory: ActorFactory,
        member_lookup: MemberLookup,
        member_actor_factory: MemberActorFactory,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__()
        self._store = store
        self._guild_id = guild_id
        self._actor_factory = actor_factory
        self._member_lookup = member_lookup
        self._member_actor_factory = member_actor_factory
        self._clock = clock
        for label, style, callback in (
            (presentation.EDIT_PROFILE, discord.ButtonStyle.secondary, self._edit_profile),
            (
                presentation.BROWSE_CATEGORIES,
                discord.ButtonStyle.secondary,
                self._browse_categories,
            ),
            (
                presentation.FIND_BY_GITHUB_USERNAME,
                discord.ButtonStyle.secondary,
                self._find_by_github_username,
            ),
            (presentation.CLEAR_PROFILE, discord.ButtonStyle.danger, self._clear_profile),
        ):
            button = discord.ui.Button(label=label, style=style)
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _check_participant(interaction, self._actor_factory)

    async def send(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            presentation.DEVELOPER_PROFILE_COMMAND,
            view=self,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _edit_profile(self, interaction: discord.Interaction) -> None:
        actor = self._actor_factory(interaction)
        profile = await self._store.get_profile(self._guild_id, actor.user_id)
        categories = await self._store.list_categories(self._guild_id)
        await interaction.response.send_modal(
            EditProfileModal(
                self._store,
                guild_id=self._guild_id,
                categories=categories,
                profile=profile,
                actor_factory=self._actor_factory,
                clock=self._clock,
            )
        )

    async def _browse_categories(self, interaction: discord.Interaction) -> None:
        categories = await self._store.list_categories(self._guild_id)
        if not categories:
            await interaction.response.send_message(
                presentation.NO_CATEGORIES_CONFIGURED,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.edit_message(
            content=presentation.BROWSE_CATEGORIES,
            view=CategoryBrowser(
                self._store,
                guild_id=self._guild_id,
                categories=categories,
                back_view=self,
                actor_factory=self._actor_factory,
                member_lookup=self._member_lookup,
                member_actor_factory=self._member_actor_factory,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _find_by_github_username(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.send_modal(
            GitHubUsernameLookupModal(
                self._store,
                guild_id=self._guild_id,
                actor_factory=self._actor_factory,
                member_lookup=self._member_lookup,
                member_actor_factory=self._member_actor_factory,
            )
        )

    async def _clear_profile(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            presentation.ARE_YOU_SURE,
            view=ClearProfileConfirmation(
                self._store,
                guild_id=self._guild_id,
                actor_factory=self._actor_factory,
                clock=self._clock,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
