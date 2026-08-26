from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone

import discord

from . import presentation
from .coordinator import TicketActor, TicketRequest, TicketResult
from .models import Category, Profile, RoutingMode
from .store import GitHubTicketsStore

CreateTicket = Callable[[TicketRequest, TicketActor], Awaitable[TicketResult]]
ActorFactory = Callable[[discord.Interaction], TicketActor]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    await interaction.response.send_message(
        presentation.developer_profile(
            mention=f"<@{user_id}>",
            has_profile=profile is not None,
            github_username=profile.github_username if profile is not None else None,
            categories=category_names,
        ),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class EditProfileModal(discord.ui.Modal):
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

        self.github_username = discord.ui.TextInput(
            default=profile.github_username if profile is not None else None,
            required=False,
            max_length=presentation.MAX_GITHUB_USERNAME_LENGTH,
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
                text=presentation.GITHUB_USERNAME,
                description=presentation.GITHUB_USERNAME_DESCRIPTION,
                component=self.github_username,
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
            github_username=str(self.github_username.value),
            category_ids=category_ids,
            automatic_pings=self.automatic_pings.value,
            updated_at=self._clock(),
        )
        await interaction.response.defer()


class NewTicketModal(discord.ui.Modal):
    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        categories: Sequence[Category],
        create_ticket: CreateTicket,
        actor_factory: ActorFactory,
    ) -> None:
        super().__init__(title=presentation.NEW_TICKET)
        self._store = store
        self._guild_id = guild_id
        self._create_ticket = create_ticket
        self._actor_factory = actor_factory
        visible_categories = tuple(categories[:25])

        self.pr_title = discord.ui.TextInput(
            placeholder=presentation.ENTER_PR_TITLE,
            required=True,
            max_length=presentation.MAX_PR_TITLE_LENGTH,
        )
        self.pr_link = discord.ui.TextInput(
            placeholder=presentation.ENTER_PR_LINK,
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
                ),
                discord.RadioGroupOption(
                    label=presentation.AUTOMATIC,
                    value=RoutingMode.AUTOMATIC.value,
                ),
                discord.RadioGroupOption(
                    label=presentation.DIRECT_THEN_WAIT,
                    value=RoutingMode.DIRECT_WAIT.value,
                ),
                discord.RadioGroupOption(
                    label=presentation.DIRECT_THEN_AUTOMATIC,
                    value=RoutingMode.DIRECT_AUTOMATIC.value,
                ),
            ],
            required=True,
        )
        self.direct_reviewer = discord.ui.UserSelect(
            placeholder=presentation.SELECT_A_REVIEWER,
            min_values=0,
            max_values=1,
            required=False,
        )
        for label, component in (
            (presentation.PR_TITLE, self.pr_title),
            (presentation.PR_LINK, self.pr_link),
            (presentation.CATEGORIES, self.categories),
            (presentation.PING_BEHAVIOR, self.ping_behavior),
            (presentation.DIRECT_REVIEWER, self.direct_reviewer),
        ):
            self.add_item(discord.ui.Label(text=label, component=component))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _check_participant(interaction, self._actor_factory):
            return
        routing_mode = RoutingMode(self.ping_behavior.value)
        category_ids = tuple(int(value) for value in self.categories.values)
        if routing_mode in (RoutingMode.AUTOMATIC, RoutingMode.DIRECT_AUTOMATIC) and not category_ids:
            await self._send_error(interaction, presentation.AUTOMATIC_REQUIRES_CATEGORY)
            return
        direct_target_id = (
            self.direct_reviewer.values[0].id if self.direct_reviewer.values else None
        )
        if routing_mode in (RoutingMode.DIRECT_WAIT, RoutingMode.DIRECT_AUTOMATIC) and direct_target_id is None:
            await self._send_error(interaction, presentation.DIRECT_REQUIRES_REVIEWER)
            return

        current_categories = {
            category.category_id: category
            for category in await self._store.list_categories(self._guild_id)
        }
        if any(category_id not in current_categories for category_id in category_ids):
            await self._send_error(interaction, presentation.CATEGORY_NO_LONGER_EXISTS)
            return
        await interaction.response.defer()
        result = await self._create_ticket(
            TicketRequest(
                guild_id=self._guild_id,
                pr_title=str(self.pr_title.value).strip(),
                pr_url=str(self.pr_link.value).strip(),
                category_display=", ".join(
                    current_categories[category_id].name for category_id in category_ids
                ),
                routing_mode=routing_mode,
                direct_target_id=direct_target_id,
                category_ids=category_ids,
            ),
            self._actor_factory(interaction),
        )
        if result.success:
            return
        await interaction.followup.send(result.response, ephemeral=True)

    @staticmethod
    async def _send_error(
        interaction: discord.Interaction,
        message: str | None,
    ) -> None:
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ClearProfileConfirmation(discord.ui.View):
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


class CategoryBrowser(discord.ui.View):
    PAGE_SIZE = 10

    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        categories: Sequence[Category],
        back_view: GitHubTicketsDashboard,
        actor_factory: ActorFactory,
    ) -> None:
        super().__init__()
        self._store = store
        self._guild_id = guild_id
        self._categories = {category.category_id: category for category in categories[:25]}
        self._back_view = back_view
        self._actor_factory = actor_factory
        self._selected_category: Category | None = None
        self._profiles: Sequence[Profile] = ()
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
        self._profiles = await self._store.list_profiles_for_category(
            self._guild_id,
            category_id,
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
            content=presentation.DASHBOARD_TITLE,
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
        users = [
            f"<@{profile.user_id}>"
            + (f" | {profile.github_username}" if profile.github_username else "")
            for profile in visible_profiles
        ]
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


class GitHubTicketsDashboard(discord.ui.View):
    def __init__(
        self,
        store: GitHubTicketsStore,
        *,
        guild_id: int,
        create_ticket: CreateTicket,
        actor_factory: ActorFactory,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__()
        self._store = store
        self._guild_id = guild_id
        self._create_ticket = create_ticket
        self._actor_factory = actor_factory
        self._clock = clock
        for label, style, callback in (
            (presentation.NEW_TICKET, discord.ButtonStyle.primary, self._new_ticket),
            (presentation.EDIT_PROFILE, discord.ButtonStyle.secondary, self._edit_profile),
            (
                presentation.BROWSE_CATEGORIES,
                discord.ButtonStyle.secondary,
                self._browse_categories,
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
            presentation.DASHBOARD_TITLE,
            view=self,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _new_ticket(self, interaction: discord.Interaction) -> None:
        categories = await self._store.list_categories(self._guild_id)
        await interaction.response.send_modal(
            NewTicketModal(
                self._store,
                guild_id=self._guild_id,
                categories=categories,
                create_ticket=self._create_ticket,
                actor_factory=self._actor_factory,
            )
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
            ),
            allowed_mentions=discord.AllowedMentions.none(),
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
