from __future__ import annotations

import importlib
import sys
import types
import unittest
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
        success = "success"
        danger = "danger"
        secondary = "secondary"

    class View:
        def __init__(self, *, timeout=None):
            self.timeout = timeout
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class Button:
        def __init__(self, *, label, style, custom_id=None):
            self.label = label
            self.style = style
            self.custom_id = custom_id
            self.callback = None

    class Select:
        def __init__(
            self,
            *,
            placeholder,
            options,
            min_values,
            max_values,
            required,
        ):
            self.placeholder = placeholder
            self.options = options
            self.min_values = min_values
            self.max_values = max_values
            self.required = required
            self.values = []

    class SelectOption:
        def __init__(self, *, label, value):
            self.label = label
            self.value = value

    ui.View = View
    ui.Button = Button
    ui.Select = Select
    discord.ui = ui
    discord.ButtonStyle = ButtonStyle
    discord.SelectOption = SelectOption
    discord.Interaction = type("Interaction", (), {})
    sys.modules["discord"] = discord
    sys.modules["discord.ui"] = ui
    return discord


discord = _install_discord_stub()


def _load_ticket_views():
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

    return importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.ticket_views")


ticket_views = _load_ticket_views()


class FakeResponse:
    def __init__(self):
        self.defer_calls = 0
        self.messages = []
        self.message_kwargs = []
        self.edit_calls = []

    async def defer(self):
        self.defer_calls += 1

    async def send_message(self, content, *, ephemeral, **kwargs):
        self.messages.append((content, ephemeral))
        self.message_kwargs.append(kwargs)

    async def edit_message(self, **kwargs):
        self.edit_calls.append(kwargs)


class FakeInteraction:
    def __init__(self):
        self.response = FakeResponse()
        self.followup = types.SimpleNamespace(send=mock.AsyncMock())
        self.edit_original_response = mock.AsyncMock()


class TicketControlsTests(unittest.IsolatedAsyncioTestCase):
    def actions(self, result=None):
        calls = []
        action_result = result or types.SimpleNamespace(success=True, response=None)

        def action(name):
            async def callback(public_token, actor):
                calls.append((name, public_token, actor))
                if isinstance(action_result, Exception):
                    raise action_result
                return action_result

            return callback

        return calls, {
            "claim": action("claim"),
            "decline": action("decline"),
            "unassign": action("unassign"),
            "mark_finished": action("mark_finished"),
        }

    def view(self, *, claimed, result=None):
        actor = object()
        actor_interactions = []
        calls, actions = self.actions(result)

        def actor_factory(interaction):
            actor_interactions.append(interaction)
            return actor

        view = ticket_views.TicketControls(
            321,
            "opaque-ticket-token",
            claimed=claimed,
            actor_factory=actor_factory,
            **actions,
        )
        return view, actor, actor_interactions, calls

    def test_open_and_claimed_controls_have_exact_order_styles_and_stable_ids(self):
        open_view, *_ = self.view(claimed=False)
        claimed_view, *_ = self.view(claimed=True)

        self.assertIsNone(open_view.timeout)
        self.assertEqual(
            [(button.label, button.style, button.custom_id) for button in open_view.children],
            [
                ("Mark Finished", discord.ButtonStyle.success, "githubtickets:opaque-ticket-token:mark_finished"),
                ("Claim", discord.ButtonStyle.primary, "githubtickets:opaque-ticket-token:claim"),
                ("Decline", discord.ButtonStyle.danger, "githubtickets:opaque-ticket-token:decline"),
            ],
        )
        self.assertIsNone(claimed_view.timeout)
        self.assertEqual(
            [(button.label, button.style, button.custom_id) for button in claimed_view.children],
            [
                ("Mark Finished", discord.ButtonStyle.success, "githubtickets:opaque-ticket-token:mark_finished"),
                ("Unassign", discord.ButtonStyle.danger, "githubtickets:opaque-ticket-token:unassign"),
            ],
        )

    async def test_each_button_routes_ticket_and_cached_actor_then_stays_quiet_on_success(self):
        open_view, actor, open_actor_interactions, open_calls = self.view(claimed=False)
        claimed_view, claimed_actor, claimed_actor_interactions, claimed_calls = self.view(
            claimed=True
        )

        open_interactions = [FakeInteraction() for _button in open_view.children]
        for button, interaction in zip(open_view.children, open_interactions, strict=True):
            await button.callback(interaction)

        claimed_interactions = [FakeInteraction() for _button in claimed_view.children]
        for button, interaction in zip(claimed_view.children, claimed_interactions, strict=True):
            await button.callback(interaction)

        self.assertEqual(
            open_calls,
            [
                ("mark_finished", "opaque-ticket-token", actor),
                ("claim", "opaque-ticket-token", actor),
                ("decline", "opaque-ticket-token", actor),
            ],
        )
        self.assertEqual(
            claimed_calls,
            [
                ("mark_finished", "opaque-ticket-token", claimed_actor),
                ("unassign", "opaque-ticket-token", claimed_actor),
            ],
        )
        self.assertEqual(open_actor_interactions, open_interactions)
        self.assertEqual(claimed_actor_interactions, claimed_interactions)
        for interaction in (*open_interactions, *claimed_interactions):
            self.assertEqual(interaction.response.defer_calls, 1)
            self.assertEqual(interaction.response.messages, [])

    async def test_failed_action_returns_only_ticket_result_response_ephemerally(self):
        result = types.SimpleNamespace(success=False, response="This ticket is no longer active")
        view, _actor, actor_interactions, calls = self.view(claimed=False, result=result)
        interaction = FakeInteraction()

        await view.children[1].callback(interaction)

        self.assertEqual([name for name, _ticket_id, _actor in calls], ["claim"])
        self.assertEqual(actor_interactions, [interaction])
        self.assertEqual(interaction.response.defer_calls, 1)
        self.assertEqual(interaction.response.messages, [])
        interaction.followup.send.assert_awaited_once_with(
            "This ticket is no longer active",
            ephemeral=True,
        )

    async def test_unexpected_action_failure_is_logged_and_accepted_ephemerally(self):
        view, _actor, _actor_interactions, _calls = self.view(
            claimed=False,
            result=RuntimeError("controlled callback failure"),
        )
        interaction = FakeInteraction()

        with self.assertLogs(ticket_views.log, level="ERROR"):
            await view.children[1].callback(interaction)

        interaction.followup.send.assert_awaited_once_with(
            ticket_views.presentation.COULD_NOT_COMPLETE_ACTION,
            ephemeral=True,
        )


class GitHubLifecycleControlsTests(unittest.IsolatedAsyncioTestCase):
    def actor(self, *, participant=True, staff=False):
        return types.SimpleNamespace(
            can_participate=participant or staff,
            can_manage_messages=staff,
        )

    async def test_category_prompt_opens_selection_and_confirms_public_action(self):
        categories = (
            types.SimpleNamespace(category_id=1, name="rendering"),
            types.SimpleNamespace(category_id=2, name="mixins"),
        )
        actor = self.actor()
        category_calls = []
        action_calls = []

        async def get_categories(guild_id):
            category_calls.append(guild_id)
            return categories

        async def add_categories(public_token, category_ids, selected_actor):
            action_calls.append((public_token, category_ids, selected_actor))
            return types.SimpleNamespace(success=True, response=None)

        prompt = ticket_views.GitHubCategoryPrompt(
            10,
            "opaque-ticket-token",
            actor_factory=lambda _interaction: actor,
            get_categories=get_categories,
            add_categories=add_categories,
        )
        open_interaction = FakeInteraction()

        await prompt.children[0].callback(open_interaction)

        self.assertEqual(
            (
                prompt.children[0].label,
                prompt.children[0].custom_id,
            ),
            (
                ticket_views.presentation.ADD_CATEGORIES,
                "githubtickets:opaque-ticket-token:add_categories",
            ),
        )
        selection = open_interaction.response.message_kwargs[0]["view"]
        selection.categories.values = ["2"]
        confirm_interaction = FakeInteraction()

        await selection.children[1].callback(confirm_interaction)

        self.assertEqual(category_calls, [10, 10])
        self.assertEqual(
            action_calls,
            [("opaque-ticket-token", (2,), actor)],
        )
        confirm_interaction.edit_original_response.assert_awaited_once_with(
            content=ticket_views.presentation.CATEGORIES_ADDED,
            view=None,
        )

    async def test_category_prompt_rejects_non_participant_before_catalog_lookup(self):
        get_categories = mock.AsyncMock()
        prompt = ticket_views.GitHubCategoryPrompt(
            10,
            "opaque-ticket-token",
            actor_factory=lambda _interaction: self.actor(participant=False),
            get_categories=get_categories,
            add_categories=mock.AsyncMock(),
        )
        interaction = FakeInteraction()

        await prompt.children[0].callback(interaction)

        self.assertEqual(
            interaction.response.messages,
            [(ticket_views.presentation.CANNOT_USE_ACTION, True)],
        )
        get_categories.assert_not_awaited()

    async def test_draft_keep_retains_only_remove_and_remove_defers_cleanup(self):
        actor = self.actor(participant=False, staff=True)
        calls = []

        async def keep_ticket(public_token, selected_actor):
            calls.append(("keep", public_token, selected_actor))
            return types.SimpleNamespace(success=True, response=None)

        async def remove_ticket(public_token, selected_actor):
            calls.append(("remove", public_token, selected_actor))
            return types.SimpleNamespace(success=True, response=None)

        view = ticket_views.DraftTicketControls(
            "opaque-ticket-token",
            actor_factory=lambda _interaction: actor,
            keep_ticket=keep_ticket,
            remove_ticket=remove_ticket,
        )
        self.assertEqual(
            [(button.label, button.custom_id) for button in view.children],
            [
                (
                    ticket_views.presentation.KEEP_TICKET,
                    "githubtickets:opaque-ticket-token:keep_draft_ticket",
                ),
                (
                    ticket_views.presentation.REMOVE_TICKET,
                    "githubtickets:opaque-ticket-token:remove_draft_ticket",
                ),
            ],
        )
        keep_interaction = FakeInteraction()

        await view.children[0].callback(keep_interaction)

        retained = keep_interaction.response.edit_calls[0]["view"]
        self.assertEqual(
            [(button.label, button.custom_id) for button in retained.children],
            [
                (
                    ticket_views.presentation.REMOVE_TICKET,
                    "githubtickets:opaque-ticket-token:remove_draft_ticket",
                )
            ],
        )
        remove_interaction = FakeInteraction()
        await retained.children[0].callback(remove_interaction)

        self.assertEqual(
            calls,
            [
                ("keep", "opaque-ticket-token", actor),
                ("remove", "opaque-ticket-token", actor),
            ],
        )
        self.assertEqual(remove_interaction.response.defer_calls, 1)
