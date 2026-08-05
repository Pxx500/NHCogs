import asyncio
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

from tests.test_gatecount import nhmisc


class GateRevokeReviewTests(unittest.TestCase):
    def test_review_shows_target_transition_removed_ordinal_and_proof(self):
        self.assertTrue(hasattr(nhmisc.NHMisc, "_build_gate_revoke_embed"))
        member = SimpleNamespace(id=42, display_name="Gatekeeper")
        award = SimpleNamespace(
            ordinal=3,
            source_channel_id=20,
            source_message_id=30,
        )

        embed = nhmisc.NHMisc._build_gate_revoke_embed(10, member, award)

        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Target"], "<@42>")
        self.assertEqual(fields["Current Gate count"], "3")
        self.assertEqual(fields["Role transition"], "3 → 2")
        self.assertEqual(fields["Removing"], "Stargate 3")
        self.assertEqual(
            fields["Proof"],
            "[Open message](https://discord.com/channels/10/20/30)",
        )

    def test_review_is_explicit_when_latest_gate_has_no_proof(self):
        self.assertTrue(hasattr(nhmisc.NHMisc, "_build_gate_revoke_embed"))
        member = SimpleNamespace(id=42, display_name="Gatekeeper")
        award = SimpleNamespace(
            ordinal=1,
            source_channel_id=None,
            source_message_id=None,
        )

        embed = nhmisc.NHMisc._build_gate_revoke_embed(10, member, award)

        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Role transition"], "1 → 0")
        self.assertEqual(fields["Proof"], "No proof stored")


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
        award = SimpleNamespace(
            award_id=7,
            ordinal=2,
            source_channel_id=20,
            source_message_id=30,
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            get_latest_stargate=mock.AsyncMock(return_value=award),
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

        class FakeGateRevokeView:
            def __init__(self, cog, opener_id, member, award):
                self.cog = cog
                self.opener_id = opener_id
                self.member = member
                self.award = award
                self.message = None

            def render_embed(self):
                return self.cog._build_gate_revoke_embed(
                    10,
                    self.member,
                    self.award,
                )

        package = ModuleType("_gatecount_nhmisc")
        package.__path__ = []
        views = ModuleType("_gatecount_nhmisc.achievement_views")
        views.GateRevokeView = FakeGateRevokeView
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "_gatecount_nhmisc": package,
                    "_gatecount_nhmisc.achievement_views": views,
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
        self.assertEqual(kwargs["embed"].fields[2].value, "2 → 1")
        self.assertEqual(kwargs["allowed_mentions"], "no-mentions")
        self.assertIs(kwargs["view"].message, response_message)

    async def test_user_without_a_gate_is_rejected_without_a_role_change(self):
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            get_latest_stargate=mock.AsyncMock(return_value=None),
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

        self.assertEqual(
            tree.added,
            [
                ("gaterevoke", True),
                ("Revoke latest Gate", True),
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
                ("Revoke latest Gate", "user"),
                ("achievements", "chat_input"),
                ("View achievements", "user"),
                ("Grant achievements", "message"),
                ("Add Gate Proof", "message"),
            ],
        )


class _GateRevokeStore:
    def __init__(self, award):
        self.award = award
        self.delete_calls = []

    async def get_latest_stargate(self, _guild_id, _user_id):
        return self.award

    async def delete_latest_stargate(
        self,
        guild_id,
        user_id,
        *,
        expected_award_id,
    ):
        self.delete_calls.append((guild_id, user_id, expected_award_id))
        await asyncio.sleep(0)
        if self.award is None or self.award.award_id != expected_award_id:
            return None
        deleted = self.award
        self.award = None
        return deleted


class _GateRevokeView:
    def __init__(self, member, award):
        self.opener_id = 99
        self.member = member
        self.award = award
        self.stopped = False

    def stop(self):
        self.stopped = True

    def render_embed(self, *, notice=None):
        return SimpleNamespace(notice=notice)


class GateRevokeExecutionTests(unittest.IsolatedAsyncioTestCase):
    def _fixture(self, *, ordinal=2, award_id=7):
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
                roles[nhmisc.GATE_TIER_ROLE_IDS[ordinal - 1]],
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
        )
        award = SimpleNamespace(
            award_id=award_id,
            guild_id=guild.id,
            user_id=member.id,
            ordinal=ordinal,
            source_channel_id=20,
            source_message_id=30,
        )
        store = _GateRevokeStore(award)
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._gate_revoke_locks = {}
        cog._authorized_gate_role_edits = {}
        cog._require_private_moderation_log_channel = mock.AsyncMock(
            return_value=SimpleNamespace(id=88)
        )
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        return cog, interaction, member, guild, store, award

    async def test_confirm_removes_only_latest_gate_and_preserves_other_roles(self):
        cog, interaction, member, _guild, store, award = self._fixture(ordinal=2)
        view = _GateRevokeView(member, award)

        await cog._confirm_gate_revoke(interaction, view)

        changed_role_ids = {role.id for role in member.edit.await_args_list[0].kwargs["roles"]}
        self.assertEqual(
            changed_role_ids,
            {
                500,
                nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
                nhmisc.GATE_TIER_ROLE_IDS[0],
            },
        )
        self.assertEqual(store.delete_calls, [(10, 42, 7)])
        self.assertIsNone(store.award)
        self.assertTrue(view.stopped)
        cog._send_moderation_log.assert_awaited_once()
        self.assertIs(cog._send_moderation_log.await_args.args[0], interaction.guild)
        self.assertNotIn((10, 42), cog._authorized_gate_role_edits)

    async def test_confirming_gate_one_removes_the_gate_role(self):
        cog, interaction, member, _guild, _store, award = self._fixture(ordinal=1)

        await cog._confirm_gate_revoke(
            interaction,
            _GateRevokeView(member, award),
        )

        changed_role_ids = {role.id for role in member.edit.await_args.kwargs["roles"]}
        self.assertEqual(
            changed_role_ids,
            {500, nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID},
        )

    async def test_stale_confirmation_does_not_change_discord_or_database(self):
        cog, interaction, member, _guild, store, reviewed_award = self._fixture()
        store.award = SimpleNamespace(**{**reviewed_award.__dict__, "award_id": 8})

        await cog._confirm_gate_revoke(
            interaction,
            _GateRevokeView(member, reviewed_award),
        )

        member.edit.assert_not_awaited()
        self.assertEqual(store.delete_calls, [])
        self.assertIn(
            "changed",
            interaction.edit_original_response.await_args.kwargs["embed"].notice,
        )

    async def test_discord_failure_leaves_database_unchanged(self):
        cog, interaction, member, _guild, store, award = self._fixture()
        member.edit.side_effect = RuntimeError("Discord unavailable")

        await cog._confirm_gate_revoke(
            interaction,
            _GateRevokeView(member, award),
        )

        self.assertIs(store.award, award)
        self.assertEqual(store.delete_calls, [])

    async def test_conditional_delete_failure_restores_original_discord_roles(self):
        cog, interaction, member, _guild, store, award = self._fixture()

        async def reject_delete(*_args, **_kwargs):
            return None

        store.delete_latest_stargate = reject_delete

        await cog._confirm_gate_revoke(
            interaction,
            _GateRevokeView(member, award),
        )

        self.assertEqual(member.edit.await_count, 2)
        restored_role_ids = {
            role.id for role in member.edit.await_args_list[1].kwargs["roles"]
        }
        self.assertEqual(restored_role_ids, {role.id for role in member.roles} - {0})
        self.assertIs(store.award, award)

    async def test_two_concurrent_confirms_can_delete_only_once(self):
        cog, interaction, member, _guild, store, award = self._fixture()
        second_interaction = SimpleNamespace(
            guild=interaction.guild,
            user=interaction.user,
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )

        await asyncio.gather(
            cog._confirm_gate_revoke(interaction, _GateRevokeView(member, award)),
            cog._confirm_gate_revoke(
                second_interaction,
                _GateRevokeView(member, award),
            ),
        )

        self.assertEqual(member.edit.await_count, 1)
        self.assertEqual(store.delete_calls, [(10, 42, 7)])


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
