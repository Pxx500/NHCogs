from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sqlite3
import sys
import types
import unittest
from datetime import date, datetime, timezone
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


def _ensure_gate_increment_views():
    views = _load_gate_increment_views()
    package_name = nhmisc.__package__
    parent_name, _, _ = package_name.rpartition(".")
    nhmisc_dir = Path(nhmisc.__file__).parent
    if parent_name and parent_name not in sys.modules:
        parent = types.ModuleType(parent_name)
        parent.__path__ = [str(nhmisc_dir.parent)]
        sys.modules[parent_name] = parent
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        sys.modules[package_name] = package
    package.__path__ = [str(nhmisc_dir)]
    sys.modules[nhmisc.__name__] = nhmisc
    sys.modules[f"{package_name}.gate_increment_views"] = views
    return views


def _review_view(cog, source, candidates, **overrides):
    source.jump_url = getattr(source, "jump_url", "https://example.invalid/source")
    view = _load_gate_increment_views().GateIncrementReviewView(
        cog,
        source,
        99,
        candidates,
        custom_achievements=overrides.pop("custom_achievements", ()),
        ephemeral=True,
    )
    for name, value in overrides.items():
        setattr(view, name, value)
    return view


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


def _manageable_gate_guild(*, managed_role_id=None, manage_roles=True):
    roles = {
        role_id: SimpleNamespace(
            id=role_id,
            managed=role_id == managed_role_id,
            position=position,
        )
        for position, role_id in enumerate(nhmisc.GATE_TIER_ROLE_IDS, start=1)
    }
    return SimpleNamespace(
        id=1,
        me=SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=manage_roles),
            top_role=SimpleNamespace(position=len(roles) + 1),
        ),
        get_role=roles.get,
    )


class GateIncrementPlanningTests(unittest.TestCase):


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

    def test_custom_achievements_start_unselected_and_survive_recipient_changes(self):
        views = _load_gate_increment_views()
        first = nhmisc.GateIncrementCandidate(
            1, "one", (), None, nhmisc.GATE_TIER_ROLE_IDS[0]
        )
        second = nhmisc.GateIncrementCandidate(
            2, "two", (), None, nhmisc.GATE_TIER_ROLE_IDS[0]
        )
        achievement = SimpleNamespace(
            key="garden_of_grind",
            display_name="Garden of Grind",
            role_id=50,
        )
        view = views.GateIncrementReviewView(
            SimpleNamespace(),
            SimpleNamespace(jump_url="https://example.invalid/source"),
            42,
            (first, second),
            custom_achievements=(achievement,),
            ephemeral=True,
        )

        self.assertEqual(view.selected_custom_achievement_keys, set())
        self.assertFalse(view.achievement_select.options[0].default)
        view.selected_custom_achievement_keys = {"garden_of_grind"}

        view.replace_candidates((first,))

        self.assertEqual(
            view.selected_custom_achievement_keys,
            {"garden_of_grind"},
        )
        self.assertIn("Achievements: 1 selected", view.render_embed().description)


    def test_selected_definition_drift_requires_reconfirmation(self):
        preview = SimpleNamespace(
            key="garden_of_grind",
            display_name="Garden of Grind",
            role_id=50,
        )
        view = SimpleNamespace(
            custom_achievements=(preview,),
            selected_custom_achievement_keys={"garden_of_grind"},
        )
        changed = SimpleNamespace(
            key="garden_of_grind",
            display_name="Garden of Grind",
            role_id=51,
        )

        self.assertTrue(
            nhmisc.NHMisc._gate_increment_achievement_selection_is_stale(
                view, (changed,)
            )
        )
        self.assertFalse(
            nhmisc.NHMisc._gate_increment_achievement_selection_is_stale(
                view, (preview,)
            )
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

    def _insert_definitions(self, guild_id, *definitions):
        connection = sqlite3.connect(self.store._path)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS achievement_definitions (
                    guild_id INTEGER NOT NULL,
                    achievement_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    role_id INTEGER,
                    grantable INTEGER NOT NULL,
                    revocable INTEGER NOT NULL,
                    display_order INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, achievement_key)
                )
                """
            )
            connection.executemany(
                "INSERT INTO achievement_definitions VALUES (?, ?, ?, 'boolean', ?, 1, 1, ?)",
                (
                    (guild_id, key, name, role_id, position)
                    for position, (key, name, role_id) in enumerate(definitions)
                ),
            )
            connection.commit()
        finally:
            connection.close()

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

    async def test_custom_achievement_roles_are_applied_in_the_gate_edit(self):
        default_role = SimpleNamespace(id=0, position=0, managed=False)
        unrelated_role = SimpleNamespace(id=50, position=1, managed=False)
        gate_role = SimpleNamespace(
            id=nhmisc.GATE_TIER_ROLE_IDS[0], position=2, managed=False
        )
        achievement_role = SimpleNamespace(id=51, position=3, managed=False)
        self._insert_definitions(
            70,
            ("garden_of_grind", "Garden of Grind", achievement_role.id),
            ("flawless", "Flawless", None),
        )
        roles = {
            role.id: role
            for role in (default_role, unrelated_role, gate_role, achievement_role)
        }
        for position, role_id in enumerate(nhmisc.GATE_TIER_ROLE_IDS, start=2):
            roles.setdefault(
                role_id,
                SimpleNamespace(id=role_id, position=position, managed=False),
            )
        member = _EditableMember(
            60,
            (default_role, unrelated_role),
            top_role=unrelated_role,
        )
        guild = SimpleNamespace(
            id=70,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=100),
            ),
            default_role=default_role,
            get_role=roles.get,
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
            (nhmisc.GateIncrementMemberPlan(member.id, (), gate_role.id),),
            (
                nhmisc.GateIncrementAchievementPlan(
                    "garden_of_grind", "Garden of Grind", achievement_role.id
                ),
                nhmisc.GateIncrementAchievementPlan("flawless", "Flawless"),
            ),
        )

        result = await cog._execute_gate_increment_operation(source, 100)

        self.assertEqual(result.operation.state, nhmisc.OperationState.COMPLETED)
        self.assertEqual(len(member.edits), 1)
        self.assertEqual(
            {role.id for role in member.edits[0][0]},
            {unrelated_role.id, gate_role.id, achievement_role.id},
        )
        self.assertEqual(
            result.members[0].custom_achievement_keys,
            ("garden_of_grind", "flawless"),
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
        custom_role_id = 900
        self._insert_definitions(
            120,
            ("garden_of_grind", "Garden of Grind", custom_role_id),
            ("flawless", "Flawless", None),
        )
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
            (
                nhmisc.GateIncrementAchievementPlan(
                    "garden_of_grind", "Garden of Grind", custom_role_id
                ),
                nhmisc.GateIncrementAchievementPlan("flawless", "Flawless"),
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
            f"<@&{nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID}> "
            f"<@&{custom_role_id}> Flawless",
        )
        self.assertTrue(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.replied_user)
        self.assertEqual(
            [user.id for user in allowed_mentions.users],
            [124],
        )
        self.assertEqual(
            source.reply.await_args.kwargs["nonce"],
            f"gate-{snapshot.operation.operation_id}",
        )
        persisted = await self.store.get_operation(key)
        self.assertEqual(persisted.operation.result_message_id, 125)

    async def test_recovery_updates_the_single_existing_congratulations_message(self):
        key = nhmisc.SourceMessageKey(120, 121, 122)
        target_role_id = nhmisc.GATE_TIER_ROLE_IDS[0]
        self._insert_definitions(120, ("flawless", "Flawless", None))
        await self.store.claim(
            key,
            123,
            (
                nhmisc.GateIncrementMemberPlan(124, (), target_role_id),
                nhmisc.GateIncrementMemberPlan(125, (), target_role_id),
            ),
            (nhmisc.GateIncrementAchievementPlan("flawless", "Flawless"),),
        )
        await self.store.mark_member_completed(key, 0)
        await self.store.mark_member_failed(key, 1, "discord_error")
        partial = await self.store.finalize_operation(key)
        result = SimpleNamespace(
            id=126,
            channel=SimpleNamespace(id=121),
            edit=mock.AsyncMock(),
        )
        channel = SimpleNamespace(
            id=121,
            get_partial_message=mock.Mock(return_value=result),
        )
        source = SimpleNamespace(
            id=122,
            channel=channel,
            reply=mock.AsyncMock(return_value=result),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._gate_increment_store = self.store

        with mock.patch.object(
            nhmisc.discord,
            "AllowedMentions",
            side_effect=SimpleNamespace,
        ):
            self.assertTrue(
                await cog._publish_gate_increment_result(source, partial)
            )
            await self.store.mark_member_completed(key, 1)
            completed = await self.store.finalize_operation(key)
            self.assertTrue(
                await cog._publish_gate_increment_result(source, completed)
            )

        source.reply.assert_awaited_once()
        channel.get_partial_message.assert_called_once_with(result.id)
        result.edit.assert_awaited_once()
        updated_content = result.edit.await_args.kwargs["content"]
        self.assertIn("<@124>", updated_content)
        self.assertIn("<@125>", updated_content)
        self.assertEqual(updated_content.count("🎉 **Congratulations!**"), 1)

    async def test_recovery_logs_only_newly_completed_members(self):
        key = nhmisc.SourceMessageKey(120, 121, 122)
        target_role_id = nhmisc.GATE_TIER_ROLE_IDS[0]
        await self.store.claim(
            key,
            123,
            (
                nhmisc.GateIncrementMemberPlan(124, (), target_role_id),
                nhmisc.GateIncrementMemberPlan(125, (), target_role_id),
            ),
        )
        await self.store.mark_member_completed(key, 0)
        await self.store.mark_member_failed(key, 1, "discord_error")
        partial = await self.store.finalize_operation(key)
        source = SimpleNamespace(
            id=122,
            guild=SimpleNamespace(id=120),
            channel=SimpleNamespace(id=121),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._gate_increment_store = self.store
        cog._send_moderation_log = mock.AsyncMock(return_value=True)

        self.assertTrue(
            await cog._publish_gate_increment_moderation_log(source, 123, partial)
        )
        await self.store.mark_member_completed(key, 1)
        completed = await self.store.finalize_operation(key)
        self.assertTrue(
            await cog._publish_gate_increment_moderation_log(
                source, 123, completed
            )
        )

        self.assertEqual(cog._send_moderation_log.await_count, 2)
        first_log = cog._send_moderation_log.await_args_list[0].args[1]
        second_log = cog._send_moderation_log.await_args_list[1].args[1]
        self.assertIn("<@124>", first_log)
        self.assertNotIn("<@125>", first_log)
        self.assertNotIn("<@124>", second_log)
        self.assertIn("<@125>", second_log)


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
        source = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=2),
            id=3,
        )
        candidate = nhmisc.GateIncrementCandidate(
            10, "Player", (), None, nhmisc.GATE_TIER_ROLE_IDS[0]
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(
            side_effect=nhmisc.commands.UserFeedbackCheckFailure("confirm failed")
        )
        view = _review_view(cog, source, (candidate,))

        await view.confirm.callback(interaction)

        edited = interaction.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("confirm failed", edited.description)
        self.assertIn("Gate increment review", edited.title)

    async def test_confirmation_definition_failure_uses_operational_error_handler(self):
        interaction = self._interaction()
        source = SimpleNamespace(
            guild=_manageable_gate_guild(),
            channel=SimpleNamespace(id=2),
            id=3,
        )
        candidate = nhmisc.GateIncrementCandidate(
            10, "Player", (), None, nhmisc.GATE_TIER_ROLE_IDS[0]
        )
        error = RuntimeError("definitions unavailable")
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(side_effect=error),
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._fetch_gate_increment_candidates = mock.AsyncMock(
            return_value=(candidate,)
        )
        cog._validate_gate_increment_candidate_count = mock.Mock()
        cog._require_private_moderation_log_channel = mock.AsyncMock()
        cog._handle_achievement_interaction_failure = mock.AsyncMock()
        view = _review_view(cog, source, (candidate,))

        await view.confirm.callback(interaction)

        cog._handle_achievement_interaction_failure.assert_awaited_once_with(
            interaction,
            "confirm gate increment review",
            error,
            public_defer=False,
        )

    async def test_successful_confirm_emits_one_moderation_log(self):
        interaction = self._interaction()
        source = SimpleNamespace(
            guild=_manageable_gate_guild(),
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
        definition = SimpleNamespace(
            key="garden_of_grind",
            display_name="Garden of Grind",
            role_id=50,
            grantable=True,
        )
        snapshot = SimpleNamespace(
            operation=SimpleNamespace(key=nhmisc.SourceMessageKey(1, 2, 3)),
            custom_achievements=(
                nhmisc.GateIncrementAchievementPlan(
                    "garden_of_grind", "Garden of Grind", 50
                ),
            ),
            members=(
                StoredGateIncrementMember(
                    position=0,
                    user_id=10,
                    expected_gate_role_ids=(),
                    target_role_id=nhmisc.GATE_TIER_ROLE_IDS[0],
                    state=nhmisc.MemberState.COMPLETED,
                    failure_code=None,
                    custom_achievement_keys=("garden_of_grind",),
                ),
            )
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            get_profile=mock.AsyncMock(
                return_value=SimpleNamespace(boolean_keys=())
            ),
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._fetch_gate_increment_candidates = mock.AsyncMock(
            return_value=(candidate,)
        )
        cog._validate_gate_increment_candidate_count = mock.Mock()
        cog._gate_increment_store = SimpleNamespace(
            claim=mock.AsyncMock(return_value=SimpleNamespace(created=True)),
            mark_moderation_logged=mock.AsyncMock(),
        )
        cog._execute_gate_increment_operation = mock.AsyncMock(return_value=snapshot)
        cog._publish_gate_increment_result = mock.AsyncMock(return_value=True)
        cog._format_gate_increment_completion = mock.Mock(return_value="done")
        cog._finish_gate_increment_review = mock.AsyncMock()
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        cog._require_private_moderation_log_channel = mock.AsyncMock()
        view = _review_view(
            cog,
            source,
            (candidate,),
            custom_achievements=(definition,),
            selected_custom_achievement_keys={"garden_of_grind"},
        )

        await view.confirm.callback(interaction)

        cog._send_moderation_log.assert_awaited_once()
        claimed_achievements = cog._gate_increment_store.claim.await_args.args[3]
        self.assertEqual(
            claimed_achievements,
            (
                nhmisc.GateIncrementAchievementPlan(
                    "garden_of_grind", "Garden of Grind", 50
                ),
            ),
        )
        audit = cog._send_moderation_log.await_args.args[1]
        self.assertIn("Gate incremented", audit)
        self.assertIn("<@10> Gate 1", audit)
        self.assertIn("<@&50>", audit)
        self.assertIn("https://discord.com/channels/1/2/3", audit)

    async def test_moderation_log_failure_does_not_block_congratulations(self):
        interaction = self._interaction()
        source = SimpleNamespace(
            guild=_manageable_gate_guild(),
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
        snapshot = SimpleNamespace(
            custom_achievements=(),
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
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=()),
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._fetch_gate_increment_candidates = mock.AsyncMock(return_value=(candidate,))
        cog._validate_gate_increment_candidate_count = mock.Mock()
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
        view = _review_view(cog, source, (candidate,))

        await view.confirm.callback(interaction)

        cog._publish_gate_increment_result.assert_awaited_once_with(source, snapshot)
        status = cog._finish_gate_increment_review.await_args.args[2]
        self.assertIn("moderation log", status.lower())

    async def test_missing_private_moderation_log_blocks_claim(self):
        interaction = self._interaction()
        source = SimpleNamespace(
            guild=_manageable_gate_guild(),
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
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=()),
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._fetch_gate_increment_candidates = mock.AsyncMock(return_value=(candidate,))
        cog._validate_gate_increment_candidate_count = mock.Mock()
        cog._require_private_moderation_log_channel = mock.AsyncMock(
            side_effect=nhmisc.commands.UserFeedbackCheckFailure("Configure a private log")
        )
        view = _review_view(cog, source, (candidate,))

        with TemporaryDirectory() as directory:
            cog._gate_increment_store = nhmisc.GateIncrementStore(
                Path(directory) / "gates.sqlite"
            )
            await cog._gate_increment_store.initialize()
            await view.confirm.callback(interaction)

            edited = interaction.edit_original_response.await_args.kwargs["embed"]
            self.assertIn("Configure a private log", edited.description)
            self.assertIsNone(
                await cog._gate_increment_store.get_operation(
                    nhmisc.SourceMessageKey(1, 2, 3)
                )
            )


class GateIncrementReviewOutcomeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _interaction():
        return GateIncrementReviewCallbackTests._interaction()

    async def test_review_lists_author_then_mentions_without_bots_or_duplicates(self):
        _ensure_gate_increment_views()
        author = _member(1)
        mentioned = _member(2)
        bot = _member(3, bot=True)
        members = {1: author, 2: mentioned, 3: bot}

        async def fetch_member(user_id):
            member = members.get(user_id)
            if member is None:
                raise nhmisc.discord.NotFound()
            return member

        guild = _manageable_gate_guild()
        guild.fetch_member = fetch_member
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=()),
            get_active_stargates=mock.AsyncMock(return_value=()),
        )
        source = SimpleNamespace(
            guild=guild,
            author=author,
            webhook_id=None,
            raw_mentions=(2, 1, 2, 3, 4),
            channel=SimpleNamespace(id=2),
            id=3,
            jump_url="https://example.invalid/source",
        )

        view = await cog._create_gate_increment_review(source, 99, ephemeral=True)

        self.assertEqual(view.candidate_ids, (1, 2))
        description = view.render_embed().description
        self.assertLess(description.index("<@1>"), description.index("<@2>"))
        self.assertNotIn("<@3>", description)
        self.assertNotIn("<@4>", description)

        source.webhook_id = 50
        source.author = _member(8)
        source.raw_mentions = (2,)
        webhook_view = await cog._create_gate_increment_review(
            source, 99, ephemeral=True
        )
        webhook_description = webhook_view.render_embed().description
        self.assertEqual(webhook_view.candidate_ids, (2,))
        self.assertIn("<@2>", webhook_description)
        self.assertNotIn("<@8>", webhook_description)

    async def test_unmanageable_gate_ladder_rejects_confirmation(self):
        interaction = self._interaction()
        source = SimpleNamespace(
            guild=_manageable_gate_guild(
                managed_role_id=nhmisc.GATE_TIER_ROLE_IDS[2]
            ),
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
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        view = _review_view(cog, source, (candidate,))

        await view.confirm.callback(interaction)

        edited = interaction.edit_original_response.await_args.kwargs["embed"]
        self.assertIn("Gate roles are configured incorrectly", edited.description)

    async def test_only_selected_role_drift_blocks_confirmation(self):
        first = nhmisc.GateIncrementCandidate(
            1,
            "one",
            (),
            None,
            nhmisc.GATE_TIER_ROLE_IDS[0],
            target_ordinal=1,
            highest_ordinal=0,
        )
        second = nhmisc.GateIncrementCandidate(
            2,
            "two",
            (nhmisc.GATE_TIER_ROLE_IDS[0],),
            1,
            nhmisc.GATE_TIER_ROLE_IDS[1],
            target_ordinal=2,
            highest_ordinal=1,
        )
        source = SimpleNamespace(
            guild=_manageable_gate_guild(),
            channel=SimpleNamespace(id=2),
            id=3,
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=()),
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._require_private_moderation_log_channel = mock.AsyncMock()
        cog._execute_gate_increment_operation = mock.AsyncMock(
            return_value=SimpleNamespace(
                operation=SimpleNamespace(key=nhmisc.SourceMessageKey(1, 2, 3)),
                custom_achievements=(),
                members=(),
            )
        )
        cog._publish_gate_increment_result = mock.AsyncMock(return_value=True)
        cog._format_gate_increment_completion = mock.Mock(return_value="done")
        cog._finish_gate_increment_review = mock.AsyncMock()
        unselected_changed = nhmisc.GateIncrementCandidate(
            2,
            "two",
            (nhmisc.GATE_TIER_ROLE_IDS[1],),
            2,
            nhmisc.GATE_TIER_ROLE_IDS[2],
            target_ordinal=3,
            highest_ordinal=2,
        )
        selected_changed = nhmisc.GateIncrementCandidate(
            1,
            "one",
            (nhmisc.GATE_TIER_ROLE_IDS[0],),
            1,
            nhmisc.GATE_TIER_ROLE_IDS[1],
            target_ordinal=2,
            highest_ordinal=1,
        )

        with TemporaryDirectory() as directory:
            cog._gate_increment_store = nhmisc.GateIncrementStore(
                Path(directory) / "gates.sqlite"
            )
            await cog._gate_increment_store.initialize()
            cog._fetch_gate_increment_candidates = mock.AsyncMock(
                return_value=(first, unselected_changed)
            )
            view = _review_view(cog, source, (first, second))
            view.selected_user_ids = {1}

            await view.confirm.callback(self._interaction())

            claimed = await cog._gate_increment_store.get_operation(
                nhmisc.SourceMessageKey(1, 2, 3)
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(tuple(member.user_id for member in claimed.members), (1,))

        with TemporaryDirectory() as directory:
            cog._gate_increment_store = nhmisc.GateIncrementStore(
                Path(directory) / "drift.sqlite"
            )
            await cog._gate_increment_store.initialize()
            cog._fetch_gate_increment_candidates = mock.AsyncMock(
                return_value=(selected_changed, second)
            )
            interaction = self._interaction()
            view = _review_view(cog, source, (first, second))
            view.selected_user_ids = {1}

            await view.confirm.callback(interaction)

            edited = interaction.edit_original_response.await_args.kwargs["embed"]
            self.assertIn("member roles changed", edited.description)
            self.assertIsNone(
                await cog._gate_increment_store.get_operation(
                    nhmisc.SourceMessageKey(1, 2, 3)
                )
            )

    async def test_review_excludes_system_achievements_and_rejects_more_than_25(self):
        _ensure_gate_increment_views()
        author = _member(1)

        async def fetch_member(_user_id):
            return author

        guild = _manageable_gate_guild()
        guild.fetch_member = fetch_member
        definitions = (
            SimpleNamespace(
                key="stargate_completed",
                display_name="Stargate",
                role_id=None,
                grantable=True,
            ),
            SimpleNamespace(
                key="solo_gater",
                display_name="Solo",
                role_id=None,
                grantable=True,
            ),
            SimpleNamespace(
                key="garden_of_grind",
                display_name="Garden of Grind",
                role_id=50,
                grantable=True,
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=definitions),
            get_active_stargates=mock.AsyncMock(return_value=()),
        )
        source = SimpleNamespace(
            guild=guild,
            author=author,
            webhook_id=None,
            raw_mentions=(),
            channel=SimpleNamespace(id=2),
            id=3,
            jump_url="https://example.invalid/source",
        )

        view = await cog._create_gate_increment_review(source, 99, ephemeral=True)

        self.assertEqual(
            tuple(item.key for item in view.custom_achievements),
            ("garden_of_grind",),
        )
        self.assertEqual(
            tuple(option.value for option in view.achievement_select.options),
            ("garden_of_grind",),
        )

        cog._achievement_store.list_definitions = mock.AsyncMock(
            return_value=tuple(
                SimpleNamespace(
                    key=f"custom_{index}",
                    display_name=f"Custom {index}",
                    role_id=None,
                    grantable=True,
                )
                for index in range(26)
            )
        )
        cog._gate_increment_store = SimpleNamespace(
            get_operation=mock.AsyncMock(return_value=None)
        )
        interaction = self._interaction()

        await cog._gate_increment_context_action_after_defer(interaction, source)

        self.assertIn(
            "at most 25",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_oversized_plan_is_rejected_without_a_claim_row(self):
        user_ids = tuple(100000000000000000 + index for index in range(4))
        candidates = tuple(
            nhmisc.GateIncrementCandidate(
                user_id,
                f"member-{user_id}",
                (),
                None,
                nhmisc.GATE_TIER_ROLE_IDS[0],
                target_ordinal=1,
                highest_ordinal=0,
            )
            for user_id in user_ids
        )
        achievements = tuple(
            SimpleNamespace(
                key=f"custom_{index}",
                display_name=f"Custom {index}",
                role_id=200000000000000000 + index,
                grantable=True,
            )
            for index in range(25)
        )
        source = SimpleNamespace(
            guild=_manageable_gate_guild(),
            channel=SimpleNamespace(id=2),
            id=3,
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=achievements),
            get_profile=mock.AsyncMock(
                return_value=SimpleNamespace(boolean_keys=())
            ),
        )
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._fetch_gate_increment_candidates = mock.AsyncMock(
            return_value=candidates
        )
        cog._require_private_moderation_log_channel = mock.AsyncMock()
        view = _review_view(
            cog,
            source,
            candidates,
            custom_achievements=achievements,
            selected_custom_achievement_keys={item.key for item in achievements},
        )
        interaction = self._interaction()

        with TemporaryDirectory() as directory:
            cog._gate_increment_store = nhmisc.GateIncrementStore(
                Path(directory) / "gates.sqlite"
            )
            await cog._gate_increment_store.initialize()

            await view.confirm.callback(interaction)

            edited = interaction.edit_original_response.await_args.kwargs["embed"]
            self.assertIn("too large for one Discord message", edited.description)
            self.assertIsNone(
                await cog._gate_increment_store.get_operation(
                    nhmisc.SourceMessageKey(1, 2, 3)
                )
            )


class GateIncrementPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_red_user_deletion_reaches_gate_increment_storage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cog = object.__new__(nhmisc.NHMisc)
            cog._activity_store = nhmisc.ActivityStore(root / "activity.sqlite")
            cog._sticky_roles = nhmisc.StickyRoleStore(root / "sticky.sqlite")
            cog._role_analytics_store = nhmisc.RoleAnalyticsStore(root / "roles.sqlite")
            cog._achievement_store = nhmisc.AchievementStore(root / "achievements.sqlite")
            cog._gate_increment_store = nhmisc.GateIncrementStore(
                root / "achievements.sqlite"
            )
            for store in (
                cog._activity_store,
                cog._sticky_roles,
                cog._role_analytics_store,
                cog._achievement_store,
                cog._gate_increment_store,
            ):
                await store.initialize()
            kept = nhmisc.SourceMessageKey(10, 20, 99)
            removed = nhmisc.SourceMessageKey(10, 20, 42)
            await cog._gate_increment_store.claim(
                removed, 42, (nhmisc.GateIncrementMemberPlan(42, (), 8),)
            )
            await cog._gate_increment_store.claim(
                kept, 99, (nhmisc.GateIncrementMemberPlan(99, (), 9),)
            )
            await cog._activity_store.record_message(
                guild_id=10,
                date_utc=date(2026, 7, 26),
                hour_utc=12,
                user_id=42,
                channel_id=100,
                thread_id=None,
                now_utc=datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
            )

            await cog.red_delete_data_for_user(
                requester="discord_deleted_user", user_id=42
            )

            removed_operation = await cog._gate_increment_store.get_operation(removed)
            kept_operation = await cog._gate_increment_store.get_operation(kept)
            self.assertIsNone(removed_operation.operation.moderator_id)
            self.assertIsNone(removed_operation.members[0].user_id)
            self.assertEqual(kept_operation.operation.moderator_id, 99)
            self.assertEqual(kept_operation.members[0].user_id, 99)
            pending_awards = _pending_stargate_user_ids(root / "achievements.sqlite")
            self.assertNotIn(42, pending_awards)
            self.assertIn(99, pending_awards)
            stats = await cog._activity_store.get_user_stats(10, 42, date(2026, 7, 26), 7)
            self.assertEqual(stats.total_messages, 0)


class GateIncrementRecoveryReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_operation_read_failure_reaches_private_reporter(self):
        failure = OSError("sqlite failed")
        cog = object.__new__(nhmisc.NHMisc)
        cog._gate_increment_store = SimpleNamespace(
            list_interrupted_operations=mock.AsyncMock(side_effect=failure)
        )
        cog.bot = SimpleNamespace(guilds=(SimpleNamespace(id=100),))
        cog.report_operational_error = mock.AsyncMock()

        await cog._recover_interrupted_gate_increments()

        cog.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="NHMisc",
            action="read interrupted Gate increments",
            error=failure,
        )


def _pending_stargate_user_ids(path):
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            SELECT user_id FROM achievement_awards
            WHERE state = 'pending' AND achievement_key = 'stargate_completed'
            """
        ).fetchall()
    return {row[0] for row in rows}


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
