import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from tests.test_gatecount import nhmisc


def _load_achievement_views():
    class FakeButton:
        def __init__(self, **kwargs):
            self.disabled = False
            for key, value in kwargs.items():
                setattr(self, key, value)

    class FakeSelect(FakeButton):
        def __init__(self, **kwargs):
            self.values = []
            super().__init__(**kwargs)

    class FakeView:
        def __init__(self, **_kwargs):
            self.children = []
            for parent in reversed(type(self).__mro__):
                for name, method in parent.__dict__.items():
                    component = getattr(method, "__component__", None)
                    if component is None:
                        continue
                    kind, kwargs = component
                    item = (FakeButton if kind == "button" else FakeSelect)(
                        **kwargs
                    )
                    bound = method.__get__(self, type(self))

                    async def callback(
                        interaction,
                        *,
                        _bound=bound,
                        _item=item,
                    ):
                        await _bound(interaction, _item)

                    item.callback = callback
                    setattr(self, name, item)
                    self.children.append(item)

        def add_item(self, item):
            self.children.append(item)

        def remove_item(self, item):
            self.children.remove(item)

        def stop(self):
            pass

    def component(kind, **kwargs):
        def decorator(function):
            function.__component__ = (kind, kwargs)
            return function

        return decorator

    class FakeEmbed:
        def __init__(self, *, title=None, description=None, color=None):
            self.title = title
            self.description = description
            self.color = color
            self.fields = []

        def add_field(self, *, name, value, inline=True):
            self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))

    class FakeSelectOption(SimpleNamespace):
        pass

    discord = ModuleType("discord")
    discord.ui = SimpleNamespace(
        View=FakeView,
        Button=FakeButton,
        Select=FakeSelect,
        button=lambda **kwargs: component("button", **kwargs),
        select=lambda **kwargs: component("select", **kwargs),
    )
    discord.ButtonStyle = SimpleNamespace(
        danger="danger",
        green="green",
        primary="primary",
        secondary="secondary",
        success="success",
    )
    discord.Color = SimpleNamespace(
        blue=lambda: "blue",
        orange=lambda: "orange",
        red=lambda: "red",
    )
    discord.AllowedMentions = SimpleNamespace(none=lambda: "no-mentions")
    discord.SelectOption = FakeSelectOption
    discord.Embed = FakeEmbed
    discord.HTTPException = RuntimeError
    discord.Interaction = object
    discord.Member = object
    discord.Message = object
    discord.Role = object

    path = Path(__file__).resolve().parents[1] / "NHMisc" / "achievement_views.py"
    spec = importlib.util.spec_from_file_location("_gate_proof_views", path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"discord": discord}):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module, FakeSelect


class GateProofCandidateTests(unittest.TestCase):
    def test_author_and_mentions_keep_individual_missing_gate_ordinals(self):
        members = {
            10: SimpleNamespace(id=10, display_name="Author", bot=False),
            11: SimpleNamespace(id=11, display_name="Second", bot=False),
            12: SimpleNamespace(id=12, display_name="Bot", bot=True),
        }
        source_message = SimpleNamespace(
            webhook_id=None,
            author=members[10],
            raw_mentions=(11, 10, 12, 11),
            guild=SimpleNamespace(get_member=members.get),
        )

        candidates = nhmisc._build_gate_proof_candidates(
            source_message,
            {10: (2, 5), 11: (1,), 12: (3,)},
        )

        self.assertEqual(
            tuple(
                (candidate.member.id, candidate.missing_ordinals)
                for candidate in candidates
            ),
            ((10, (2, 5)), (11, (1,))),
        )


class GateProofViewTests(unittest.IsolatedAsyncioTestCase):
    def test_four_users_per_page_preserves_individual_default_and_selection(self):
        views, fake_select = _load_achievement_views()
        candidates = tuple(
            SimpleNamespace(
                member=SimpleNamespace(id=user_id, display_name=f"Player {user_id}"),
                missing_ordinals=(() if user_id == 11 else (1, 2)),
            )
            for user_id in range(10, 16)
        )
        view = views.GateProofView(
            SimpleNamespace(),
            SimpleNamespace(jump_url="https://discord.example/message"),
            99,
            candidates,
        )

        first_page_selects = [
            child for child in view.children if isinstance(child, fake_select)
        ]
        self.assertEqual(view.page_count, 2)
        self.assertEqual(len(first_page_selects), 4)
        self.assertTrue(first_page_selects[0].options[0].default)
        self.assertIn("Don't add proof", first_page_selects[0].options[0].label)
        self.assertTrue(first_page_selects[1].disabled)
        self.assertTrue(view.review.disabled)

        view.assignments[10] = 2
        view._configure_controls()
        view.page_index = 1
        view._configure_page()

        second_page_selects = [
            child for child in view.children if isinstance(child, fake_select)
        ]
        self.assertEqual(len(second_page_selects), 2)
        self.assertEqual(view.assignments[10], 2)
        self.assertFalse(view.review.disabled)

        view.reviewing = True
        view._configure_page()

        self.assertFalse(
            any(isinstance(child, fake_select) for child in view.children)
        )
        self.assertEqual(view.previous.label, "Back")
        self.assertEqual(view.review.label, "Attach proofs")

    async def test_selected_gate_remains_the_rendered_dropdown_default(self):
        views, fake_select = _load_achievement_views()
        candidate = SimpleNamespace(
            member=SimpleNamespace(id=10, display_name="Player"),
            missing_ordinals=(1, 2),
        )
        view = views.GateProofView(
            SimpleNamespace(),
            SimpleNamespace(jump_url="https://discord.example/message"),
            99,
            (candidate,),
        )
        select = next(
            child for child in view.children if isinstance(child, fake_select)
        )
        select.values = ["2"]
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock())
        )

        await select.callback(interaction)

        refreshed = next(
            child for child in view.children if isinstance(child, fake_select)
        )
        defaults = {
            option.value: option.default for option in refreshed.options
        }
        self.assertEqual(view.assignments[10], 2)
        self.assertEqual(defaults, {"none": False, "1": False, "2": True})


class GateProofEntryPointTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_requires_manage_messages_at_runtime(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=99),
            permissions=SimpleNamespace(manage_messages=False),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)

        await cog._add_gate_proof_context_action(
            interaction,
            SimpleNamespace(),
        )

        interaction.response.send_message.assert_awaited_once_with(
            "You need Manage Messages permission",
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()

    async def test_action_opens_private_review_for_every_message_candidate(self):
        members = {
            user_id: SimpleNamespace(
                id=user_id,
                display_name=f"Player {user_id}",
                bot=False,
            )
            for user_id in range(10, 17)
        }
        guild = SimpleNamespace(id=1, get_member=members.get)
        source_message = SimpleNamespace(
            id=30,
            channel=SimpleNamespace(id=20),
            guild=guild,
            webhook_id=None,
            author=members[10],
            raw_mentions=tuple(range(11, 17)),
        )
        response_message = SimpleNamespace()
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=99),
            permissions=SimpleNamespace(manage_messages=True),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            edit_original_response=mock.AsyncMock(),
            original_response=mock.AsyncMock(return_value=response_message),
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            missing_stargate_proofs=mock.AsyncMock(
                return_value={
                    user_id: (1, user_id - 8) for user_id in members
                }
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._log_achievement_interaction_start = mock.Mock()

        class FakeGateProofView:
            def __init__(self, cog, source_message, opener_id, candidates):
                self.cog = cog
                self.source_message = source_message
                self.opener_id = opener_id
                self.candidates = candidates
                self.message = None

            def render_embed(self):
                return "gate-proof-review"

        package = ModuleType("_gatecount_nhmisc")
        package.__path__ = []
        views = ModuleType("_gatecount_nhmisc.achievement_views")
        views.GateProofView = FakeGateProofView
        with mock.patch.dict(
            sys.modules,
            {
                "_gatecount_nhmisc": package,
                "_gatecount_nhmisc.achievement_views": views,
            },
        ):
            await cog._add_gate_proof_context_action(
                interaction,
                source_message,
            )

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        store.missing_stargate_proofs.assert_awaited_once_with(
            1,
            tuple(range(10, 17)),
        )
        kwargs = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(len(kwargs["view"].candidates), 7)
        self.assertEqual(kwargs["embed"], "gate-proof-review")
        self.assertEqual(kwargs["allowed_mentions"], "no-mentions")
        self.assertIs(kwargs["view"].message, response_message)


class _CommandTree:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_command(self, command, *, override=False):
        self.added.append((command.name, override))

    def remove_command(self, name, *, type):
        self.removed.append((name, type))


class GateProofRegistrationTests(unittest.TestCase):
    def test_message_action_registers_and_unregisters_with_achievement_commands(self):
        tree = _CommandTree()
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = SimpleNamespace(tree=tree)
        cog._gate_revoke_slash_command = SimpleNamespace(name="gaterevoke")
        cog._gate_revoke_user_context_menu = SimpleNamespace(
            name="Revoke latest Gate"
        )
        cog._achievements_slash_command = SimpleNamespace(name="achievements")
        cog._achievements_user_context_menu = SimpleNamespace(
            name="View achievements"
        )
        cog._grant_achievements_context_menu = SimpleNamespace(
            name="Grant achievements"
        )
        cog._add_gate_proof_context_menu = SimpleNamespace(name="Add Gate Proof")
        cog._achievement_commands_registered = False
        command_types = SimpleNamespace(
            chat_input="chat_input",
            user="user",
            message="message",
        )

        with mock.patch.object(
            nhmisc.discord,
            "AppCommandType",
            command_types,
            create=True,
        ):
            cog._register_achievement_commands()
            cog._unregister_achievement_commands()

        self.assertIn(("Add Gate Proof", True), tree.added)
        self.assertIn(("Add Gate Proof", "message"), tree.removed)


class GateProofConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_attaches_selected_proofs_and_logs_the_mapping(self):
        members = {
            10: SimpleNamespace(id=10, bot=False),
            11: SimpleNamespace(id=11, bot=False),
        }
        guild = SimpleNamespace(id=1, get_member=members.get)
        source_message = SimpleNamespace(
            id=30,
            channel=SimpleNamespace(id=20),
            guild=guild,
            webhook_id=None,
            author=members[10],
            raw_mentions=(11,),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        view = SimpleNamespace(
            source_message=source_message,
            candidate_ids=(10, 11),
            selected_assignments={10: 5, 11: 2},
            stop=mock.Mock(),
            render_embed=mock.Mock(),
        )
        store = SimpleNamespace(
            attach_stargate_proofs=mock.AsyncMock(return_value=(object(), object()))
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._fetch_gate_increment_source = mock.AsyncMock(
            return_value=source_message
        )
        cog._require_private_moderation_log_channel = mock.AsyncMock(
            return_value=SimpleNamespace(id=40)
        )
        cog._send_moderation_log = mock.AsyncMock(return_value=True)

        await cog._confirm_gate_proofs(interaction, view)

        interaction.response.defer.assert_awaited_once_with()
        store.attach_stargate_proofs.assert_awaited_once_with(
            1,
            {10: 5, 11: 2},
            source_channel_id=20,
            source_message_id=30,
        )
        cog._send_moderation_log.assert_awaited_once()
        _, log_message = cog._send_moderation_log.await_args.args
        self.assertIn("Moderator: <@99>", log_message)
        self.assertIn("<@10>: Gate 5", log_message)
        self.assertIn("<@11>: Gate 2", log_message)
        self.assertIn("https://discord.com/channels/1/20/30", log_message)
        view.stop.assert_called_once_with()
        interaction.edit_original_response.assert_awaited_once_with(
            content="Attached 2 Gate proofs",
            embed=None,
            view=None,
            allowed_mentions="no-mentions",
        )

    async def test_stale_proof_selection_stops_without_a_partial_success_log(self):
        member = SimpleNamespace(id=10, bot=False)
        guild = SimpleNamespace(id=1, get_member=lambda _user_id: member)
        source_message = SimpleNamespace(
            id=30,
            channel=SimpleNamespace(id=20),
            guild=guild,
            webhook_id=None,
            author=member,
            raw_mentions=(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        view = SimpleNamespace(
            source_message=source_message,
            candidate_ids=(10,),
            selected_assignments={10: 1},
            stop=mock.Mock(),
            render_embed=mock.Mock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            attach_stargate_proofs=mock.AsyncMock(
                side_effect=nhmisc.GateProofConflict("stale")
            )
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(
            return_value=source_message
        )
        cog._require_private_moderation_log_channel = mock.AsyncMock(
            return_value=SimpleNamespace(id=40)
        )
        cog._send_moderation_log = mock.AsyncMock(return_value=True)

        await cog._confirm_gate_proofs(interaction, view)

        cog._send_moderation_log.assert_not_awaited()
        view.stop.assert_called_once_with()
        interaction.edit_original_response.assert_awaited_once_with(
            content="Gate proof data changed. Start again",
            embed=None,
            view=None,
            allowed_mentions="no-mentions",
        )


if __name__ == "__main__":
    unittest.main()
