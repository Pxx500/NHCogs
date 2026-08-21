from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.test_gate_proof_flow import _load_achievement_views
from tests.test_gatecount import nhmisc

StoredGateIncrementMember = nhmisc.GateIncrementStore._get_operation_sync.__globals__[
    "StoredGateIncrementMember"
]


def _load_gate_increment_views():
    achievement_views, _fake_select = _load_achievement_views()
    path = Path(__file__).resolve().parents[1] / "NHCogs" / "nhmisc" / "gate_increment_views.py"
    spec = importlib.util.spec_from_file_location("_gate_increment_views", path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"discord": achievement_views.discord}):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module


class _Guild:
    def __init__(self, members):
        self._members = {member.id: member for member in members}

    def get_member(self, user_id):
        return self._members.get(user_id)


def _member(user_id, *, bot=False, role_ids=()):
    return SimpleNamespace(
        id=user_id,
        bot=bot,
        display_name=f"member-{user_id}",
        roles=tuple(SimpleNamespace(id=role_id) for role_id in role_ids),
    )


class GateIncrementPlanningTests(unittest.TestCase):
    def test_prefix_command_is_not_exposed(self):
        self.assertFalse(hasattr(nhmisc.NHMisc, "gateincrement"))

    def test_candidates_are_author_then_mentions_without_duplicates_or_bots(self):
        author = _member(1)
        mentioned = _member(2)
        bot = _member(3, bot=True)
        guild = _Guild((author, mentioned, bot))
        source = SimpleNamespace(
            guild=guild,
            author=author,
            webhook_id=None,
            raw_mentions=(2, 1, 2, 3, 4),
        )

        candidates = nhmisc._build_gate_increment_candidates(source)

        self.assertEqual(
            tuple(candidate.user_id for candidate in candidates),
            (1, 2),
        )

    def test_webhook_author_is_not_implicitly_included(self):
        webhook_author = _member(1)
        mentioned = _member(2)
        source = SimpleNamespace(
            guild=_Guild((mentioned,)),
            author=webhook_author,
            webhook_id=99,
            raw_mentions=(2,),
        )

        candidates = nhmisc._build_gate_increment_candidates(source)

        self.assertEqual(
            tuple(candidate.user_id for candidate in candidates),
            (2,),
        )

    def test_candidate_plan_uses_highest_gate_and_keeps_maximum_visible(self):
        gate_roles = nhmisc.GATE_TIER_ROLE_IDS
        duplicate_member = _member(1, role_ids=(gate_roles[0], gate_roles[2]))
        maximum_member = _member(2, role_ids=(gate_roles[-1],))
        source = SimpleNamespace(
            guild=_Guild((duplicate_member, maximum_member)),
            author=duplicate_member,
            webhook_id=None,
            raw_mentions=(2,),
        )

        candidates = nhmisc._build_gate_increment_candidates(source)

        self.assertEqual(candidates[0].current_gate_role_ids, (gate_roles[0], gate_roles[2]))
        self.assertEqual(candidates[0].target_role_id, gate_roles[3])
        self.assertEqual(candidates[1].current_tier, 6)
        self.assertIsNone(candidates[1].target_role_id)

    def test_preflight_requires_manageable_complete_gate_ladder(self):
        roles = {
            role_id: SimpleNamespace(id=role_id, managed=False, position=position)
            for position, role_id in enumerate(nhmisc.GATE_TIER_ROLE_IDS, start=1)
        }
        guild = SimpleNamespace(
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=10),
            ),
            get_role=roles.get,
        )

        resolved = nhmisc._validate_gate_increment_configuration(guild)

        self.assertEqual(tuple(role.id for role in resolved), nhmisc.GATE_TIER_ROLE_IDS)
        roles[nhmisc.GATE_TIER_ROLE_IDS[2]].managed = True
        with self.assertRaises(nhmisc.commands.UserFeedbackCheckFailure):
            nhmisc._validate_gate_increment_configuration(guild)

    def test_only_selected_role_drift_requires_reconfirmation(self):
        first = nhmisc.GateIncrementCandidate(1, "one", (), None, 10)
        second = nhmisc.GateIncrementCandidate(2, "two", (10,), 1, 11)
        view = SimpleNamespace(
            candidate_ids=(1, 2),
            candidates=(first, second),
            selected_user_ids={1},
        )
        unselected_changed = nhmisc.GateIncrementCandidate(
            2, "two", (11,), 2, 12
        )

        self.assertFalse(
            nhmisc.NHMisc._gate_increment_review_is_stale(
                view,
                (first, unselected_changed),
            )
        )
        selected_changed = nhmisc.GateIncrementCandidate(1, "one", (10,), 1, 11)
        self.assertTrue(
            nhmisc.NHMisc._gate_increment_review_is_stale(
                view,
                (selected_changed, second),
            )
        )

    def test_review_hides_deselected_users_and_warns_when_increment_fills_gap(self):
        views = _load_gate_increment_views()
        selected = nhmisc.GateIncrementCandidate(
            1,
            "one",
            (nhmisc.GATE_TIER_ROLE_IDS[2],),
            3,
            nhmisc.GATE_TIER_ROLE_IDS[2],
            target_ordinal=2,
            highest_ordinal=4,
        )
        deselected = nhmisc.GateIncrementCandidate(
            2,
            "two",
            (),
            None,
            nhmisc.GATE_TIER_ROLE_IDS[0],
            target_ordinal=1,
            highest_ordinal=0,
        )
        maximum = nhmisc.GateIncrementCandidate(
            3,
            "three",
            (nhmisc.GATE_TIER_ROLE_IDS[-1],),
            6,
            None,
            highest_ordinal=6,
        )
        view = views.GateIncrementReviewView(
            SimpleNamespace(),
            SimpleNamespace(jump_url="https://example.invalid/source"),
            42,
            (selected, deselected, maximum),
            ephemeral=True,
        )
        view.selected_user_ids = {1}

        description = view.render_embed().description

        self.assertIn("<@1>", description)
        self.assertNotIn("<@2>", description)
        self.assertIn("<@3>", description)
        self.assertIn(
            "will fill missing Stargate 2 instead of adding Stargate 5",
            description,
        )


class GateIncrementDatabasePlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_uses_active_count_for_role_and_lowest_gap_for_ordinal(self):
        member = _member(10, role_ids=(nhmisc.GATE_TIER_ROLE_IDS[2],))
        awards = tuple(
            SimpleNamespace(ordinal=ordinal) for ordinal in (1, 3, 4)
        )
        guild = SimpleNamespace(
            id=1,
            fetch_member=mock.AsyncMock(return_value=member),
        )
        source = SimpleNamespace(
            guild=guild,
            webhook_id=99,
            raw_mentions=(10,),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            get_active_stargates=mock.AsyncMock(return_value=awards)
        )

        candidates = await cog._fetch_gate_increment_candidates(source)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.current_tier, 3)
        self.assertEqual(candidate.target_role_id, nhmisc.GATE_TIER_ROLE_IDS[3])
        self.assertEqual(candidate.target_ordinal, 2)
        self.assertEqual(candidate.highest_ordinal, 4)


class _EditableMember:
    def __init__(self, user_id, roles, *, top_role):
        self.id = user_id
        self.bot = False
        self.display_name = f"member-{user_id}"
        self.roles = list(roles)
        self.top_role = top_role
        self.edits = []

    async def edit(self, *, roles, reason):
        self.edits.append((tuple(roles), reason))
        self.roles = list(roles)


class GateIncrementExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = nhmisc.GateIncrementStore(
            Path(self.temp_dir.name) / "gate-increment.sqlite"
        )
        await self.store.initialize()

    async def test_claimed_member_is_edited_once_with_fixed_target(self):
        role_by_id = {
            role_id: SimpleNamespace(id=role_id, managed=False, position=position)
            for position, role_id in enumerate(nhmisc.GATE_TIER_ROLE_IDS, start=1)
        }
        default_role = SimpleNamespace(id=0, managed=False, position=0)
        unrelated_role = SimpleNamespace(id=50, managed=False, position=1)
        solo_role = SimpleNamespace(
            id=nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            managed=False,
            position=2,
        )
        role_by_id.update(
            {
                default_role.id: default_role,
                unrelated_role.id: unrelated_role,
                solo_role.id: solo_role,
            }
        )
        member = _EditableMember(
            60,
            (
                default_role,
                unrelated_role,
                solo_role,
                role_by_id[nhmisc.GATE_TIER_ROLE_IDS[0]],
            ),
            top_role=unrelated_role,
        )
        guild = SimpleNamespace(
            id=70,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=100),
            ),
            default_role=default_role,
            get_role=role_by_id.get,
            fetch_member=lambda _user_id: _async_value(member),
        )
        source = SimpleNamespace(
            id=80,
            guild=guild,
            channel=SimpleNamespace(id=90),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._gate_increment_store = self.store
        key = cog._gate_increment_key(source)
        await self.store.claim(
            key,
            100,
            (
                nhmisc.GateIncrementMemberPlan(
                    member.id,
                    (nhmisc.GATE_TIER_ROLE_IDS[0],),
                    nhmisc.GATE_TIER_ROLE_IDS[1],
                ),
            ),
        )

        first = await cog._execute_gate_increment_operation(source, 100)
        second = await cog._execute_gate_increment_operation(source, 101)

        self.assertEqual(first.operation.state, nhmisc.OperationState.COMPLETED)
        self.assertEqual(second.operation.state, nhmisc.OperationState.COMPLETED)
        self.assertEqual(len(member.edits), 1)
        edited_role_ids = tuple(role.id for role in member.edits[0][0])
        self.assertEqual(
            edited_role_ids,
            (
                unrelated_role.id,
                solo_role.id,
                nhmisc.GATE_TIER_ROLE_IDS[1],
            ),
        )

        member.edits.clear()
        member.roles = [
            default_role,
            unrelated_role,
            solo_role,
            role_by_id[nhmisc.GATE_TIER_ROLE_IDS[1]],
        ]
        recovery_source = SimpleNamespace(
            id=81,
            guild=guild,
            channel=source.channel,
        )
        recovery_key = cog._gate_increment_key(recovery_source)
        await self.store.claim(
            recovery_key,
            100,
            (
                nhmisc.GateIncrementMemberPlan(
                    member.id,
                    (nhmisc.GATE_TIER_ROLE_IDS[0],),
                    nhmisc.GATE_TIER_ROLE_IDS[1],
                ),
            ),
        )
        await self.store.mark_member_failed(recovery_key, 0, "discord_error")
        partial = await self.store.finalize_operation(recovery_key)
        self.assertEqual(partial.operation.state, nhmisc.OperationState.PARTIAL)

        recovered = await cog._execute_gate_increment_operation(
            recovery_source, 100
        )

        self.assertEqual(recovered.operation.state, nhmisc.OperationState.COMPLETED)
        self.assertEqual(member.edits, [])

    async def test_solo_selection_adds_solo_role_in_same_member_edit(self):
        default_role = SimpleNamespace(id=0, position=0)
        unrelated_role = SimpleNamespace(id=50, position=1)
        gate_role = SimpleNamespace(id=nhmisc.GATE_TIER_ROLE_IDS[0], position=2)
        solo_role = SimpleNamespace(
            id=nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            position=3,
        )
        roles = {
            role.id: role
            for role in (default_role, unrelated_role, gate_role, solo_role)
        }
        member = _EditableMember(
            60,
            (default_role, unrelated_role),
            top_role=unrelated_role,
        )
        guild = SimpleNamespace(
            me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
            default_role=default_role,
            get_role=roles.get,
        )

        failure = await nhmisc.NHMisc._apply_fixed_gate_target(
            guild,
            member,
            gate_role.id,
            nhmisc.SourceMessageKey(70, 80, 90),
            100,
            grant_solo=True,
        )

        self.assertIsNone(failure)
        self.assertEqual(len(member.edits), 1)
        self.assertEqual(
            {role.id for role in member.edits[0][0]},
            {unrelated_role.id, gate_role.id, solo_role.id},
        )

    async def test_gate_projection_replaces_manual_gate_change_exactly(self):
        role_by_id = {
            role_id: SimpleNamespace(id=role_id, managed=False, position=position)
            for position, role_id in enumerate(nhmisc.GATE_TIER_ROLE_IDS, start=1)
        }
        default_role = SimpleNamespace(id=0, managed=False, position=0)
        unrelated_role = SimpleNamespace(id=50, managed=False, position=1)
        member = _EditableMember(
            60,
            (
                default_role,
                unrelated_role,
                role_by_id[nhmisc.GATE_TIER_ROLE_IDS[2]],
            ),
            top_role=unrelated_role,
        )
        role_by_id.update({0: default_role, 50: unrelated_role})
        guild = SimpleNamespace(
            id=70,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=100),
            ),
            default_role=default_role,
            get_role=role_by_id.get,
        )

        restored = await nhmisc.NHMisc._restore_gate_projection(
            guild,
            member,
            1,
            reason="test",
        )

        self.assertTrue(restored)
        self.assertEqual(
            {role.id for role in member.edits[0][0]},
            {unrelated_role.id, nhmisc.GATE_TIER_ROLE_IDS[0]},
        )

    async def test_congratulations_ping_users_but_not_roles_or_reply_author(self):
        key = nhmisc.SourceMessageKey(120, 121, 122)
        target_role_id = nhmisc.GATE_TIER_ROLE_IDS[0]
        await self.store.claim(
            key,
            123,
            (
                nhmisc.GateIncrementMemberPlan(
                    124,
                    (),
                    target_role_id,
                    grant_solo=True,
                ),
            ),
        )
        await self.store.mark_member_completed(key, 0)
        snapshot = await self.store.finalize_operation(key)
        result = SimpleNamespace(id=125, channel=SimpleNamespace(id=121))
        source = SimpleNamespace(
            id=122,
            reply=mock.AsyncMock(return_value=result),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._gate_increment_store = self.store

        with mock.patch.object(
            nhmisc.discord,
            "AllowedMentions",
            side_effect=SimpleNamespace,
        ):
            published = await cog._publish_gate_increment_result(source, snapshot)

        self.assertTrue(published)
        content = source.reply.await_args.args[0]
        allowed_mentions = source.reply.await_args.kwargs["allowed_mentions"]
        self.assertEqual(
            content,
            f"🎉 **Congratulations!**\n"
            f"<@124> <@&{target_role_id}> "
            f"<@&{nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID}>",
        )
        self.assertTrue(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.replied_user)
        persisted = await self.store.get_operation(key)
        self.assertEqual(persisted.operation.result_message_id, 125)


class _CommandTree:
    def __init__(self):
        self.command = None
        self.add_count = 0
        self.remove_count = 0

    def get_command(self, _name, *, type):
        return self.command

    def add_command(self, command, *, override=False):
        self.command = command
        self.add_count += 1
        self.override = override

    def remove_command(self, _name, *, type):
        removed = self.command
        self.command = None
        self.remove_count += 1
        return removed


class GateIncrementLifecycleTests(unittest.TestCase):
    def test_context_action_registration_and_removal_are_idempotent(self):
        tree = _CommandTree()
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = SimpleNamespace(tree=tree)
        cog._gate_increment_context_menu = SimpleNamespace(
            name="Increment Gate roles"
        )
        cog._gate_increment_context_registered = False

        with mock.patch.object(
            nhmisc.discord,
            "AppCommandType",
            SimpleNamespace(message="message"),
            create=True,
        ):
            cog._register_gate_increment_context_menu()
            cog._register_gate_increment_context_menu()
            cog._unregister_gate_increment_context_menu()
            cog._unregister_gate_increment_context_menu()

        self.assertEqual(tree.add_count, 1)
        self.assertEqual(tree.remove_count, 1)
        self.assertTrue(tree.override)


class GateIncrementReviewCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_action_defers_before_waiting_for_store(self):
        release_store = asyncio.Event()
        deferred = asyncio.Event()
        events = []

        async def blocked_is_bootstrapped(_guild_id):
            events.append("store")
            await release_store.wait()
            return True

        async def defer(**_kwargs):
            events.append("defer")
            deferred.set()

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=42),
            permissions=SimpleNamespace(manage_messages=True),
            response=SimpleNamespace(defer=mock.AsyncMock(side_effect=defer)),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(side_effect=blocked_is_bootstrapped)
        )
        task = asyncio.create_task(
            cog._gate_increment_context_action(
                interaction,
                SimpleNamespace(),
            )
        )
        try:
            await asyncio.wait_for(deferred.wait(), timeout=0.1)
            self.assertEqual(events[0], "defer")
            interaction.response.defer.assert_awaited_once_with(
                ephemeral=True,
                thinking=True,
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @staticmethod
    def _interaction():
        return SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )

    async def test_resume_does_not_report_applying_operation_as_completed(self):
        key = nhmisc.SourceMessageKey(1, 2, 3)
        snapshot = SimpleNamespace(
            operation=SimpleNamespace(
                key=key,
                state=nhmisc.OperationState.APPLYING,
            )
        )
        view = SimpleNamespace(snapshot=snapshot)
        interaction = self._interaction()
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(
            return_value=SimpleNamespace()
        )
        cog._execute_gate_increment_operation = mock.AsyncMock(
            return_value=snapshot
        )
        cog._publish_gate_increment_result = mock.AsyncMock()
        cog._finish_gate_increment_review = mock.AsyncMock()
        cog._format_gate_increment_operation = mock.Mock(
            return_value="Gate increment is still applying"
        )

        await cog._resume_gate_increment_review(interaction, view)

        cog._publish_gate_increment_result.assert_not_awaited()
        cog._finish_gate_increment_review.assert_awaited_once_with(
            interaction,
            view,
            "Gate increment is still applying",
        )

    async def test_refresh_error_keeps_review_embed_visible(self):
        interaction = self._interaction()
        rendered = object()
        view = SimpleNamespace(
            source_message=SimpleNamespace(
                guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=2),
                id=3,
            ),
            render_embed=mock.Mock(return_value=rendered),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(
            side_effect=nhmisc.commands.UserFeedbackCheckFailure("refresh failed")
        )

        await cog._refresh_gate_increment_review(interaction, view)

        view.render_embed.assert_called_once_with(notice="refresh failed")
        self.assertIs(
            interaction.edit_original_response.await_args.kwargs["embed"],
            rendered,
        )

    async def test_confirm_error_keeps_review_embed_visible(self):
        interaction = self._interaction()
        rendered = object()
        view = SimpleNamespace(
            source_message=SimpleNamespace(
                guild=SimpleNamespace(id=1),
                channel=SimpleNamespace(id=2),
                id=3,
            ),
            selected_user_ids={10},
            render_embed=mock.Mock(return_value=rendered),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(
            side_effect=nhmisc.commands.UserFeedbackCheckFailure("confirm failed")
        )

        await cog._confirm_gate_increment_review(interaction, view)

        view.render_embed.assert_called_once_with(notice="confirm failed")
        self.assertIs(
            interaction.edit_original_response.await_args.kwargs["embed"],
            rendered,
        )

    async def test_successful_confirm_emits_one_moderation_log(self):
        interaction = self._interaction()
        guild = SimpleNamespace(id=1)
        source = SimpleNamespace(
            guild=guild,
            channel=SimpleNamespace(id=2),
            id=3,
        )
        candidate = nhmisc.GateIncrementCandidate(
            10,
            "Player",
            (),
            None,
            nhmisc.GATE_TIER_ROLE_IDS[0],
            target_ordinal=1,
            highest_ordinal=0,
        )
        view = SimpleNamespace(
            source_message=source,
            selected_user_ids={10},
            solo_gater_enabled=False,
        )
        snapshot = SimpleNamespace(
            members=(
                StoredGateIncrementMember(
                    position=0,
                    user_id=10,
                    expected_gate_role_ids=(),
                    target_role_id=nhmisc.GATE_TIER_ROLE_IDS[0],
                    state=nhmisc.MemberState.COMPLETED,
                    failure_code=None,
                ),
            )
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._fetch_gate_increment_candidates = mock.AsyncMock(
            return_value=(candidate,)
        )
        cog._validate_gate_increment_candidate_count = mock.Mock()
        cog._gate_increment_review_is_stale = mock.Mock(return_value=False)
        cog._gate_increment_store = SimpleNamespace(
            claim=mock.AsyncMock(return_value=SimpleNamespace(created=True))
        )
        cog._execute_gate_increment_operation = mock.AsyncMock(return_value=snapshot)
        cog._publish_gate_increment_result = mock.AsyncMock(return_value=True)
        cog._format_gate_increment_completion = mock.Mock(return_value="done")
        cog._finish_gate_increment_review = mock.AsyncMock()
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        cog._require_private_moderation_log_channel = mock.AsyncMock()

        with mock.patch.object(nhmisc, "_validate_gate_increment_configuration"):
            await cog._confirm_gate_increment_review(interaction, view)

        cog._send_moderation_log.assert_awaited_once()
        audit = cog._send_moderation_log.await_args.args[1]
        self.assertIn("Gate incremented", audit)
        self.assertIn("<@10> Gate 1", audit)
        self.assertIn("https://discord.com/channels/1/2/3", audit)

    async def test_moderation_log_failure_does_not_block_congratulations(self):
        interaction = self._interaction()
        guild = SimpleNamespace(id=1)
        source = SimpleNamespace(
            guild=guild,
            channel=SimpleNamespace(id=2),
            id=3,
        )
        candidate = nhmisc.GateIncrementCandidate(
            10,
            "Player",
            (),
            None,
            nhmisc.GATE_TIER_ROLE_IDS[0],
            target_ordinal=1,
            highest_ordinal=0,
        )
        view = SimpleNamespace(
            source_message=source,
            selected_user_ids={10},
            solo_gater_enabled=False,
        )
        snapshot = SimpleNamespace(
            members=(
                StoredGateIncrementMember(
                    position=0,
                    user_id=10,
                    expected_gate_role_ids=(),
                    target_role_id=nhmisc.GATE_TIER_ROLE_IDS[0],
                    state=nhmisc.MemberState.COMPLETED,
                    failure_code=None,
                ),
            )
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._fetch_gate_increment_candidates = mock.AsyncMock(return_value=(candidate,))
        cog._validate_gate_increment_candidate_count = mock.Mock()
        cog._gate_increment_review_is_stale = mock.Mock(return_value=False)
        cog._gate_increment_store = SimpleNamespace(
            claim=mock.AsyncMock(return_value=SimpleNamespace(created=True))
        )
        cog._execute_gate_increment_operation = mock.AsyncMock(return_value=snapshot)
        cog._publish_gate_increment_result = mock.AsyncMock(return_value=True)
        cog._format_gate_increment_completion = mock.Mock(return_value="done")
        cog._finish_gate_increment_review = mock.AsyncMock()
        cog._send_moderation_log = mock.AsyncMock(side_effect=RuntimeError("offline"))
        cog._send_maintenance_log = mock.AsyncMock()
        cog._require_private_moderation_log_channel = mock.AsyncMock()

        with mock.patch.object(nhmisc, "_validate_gate_increment_configuration"):
            await cog._confirm_gate_increment_review(interaction, view)

        cog._publish_gate_increment_result.assert_awaited_once_with(source, snapshot)
        status = cog._finish_gate_increment_review.await_args.args[2]
        self.assertIn("moderation log", status.lower())

    async def test_missing_private_moderation_log_blocks_claim(self):
        interaction = self._interaction()
        guild = SimpleNamespace(id=1)
        source = SimpleNamespace(guild=guild, channel=SimpleNamespace(id=2), id=3)
        candidate = nhmisc.GateIncrementCandidate(
            10,
            "Player",
            (),
            None,
            nhmisc.GATE_TIER_ROLE_IDS[0],
            target_ordinal=1,
            highest_ordinal=0,
        )
        view = SimpleNamespace(
            source_message=source,
            selected_user_ids={10},
            solo_gater_enabled=False,
            render_embed=mock.Mock(return_value=object()),
        )
        claim = mock.AsyncMock()
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._fetch_gate_increment_candidates = mock.AsyncMock(return_value=(candidate,))
        cog._validate_gate_increment_candidate_count = mock.Mock()
        cog._gate_increment_review_is_stale = mock.Mock(return_value=False)
        cog._gate_increment_store = SimpleNamespace(claim=claim)
        cog._require_private_moderation_log_channel = mock.AsyncMock(
            side_effect=nhmisc.commands.UserFeedbackCheckFailure("Configure a private log")
        )

        with mock.patch.object(nhmisc, "_validate_gate_increment_configuration"):
            await cog._confirm_gate_increment_review(interaction, view)

        claim.assert_not_awaited()
        view.render_embed.assert_called_once_with(notice="Configure a private log")


class GateIncrementPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_red_user_deletion_reaches_gate_increment_storage(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog._activity_store = SimpleNamespace(
            delete_user_everywhere=mock.AsyncMock()
        )
        cog._sticky_roles = SimpleNamespace(
            delete_user_everywhere=mock.AsyncMock()
        )
        cog._role_analytics_store = SimpleNamespace(
            delete_user_everywhere=mock.AsyncMock()
        )
        cog._achievement_store = SimpleNamespace(
            delete_user_everywhere=mock.AsyncMock()
        )
        cog._gate_increment_store = SimpleNamespace(
            redact_user_data=mock.AsyncMock()
        )

        await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=42)

        cog._activity_store.delete_user_everywhere.assert_awaited_once_with(42)
        cog._sticky_roles.delete_user_everywhere.assert_awaited_once_with(42)
        cog._role_analytics_store.delete_user_everywhere.assert_awaited_once_with(42)
        cog._achievement_store.delete_user_everywhere.assert_awaited_once_with(42)
        cog._gate_increment_store.redact_user_data.assert_awaited_once_with(42)


class GateIncrementDocumentationTests(unittest.TestCase):
    def test_supported_entry_points_and_storage_are_disclosed(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "NHCogs" / "nhmisc" / "README.md").read_text(encoding="utf-8")
        info = json.loads(
            (root / "NHCogs" / "nhmisc" / "info.json").read_text(encoding="utf-8-sig")
        )

        self.assertNotIn("[p]gateincrement", readme)
        self.assertIn("Apps → Increment Gate roles", readme)
        self.assertIn("/achievements [user]", readme)
        self.assertIn("[p]achievement revoke", readme)
        self.assertIn("Gate tier from 1 through 6", readme)
        disclosure = info["end_user_data_statement"]
        self.assertIn("Achievements store", disclosure)
        self.assertIn("source message", disclosure)


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
