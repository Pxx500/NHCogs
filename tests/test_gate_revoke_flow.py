import asyncio
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest import mock

from tests.test_gate_proof_flow import _load_achievement_views
from tests.test_gatecount import nhmisc

AchievementAward = nhmisc.AchievementStore._award_from_row.__globals__["AchievementAward"]
StargateProof = nhmisc.AchievementStore._award_from_row.__globals__["StargateProof"]
STARGATE_COMPLETED_KEY = nhmisc.AchievementStore._award_from_row.__globals__[
    "STARGATE_COMPLETED_KEY"
]


class GateRevokeReviewTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _award(award_id, ordinal, channel_id=None, message_id=None):
        return AchievementAward(
            award_id=award_id,
            guild_id=10,
            user_id=42,
            achievement_key=STARGATE_COMPLETED_KEY,
            ordinal=ordinal,
            awarded_at=f"time-{award_id}",
            source_channel_id=channel_id,
            source_message_id=message_id,
        )

    def test_review_lists_every_gate_and_starts_without_a_selection(self):
        views, _fake_select = _load_achievement_views()
        member = SimpleNamespace(id=42, display_name="Gatekeeper")
        awards = (
            self._award(1, 1),
            self._award(2, 3, 20, 30),
        )
        cog = SimpleNamespace(_build_gate_revoke_embed=nhmisc.NHMisc._build_gate_revoke_embed)

        view = views.GateRevokeView(cog, 99, member, awards)
        embed = view.render_embed()

        self.assertIsNone(view.selected_award_id)
        self.assertEqual(
            tuple(option.label for option in view.gate_select.options),
            ("Stargate 1", "Stargate 3"),
        )
        self.assertIn("Stargate 1: No proof stored", embed.description)
        self.assertIn(
            "Stargate 3: [Open proof](https://discord.com/channels/10/20/30)",
            embed.description,
        )
        self.assertNotIn(view.shift, view.children)
        self.assertNotIn(view.leave_gap, view.children)

    async def test_selecting_a_non_latest_gate_offers_shift_and_leave(self):
        views, _fake_select = _load_achievement_views()
        member = SimpleNamespace(id=42, display_name="Gatekeeper")
        awards = (
            self._award(1, 1),
            self._award(2, 2),
            self._award(3, 4),
        )
        cog = SimpleNamespace(_build_gate_revoke_embed=nhmisc.NHMisc._build_gate_revoke_embed)
        view = views.GateRevokeView(cog, 99, member, awards)
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=mock.AsyncMock()))
        view.gate_select.values = ["2"]

        await view.gate_select.callback(interaction)

        self.assertEqual(view.selected_award_id, 2)
        self.assertIn(view.shift, view.children)
        self.assertIn(view.leave_gap, view.children)
        self.assertEqual(view.shift.label, "Shift to fill gap")
        self.assertEqual(view.leave_gap.label, "Leave gap")

    async def test_selecting_latest_gate_uses_one_revoke_action(self):
        views, _fake_select = _load_achievement_views()
        member = SimpleNamespace(id=42, display_name="Gatekeeper")
        awards = (self._award(1, 1), self._award(2, 4))
        cog = SimpleNamespace(_build_gate_revoke_embed=nhmisc.NHMisc._build_gate_revoke_embed)
        view = views.GateRevokeView(cog, 99, member, awards)
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=mock.AsyncMock()))
        view.gate_select.values = ["2"]

        await view.gate_select.callback(interaction)

        self.assertIn(view.shift, view.children)
        self.assertNotIn(view.leave_gap, view.children)
        self.assertEqual(view.shift.label, "Revoke Stargate 4")


class GateRevokeEntryPointTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_and_user_action_share_the_same_workflow(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog._start_gate_revoke = mock.AsyncMock()
        interaction = SimpleNamespace()
        member = SimpleNamespace(id=42)

        await cog._gate_revoke_slash(interaction, member)
        await cog._gate_revoke_user_context_action(interaction, member)

        self.assertEqual(
            cog._start_gate_revoke.await_args_list,
            [mock.call(interaction, member), mock.call(interaction, member)],
        )

    async def test_manage_messages_is_checked_at_runtime(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            user=SimpleNamespace(id=99),
            permissions=SimpleNamespace(manage_messages=False),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)

        await cog._start_gate_revoke(interaction, SimpleNamespace(id=42))

        interaction.response.send_message.assert_awaited_once_with(
            "You need Manage Messages permission",
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()

    async def test_review_is_private_and_bound_to_the_invoking_moderator(self):
        award = AchievementAward(
            award_id=7,
            guild_id=10,
            user_id=42,
            achievement_key=STARGATE_COMPLETED_KEY,
            ordinal=2,
            awarded_at="now",
            source_channel_id=20,
            source_message_id=30,
        )
        awards = (award,)
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            get_active_stargates=mock.AsyncMock(return_value=awards),
        )
        gate_role = SimpleNamespace(id=nhmisc.GATE_TIER_ROLE_IDS[1])
        member = SimpleNamespace(
            id=42,
            display_name="Gatekeeper",
            top_role=SimpleNamespace(position=5),
            roles=(gate_role,),
        )
        guild = SimpleNamespace(
            id=10,
            me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
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
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._require_private_moderation_log_channel = mock.AsyncMock(
            return_value=SimpleNamespace(id=88)
        )

        views, _fake_select = _load_achievement_views()
        package = ModuleType("_gatecount_root.nhmisc")
        package.__path__ = []
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "_gatecount_root.nhmisc": package,
                    "_gatecount_root.nhmisc.achievement_views": views,
                },
            ),
            mock.patch.object(
                nhmisc,
                "_plan_gate_revoke_roles",
                return_value=(1, (), ()),
            ),
        ):
            await cog._start_gate_revoke(interaction, member)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        kwargs = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(kwargs["view"].opener_id, 99)
        self.assertEqual(kwargs["view"].awards, awards)
        self.assertIn("Gate role: 1 → 0", kwargs["embed"].description)
        self.assertEqual(kwargs["allowed_mentions"], "no-mentions")
        self.assertIs(kwargs["view"].message, response_message)

    async def test_user_without_a_gate_is_rejected_without_a_role_change(self):
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            get_active_stargates=mock.AsyncMock(return_value=()),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            user=SimpleNamespace(id=99),
            permissions=SimpleNamespace(manage_messages=True),
            response=SimpleNamespace(
                send_message=mock.AsyncMock(),
                defer=mock.AsyncMock(),
            ),
            edit_original_response=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store

        await cog._start_gate_revoke(interaction, SimpleNamespace(id=42))

        interaction.edit_original_response.assert_awaited_once_with(
            content="This user has no Gate to revoke"
        )

    async def test_public_moderation_log_channel_is_rejected(self):
        channel = SimpleNamespace(
            permissions_for=mock.Mock(
                return_value=SimpleNamespace(
                    view_channel=True,
                    send_messages=True,
                )
            )
        )
        guild = SimpleNamespace(id=10, default_role=object())
        config = SimpleNamespace(
            moderation_log_channel=mock.AsyncMock(return_value=88)
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog.config = SimpleNamespace(guild=mock.Mock(return_value=config))
        cog._get_log_channel = mock.Mock(return_value=channel)

        with self.assertRaisesRegex(
            nhmisc.commands.UserFeedbackCheckFailure,
            "hidden from @everyone",
        ):
            await cog._require_private_moderation_log_channel(guild)


class _AchievementCommandTree:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_command(self, command, *, override=False):
        self.added.append((command.name, override))

    def remove_command(self, name, *, type):
        self.removed.append((name, type))


class GateRevokeRegistrationTests(unittest.TestCase):
    def test_revoke_commands_register_and_unregister_with_achievement_commands(self):
        tree = _AchievementCommandTree()
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = SimpleNamespace(tree=tree)
        cog._gate_revoke_slash_command = SimpleNamespace(name="gaterevoke")
        cog._gate_revoke_user_context_menu = SimpleNamespace(
            name="Revoke Gate"
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

        self.assertEqual(
            tree.added,
            [
                ("gaterevoke", True),
                ("Revoke Gate", True),
                ("achievements", True),
                ("View achievements", True),
                ("Grant achievements", True),
                ("Add Gate Proof", True),
            ],
        )
        self.assertEqual(
            tree.removed,
            [
                ("gaterevoke", "chat_input"),
                ("Revoke Gate", "user"),
                ("achievements", "chat_input"),
                ("View achievements", "user"),
                ("Grant achievements", "message"),
                ("Add Gate Proof", "message"),
            ],
        )


class GateRevokeExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def _fixture(self, *, ordinals=(1, 2), selected_ordinal=2):
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "achievements.sqlite"
        store = nhmisc.AchievementStore(path)
        await store.initialize()
        connection = sqlite3.connect(path)
        try:
            connection.executemany(
                """
                INSERT INTO achievement_awards (
                    guild_id, user_id, achievement_key, ordinal, awarded_at,
                    source_channel_id, source_message_id, state
                ) VALUES (10, 42, 'stargate_completed', ?, ?, 20, ?, 'active')
                """,
                (
                    (ordinal, f"time-{ordinal}", 100 + ordinal)
                    for ordinal in ordinals
                ),
            )
            connection.commit()
        finally:
            connection.close()
        awards = await store.get_active_stargates(10, 42)
        selected_award = next(
            award for award in awards if award.ordinal == selected_ordinal
        )
        roles = {
            role_id: SimpleNamespace(
                id=role_id,
                managed=False,
                position=index,
            )
            for index, role_id in enumerate(nhmisc.GATE_TIER_ROLE_IDS, start=1)
        }
        default_role = SimpleNamespace(id=0, managed=False, position=0)
        unrelated_role = SimpleNamespace(id=500, managed=False, position=10)
        solo_role = SimpleNamespace(
            id=nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            managed=False,
            position=11,
        )
        roles.update(
            {
                default_role.id: default_role,
                unrelated_role.id: unrelated_role,
                solo_role.id: solo_role,
            }
        )
        member = SimpleNamespace(
            id=42,
            display_name="Gatekeeper",
            top_role=unrelated_role,
            roles=(
                default_role,
                unrelated_role,
                solo_role,
                roles[nhmisc.GATE_TIER_ROLE_IDS[len(ordinals) - 1]],
            ),
            edit=mock.AsyncMock(),
        )
        guild = SimpleNamespace(
            id=10,
            default_role=default_role,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=100),
            ),
            get_role=roles.get,
        )
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=99, __str__=lambda _self: "Moderator"),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
            delete_original_response=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._gate_revoke_locks = {}
        cog._authorized_gate_role_edits = {}
        cog._require_private_moderation_log_channel = mock.AsyncMock(
            return_value=SimpleNamespace(id=88)
        )
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        views, _fake_select = _load_achievement_views()
        view = views.GateRevokeView(cog, 99, member, awards)
        view.selected_award_id = selected_award.award_id
        view._configure_select()
        view._configure_actions()
        return cog, interaction, member, guild, store, awards, view

    async def test_leave_gap_revokes_selected_gate_and_sets_role_from_active_count(self):
        cog, interaction, member, _guild, store, _awards, view = await self._fixture(
            ordinals=(1, 2, 3),
            selected_ordinal=2,
        )

        await cog._confirm_gate_revoke(interaction, view, compact=False)

        changed_role_ids = {role.id for role in member.edit.await_args_list[0].kwargs["roles"]}
        self.assertEqual(
            changed_role_ids,
            {
                500,
                nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
                nhmisc.GATE_TIER_ROLE_IDS[1],
            },
        )
        self.assertEqual(
            tuple(award.ordinal for award in await store.get_active_stargates(10, 42)),
            (1, 3),
        )
        cog._send_moderation_log.assert_awaited_once()
        self.assertIs(cog._send_moderation_log.await_args.args[0], interaction.guild)
        self.assertIn("Mode: leave gap", cog._send_moderation_log.await_args.args[1])
        interaction.delete_original_response.assert_awaited_once()
        interaction.edit_original_response.assert_not_awaited()
        self.assertNotIn((10, 42), cog._authorized_gate_role_edits)

    async def test_shift_compacts_all_remaining_ordinals_and_proofs(self):
        cog, interaction, member, _guild, store, _awards, view = await self._fixture(
            ordinals=(1, 3, 5),
            selected_ordinal=3,
        )

        await cog._confirm_gate_revoke(interaction, view, compact=True)

        self.assertEqual(
            tuple(
                (award.ordinal, award.source_message_id)
                for award in await store.get_active_stargates(10, 42)
            ),
            ((1, 101), (2, 105)),
        )
        audit = cog._send_moderation_log.await_args.args[1]
        self.assertIn("Mode: shift to fill gap", audit)
        self.assertIn("Stargate 5 → Stargate 2", audit)

    async def test_revoking_the_only_gate_removes_the_gate_role(self):
        cog, interaction, member, _guild, store, _awards, view = await self._fixture(
            ordinals=(1,),
            selected_ordinal=1,
        )

        await cog._confirm_gate_revoke(interaction, view, compact=True)

        changed_role_ids = {role.id for role in member.edit.await_args.kwargs["roles"]}
        self.assertEqual(
            changed_role_ids,
            {500, nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID},
        )
        self.assertEqual(await store.get_active_stargates(10, 42), ())

    async def test_stale_confirmation_does_not_change_discord_or_database(self):
        cog, interaction, member, _guild, store, awards, view = await self._fixture()
        await store.replace_stargate_proof_links(
            10,
            42,
            (StargateProof(1, 99, 100),),
            expected_proofs={1: StargateProof(1, 20, 101)},
        )

        await cog._confirm_gate_revoke(interaction, view, compact=True)

        member.edit.assert_not_awaited()
        self.assertIn(
            "changed",
            interaction.edit_original_response.await_args.kwargs["embed"].description,
        )
        self.assertNotEqual(await store.get_active_stargates(10, 42), awards)

    async def test_discord_failure_leaves_database_unchanged(self):
        cog, interaction, member, _guild, store, awards, view = await self._fixture()
        member.edit.side_effect = RuntimeError("Discord unavailable")

        await cog._confirm_gate_revoke(interaction, view, compact=True)

        self.assertEqual(await store.get_active_stargates(10, 42), awards)

    async def test_transaction_conflict_restores_original_discord_roles(self):
        cog, interaction, member, _guild, store, awards, view = await self._fixture()

        async def change_proof_after_role_edit(**_kwargs):
            if member.edit.await_count == 1:
                await store.replace_stargate_proof_links(
                    10,
                    42,
                    (StargateProof(1, 50, 60),),
                    expected_proofs={1: StargateProof(1, 20, 101)},
                )

        member.edit.side_effect = change_proof_after_role_edit

        await cog._confirm_gate_revoke(interaction, view, compact=True)

        self.assertEqual(member.edit.await_count, 2)
        restored_role_ids = {
            role.id for role in member.edit.await_args_list[1].kwargs["roles"]
        }
        self.assertEqual(restored_role_ids, {role.id for role in member.roles} - {0})
        self.assertEqual(len(await store.get_active_stargates(10, 42)), len(awards))

    async def test_two_concurrent_confirms_can_revoke_only_once(self):
        cog, interaction, member, _guild, store, _awards, view = await self._fixture()
        second_interaction = SimpleNamespace(
            guild=interaction.guild,
            user=interaction.user,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
            delete_original_response=mock.AsyncMock(),
        )
        views, _fake_select = _load_achievement_views()
        second_view = views.GateRevokeView(cog, 99, member, view.awards)
        second_view.selected_award_id = view.selected_award_id
        second_view._configure_select()
        second_view._configure_actions()

        await asyncio.gather(
            cog._confirm_gate_revoke(interaction, view, compact=True),
            cog._confirm_gate_revoke(second_interaction, second_view, compact=True),
        )

        self.assertEqual(member.edit.await_count, 1)
        self.assertEqual(len(await store.get_active_stargates(10, 42)), 1)


class GateRevokeRoleListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_revoke_role_event_is_not_reverted(self):
        tier_two = SimpleNamespace(id=nhmisc.GATE_TIER_ROLE_IDS[1])
        tier_one = SimpleNamespace(id=nhmisc.GATE_TIER_ROLE_IDS[0])
        guild = SimpleNamespace(id=10)
        before = SimpleNamespace(
            id=42,
            bot=False,
            guild=guild,
            roles=(tier_two,),
        )
        after = SimpleNamespace(
            id=42,
            bot=False,
            guild=guild,
            roles=(tier_one,),
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=()),
            get_gate_projection=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._authorized_gate_role_edits = {
            (10, 42): frozenset({nhmisc.GATE_TIER_ROLE_IDS[0]})
        }

        await cog.on_achievement_member_update(before, after)

        store.get_gate_projection.assert_not_awaited()
