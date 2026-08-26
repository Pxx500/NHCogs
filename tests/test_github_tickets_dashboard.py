from __future__ import annotations

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
    discord.SelectOption = SelectOption
    discord.RadioGroupOption = RadioGroupOption
    discord.Interaction = type("Interaction", (), {})
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

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def defer(self):
        self.defer_calls += 1


class FakeInteraction:
    def __init__(self):
        self.response = FakeResponse()
        self.followup = types.SimpleNamespace(send=mock.AsyncMock())
        self.deleted_original_responses = 0

    async def delete_original_response(self):
        self.deleted_original_responses += 1


class FakeStore:
    def __init__(self):
        self.categories = []
        self.profiles = {}
        self.category_profiles = {}
        self.save_calls = []

    async def list_categories(self, _guild_id):
        return tuple(self.categories)

    async def get_profile(self, guild_id, user_id):
        return self.profiles.get((guild_id, user_id))

    async def save_profile(self, **kwargs):
        self.save_calls.append(kwargs)

    async def list_profiles_for_category(self, guild_id, category_id):
        return tuple(self.category_profiles.get((guild_id, category_id), ()))


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def make_dashboard(self, store=None):
        store = store or FakeStore()
        actor = coordinator.TicketActor(10, True, False)

        async def create_ticket(_request, _actor):
            return coordinator.TicketResult(True)

        view = dashboard_module.GitHubTicketsDashboard(
            store,
            guild_id=100,
            create_ticket=create_ticket,
            actor_factory=lambda _interaction: actor,
            clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
        )
        return view, store, actor

    async def test_dashboard_is_exact_ephemeral_four_button_interface(self):
        view, _store, _actor = self.make_dashboard()
        interaction = FakeInteraction()

        await view.send(interaction)

        self.assertEqual(
            [(button.label, button.style) for button in view.children],
            [
                (presentation.NEW_TICKET, discord.ButtonStyle.primary),
                (presentation.EDIT_PROFILE, discord.ButtonStyle.secondary),
                (presentation.BROWSE_CATEGORIES, discord.ButtonStyle.secondary),
                (presentation.CLEAR_PROFILE, discord.ButtonStyle.danger),
            ],
        )
        self.assertEqual(len(interaction.response.messages), 1)
        content, kwargs = interaction.response.messages[0]
        self.assertEqual(content, presentation.DASHBOARD_TITLE)
        self.assertIs(kwargs["view"], view)
        self.assertTrue(kwargs["ephemeral"])
        allowed_mentions = kwargs["allowed_mentions"]
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)

    async def test_dashboard_descendants_recheck_participant_access_after_role_loss(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.categories = [models.Category(1, 100, "rendering", now)]
        current_actor = [coordinator.TicketActor(10, True, False)]

        async def create_ticket(_request, _actor):
            return coordinator.TicketResult(True)

        dashboard = dashboard_module.GitHubTicketsDashboard(
            store,
            guild_id=100,
            create_ticket=create_ticket,
            actor_factory=lambda _interaction: current_actor[0],
        )

        profile_open = FakeInteraction()
        await dashboard.children[1].callback(profile_open)
        profile_modal = profile_open.response.modals[0]

        clear_open = FakeInteraction()
        await dashboard.children[3].callback(clear_open)
        clear_confirmation = clear_open.response.messages[0][1]["view"]

        browse_open = FakeInteraction()
        await dashboard.children[2].callback(browse_open)
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

        await view.children[1].callback(open_interaction)

        self.assertEqual(len(open_interaction.response.modals), 1)
        modal = open_interaction.response.modals[0]
        self.assertEqual(modal.title, presentation.EDIT_PROFILE)
        self.assertEqual(
            [label.text for label in modal.children],
            [
                presentation.GITHUB_USERNAME,
                presentation.CATEGORIES,
                presentation.ALLOW_AUTOMATIC_PINGS,
            ],
        )
        self.assertEqual(
            [label.description for label in modal.children],
            [presentation.GITHUB_USERNAME_DESCRIPTION, None, None],
        )
        self.assertIsInstance(modal.github_username, discord.ui.TextInput)
        self.assertFalse(modal.github_username.required)
        self.assertEqual(modal.github_username.default, "old-name")
        self.assertEqual(
            modal.github_username.max_length,
            presentation.MAX_GITHUB_USERNAME_LENGTH,
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

        modal.github_username.value = "  new-name  "
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
                    "github_username": "  new-name  ",
                    "category_ids": (2,),
                    "automatic_pings": True,
                    "updated_at": datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
                }
            ],
        )
        self.assertEqual(submit_interaction.response.defer_calls, 1)
        self.assertEqual(submit_interaction.response.messages, [])

    async def test_edit_profile_rejects_automatic_pings_without_categories(self):
        view, store, _actor = self.make_dashboard()
        open_interaction = FakeInteraction()
        await view.children[1].callback(open_interaction)
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
        await view.children[1].callback(open_interaction)
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
        await view.children[1].callback(profile_interaction)
        profile_select = profile_interaction.response.modals[0].categories

        ticket_interaction = FakeInteraction()
        await view.children[0].callback(ticket_interaction)
        ticket_select = ticket_interaction.response.modals[0].categories

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

        await view.children[2].callback(interaction)

        self.assertEqual(interaction.response.messages[0][0], presentation.NO_CATEGORIES_CONFIGURED)
        self.assertTrue(interaction.response.messages[0][1]["ephemeral"])
        self.assertFalse(interaction.response.messages[0][1]["allowed_mentions"].users)

    async def test_category_browser_pages_non_pinging_profiles_and_returns_to_dashboard(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        category = models.Category(1, 100, "rendering", now)
        store.categories = [category]
        store.category_profiles[(100, 1)] = [
            models.Profile(
                100,
                user_id,
                f"github-{user_id}" if user_id % 2 == 0 else None,
                False,
                (1,),
                now,
            )
            for user_id in range(1, 13)
        ]
        dashboard, _store, _actor = self.make_dashboard(store)
        open_interaction = FakeInteraction()

        await dashboard.children[2].callback(open_interaction)

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
            f"<@{user_id}>" + (f" | github-{user_id}" if user_id % 2 == 0 else "")
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
                users=["<@11>", "<@12> | github-12"],
                page=2,
                page_count=2,
            ),
        )

        back_interaction = FakeInteraction()
        await browser.children[3].callback(back_interaction)
        back = back_interaction.response.edits[0]
        self.assertEqual(back["content"], presentation.DASHBOARD_TITLE)
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

    async def test_new_ticket_modal_has_all_five_exact_components_at_once(self):
        store = FakeStore()
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store.categories = [
            models.Category(category_id, 100, f"category-{category_id}", now)
            for category_id in range(1, 27)
        ]
        view, _store, _actor = self.make_dashboard(store)
        interaction = FakeInteraction()

        await view.children[0].callback(interaction)

        modal = interaction.response.modals[0]
        self.assertEqual(modal.title, presentation.NEW_TICKET)
        self.assertEqual(
            [label.text for label in modal.children],
            [
                presentation.PR_TITLE,
                presentation.PR_LINK,
                presentation.CATEGORIES,
                presentation.PING_BEHAVIOR,
                presentation.DIRECT_REVIEWER,
            ],
        )
        self.assertEqual([label.description for label in modal.children], [None] * 5)
        self.assertIsInstance(modal.pr_title, discord.ui.TextInput)
        self.assertTrue(modal.pr_title.required)
        self.assertEqual(modal.pr_title.placeholder, presentation.ENTER_PR_TITLE)
        self.assertEqual(modal.pr_title.max_length, presentation.MAX_PR_TITLE_LENGTH)
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
        dashboard, _store, _actor = self.make_dashboard(store)
        interaction = FakeInteraction()

        await dashboard.children[0].callback(interaction)

        modal = interaction.response.modals[0]
        selected = [option.label for option in modal.categories.options[: modal.categories.max_values]]
        content = presentation.ticket_message(
            title="x" * modal.pr_title.max_length,
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
                    title="x" * modal.pr_title.max_length,
                    url="x" * modal.pr_link.max_length,
                    author_mention="<@18446744073709551615>",
                    categories=[*selected, "x" * 100],
                    reviewer_mention="<@18446744073709551615>",
                    reviewer_github="x" * presentation.MAX_GITHUB_USERNAME_LENGTH,
                )
            ),
            presentation.DISCORD_MESSAGE_LIMIT,
        )

    async def test_new_ticket_maps_all_routing_modes_and_only_trims_title_and_link(self):
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

                async def create_ticket(request, selected_actor, calls=create_calls):
                    calls.append((request, selected_actor))
                    return coordinator.TicketResult(True)

                dashboard = dashboard_module.GitHubTicketsDashboard(
                    store,
                    guild_id=100,
                    create_ticket=create_ticket,
                    actor_factory=lambda _interaction, selected_actor=actor: selected_actor,
                    clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
                )
                open_interaction = FakeInteraction()
                await dashboard.children[0].callback(open_interaction)
                modal = open_interaction.response.modals[0]
                modal.pr_title.value = "  title  "
                modal.pr_link.value = "  not validated  "
                modal.categories.values = list(category_values)
                modal.ping_behavior.value = mode.value
                modal.direct_reviewer.values = list(reviewer_values)
                submit_interaction = FakeInteraction()

                await modal.on_submit(submit_interaction)

                request, selected_actor = create_calls[0]
                self.assertEqual(selected_actor, actor)
                self.assertEqual(
                    request,
                    coordinator.TicketRequest(
                        guild_id=100,
                        pr_title="title",
                        pr_url="not validated",
                        category_display=display,
                        routing_mode=mode,
                        direct_target_id=target_id,
                        category_ids=tuple(int(value) for value in category_values),
                    ),
                )
                self.assertEqual(submit_interaction.response.defer_calls, 1)
                self.assertEqual(submit_interaction.response.messages, [])

    async def test_new_ticket_returns_only_accepted_validation_and_coordinator_errors(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
        store = FakeStore()
        store.categories = [models.Category(1, 100, "rendering", now)]
        actor = coordinator.TicketActor(10, True, False)
        create_calls = []

        async def create_ticket(request, selected_actor):
            create_calls.append((request, selected_actor))
            return coordinator.TicketResult(False, "coordinator error")

        async def modal():
            dashboard = dashboard_module.GitHubTicketsDashboard(
                store,
                guild_id=100,
                create_ticket=create_ticket,
                actor_factory=lambda _interaction: actor,
            )
            interaction = FakeInteraction()
            await dashboard.children[0].callback(interaction)
            return interaction.response.modals[0]

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
