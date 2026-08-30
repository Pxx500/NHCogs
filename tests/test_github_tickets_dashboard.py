from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "NHCogs"
PACKAGE_PATH = ROOT / PACKAGE_NAME
GITHUBTICKETS_PACKAGE_NAME = f"{PACKAGE_NAME}.githubtickets"
GITHUBTICKETS_PACKAGE_PATH = PACKAGE_PATH / "githubtickets"


def _install_discord_stub():
    discord = types.ModuleType("discord")
    ui = types.ModuleType("discord.ui")

    class ButtonStyle:
        primary = "primary"
        secondary = "secondary"
        danger = "danger"

    class AllowedMentions:
        def __init__(self, *, everyone=True, users=True, roles=True, replied_user=True):
            self.everyone = everyone
            self.users = users
            self.roles = roles
            self.replied_user = replied_user

        @classmethod
        def none(cls):
            return cls(everyone=False, users=False, roles=False, replied_user=False)

    class Embed:
        def __init__(self, *, description=None):
            self.description = description

    class Item:
        pass

    class View:
        def __init__(self, *, timeout=180):
            self.timeout = timeout
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class Modal(View):
        def __init__(self, *, title, timeout=None, custom_id=None):
            super().__init__(timeout=timeout)
            self.title = title
            self.custom_id = custom_id

    class Button(Item):
        def __init__(self, *, label, style, custom_id=None, disabled=False):
            self.label = label
            self.style = style
            self.custom_id = custom_id
            self.disabled = disabled
            self.callback = None

    class Label(Item):
        def __init__(self, *, text, component, description=None, id=None):
            self.text = text
            self.component = component
            self.description = description
            self.id = id

    class TextInput(Item):
        def __init__(
            self,
            *,
            label=None,
            placeholder=None,
            default=None,
            required=True,
            **_kwargs,
        ):
            self.label = label
            self.placeholder = placeholder
            self.default = default
            self.required = required
            self.value = default or ""
            self.__dict__.update(_kwargs)

    class Select(Item):
        def __init__(
            self,
            *,
            placeholder=None,
            options=None,
            min_values=1,
            max_values=1,
            required=True,
            disabled=False,
            **_kwargs,
        ):
            self.placeholder = placeholder
            self.options = options or []
            self.min_values = min_values
            self.max_values = max_values
            self.required = required
            self.disabled = disabled
            self.values = []
            self.callback = None
            self.__dict__.update(_kwargs)

    class UserSelect(Select):
        pass

    class Checkbox(Item):
        def __init__(self, *, default=False, **_kwargs):
            self.default = default
            self.value = default

    class RadioGroup(Item):
        def __init__(self, *, options, required=True, **_kwargs):
            self.options = options
            self.required = required
            self.value = None

    class SelectOption:
        def __init__(self, *, label, value=None, description=None, default=False):
            self.label = label
            self.value = label if value is None else value
            self.description = description
            self.default = default

    class RadioGroupOption(SelectOption):
        pass

    ui.View = View
    ui.Modal = Modal
    ui.Button = Button
    ui.Label = Label
    ui.TextInput = TextInput
    ui.Select = Select
    ui.UserSelect = UserSelect
    ui.Checkbox = Checkbox
    ui.RadioGroup = RadioGroup
    discord.ui = ui
    discord.ButtonStyle = ButtonStyle
    discord.AllowedMentions = AllowedMentions
    discord.Embed = Embed
    discord.SelectOption = SelectOption
    discord.RadioGroupOption = RadioGroupOption
    discord.Interaction = type("Interaction", (), {})
    discord.Member = type("Member", (), {})
    discord.Object = lambda *, id: types.SimpleNamespace(id=id)
    discord.utils = types.SimpleNamespace(
        escape_markdown=lambda value: value.replace("_", r"\_"),
        escape_mentions=lambda value: value.replace("@", "@\u200b"),
    )
    sys.modules["discord"] = discord
    sys.modules["discord.ui"] = ui
    return discord


discord = _install_discord_stub()


def _load_modules():
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules[PACKAGE_NAME] = package

    githubtickets_package = sys.modules.get(GITHUBTICKETS_PACKAGE_NAME)
    if githubtickets_package is None:
        githubtickets_package = types.ModuleType(GITHUBTICKETS_PACKAGE_NAME)
        githubtickets_package.__path__ = [str(GITHUBTICKETS_PACKAGE_PATH)]
        sys.modules[GITHUBTICKETS_PACKAGE_NAME] = githubtickets_package

    dashboard = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.dashboard")
    coordinator = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.coordinator")
    models = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.models")
    presentation = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.presentation")
    return dashboard, coordinator, models, presentation


dashboard_module, coordinator, models, presentation = _load_modules()


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.modals = []
        self.edits = []
        self.defer_calls = 0
        self.defer_kwargs = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    def is_done(self):
        return self.defer_calls > 0 or bool(self.messages) or bool(self.modals)

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def defer(self, **kwargs):
        self.defer_calls += 1
        self.defer_kwargs.append(kwargs)


class FakeInteraction:
    def __init__(self):
        self.response = FakeResponse()
        self.followup = types.SimpleNamespace(send=mock.AsyncMock())
        self.deleted_original_responses = 0

    async def delete_original_response(self):
        self.deleted_original_responses += 1


class FakeGuild:
    def __init__(self, members=()):
        self._members = {member.id: member for member in members}

    def get_member(self, user_id):
        return self._members.get(user_id)


class FakeStore:
    def __init__(self):
        self.categories = []
        self.profiles = {}
        self.category_profiles = {}
        self.matching_profiles = {}
        self.github_profiles = {}
        self.save_calls = []

    async def list_categories(self, _guild_id):
        return tuple(self.categories)

    async def get_profile(self, guild_id, user_id):
        return self.profiles.get((guild_id, user_id))

    async def save_profile(self, **kwargs):
        self.save_calls.append(kwargs)

    async def list_profiles_for_category(self, guild_id, category_id):
        return tuple(self.category_profiles.get((guild_id, category_id), ()))

    async def list_matching_profiles(self, guild_id, category_ids):
        return tuple(self.matching_profiles.get((guild_id, tuple(category_ids)), ()))

    async def list_profiles_by_github_username(self, guild_id, github_username):
        return tuple(self.github_profiles.get((guild_id, github_username.casefold()), ()))


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def make_dashboard(self, store=None, *, guild=None, member_actor_factory=None):
        store = store or FakeStore()
        actor = coordinator.TicketActor(10, True, False)
        guild = guild or FakeGuild()
        member_actor_factory = member_actor_factory or (
            lambda member: coordinator.TicketActor(member.id, True, False)
        )
        view = dashboard_module.GitHubTicketsDashboard(
            store,
            guild_id=100,
            member_lookup=guild.get_member,
            actor_factory=lambda _interaction: actor,
            member_actor_factory=member_actor_factory,
            clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
        )
        return view, store, actor

    async def open_ticket_modal(
        self,
        store=None,
        actor=None,
        create_ticket=None,
        *,
        count_candidates=None,
        fetch_pull_request=None,
        expected_organization="NewHorizons",
    ):
        store = store or FakeStore()
        actor = actor or coordinator.TicketActor(10, True, False)

        async def successful_create(_request, _actor, _pull_request):
            return coordinator.TicketResult(True)

        async def successful_fetch(_owner, _repository, number):
            return types.SimpleNamespace(
                pull_request_id=700,
                number=number,
                repository_id=100,
                repository_full_name="NewHorizons/NHCogs",
                title="Fetched pull request",
                url=f"https://github.com/NewHorizons/NHCogs/pull/{number}",
                state="open",
                draft=False,
                merged=False,
                author_id=900,
                author_login="author",
                updated_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
                labels=(),
                assignees=(),
            )

        async def no_candidates(_guild_id, _category_ids, _excluded_user_ids):
            return 0

        interaction = FakeInteraction()
        await dashboard_module.send_new_ticket_modal(
            interaction,
            store,
            guild_id=100,
            create_ticket=create_ticket or successful_create,
            fetch_pull_request=fetch_pull_request or successful_fetch,
            expected_organization=expected_organization,
            actor_factory=lambda _interaction: actor,
            count_automatic_candidates=count_candidates or no_candidates,
        )
        modal = interaction.response.modals[0]
        modal.pr_link.value = "https://github.com/NewHorizons/NHCogs/pull/42"
        return modal

    async def test_dashboard_is_exact_ephemeral_profile_interface(self):
        view, _store, _actor = self.make_dashboard()
        interaction = FakeInteraction()

        await view.send(interaction)

        self.assertEqual(
            [(button.label, button.style) for button in view.children],
            [
                (presentation.EDIT_PROFILE, discord.ButtonStyle.secondary),
                (presentation.BROWSE_CATEGORIES, discord.ButtonStyle.secondary),
                (presentation.FIND_BY_GITHUB_USERNAME, discord.ButtonStyle.secondary),
                (presentation.CLEAR_PROFILE, discord.ButtonStyle.danger),
            ],
        )
        self.assertEqual(len(interaction.response.messages), 1)
        content, kwargs = interaction.response.messages[0]
        self.assertEqual(content, presentation.DEVELOPER_PROFILE_COMMAND)
        self.assertIs(kwargs["view"], view)
        self.assertTrue(kwargs["ephemeral"])
        allowed_mentions = kwargs["allowed_mentions"]
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)

    async def test_github_username_lookup_returns_only_cached_participants_without_pinging(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.github_profiles[(100, "someuser")] = [
            models.Profile(100, 20, "SomeUser", False, (), now),
            models.Profile(100, 21, "someuser", False, (), now),
            models.Profile(100, 22, "SOMEUSER", False, (), now),
        ]
        members = [
            types.SimpleNamespace(
                id=20,
                name="discord_user",
                is_participant=True,
                can_manage_messages=False,
            ),
            types.SimpleNamespace(
                id=21,
                name="staff_user",
                is_participant=False,
                can_manage_messages=True,
            ),
        ]
        dashboard, _store, _actor = self.make_dashboard(
            store,
            guild=FakeGuild(members),
            member_actor_factory=lambda member: coordinator.TicketActor(
                member.id,
                member.is_participant,
                member.can_manage_messages,
            ),
        )
        open_interaction = FakeInteraction()

        await dashboard.children[2].callback(open_interaction)

        modal = open_interaction.response.modals[0]
        self.assertEqual(modal.title, presentation.FIND_BY_GITHUB_USERNAME)
        self.assertEqual(modal.github_username.placeholder, presentation.ENTER_GITHUB_USERNAME)
        modal.github_username.value = "SomeUser"
        submit_interaction = FakeInteraction()
        await modal.on_submit(submit_interaction)

        content, kwargs = submit_interaction.response.messages[0]
        self.assertEqual(
            content,
            "<@20> | discord\\_user | SomeUser\n<@21> | staff\\_user | someuser",
        )
        self.assertTrue(kwargs["ephemeral"])
        self.assertFalse(kwargs["allowed_mentions"].users)

    async def test_github_username_lookup_reports_no_profile(self):
        dashboard, _store, _actor = self.make_dashboard()
        open_interaction = FakeInteraction()
        await dashboard.children[2].callback(open_interaction)
        modal = open_interaction.response.modals[0]
        modal.github_username.value = "missing"
        submit_interaction = FakeInteraction()

        await modal.on_submit(submit_interaction)

        self.assertEqual(submit_interaction.response.messages[0][0], presentation.NO_PROFILE)

    async def test_dashboard_descendants_recheck_participant_access_after_role_loss(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.categories = [models.Category(1, 100, "rendering", now)]
        current_actor = [coordinator.TicketActor(10, True, False)]

        dashboard = dashboard_module.GitHubTicketsDashboard(
            store,
            guild_id=100,
            member_lookup=FakeGuild().get_member,
            actor_factory=lambda _interaction: current_actor[0],
            member_actor_factory=lambda member: coordinator.TicketActor(
                member.id,
                True,
                False,
            ),
        )

        profile_open = FakeInteraction()
        await dashboard.children[0].callback(profile_open)
        profile_modal = profile_open.response.modals[0]

        clear_open = FakeInteraction()
        await dashboard.children[3].callback(clear_open)
        clear_confirmation = clear_open.response.messages[0][1]["view"]

        browse_open = FakeInteraction()
        await dashboard.children[1].callback(browse_open)
        category_browser = browse_open.response.edits[0]["view"]

        current_actor[0] = coordinator.TicketActor(10, False, False)

        for surface in (dashboard, clear_confirmation, category_browser):
            with self.subTest(surface=type(surface).__name__):
                interaction = FakeInteraction()
                self.assertFalse(await surface.interaction_check(interaction))
                self.assertEqual(
                    interaction.response.messages[0][0],
                    presentation.CANNOT_USE_ACTION,
                )
                self.assertTrue(interaction.response.messages[0][1]["ephemeral"])

        submit_interaction = FakeInteraction()
        await profile_modal.on_submit(submit_interaction)

        self.assertEqual(store.save_calls, [])
        self.assertEqual(
            submit_interaction.response.messages[0][0],
            presentation.CANNOT_USE_ACTION,
        )

    async def test_developer_profile_uses_one_embed_when_valid_content_exceeds_2000(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.categories = [
            models.Category(index, 100, f"category-{index}-" + "x" * 80, now)
            for index in range(1, 26)
        ]
        store.profiles[(100, 10)] = models.Profile(
            100,
            10,
            "developer-name",
            True,
            tuple(range(1, 26)),
            now,
        )
        interaction = FakeInteraction()

        await dashboard_module.send_developer_profile(
            interaction,
            store,
            guild_id=100,
            user_id=10,
        )

        content, kwargs = interaction.response.messages[0]
        self.assertIsNone(content)
        self.assertLessEqual(
            len(kwargs["embed"].description),
            presentation.DISCORD_EMBED_DESCRIPTION_LIMIT,
        )
        self.assertTrue(kwargs["ephemeral"])

    async def test_dashboard_error_boundary_returns_only_generic_copy(self):
        view, _store, _actor = self.make_dashboard()
        interaction = FakeInteraction()
        failure = RuntimeError("private database detail")
        handler = getattr(view, "on_error", None)

        self.assertIsNotNone(handler)
        await handler(interaction, failure, view.children[0])

        self.assertEqual(
            interaction.response.messages[0][0],
            presentation.COULD_NOT_COMPLETE_ACTION,
        )
        self.assertNotIn("private database detail", interaction.response.messages[0][0])
        self.assertTrue(interaction.response.messages[0][1]["ephemeral"])

    async def test_edit_profile_modal_uses_exact_components_and_saves_silently(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "performance", now),
        ]
        store.profiles[(100, 10)] = models.Profile(
            100,
            10,
            "old-name",
            True,
            (1,),
            now,
        )
        view, _store, _actor = self.make_dashboard(store)
        open_interaction = FakeInteraction()

        await view.children[0].callback(open_interaction)

        self.assertEqual(len(open_interaction.response.modals), 1)
        modal = open_interaction.response.modals[0]
        self.assertEqual(modal.title, presentation.EDIT_PROFILE)
        self.assertEqual(
            [label.text for label in modal.children],
            [
                presentation.GITHUB_PROFILE_LINK,
                presentation.CATEGORIES,
                presentation.ALLOW_AUTOMATIC_PINGS,
            ],
        )
        self.assertEqual(
            [label.description for label in modal.children],
            [presentation.GITHUB_PROFILE_LINK_DESCRIPTION, None, None],
        )
        self.assertIsInstance(modal.github_profile_link, discord.ui.TextInput)
        self.assertFalse(modal.github_profile_link.required)
        self.assertEqual(
            modal.github_profile_link.default,
            "https://github.com/old-name",
        )
        self.assertEqual(
            modal.github_profile_link.max_length,
            presentation.MAX_GITHUB_PROFILE_LINK_LENGTH,
        )
        self.assertIsInstance(modal.categories, discord.ui.Select)
        self.assertFalse(modal.categories.required)
        self.assertEqual(modal.categories.min_values, 0)
        self.assertEqual(modal.categories.max_values, 2)
        self.assertEqual(
            [(option.label, option.value, option.default, option.description) for option in modal.categories.options],
            [
                ("rendering", "1", True, None),
                ("performance", "2", False, None),
            ],
        )
        self.assertIsInstance(modal.automatic_pings, discord.ui.Checkbox)
        self.assertTrue(modal.automatic_pings.default)

        modal.github_profile_link.value = "  https://github.com/new-name/  "
        modal.categories.values = ["2"]
        modal.automatic_pings.value = True
        submit_interaction = FakeInteraction()
        await modal.on_submit(submit_interaction)

        self.assertEqual(
            store.save_calls,
            [
                {
                    "guild_id": 100,
                    "user_id": 10,
                    "github_username": "new-name",
                    "category_ids": (2,),
                    "automatic_pings": True,
                    "updated_at": datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
                }
            ],
        )
        self.assertEqual(submit_interaction.response.defer_calls, 1)
        self.assertEqual(submit_interaction.response.messages, [])

    async def test_edit_profile_rejects_non_profile_github_links_without_saving(self):
        view, store, _actor = self.make_dashboard()
        open_interaction = FakeInteraction()
        await view.children[0].callback(open_interaction)
        modal = open_interaction.response.modals[0]

        for invalid_value in (
            "some-user",
            "http://github.com/some-user",
            "https://github.com/some-user/repositories",
            "https://example.com/some-user",
        ):
            with self.subTest(value=invalid_value):
                modal.github_profile_link.value = invalid_value
                submit_interaction = FakeInteraction()

                await modal.on_submit(submit_interaction)

                content, kwargs = submit_interaction.response.messages[0]
                self.assertEqual(content, presentation.INVALID_GITHUB_PROFILE_LINK)
                self.assertTrue(kwargs["ephemeral"])
                self.assertFalse(kwargs["allowed_mentions"].users)

        self.assertEqual(store.save_calls, [])

    async def test_edit_profile_rejects_automatic_pings_without_categories(self):
        view, store, _actor = self.make_dashboard()
        open_interaction = FakeInteraction()
        await view.children[0].callback(open_interaction)
        modal = open_interaction.response.modals[0]
        modal.automatic_pings.value = True
        modal.categories.values = []
        submit_interaction = FakeInteraction()

        await modal.on_submit(submit_interaction)

        self.assertEqual(store.save_calls, [])
        self.assertEqual(
            submit_interaction.response.messages[0][0],
            presentation.AUTOMATIC_REQUIRES_CATEGORY,
        )
        self.assertTrue(submit_interaction.response.messages[0][1]["ephemeral"])

    async def test_profile_editor_rejects_a_category_deleted_while_modal_is_open(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.categories = [models.Category(1, 100, "rendering", now)]
        view, _store, _actor = self.make_dashboard(store)
        open_interaction = FakeInteraction()
        await view.children[0].callback(open_interaction)
        modal = open_interaction.response.modals[0]
        modal.categories.values = ["1"]
        store.categories = []
        submit_interaction = FakeInteraction()

        await modal.on_submit(submit_interaction)

        self.assertEqual(store.save_calls, [])
        self.assertEqual(
            submit_interaction.response.messages[0][0],
            presentation.CATEGORY_NO_LONGER_EXISTS,
        )

    async def test_empty_category_catalog_keeps_modal_selects_valid_and_disabled(self):
        view, _store, _actor = self.make_dashboard()

        profile_interaction = FakeInteraction()
        await view.children[0].callback(profile_interaction)
        profile_select = profile_interaction.response.modals[0].categories

        ticket_select = (await self.open_ticket_modal(_store)).categories

        for select in (profile_select, ticket_select):
            self.assertTrue(select.disabled)
            self.assertEqual(len(select.options), 1)
            self.assertEqual(select.options[0].label, presentation.NO_CATEGORIES_CONFIGURED)

    async def test_clear_profile_uses_one_button_confirmation_and_clears_silently(self):
        view, store, _actor = self.make_dashboard()
        open_interaction = FakeInteraction()

        await view.children[3].callback(open_interaction)

        self.assertEqual(len(open_interaction.response.messages), 1)
        content, kwargs = open_interaction.response.messages[0]
        self.assertEqual(content, presentation.ARE_YOU_SURE)
        self.assertTrue(kwargs["ephemeral"])
        confirmation = kwargs["view"]
        self.assertEqual(
            [(button.label, button.style) for button in confirmation.children],
            [(presentation.CLEAR_PROFILE, discord.ButtonStyle.danger)],
        )

        confirm_interaction = FakeInteraction()
        await confirmation.children[0].callback(confirm_interaction)

        self.assertEqual(
            store.save_calls,
            [
                {
                    "guild_id": 100,
                    "user_id": 10,
                    "github_username": None,
                    "category_ids": (),
                    "automatic_pings": False,
                    "updated_at": datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
                }
            ],
        )
        self.assertEqual(confirm_interaction.response.defer_calls, 1)
        self.assertEqual(confirm_interaction.response.messages, [])
        self.assertEqual(confirm_interaction.deleted_original_responses, 1)

    async def test_browse_categories_reports_empty_catalog_exactly(self):
        view, _store, _actor = self.make_dashboard()
        interaction = FakeInteraction()

        await view.children[1].callback(interaction)

        self.assertEqual(interaction.response.messages[0][0], presentation.NO_CATEGORIES_CONFIGURED)
        self.assertTrue(interaction.response.messages[0][1]["ephemeral"])
        self.assertFalse(interaction.response.messages[0][1]["allowed_mentions"].users)

    async def test_category_browser_filters_cached_participants_before_pagination(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        category = models.Category(1, 100, "rendering", now)
        store.categories = [category]
        profile_ids = [20, *range(1, 6), 21, *range(6, 13)]
        store.category_profiles[(100, 1)] = [
            models.Profile(
                100,
                user_id,
                f"github-{user_id}" if user_id % 2 == 0 else None,
                False,
                (1,),
                now,
            )
            for user_id in profile_ids
        ]
        members = [
            types.SimpleNamespace(
                id=user_id,
                name=f"user_{user_id}",
                is_participant=user_id not in (12, 20),
                can_manage_messages=user_id == 12,
            )
            for user_id in [*range(1, 13), 20]
        ]
        guild = FakeGuild(members)
        dashboard, _store, _actor = self.make_dashboard(
            store,
            guild=guild,
            member_actor_factory=lambda member: coordinator.TicketActor(
                member.id,
                member.is_participant,
                member.can_manage_messages,
            ),
        )
        open_interaction = FakeInteraction()

        await dashboard.children[1].callback(open_interaction)

        self.assertEqual(len(open_interaction.response.edits), 1)
        initial = open_interaction.response.edits[0]
        self.assertEqual(initial["content"], presentation.BROWSE_CATEGORIES)
        browser = initial["view"]
        self.assertEqual(browser.children[0].placeholder, presentation.SELECT_A_CATEGORY)
        self.assertEqual(
            [(button.label, button.style) for button in browser.children[1:]],
            [
                (presentation.PREVIOUS, discord.ButtonStyle.secondary),
                (presentation.NEXT, discord.ButtonStyle.secondary),
                (presentation.BACK, discord.ButtonStyle.secondary),
            ],
        )

        browser.children[0].values = ["1"]
        select_interaction = FakeInteraction()
        await browser.children[0].callback(select_interaction)
        first_page = select_interaction.response.edits[0]
        expected_first_users = [
            f"<@{user_id}> | user\\_{user_id}"
            + (f" | github-{user_id}" if user_id % 2 == 0 else "")
            for user_id in range(1, 11)
        ]
        self.assertEqual(
            first_page["content"],
            presentation.category_page(
                category="rendering",
                users=expected_first_users,
                page=1,
                page_count=2,
            ),
        )
        self.assertFalse(first_page["allowed_mentions"].users)
        self.assertTrue(browser.children[1].disabled)
        self.assertFalse(browser.children[2].disabled)

        next_interaction = FakeInteraction()
        await browser.children[2].callback(next_interaction)
        self.assertEqual(
            next_interaction.response.edits[0]["content"],
            presentation.category_page(
                category="rendering",
                users=["<@11> | user\\_11", "<@12> | user\\_12 | github-12"],
                page=2,
                page_count=2,
            ),
        )

        back_interaction = FakeInteraction()
        await browser.children[3].callback(back_interaction)
        back = back_interaction.response.edits[0]
        self.assertEqual(back["content"], presentation.DEVELOPER_PROFILE_COMMAND)
        self.assertIs(back["view"], dashboard)
        self.assertFalse(back["allowed_mentions"].users)

    async def test_developer_profile_helper_uses_exact_non_pinging_variants(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "performance", now),
        ]

        missing_interaction = FakeInteraction()
        await dashboard_module.send_developer_profile(
            missing_interaction,
            store,
            guild_id=100,
            user_id=41,
        )
        self.assertEqual(missing_interaction.response.messages[0][0], presentation.NO_PROFILE)
        self.assertFalse(missing_interaction.response.messages[0][1]["allowed_mentions"].users)

        store.profiles[(100, 42)] = models.Profile(
            100,
            42,
            "nova-dev",
            True,
            (1, 2),
            now,
        )
        profile_interaction = FakeInteraction()
        await dashboard_module.send_developer_profile(
            profile_interaction,
            store,
            guild_id=100,
            user_id=42,
        )
        self.assertEqual(
            profile_interaction.response.messages[0][0],
            presentation.developer_profile(
                mention="<@42>",
                has_profile=True,
                github_username="nova-dev",
                categories=("rendering", "performance"),
            ),
        )
        self.assertTrue(profile_interaction.response.messages[0][1]["ephemeral"])
        self.assertFalse(profile_interaction.response.messages[0][1]["allowed_mentions"].users)

    async def test_new_ticket_modal_has_link_and_existing_routing_components(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.categories = [
            models.Category(category_id, 100, f"category-{category_id}", now)
            for category_id in range(1, 27)
        ]
        modal = await self.open_ticket_modal(store)
        self.assertEqual(modal.title, presentation.NEW_TICKET)
        self.assertEqual(
            [label.text for label in modal.children],
            [
                presentation.PR_LINK,
                presentation.CATEGORIES,
                presentation.PING_BEHAVIOR,
                presentation.DIRECT_REVIEWER,
            ],
        )
        ping_label = next(
            label for label in modal.children if label.text == presentation.PING_BEHAVIOR
        )
        self.assertEqual(ping_label.description, presentation.SELECT_PING_BEHAVIOR)
        self.assertEqual(
            [label.description for label in modal.children],
            [
                None,
                None,
                presentation.SELECT_PING_BEHAVIOR,
                "Ignored unless a direct ping option is selected",
            ],
        )
        self.assertIsInstance(modal.pr_link, discord.ui.TextInput)
        self.assertTrue(modal.pr_link.required)
        self.assertEqual(modal.pr_link.placeholder, presentation.ENTER_PR_LINK)
        self.assertEqual(modal.pr_link.max_length, presentation.MAX_PR_URL_LENGTH)
        self.assertIsInstance(modal.categories, discord.ui.Select)
        self.assertFalse(modal.categories.required)
        self.assertEqual(modal.categories.min_values, 0)
        self.assertEqual(modal.categories.max_values, 25)
        self.assertEqual(len(modal.categories.options), 25)
        self.assertIsInstance(modal.ping_behavior, discord.ui.RadioGroup)
        self.assertTrue(modal.ping_behavior.required)
        self.assertEqual(
            [(option.label, option.value, option.description) for option in modal.ping_behavior.options],
            [
                (presentation.NO_PING, models.RoutingMode.NONE.value, None),
                (presentation.AUTOMATIC, models.RoutingMode.AUTOMATIC.value, None),
                (presentation.DIRECT_THEN_WAIT, models.RoutingMode.DIRECT_WAIT.value, None),
                (
                    presentation.DIRECT_THEN_AUTOMATIC,
                    models.RoutingMode.DIRECT_AUTOMATIC.value,
                    None,
                ),
            ],
        )
        self.assertIsInstance(modal.direct_reviewer, discord.ui.UserSelect)
        self.assertFalse(modal.direct_reviewer.required)
        self.assertEqual(modal.direct_reviewer.min_values, 0)
        self.assertEqual(modal.direct_reviewer.max_values, 1)

    async def test_native_valid_ticket_input_always_fits_one_discord_message(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.categories = [
            models.Category(category_id, 100, "x" * 100, now)
            for category_id in range(1, 26)
        ]
        modal = await self.open_ticket_modal(store)
        selected = [option.label for option in modal.categories.options[: modal.categories.max_values]]
        content = presentation.ticket_message(
            title="x" * presentation.MAX_PR_TITLE_LENGTH,
            url="x" * modal.pr_link.max_length,
            author_mention="<@18446744073709551615>",
            categories=selected,
            reviewer_mention="<@18446744073709551615>",
            reviewer_github="x" * presentation.MAX_GITHUB_USERNAME_LENGTH,
        )

        self.assertLessEqual(len(content), presentation.DISCORD_MESSAGE_LIMIT)
        self.assertLess(modal.categories.max_values, len(modal.categories.options))
        self.assertGreater(
            len(
                presentation.ticket_message(
                    title="x" * presentation.MAX_PR_TITLE_LENGTH,
                    url="x" * modal.pr_link.max_length,
                    author_mention="<@18446744073709551615>",
                    categories=[*selected, "x" * 100],
                    reviewer_mention="<@18446744073709551615>",
                    reviewer_github="x" * presentation.MAX_GITHUB_USERNAME_LENGTH,
                )
            ),
            presentation.DISCORD_MESSAGE_LIMIT,
        )

    async def test_new_ticket_fetches_canonical_link_once_and_preserves_routing(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        cases = (
            (models.RoutingMode.NONE, (), (), "", None),
            (models.RoutingMode.AUTOMATIC, ("1",), (), "rendering", None),
            (models.RoutingMode.DIRECT_WAIT, (), (types.SimpleNamespace(id=99),), "", 99),
            (
                models.RoutingMode.DIRECT_AUTOMATIC,
                ("1",),
                (types.SimpleNamespace(id=99),),
                "rendering",
                99,
            ),
        )

        for mode, category_values, reviewer_values, display, target_id in cases:
            with self.subTest(mode=mode):
                store = FakeStore()
                store.categories = [models.Category(1, 100, "rendering", now)]
                actor = coordinator.TicketActor(10, True, False)
                create_calls = []
                fetch_calls = []

                async def create_ticket(
                    request,
                    selected_actor,
                    pull_request,
                    calls=create_calls,
                ):
                    calls.append((request, selected_actor, pull_request))
                    return coordinator.TicketResult(True)

                async def fetch_pull_request(
                    owner,
                    repository,
                    number,
                    calls=fetch_calls,
                ):
                    calls.append((owner, repository, number))
                    return types.SimpleNamespace(
                        pull_request_id=700,
                        number=42,
                        repository_id=100,
                        repository_full_name="NewHorizons/NHCogs",
                        title="Fetched pull request",
                        url="https://github.com/NewHorizons/NHCogs/pull/42",
                        state="open",
                        draft=False,
                        merged=False,
                        author_id=900,
                        author_login="author",
                        updated_at=now,
                        labels=("bug",),
                        assignees=("reviewer",),
                    )

                modal = await self.open_ticket_modal(
                    store,
                    actor,
                    create_ticket,
                    fetch_pull_request=fetch_pull_request,
                )
                modal.pr_link.value = (
                    "https://github.com/NewHorizons/NHCogs/pull/42"
                )
                modal.categories.values = list(category_values)
                modal.ping_behavior.value = mode.value
                modal.direct_reviewer.values = list(reviewer_values)
                submit_interaction = FakeInteraction()

                await modal.on_submit(submit_interaction)

                request, selected_actor, pull_request = create_calls[0]
                self.assertEqual(selected_actor, actor)
                self.assertEqual(
                    fetch_calls,
                    [("NewHorizons", "NHCogs", 42)],
                )
                self.assertEqual(
                    request,
                    coordinator.TicketRequest(
                        guild_id=100,
                        pr_title="Fetched pull request",
                        pr_url="https://github.com/NewHorizons/NHCogs/pull/42",
                        category_display=display,
                        routing_mode=mode,
                        direct_target_id=target_id,
                        category_ids=tuple(int(value) for value in category_values),
                    ),
                )
                self.assertEqual(
                    pull_request,
                    models.GitHubPullRequest(
                        repository_id=100,
                        pr_number=42,
                        github_pr_id=700,
                        github_author_id=900,
                        repository_full_name="NewHorizons/NHCogs",
                        url="https://github.com/NewHorizons/NHCogs/pull/42",
                        title="Fetched pull request",
                        github_author_login="author",
                        draft=False,
                        open=True,
                        labels=("bug",),
                        github_updated_at=now,
                        assignees=("reviewer",),
                    ),
                )
                self.assertEqual(submit_interaction.response.defer_calls, 1)
                self.assertEqual(
                    submit_interaction.response.defer_kwargs,
                    [{"ephemeral": True}],
                )
                self.assertEqual(submit_interaction.response.messages, [])

    async def test_new_ticket_rejects_invalid_links_and_ineligible_pull_requests(self):
        invalid_links = (
            "http://github.com/NewHorizons/NHCogs/pull/42",
            "https://www.github.com/NewHorizons/NHCogs/pull/42",
            "https://github.com/NewHorizons/NHCogs/issues/42",
            "https://github.com/NewHorizons/NHCogs/pull/42/",
            "https://github.com/NewHorizons/NHCogs/pull/42?diff=split",
        )
        for link in invalid_links:
            with self.subTest(link=link):
                fetch_pull_request = mock.AsyncMock()
                create_ticket = mock.AsyncMock()
                modal = await self.open_ticket_modal(
                    create_ticket=create_ticket,
                    fetch_pull_request=fetch_pull_request,
                )
                modal.pr_link.value = link
                modal.ping_behavior.value = models.RoutingMode.NONE.value
                interaction = FakeInteraction()

                await modal.on_submit(interaction)

                self.assertEqual(
                    interaction.response.messages[0][0],
                    presentation.COULD_NOT_CREATE_TICKET,
                )
                fetch_pull_request.assert_not_awaited()
                create_ticket.assert_not_awaited()

        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        cases = (
            {
                "repository_full_name": "OtherOrganization/NHCogs",
            },
            {"state": "closed"},
            {"draft": True},
        )
        for overrides in cases:
            with self.subTest(snapshot=overrides):
                values = {
                    "pull_request_id": 700,
                    "number": 42,
                    "repository_id": 100,
                    "repository_full_name": "NewHorizons/NHCogs",
                    "title": "Fetched pull request",
                    "url": "https://github.com/NewHorizons/NHCogs/pull/42",
                    "state": "open",
                    "draft": False,
                    "merged": False,
                    "author_id": 900,
                    "author_login": "author",
                    "updated_at": now,
                    "labels": (),
                    "assignees": (),
                }
                values.update(overrides)
                fetch_pull_request = mock.AsyncMock(
                    return_value=types.SimpleNamespace(**values)
                )
                create_ticket = mock.AsyncMock()
                modal = await self.open_ticket_modal(
                    create_ticket=create_ticket,
                    fetch_pull_request=fetch_pull_request,
                )
                modal.ping_behavior.value = models.RoutingMode.NONE.value
                interaction = FakeInteraction()

                await modal.on_submit(interaction)

                fetch_pull_request.assert_awaited_once_with(
                    "NewHorizons",
                    "NHCogs",
                    42,
                )
                self.assertEqual(
                    interaction.followup.send.await_args.args[0],
                    presentation.COULD_NOT_CREATE_TICKET,
                )
                self.assertNotIn("OtherOrganization", str(interaction.followup.send.await_args))
                create_ticket.assert_not_awaited()

        fetch_pull_request = mock.AsyncMock(side_effect=RuntimeError("private failure"))
        create_ticket = mock.AsyncMock()
        modal = await self.open_ticket_modal(
            create_ticket=create_ticket,
            fetch_pull_request=fetch_pull_request,
        )
        modal.ping_behavior.value = models.RoutingMode.NONE.value
        interaction = FakeInteraction()

        await modal.on_submit(interaction)

        self.assertEqual(
            interaction.followup.send.await_args.args[0],
            presentation.COULD_NOT_CREATE_TICKET,
        )
        self.assertNotIn("private failure", str(interaction.followup.send.await_args))
        create_ticket.assert_not_awaited()

    async def test_new_ticket_rejects_foreign_organization_before_fetch(self):
        fetch_pull_request = mock.AsyncMock()
        create_ticket = mock.AsyncMock()
        modal = await self.open_ticket_modal(
            create_ticket=create_ticket,
            fetch_pull_request=fetch_pull_request,
            expected_organization="NewHorizons",
        )
        modal.pr_link.value = "https://github.com/OtherOrganization/NHCogs/pull/42"
        modal.ping_behavior.value = models.RoutingMode.NONE.value
        interaction = FakeInteraction()

        await modal.on_submit(interaction)

        self.assertEqual(
            interaction.response.messages[0][0],
            presentation.COULD_NOT_CREATE_TICKET,
        )
        fetch_pull_request.assert_not_awaited()
        create_ticket.assert_not_awaited()

    async def test_multiple_automatic_categories_open_confirmation_before_create(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "mixins", now),
        ]
        actor = coordinator.TicketActor(10, True, False)
        create_calls = []
        count_calls = []

        async def create_ticket(request, selected_actor, _pull_request):
            create_calls.append((request, selected_actor))
            return coordinator.TicketResult(True)

        async def count_candidates(guild_id, category_ids, excluded_user_ids):
            count_calls.append((guild_id, category_ids, excluded_user_ids))
            return 4

        modal = await self.open_ticket_modal(
            store,
            actor,
            create_ticket,
            count_candidates=count_candidates,
        )
        modal.categories.values = ["1", "2"]
        modal.ping_behavior.value = models.RoutingMode.AUTOMATIC.value
        submit_interaction = FakeInteraction()

        await modal.on_submit(submit_interaction)

        self.assertEqual(create_calls, [])
        self.assertEqual(count_calls, [(100, (1, 2), frozenset({10}))])
        content = submit_interaction.followup.send.await_args.args[0]
        kwargs = submit_interaction.followup.send.await_args.kwargs
        self.assertEqual(
            content,
            "Confirm categories\n4 people can receive automatic pings for all selected categories",
        )
        self.assertTrue(kwargs["ephemeral"])
        self.assertFalse(kwargs["allowed_mentions"].users)
        view = kwargs["view"]
        self.assertEqual(view.categories.min_values, 1)
        self.assertEqual(view.categories.max_values, 2)
        self.assertEqual(
            [(option.label, option.value, option.default) for option in view.categories.options],
            [
                ("rendering", "1", True),
                ("mixins", "2", True),
            ],
        )
        self.assertEqual(
            [item.label for item in view.children[1:]],
            [presentation.BACK, presentation.CREATE_TICKET],
        )

    async def test_confirmation_updates_count_creates_filtered_ticket_and_prefills_back(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "mixins", now),
        ]
        actor = coordinator.TicketActor(10, True, False)
        create_calls = []

        async def create_ticket(request, selected_actor, _pull_request):
            create_calls.append((request, selected_actor))
            return coordinator.TicketResult(True)

        async def count_candidates(_guild_id, category_ids, _excluded_user_ids):
            return len(category_ids)

        modal = await self.open_ticket_modal(
            store,
            actor,
            create_ticket,
            count_candidates=count_candidates,
        )
        modal.categories.values = ["1", "2"]
        modal.ping_behavior.value = models.RoutingMode.DIRECT_AUTOMATIC.value
        modal.direct_reviewer.values = [types.SimpleNamespace(id=99)]
        submit_interaction = FakeInteraction()
        await modal.on_submit(submit_interaction)
        view = submit_interaction.followup.send.await_args.kwargs["view"]

        view.categories.values = ["2"]
        select_interaction = FakeInteraction()
        await view.categories.callback(select_interaction)
        self.assertEqual(
            select_interaction.response.edits[0]["content"],
            "Confirm categories\n1 person can receive automatic pings for all selected categories",
        )
        self.assertEqual(
            [option.default for option in view.categories.options],
            [False, True],
        )

        back_button = next(
            item
            for item in view.children
            if getattr(item, "label", None) == presentation.BACK
        )
        back_interaction = FakeInteraction()
        await back_button.callback(back_interaction)
        reopened = back_interaction.response.modals[0]
        self.assertEqual(
            reopened.pr_link.default,
            "https://github.com/NewHorizons/NHCogs/pull/42",
        )
        self.assertEqual(
            [option.default for option in reopened.categories.options],
            [False, True],
        )
        self.assertEqual(
            [option.default for option in reopened.ping_behavior.options],
            [False, False, False, True],
        )
        self.assertEqual(reopened.direct_reviewer.default_values[0].id, 99)

        create_button = next(
            item
            for item in view.children
            if getattr(item, "label", None) == presentation.CREATE_TICKET
        )
        create_interaction = FakeInteraction()
        await create_button.callback(create_interaction)
        self.assertEqual(create_interaction.response.defer_calls, 1)
        self.assertEqual(create_calls, [])

    async def test_confirmation_create_uses_defaults_and_prevents_double_submit(self):
        class YieldingStore(FakeStore):
            async def list_categories(self, guild_id):
                await asyncio.sleep(0)
                return await super().list_categories(guild_id)

        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = YieldingStore()
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "mixins", now),
        ]
        actor = coordinator.TicketActor(10, True, False)
        create_calls = []

        async def create_ticket(request, selected_actor, _pull_request):
            await asyncio.sleep(0)
            create_calls.append((request, selected_actor))
            return coordinator.TicketResult(True)

        modal = await self.open_ticket_modal(store, actor, create_ticket)
        modal.categories.values = ["1", "2"]
        modal.ping_behavior.value = models.RoutingMode.AUTOMATIC.value
        submit_interaction = FakeInteraction()
        await modal.on_submit(submit_interaction)
        view = submit_interaction.followup.send.await_args.kwargs["view"]
        create_button = next(
            item
            for item in view.children
            if getattr(item, "label", None) == presentation.CREATE_TICKET
        )
        first = FakeInteraction()
        second = FakeInteraction()

        await asyncio.gather(
            create_button.callback(first),
            create_button.callback(second),
        )

        self.assertEqual(len(create_calls), 1)
        request, selected_actor = create_calls[0]
        self.assertEqual(selected_actor, actor)
        self.assertEqual(request.category_ids, (1, 2))
        self.assertEqual(request.category_display, "rendering, mixins")

    async def test_confirmation_selection_creates_only_the_visible_defaults(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "mixins", now),
        ]
        create_calls = []

        async def create_ticket(request, actor, _pull_request):
            create_calls.append((request, actor))
            return coordinator.TicketResult(True)

        modal = await self.open_ticket_modal(store, create_ticket=create_ticket)
        modal.categories.values = ["1", "2"]
        modal.ping_behavior.value = models.RoutingMode.AUTOMATIC.value
        submit_interaction = FakeInteraction()
        await modal.on_submit(submit_interaction)
        view = submit_interaction.followup.send.await_args.kwargs["view"]
        view.categories.values = ["2"]
        await view.categories.callback(FakeInteraction())
        create_button = next(
            item
            for item in view.children
            if getattr(item, "label", None) == presentation.CREATE_TICKET
        )

        await create_button.callback(FakeInteraction())

        self.assertEqual(create_calls[0][0].category_ids, (2,))
        self.assertEqual(create_calls[0][0].category_display, "mixins")

    async def test_direct_automatic_count_excludes_author_and_direct_target(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "mixins", now),
        ]
        count_candidates = mock.AsyncMock(return_value=1)
        modal = await self.open_ticket_modal(
            store,
            count_candidates=count_candidates,
        )
        modal.categories.values = ["1", "2"]
        modal.ping_behavior.value = models.RoutingMode.DIRECT_AUTOMATIC.value
        modal.direct_reviewer.values = [types.SimpleNamespace(id=99)]

        await modal.on_submit(FakeInteraction())

        count_candidates.assert_awaited_once_with(
            100,
            (1, 2),
            frozenset({10, 99}),
        )

    async def test_confirmation_revalidates_categories_before_refreshing_count(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "mixins", now),
        ]
        count_candidates = mock.AsyncMock(return_value=2)
        modal = await self.open_ticket_modal(
            store,
            count_candidates=count_candidates,
        )
        modal.categories.values = ["1", "2"]
        modal.ping_behavior.value = models.RoutingMode.AUTOMATIC.value
        submit_interaction = FakeInteraction()
        await modal.on_submit(submit_interaction)
        view = submit_interaction.followup.send.await_args.kwargs["view"]
        store.categories = [store.categories[0]]
        view.categories.values = ["1", "2"]
        interaction = FakeInteraction()

        await view.categories.callback(interaction)

        self.assertEqual(
            interaction.response.messages[0][0],
            presentation.CATEGORY_NO_LONGER_EXISTS,
        )
        self.assertEqual(count_candidates.await_count, 1)

    async def test_confirmation_reports_when_no_one_matches_all_categories(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "mixins", now),
        ]
        modal = await self.open_ticket_modal(store)
        modal.categories.values = ["1", "2"]
        modal.ping_behavior.value = models.RoutingMode.AUTOMATIC.value
        submit_interaction = FakeInteraction()

        await modal.on_submit(submit_interaction)

        self.assertEqual(
            submit_interaction.followup.send.await_args.args[0],
            "Confirm categories\nNo one can receive automatic pings for all selected categories",
        )

    async def test_direct_self_review_is_rejected_before_category_confirmation(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [
            models.Category(1, 100, "rendering", now),
            models.Category(2, 100, "mixins", now),
        ]
        create_ticket = mock.AsyncMock()
        count_candidates = mock.AsyncMock(return_value=4)
        modal = await self.open_ticket_modal(
            store,
            coordinator.TicketActor(10, True, False),
            create_ticket,
            count_candidates=count_candidates,
        )
        modal.categories.values = ["1", "2"]
        modal.ping_behavior.value = models.RoutingMode.DIRECT_AUTOMATIC.value
        modal.direct_reviewer.values = [types.SimpleNamespace(id=10)]
        interaction = FakeInteraction()

        await modal.on_submit(interaction)

        self.assertEqual(
            interaction.response.messages[0][0],
            coordinator.SELF_REVIEW_DENIED,
        )
        create_ticket.assert_not_awaited()
        count_candidates.assert_not_awaited()

    async def test_direct_reviewer_is_ignored_for_non_direct_routing(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [models.Category(1, 100, "rendering", now)]
        create_calls = []

        async def create_ticket(request, actor, _pull_request):
            create_calls.append((request, actor))
            return coordinator.TicketResult(True)

        modal = await self.open_ticket_modal(store, create_ticket=create_ticket)
        modal.categories.values = ["1"]
        modal.ping_behavior.value = models.RoutingMode.AUTOMATIC.value
        modal.direct_reviewer.values = [types.SimpleNamespace(id=10)]
        interaction = FakeInteraction()

        await modal.on_submit(interaction)

        self.assertEqual(interaction.response.messages, [])
        self.assertEqual(create_calls[0][0].direct_target_id, None)

    async def test_new_ticket_returns_only_accepted_validation_and_coordinator_errors(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [models.Category(1, 100, "rendering", now)]
        actor = coordinator.TicketActor(10, True, False)
        create_calls = []

        async def create_ticket(request, selected_actor, _pull_request):
            create_calls.append((request, selected_actor))
            return coordinator.TicketResult(False, "coordinator error")

        async def modal():
            return await self.open_ticket_modal(store, actor, create_ticket)

        automatic = await modal()
        automatic.ping_behavior.value = models.RoutingMode.AUTOMATIC.value
        automatic_interaction = FakeInteraction()
        await automatic.on_submit(automatic_interaction)
        self.assertEqual(
            automatic_interaction.response.messages[0][0],
            presentation.AUTOMATIC_REQUIRES_CATEGORY,
        )

        direct = await modal()
        direct.ping_behavior.value = models.RoutingMode.DIRECT_WAIT.value
        direct_interaction = FakeInteraction()
        await direct.on_submit(direct_interaction)
        self.assertEqual(
            direct_interaction.response.messages[0][0],
            presentation.DIRECT_REQUIRES_REVIEWER,
        )

        stale = await modal()
        stale.ping_behavior.value = models.RoutingMode.AUTOMATIC.value
        stale.categories.values = ["1"]
        store.categories = []
        stale_interaction = FakeInteraction()
        await stale.on_submit(stale_interaction)
        self.assertEqual(
            stale_interaction.response.messages[0][0],
            presentation.CATEGORY_NO_LONGER_EXISTS,
        )

        store.categories = [models.Category(1, 100, "rendering", now)]
        coordinator_error = await modal()
        coordinator_error.ping_behavior.value = models.RoutingMode.NONE.value
        error_interaction = FakeInteraction()
        await coordinator_error.on_submit(error_interaction)
        self.assertEqual(error_interaction.response.messages, [])
        self.assertEqual(error_interaction.response.defer_calls, 1)
        error_interaction.followup.send.assert_awaited_once_with(
            "coordinator error",
            ephemeral=True,
        )
        self.assertEqual(len(create_calls), 1)
