import asyncio
import contextlib
import csv
import io
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

from tests.test_gate_proof_flow import _load_achievement_views
from tests.test_gatecount import nhmisc


class _CapturedFile:
    def __init__(self, fp, *, filename):
        self.data = fp.read()
        self.filename = filename


class AchievementProfileRenderingTests(unittest.TestCase):
    def test_profile_orders_gate_proofs_before_boolean_achievements(self):
        profile = nhmisc.AchievementProfile(
            stargate_count=4,
            stargate_proofs=(
                SimpleNamespace(
                    ordinal=1,
                    source_channel_id=20,
                    source_message_id=21,
                ),
                SimpleNamespace(
                    ordinal=4,
                    source_channel_id=20,
                    source_message_id=30,
                ),
            ),
            boolean_keys=("solo_gater",),
        )

        embed = nhmisc.NHMisc._build_achievements_embed(
            10,
            SimpleNamespace(display_name="Player"),
            profile,
            (nhmisc.SOLO_GATER_DEFINITION,),
        )

        self.assertEqual(
            embed.description,
            "Stargates: 4\n"
            "[Stargate 1](https://discord.com/channels/10/20/21) · "
            "[Stargate 4](https://discord.com/channels/10/20/30)",
        )
        self.assertEqual(
            tuple(field.name for field in embed.fields),
            ("Achievements",),
        )
        self.assertEqual(embed.fields[0].value, "Solo Gater")
        self.assertNotIn("Proof", embed.description)

    def test_empty_profile_is_explicit(self):
        profile = nhmisc.AchievementProfile(0, (), ())

        embed = nhmisc.NHMisc._build_achievements_embed(
            10,
            SimpleNamespace(display_name="Player"),
            profile,
            (),
        )

        self.assertEqual(embed.description, "No achievements recorded")


class AchievementGrantViewTests(unittest.TestCase):
    def test_achievements_start_unselected(self):
        views, _fake_select = _load_achievement_views()
        candidates = (SimpleNamespace(id=10, display_name="Player"),)
        definitions = (nhmisc.SOLO_GATER_DEFINITION,)

        view = views.AchievementGrantView(
            SimpleNamespace(),
            SimpleNamespace(jump_url="https://discord.com/channels/1/2/3"),
            99,
            candidates,
            definitions,
        )

        self.assertEqual(view.selected_user_ids, {10})
        self.assertEqual(view.selected_keys, set())
        self.assertTrue(view.confirm.disabled)
        self.assertFalse(view.achievement_select.options[0].default)


class AchievementWorkflowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _interaction(guild):
        return SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
            delete_original_response=mock.AsyncMock(),
        )

    async def test_reconciliation_ignores_departed_achievement_members(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="solo_gater",
            display_name="Solo Gater",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=123,
        )
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            list_gate_projections=mock.AsyncMock(return_value={10: 1}),
            projected_users_for_boolean=mock.AsyncMock(return_value=(10,)),
        )
        guild = SimpleNamespace(
            id=1,
            fetch_member=mock.AsyncMock(side_effect=nhmisc.discord.NotFound()),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=tuple(() for _ in (*nhmisc.GATE_TIER_ROLE_IDS, 123))
        )
        cog._restore_gate_projection = mock.AsyncMock()
        cog._edit_achievement_roles = mock.AsyncMock()
        cog._send_maintenance_log = mock.AsyncMock(return_value=True)

        await cog._reconcile_achievement_roles_for_guild(guild)

        self.assertEqual(guild.fetch_member.await_count, 2)
        cog._restore_gate_projection.assert_not_awaited()
        cog._edit_achievement_roles.assert_not_awaited()
        cog._send_maintenance_log.assert_not_awaited()

    async def test_reconciliation_reports_non_not_found_failures(self):
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=()),
            list_gate_projections=mock.AsyncMock(return_value={10: 1}),
        )
        guild = SimpleNamespace(
            id=1,
            fetch_member=mock.AsyncMock(side_effect=nhmisc.discord.Forbidden()),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=tuple(() for _ in nhmisc.GATE_TIER_ROLE_IDS)
        )
        cog._restore_gate_projection = mock.AsyncMock()
        cog._send_maintenance_log = mock.AsyncMock(return_value=True)

        with mock.patch.object(nhmisc.log, "exception"):
            await cog._reconcile_achievement_roles_for_guild(guild)

        cog._send_maintenance_log.assert_awaited_once()
        self.assertIn(
            "Members skipped: 1",
            cog._send_maintenance_log.await_args.args[1],
        )

    async def test_successful_achievement_create_emits_one_moderation_log(self):
        guild = SimpleNamespace(id=1)
        ctx = SimpleNamespace(
            guild=guild,
            author=SimpleNamespace(id=99),
            send=mock.AsyncMock(),
        )
        definition = SimpleNamespace(
            key="all_quests",
            display_name="All Quests",
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            create_boolean_definition=mock.AsyncMock(return_value=definition),
        )
        cog._send_moderation_log = mock.AsyncMock(return_value=True)

        await nhmisc.NHMisc.achievement_create(
            cog,
            ctx,
            display_name="All Quests",
        )

        cog._send_moderation_log.assert_awaited_once()
        logged_guild, content = cog._send_moderation_log.await_args.args
        self.assertIs(logged_guild, guild)
        self.assertIn("Achievement created", content)
        self.assertIn("Moderator: <@99>", content)
        self.assertIn("Key: `all_quests`", content)

    async def _assert_deferred_before_store_wait(
        self,
        callback,
        *,
        ephemeral,
    ):
        store_started = asyncio.Event()
        release_store = asyncio.Event()
        deferred = asyncio.Event()
        events = []

        async def blocked_is_bootstrapped(_guild_id):
            events.append("store")
            store_started.set()
            await release_store.wait()
            return True

        async def defer(**_kwargs):
            events.append("defer")
            deferred.set()

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=99),
            permissions=SimpleNamespace(manage_messages=True),
            response=SimpleNamespace(defer=mock.AsyncMock(side_effect=defer)),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(side_effect=blocked_is_bootstrapped)
        )
        task = asyncio.create_task(callback(cog, interaction))
        try:
            await asyncio.wait_for(deferred.wait(), timeout=0.1)
            self.assertEqual(events[0], "defer")
            interaction.response.defer.assert_awaited_once_with(
                ephemeral=ephemeral,
                thinking=True,
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_achievements_slash_defers_before_waiting_for_store(self):
        await self._assert_deferred_before_store_wait(
            lambda cog, interaction: cog._achievements_slash(interaction),
            ephemeral=True,
        )

    async def test_achievements_slash_ignores_an_expired_initial_interaction(self):
        class UnknownInteraction(Exception):
            code = 10062

        store = SimpleNamespace(is_bootstrapped=mock.AsyncMock())
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(
                defer=mock.AsyncMock(side_effect=UnknownInteraction())
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store

        with mock.patch.object(
            nhmisc.discord,
            "NotFound",
            UnknownInteraction,
            create=True,
        ):
            await cog._achievements_slash(interaction)

        store.is_bootstrapped.assert_not_awaited()

    async def test_achievements_slash_does_not_hide_other_not_found_errors(self):
        class MissingResource(Exception):
            code = 10003

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(
                defer=mock.AsyncMock(side_effect=MissingResource())
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)

        with (
            mock.patch.object(
                nhmisc.discord,
                "NotFound",
                MissingResource,
                create=True,
            ),
            self.assertRaises(MissingResource),
        ):
            await cog._achievements_slash(interaction)

    async def test_achievement_interactions_do_not_emit_routine_info_logs(self):
        guild = SimpleNamespace(id=1)
        target = SimpleNamespace(id=123)
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            get_profile=mock.AsyncMock(return_value=object()),
            list_definitions=mock.AsyncMock(return_value=()),
        )
        cog._build_achievements_embed = mock.Mock(return_value=object())

        with mock.patch.object(nhmisc.log, "info") as info:
            await cog._achievements_slash(interaction, target)

        info.assert_not_called()

    async def test_achievements_user_action_defers_before_waiting_for_store(self):
        target = SimpleNamespace(id=123)
        await self._assert_deferred_before_store_wait(
            lambda cog, interaction: cog._achievements_user_context_action(
                interaction,
                target,
            ),
            ephemeral=True,
        )

    async def test_grant_action_defers_before_waiting_for_store(self):
        source_message = SimpleNamespace()
        await self._assert_deferred_before_store_wait(
            lambda cog, interaction: cog._grant_achievements_context_action(
                interaction,
                source_message,
            ),
            ephemeral=True,
        )

    async def test_achievements_slash_edits_private_response_with_publish_view(self):
        guild = SimpleNamespace(id=1)
        target = SimpleNamespace(id=123)
        profile = object()
        definitions = (object(),)
        embed = object()
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=99),
            data={"id": "456"},
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            get_profile=mock.AsyncMock(return_value=profile),
            list_definitions=mock.AsyncMock(return_value=definitions),
        )
        cog._build_achievements_embed = mock.Mock(return_value=embed)

        class FakeAchievementProfileView:
            def __init__(self, embed, requester_id, command_mention):
                self.embed = embed
                self.requester_id = requester_id
                self.command_mention = command_mention

        package = ModuleType("_gatecount_nhmisc")
        package.__path__ = []
        views = ModuleType("_gatecount_nhmisc.achievement_views")
        views.AchievementProfileView = FakeAchievementProfileView

        with mock.patch.dict(
            sys.modules,
            {
                "_gatecount_nhmisc": package,
                "_gatecount_nhmisc.achievement_views": views,
            },
        ):
            await cog._achievements_slash(interaction, target)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        interaction.edit_original_response.assert_awaited_once()
        self.assertIs(
            interaction.edit_original_response.await_args.kwargs["embed"],
            embed,
        )
        view = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertIs(view.embed, embed)
        self.assertEqual(view.requester_id, 99)
        self.assertEqual(view.command_mention, "</achievements:456>")
        cog._achievement_store.get_profile.assert_awaited_once_with(1, 123)
        cog._achievement_store.list_definitions.assert_awaited_once_with(1)

    async def test_achievements_user_action_uses_publish_view_with_fallback_mention(self):
        guild = SimpleNamespace(id=1)
        target = SimpleNamespace(id=123)
        profile = object()
        definitions = (object(),)
        embed = object()
        interaction = SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            get_profile=mock.AsyncMock(return_value=profile),
            list_definitions=mock.AsyncMock(return_value=definitions),
        )
        cog._build_achievements_embed = mock.Mock(return_value=embed)

        class FakeAchievementProfileView:
            def __init__(self, embed, requester_id, command_mention):
                self.embed = embed
                self.requester_id = requester_id
                self.command_mention = command_mention

        package = ModuleType("_gatecount_nhmisc")
        package.__path__ = []
        views = ModuleType("_gatecount_nhmisc.achievement_views")
        views.AchievementProfileView = FakeAchievementProfileView

        with mock.patch.dict(
            sys.modules,
            {
                "_gatecount_nhmisc": package,
                "_gatecount_nhmisc.achievement_views": views,
            },
        ):
            await cog._achievements_user_context_action(interaction, target)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        interaction.edit_original_response.assert_awaited_once()
        self.assertIs(
            interaction.edit_original_response.await_args.kwargs["embed"],
            embed,
        )
        view = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertIs(view.embed, embed)
        self.assertEqual(view.requester_id, 99)
        self.assertEqual(view.command_mention, "`/achievements`")
        cog._achievement_store.get_profile.assert_awaited_once_with(1, 123)
        cog._achievement_store.list_definitions.assert_awaited_once_with(1)

    async def test_achievements_slash_timeout_finishes_with_private_error(self):
        async def blocked_is_bootstrapped(_guild_id):
            await asyncio.Event().wait()

        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(side_effect=blocked_is_bootstrapped)
        )

        with (
            mock.patch.object(
                nhmisc,
                "ACHIEVEMENT_INTERACTION_DB_TIMEOUT_SECONDS",
                0.01,
            ),
            mock.patch.object(nhmisc.log, "error") as log_error,
        ):
            await cog._achievements_slash(interaction)

        interaction.edit_original_response.assert_awaited_once_with(
            content="Achievement data is busy. Try again in a moment"
        )
        log_error.assert_not_called()

    async def test_interaction_failure_logs_only_when_discord_error_cannot_be_sent(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(id=99),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._send_achievement_interaction_error = mock.AsyncMock(
            side_effect=RuntimeError("Discord unavailable")
        )

        with mock.patch.object(nhmisc.log, "error") as log_error:
            await cog._handle_achievement_interaction_failure(
                interaction,
                "test action",
                RuntimeError("database failed"),
                public_defer=False,
            )

        log_error.assert_called_once()

    async def test_startup_requests_initialization_without_importing_roles(self):
        guild = SimpleNamespace(id=1)
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=False),
            bootstrap_guild=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = SimpleNamespace(
            wait_until_ready=mock.AsyncMock(),
            guilds=(guild,),
        )
        cog._role_analytics = SimpleNamespace(
            reconcile_enabled_guilds=mock.AsyncMock()
        )
        cog._achievement_store = store
        cog._send_maintenance_log = mock.AsyncMock(return_value=True)

        await cog._role_analytics_startup_reconcile()

        store.bootstrap_guild.assert_not_awaited()
        cog._send_maintenance_log.assert_awaited_once_with(
            guild,
            "Achievement initialization is required\n\n"
            "The achievement database has not been initialized from the current "
            "Discord roles.\n"
            "Run `!rolesync discord`.",
        )

    async def test_resume_refreshes_analytics_then_restores_database_roles(self):
        guild = SimpleNamespace(id=1)
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = SimpleNamespace(guilds=(guild,))
        cog._role_analytics = SimpleNamespace(
            reconcile_enabled_guilds=mock.AsyncMock()
        )
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._reconcile_achievement_roles_for_guild = mock.AsyncMock()

        await cog.on_resumed()

        cog._role_analytics.reconcile_enabled_guilds.assert_awaited_once_with(
            (guild,)
        )
        cog._reconcile_achievement_roles_for_guild.assert_awaited_once_with(guild)

    async def test_daily_refresh_restores_database_roles_after_analytics(self):
        guild = SimpleNamespace(id=1)
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = SimpleNamespace(
            guilds=(guild,),
            wait_until_ready=mock.AsyncMock(),
        )
        cog._role_analytics = SimpleNamespace(
            run_daily_reconciliation=mock.AsyncMock()
        )
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True)
        )
        cog._reconcile_achievement_roles_for_guild = mock.AsyncMock()

        with mock.patch.object(
            nhmisc.asyncio,
            "sleep",
            new=mock.AsyncMock(side_effect=(None, nhmisc.asyncio.CancelledError())),
        ):
            with self.assertRaises(nhmisc.asyncio.CancelledError):
                await cog._role_analytics_daily_loop()

        cog._role_analytics.run_daily_reconciliation.assert_awaited_once_with(
            (guild,)
        )
        cog._reconcile_achievement_roles_for_guild.assert_awaited_once_with(guild)

    async def test_role_bind_uses_role_id_then_achievement_dropdown(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=(definition,)),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=((10, 11),)
        )
        role = SimpleNamespace(id=123, mention="<@&123>")
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=99),
            send=mock.AsyncMock(return_value=SimpleNamespace()),
        )

        class FakeAchievementRoleBindView:
            def __init__(
                self,
                cog,
                opener_id,
                role,
                holder_ids,
                definitions,
            ):
                self.cog = cog
                self.opener_id = opener_id
                self.role = role
                self.holder_ids = holder_ids
                self.definitions = definitions
                self.achievement_select = SimpleNamespace(
                    options=tuple(
                        SimpleNamespace(label=item.display_name)
                        for item in definitions
                    )
                )

            def render_embed(self):
                return SimpleNamespace()

        package = ModuleType("_gatecount_nhmisc")
        package.__path__ = []
        views = ModuleType("_gatecount_nhmisc.achievement_views")
        views.AchievementRoleBindView = FakeAchievementRoleBindView
        with mock.patch.dict(
            sys.modules,
            {
                "_gatecount_nhmisc": package,
                "_gatecount_nhmisc.achievement_views": views,
            },
        ):
            await nhmisc.NHMisc.achievement_role_bind(cog, ctx, role)

        view = ctx.send.await_args.kwargs["view"]
        self.assertEqual(view.role.id, 123)
        self.assertEqual(view.holder_ids, (10, 11))
        self.assertEqual(
            tuple(option.label for option in view.achievement_select.options),
            ("All Quests",),
        )

    async def test_create_rejects_an_empty_achievement_name_as_user_feedback(self):
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            create_boolean_definition=mock.AsyncMock(
                side_effect=ValueError("Achievement name cannot be empty")
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            send=mock.AsyncMock(),
        )

        with self.assertRaisesRegex(
            nhmisc.commands.UserFeedbackCheckFailure,
            "Achievement name cannot be empty",
        ):
            await nhmisc.NHMisc.achievement_create(cog, ctx, display_name="   ")

        ctx.send.assert_not_awaited()

    async def test_rename_changes_the_display_name_for_the_requested_key(self):
        renamed = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="achievement_123",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            rename_definition=mock.AsyncMock(return_value=renamed),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._require_private_achievement_channel = mock.Mock()
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=99),
            send=mock.AsyncMock(),
        )

        await nhmisc.NHMisc.achievement_rename(
            cog,
            ctx,
            "achievement_123",
            display_name="All Quests",
        )

        store.rename_definition.assert_awaited_once_with(
            1,
            "achievement_123",
            "All Quests",
        )
        self.assertIn("achievement_123", ctx.send.await_args.args[0])
        self.assertIn("All Quests", ctx.send.await_args.args[0])
        cog._send_moderation_log.assert_awaited_once()
        self.assertIn(
            "Achievement renamed",
            cog._send_moderation_log.await_args.args[1],
        )

    async def test_list_exposes_stable_keys_only_in_a_private_channel(self):
        unbound = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="achievement_123",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
        )
        bound = type(unbound)(
            key="achievement_456",
            display_name="Challenge Run",
            kind=unbound.kind,
            role_id=789,
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=(unbound, bound)),
        )
        cog._require_private_achievement_channel = mock.Mock()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            send=mock.AsyncMock(),
        )

        await nhmisc.NHMisc.achievement_list(cog, ctx)

        cog._require_private_achievement_channel.assert_called_once_with(ctx)
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(
            tuple(field.name for field in embed.fields),
            ("All Quests", "Challenge Run"),
        )
        self.assertIn("achievement_123", embed.fields[0].value)
        self.assertIn("No Discord role", embed.fields[0].value)
        self.assertIn("<@&789>", embed.fields[1].value)
        self.assertEqual(ctx.send.await_args.kwargs["allowed_mentions"], "no-mentions")

    async def test_missing_proofs_previews_twenty_members_and_exports_every_match(self):
        members = [
            SimpleNamespace(
                id=user_id,
                name=f"user{user_id}",
                display_name=f"Member {23 - user_id:02}",
                bot=False,
            )
            for user_id in range(1, 23)
        ]
        bot = SimpleNamespace(id=23, name="bot", display_name="Bot", bot=True)
        default_role = object()
        bot_member = object()
        guild = SimpleNamespace(
            id=1,
            members=[*members, bot],
            member_count=23,
            chunked=True,
            default_role=default_role,
            me=bot_member,
            filesize_limit=100_000,
        )
        channel = SimpleNamespace(
            id=10,
            permissions_for=lambda subject: SimpleNamespace(
                view_channel=subject is not default_role,
                send_messages=subject is bot_member,
                attach_files=subject is bot_member,
            ),
        )
        missing = {
            member.id: tuple(range(1, member.id % 4 + 2)) for member in members
        }
        missing[bot.id] = (1,)
        missing[999] = (1, 2, 3)
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_missing_stargate_proofs=mock.AsyncMock(return_value=missing),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._send_moderation_log = mock.AsyncMock()
        ctx = SimpleNamespace(guild=guild, channel=channel, send=mock.AsyncMock())

        with mock.patch.object(nhmisc.discord, "File", _CapturedFile, create=True):
            await nhmisc.NHMisc.achievement_missingproofs(cog, ctx)

        expected = sorted(
            ((member, missing[member.id]) for member in members),
            key=lambda item: (
                -len(item[1]),
                item[0].display_name.casefold(),
                item[0].id,
            ),
        )
        ctx.send.assert_awaited_once()
        sent = ctx.send.await_args.kwargs
        embed = sent["embed"]
        self.assertEqual(embed.title, "Missing Stargate proofs")
        self.assertEqual(
            embed.description.splitlines(),
            [
                f"<@{member.id}> — Gates {', '.join(map(str, gates))}"
                for member, gates in expected[:20]
            ],
        )
        self.assertEqual(
            embed.fields[0].value,
            "Affected members: 22\n"
            f"Missing proofs: {sum(len(gates) for _, gates in expected)}",
        )
        self.assertEqual(embed.footer.text, "Complete report attached")
        self.assertEqual(sent["allowed_mentions"], "no-mentions")
        attachment = sent["file"]
        self.assertEqual(attachment.filename, "missing-stargate-proofs.csv")
        rows = list(csv.DictReader(io.StringIO(attachment.data.decode("utf-8"))))
        self.assertEqual([int(row["user_id"]) for row in rows], [m.id for m, _ in expected])
        self.assertEqual(
            rows[0]["missing_gates"],
            ", ".join(map(str, expected[0][1])),
        )
        cog._send_moderation_log.assert_not_awaited()

    async def test_missing_proofs_reports_when_every_current_holder_has_proofs(self):
        member = SimpleNamespace(id=1, name="user", display_name="User", bot=False)
        guild = SimpleNamespace(
            id=1,
            members=[member],
            member_count=1,
            chunked=True,
            default_role=object(),
            me=object(),
            filesize_limit=100_000,
        )
        channel = SimpleNamespace(
            permissions_for=lambda subject: SimpleNamespace(
                view_channel=subject is not guild.default_role,
                send_messages=True,
                attach_files=True,
            )
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_missing_stargate_proofs=mock.AsyncMock(return_value={999: (1,)}),
        )
        ctx = SimpleNamespace(guild=guild, channel=channel, send=mock.AsyncMock())

        await nhmisc.NHMisc.achievement_missingproofs(cog, ctx)

        ctx.send.assert_awaited_once_with(
            "All current Gate holders have proofs for every Gate"
        )

    async def test_missing_proofs_requires_complete_member_cache(self):
        guild = SimpleNamespace(
            id=1,
            members=[],
            member_count=1,
            chunked=True,
            default_role=object(),
            me=object(),
        )
        channel = SimpleNamespace(
            permissions_for=lambda subject: SimpleNamespace(
                view_channel=subject is not guild.default_role,
                send_messages=True,
                attach_files=True,
            )
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_missing_stargate_proofs=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        ctx = SimpleNamespace(guild=guild, channel=channel, send=mock.AsyncMock())

        with self.assertRaisesRegex(
            nhmisc.commands.UserFeedbackCheckFailure,
            "Run `!rolesync` first",
        ):
            await nhmisc.NHMisc.achievement_missingproofs(cog, ctx)

        store.list_missing_stargate_proofs.assert_not_awaited()

    async def test_missing_proofs_requires_bootstrapped_achievement_data(self):
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=False),
            list_missing_stargate_proofs=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._require_private_achievement_export_channel = mock.Mock()
        ctx = SimpleNamespace(guild=SimpleNamespace(id=1))

        with self.assertRaisesRegex(
            nhmisc.commands.UserFeedbackCheckFailure,
            "Run `!rolesync discord` first",
        ):
            await nhmisc.NHMisc.achievement_missingproofs(cog, ctx)

        store.list_missing_stargate_proofs.assert_not_awaited()

    def test_missing_proof_export_is_refused_in_public_channels(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog._channel_is_public = mock.Mock(return_value=True)
        ctx = SimpleNamespace(
            channel=SimpleNamespace(id=10),
            guild=SimpleNamespace(id=1),
        )

        with self.assertRaisesRegex(
            nhmisc.commands.UserFeedbackCheckFailure,
            "Achievement proof export is unavailable in this channel",
        ):
            cog._require_private_achievement_export_channel(ctx)

    def test_missing_proof_export_requires_bot_attachment_permission(self):
        default_role = object()
        bot_member = object()
        channel = SimpleNamespace(
            id=10,
            permissions_for=lambda subject: SimpleNamespace(
                view_channel=subject is not default_role,
                send_messages=subject is bot_member,
                attach_files=False,
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)
        ctx = SimpleNamespace(
            channel=channel,
            guild=SimpleNamespace(id=1, default_role=default_role, me=bot_member),
        )

        with self.assertRaisesRegex(
            nhmisc.commands.UserFeedbackCheckFailure,
            "Achievement proof export is unavailable in this channel",
        ):
            cog._require_private_achievement_export_channel(ctx)

    def test_achievement_keys_are_refused_in_public_channels(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog._channel_is_public = mock.Mock(return_value=True)
        ctx = SimpleNamespace()

        with self.assertRaisesRegex(
            nhmisc.commands.UserFeedbackCheckFailure,
            "unavailable in this channel",
        ):
            cog._require_private_achievement_channel(ctx)

    async def test_delete_prepares_a_destructive_review_with_every_award(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="achievement_123",
            display_name="Obsolete",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
        )
        preview = SimpleNamespace(definition=definition, award_count=7)
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            prepare_definition_deletion=mock.AsyncMock(return_value=preview),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._require_private_achievement_channel = mock.Mock()
        message = object()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=42),
            send=mock.AsyncMock(return_value=message),
        )

        class FakeAchievementDeleteView:
            def __init__(self, cog, opener_id, preview):
                self.cog = cog
                self.opener_id = opener_id
                self.preview = preview
                self.message = None

            def render_embed(self):
                return SimpleNamespace()

        package = ModuleType("_gatecount_nhmisc")
        package.__path__ = []
        views = ModuleType("_gatecount_nhmisc.achievement_views")
        views.AchievementDeleteView = FakeAchievementDeleteView

        with mock.patch.dict(
            sys.modules,
            {
                "_gatecount_nhmisc": package,
                "_gatecount_nhmisc.achievement_views": views,
            },
        ):
            await nhmisc.NHMisc.achievement_delete(cog, ctx, "achievement_123")

        store.prepare_definition_deletion.assert_awaited_once_with(
            1,
            "achievement_123",
        )
        view = ctx.send.await_args.kwargs["view"]
        self.assertIs(view.message, message)
        self.assertEqual(view.preview, preview)
        self.assertEqual(ctx.send.await_args.kwargs["allowed_mentions"], "no-mentions")

    async def test_delete_confirmation_removes_the_reviewed_achievement(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="achievement_123",
            display_name="Obsolete",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
        )
        preview = SimpleNamespace(definition=definition, award_count=7)
        store = SimpleNamespace(
            delete_definition=mock.AsyncMock(return_value=preview),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        view = SimpleNamespace(preview=preview, stop=mock.Mock())
        interaction = self._interaction(SimpleNamespace(id=1))

        await cog._confirm_achievement_delete(interaction, view)

        interaction.response.defer.assert_awaited_once_with()
        store.delete_definition.assert_awaited_once_with(
            1,
            "achievement_123",
            expected_award_count=7,
        )
        view.stop.assert_called_once_with()
        interaction.delete_original_response.assert_awaited_once_with()
        cog._send_moderation_log.assert_awaited_once()
        self.assertIn(
            "Achievement deleted",
            cog._send_moderation_log.await_args.args[1],
        )
        self.assertIn("Awards deleted: 7", cog._send_moderation_log.await_args.args[1])

    async def test_delete_confirmation_keeps_a_stale_review_open(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="achievement_123",
            display_name="Obsolete",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
        )
        preview = SimpleNamespace(definition=definition, award_count=7)
        store = SimpleNamespace(
            delete_definition=mock.AsyncMock(
                side_effect=RuntimeError("Achievement changed during deletion review")
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        view = SimpleNamespace(
            preview=preview,
            render_embed=mock.Mock(return_value=object()),
            stop=mock.Mock(),
        )
        interaction = self._interaction(SimpleNamespace(id=1))

        await cog._confirm_achievement_delete(interaction, view)

        view.stop.assert_not_called()
        self.assertIn(
            "changed",
            view.render_embed.call_args.kwargs["notice"],
        )
        self.assertIs(
            interaction.edit_original_response.await_args.kwargs["view"],
            view,
        )

    async def test_role_bind_rechecks_holders_before_importing(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
        )
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            bind_role=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=((10, 12),)
        )
        guild = SimpleNamespace(id=1)
        interaction = self._interaction(guild)
        view = SimpleNamespace(
            selected_key="all_quests",
            role=SimpleNamespace(id=123, mention="<@&123>"),
            holder_ids=(10, 11),
            render_embed=mock.Mock(return_value=SimpleNamespace()),
            stop=mock.Mock(),
        )

        await cog._confirm_achievement_role_bind(interaction, view)

        store.bind_role.assert_not_awaited()
        self.assertEqual(view.holder_ids, (10, 12))
        interaction.edit_original_response.assert_awaited_once()
        self.assertIn(
            "Role holders changed",
            view.render_embed.call_args.kwargs["notice"],
        )

    async def test_successful_role_bind_emits_one_moderation_log(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
        )
        result = SimpleNamespace(definition=definition, imported_count=2)
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            bind_role=mock.AsyncMock(return_value=result),
        )
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=((10, 11),)
        )
        cog._reconcile_achievement_roles_for_guild = mock.AsyncMock()
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        guild = SimpleNamespace(id=1)
        interaction = self._interaction(guild)
        view = SimpleNamespace(
            selected_key="all_quests",
            role=SimpleNamespace(id=123, mention="<@&123>"),
            holder_ids=(10, 11),
            stop=mock.Mock(),
        )

        await cog._confirm_achievement_role_bind(interaction, view)

        cog._send_moderation_log.assert_awaited_once()
        audit = cog._send_moderation_log.await_args.args[1]
        self.assertIn("Achievement role bound", audit)
        self.assertIn("Imported awards: 2", audit)

    async def test_role_bind_rejects_a_role_bound_during_review(self):
        definition_type = type(nhmisc.SOLO_GATER_DEFINITION)
        selected = definition_type(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
        )
        conflicting = definition_type(
            key="speedrun",
            display_name="Speedrun",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=123,
        )
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(selected, conflicting)),
            bind_role=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(return_value=((10,),))
        interaction = self._interaction(SimpleNamespace(id=1))
        view = SimpleNamespace(
            selected_key="all_quests",
            role=SimpleNamespace(id=123, mention="<@&123>"),
            holder_ids=(10,),
            render_embed=mock.Mock(return_value=SimpleNamespace()),
            stop=mock.Mock(),
        )

        await cog._confirm_achievement_role_bind(interaction, view)

        store.bind_role.assert_not_awaited()
        interaction.edit_original_response.assert_awaited_once()
        self.assertIn(
            "configuration changed",
            view.render_embed.call_args.kwargs["notice"].lower(),
        )

    async def test_role_unbind_keeps_the_discord_role_untouched(self):
        definition = SimpleNamespace(key="all_quests", display_name="All Quests")
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            unbind_role=mock.AsyncMock(return_value=definition),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._reconcile_achievement_roles_for_guild = mock.AsyncMock()
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        role = SimpleNamespace(id=123, mention="<@&123>")
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=99),
            send=mock.AsyncMock(),
        )

        await nhmisc.NHMisc.achievement_role_unbind(cog, ctx, role)

        store.unbind_role.assert_awaited_once_with(1, 123)
        cog._reconcile_achievement_roles_for_guild.assert_not_awaited()
        ctx.send.assert_awaited_once()
        cog._send_moderation_log.assert_awaited_once()
        self.assertIn(
            "Achievement role unbound",
            cog._send_moderation_log.await_args.args[1],
        )

    async def test_role_replace_opens_a_two_mode_review_without_text_confirmation(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=123,
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            projected_users_for_boolean=mock.AsyncMock(return_value=(20, 21)),
            replace_role=mock.AsyncMock(),
        )
        guild = SimpleNamespace(id=1)
        author = SimpleNamespace(id=3)
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=((10, 20), (20, 30))
        )
        message = SimpleNamespace()
        ctx = SimpleNamespace(
            guild=guild,
            author=author,
            send=mock.AsyncMock(return_value=message),
        )
        old_role = SimpleNamespace(id=123, mention="<@&123>")
        new_role = SimpleNamespace(id=456, mention="<@&456>")

        class FakeAchievementRoleReplaceView:
            def __init__(
                self,
                cog,
                opener_id,
                definition,
                old_role,
                new_role,
                *,
                stored_holder_ids,
                old_holder_ids,
                new_holder_ids,
            ):
                self.cog = cog
                self.opener_id = opener_id
                self.definition = definition
                self.old_role = old_role
                self.new_role = new_role
                self.stored_holder_ids = stored_holder_ids
                self.old_holder_ids = old_holder_ids
                self.new_holder_ids = new_holder_ids
                self.message = None

            def render_embed(self):
                return SimpleNamespace()

        package = ModuleType("_gatecount_nhmisc")
        package.__path__ = []
        views = ModuleType("_gatecount_nhmisc.achievement_views")
        views.AchievementRoleReplaceView = FakeAchievementRoleReplaceView

        with mock.patch.dict(
            sys.modules,
            {
                "_gatecount_nhmisc": package,
                "_gatecount_nhmisc.achievement_views": views,
            },
        ):
            await nhmisc.NHMisc.achievement_role_replace(
                cog,
                ctx,
                old_role,
                new_role,
            )

        store.replace_role.assert_not_awaited()
        view = ctx.send.await_args.kwargs["view"]
        self.assertEqual(view.stored_holder_ids, (20, 21))
        self.assertEqual(view.old_holder_ids, (10, 20))
        self.assertEqual(view.new_holder_ids, (20, 30))
        self.assertIs(view.message, message)

    async def test_role_replace_applies_move_and_keep_modes(self):
        for remove_old in (True, False):
            with self.subTest(remove_old=remove_old):
                definition = type(nhmisc.SOLO_GATER_DEFINITION)(
                    key="all_quests",
                    display_name="All Quests",
                    kind=nhmisc.SOLO_GATER_DEFINITION.kind,
                    role_id=123,
                )
                replacement = type(definition)(
                    key=definition.key,
                    display_name=definition.display_name,
                    kind=definition.kind,
                    role_id=456,
                )
                result = SimpleNamespace(
                    definition=replacement,
                    imported_count=2,
                )
                members = {
                    10: SimpleNamespace(id=10),
                    11: SimpleNamespace(id=11),
                    12: SimpleNamespace(id=12),
                }
                roles = {
                    123: SimpleNamespace(id=123, mention="<@&123>"),
                    456: SimpleNamespace(id=456, mention="<@&456>"),
                }
                guild = SimpleNamespace(
                    id=1,
                    get_member=members.get,
                    get_role=roles.get,
                    fetch_member=mock.AsyncMock(),
                )
                store = SimpleNamespace(
                    list_definitions=mock.AsyncMock(return_value=(definition,)),
                    replace_role=mock.AsyncMock(return_value=result),
                    projected_users_for_boolean=mock.AsyncMock(
                        return_value=(10, 11, 12)
                    ),
                )
                cog = object.__new__(nhmisc.NHMisc)
                cog._achievement_store = store
                cog._role_analytics_users_with_roles = mock.AsyncMock(
                    return_value=((10, 12), (11,))
                )
                cog._edit_achievement_roles = mock.AsyncMock()
                cog._send_moderation_log = mock.AsyncMock(return_value=True)
                cog._send_maintenance_log = mock.AsyncMock(return_value=True)
                interaction = self._interaction(guild)
                view = SimpleNamespace(
                    definition=definition,
                    old_role=roles[123],
                    new_role=roles[456],
                    stored_holder_ids=(10,),
                    old_holder_ids=(10, 12),
                    new_holder_ids=(11,),
                    stop=mock.Mock(),
                    render_embed=mock.Mock(return_value=SimpleNamespace()),
                )

                await cog._confirm_achievement_role_replace(
                    interaction,
                    view,
                    remove_old=remove_old,
                )

                store.replace_role.assert_awaited_once_with(
                    1,
                    achievement_key="all_quests",
                    old_role_id=123,
                    new_role_id=456,
                    user_ids=(10, 11, 12),
                )
                self.assertEqual(
                    cog._edit_achievement_roles.await_args_list,
                    [
                        mock.call(
                            guild,
                            members[user_id],
                            add_role_ids=(456,),
                            remove_role_ids=(123,) if remove_old else (),
                            reason="Replace achievement role by 99",
                        )
                        for user_id in (10, 12)
                    ],
                )
                cog._send_maintenance_log.assert_not_awaited()
                cog._send_moderation_log.assert_awaited_once()
                audit = cog._send_moderation_log.await_args.args[1]
                self.assertIn(
                    "Mode: move members" if remove_old else "Mode: keep old role",
                    audit,
                )
                self.assertIn("Members changed: 2", audit)
                view.stop.assert_called_once_with()

    async def test_role_replace_rejects_changed_old_or_new_role_holders(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=123,
        )
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            replace_role=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=((10,), (11, 12))
        )
        roles = {
            123: SimpleNamespace(id=123),
            456: SimpleNamespace(id=456),
        }
        guild = SimpleNamespace(id=1, get_role=roles.get)
        interaction = self._interaction(guild)
        view = SimpleNamespace(
            definition=definition,
            old_role=SimpleNamespace(id=123),
            new_role=SimpleNamespace(id=456),
            old_holder_ids=(10,),
            new_holder_ids=(11,),
            render_embed=mock.Mock(return_value=SimpleNamespace()),
        )

        await cog._confirm_achievement_role_replace(
            interaction,
            view,
            remove_old=True,
        )

        store.replace_role.assert_not_awaited()
        self.assertEqual(view.old_holder_ids, (10,))
        self.assertEqual(view.new_holder_ids, (11, 12))
        self.assertIn(
            "Role holders changed",
            view.render_embed.call_args.kwargs["notice"],
        )

    async def test_role_replace_rejects_a_deleted_new_role(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=123,
        )
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            replace_role=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=((10,), ())
        )
        guild = SimpleNamespace(
            id=1,
            get_role=lambda role_id: SimpleNamespace(id=123) if role_id == 123 else None,
        )
        interaction = self._interaction(guild)
        view = SimpleNamespace(
            definition=definition,
            old_role=SimpleNamespace(id=123),
            new_role=SimpleNamespace(id=456),
            old_holder_ids=(10,),
            new_holder_ids=(),
            render_embed=mock.Mock(return_value=SimpleNamespace()),
        )

        await cog._confirm_achievement_role_replace(
            interaction,
            view,
            remove_old=True,
        )

        store.replace_role.assert_not_awaited()
        self.assertIn(
            "no longer exists",
            view.render_embed.call_args.kwargs["notice"],
        )

    async def test_role_replace_reports_an_atomic_configuration_conflict(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=123,
        )
        roles = {
            123: SimpleNamespace(id=123),
            456: SimpleNamespace(id=456),
        }
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            replace_role=mock.AsyncMock(side_effect=LookupError("not bound")),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=((10,), ())
        )
        guild = SimpleNamespace(id=1, get_role=roles.get)
        interaction = self._interaction(guild)
        view = SimpleNamespace(
            definition=definition,
            old_role=roles[123],
            new_role=roles[456],
            old_holder_ids=(10,),
            new_holder_ids=(),
            render_embed=mock.Mock(return_value=SimpleNamespace()),
        )

        await cog._confirm_achievement_role_replace(
            interaction,
            view,
            remove_old=True,
        )

        self.assertIn(
            "configuration changed",
            view.render_embed.call_args.kwargs["notice"].lower(),
        )

    async def test_role_replace_reports_member_edit_failures_after_rebinding(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=123,
        )
        replacement = type(definition)(
            key=definition.key,
            display_name=definition.display_name,
            kind=definition.kind,
            role_id=456,
        )
        member = SimpleNamespace(id=10)
        guild = SimpleNamespace(
            id=1,
            get_member=lambda user_id: member if user_id == 10 else None,
            get_role=lambda role_id: SimpleNamespace(id=role_id),
            fetch_member=mock.AsyncMock(),
        )
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            replace_role=mock.AsyncMock(
                return_value=SimpleNamespace(
                    definition=replacement,
                    imported_count=0,
                )
            ),
            projected_users_for_boolean=mock.AsyncMock(return_value=(10,)),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=((10,), ())
        )
        cog._edit_achievement_roles = mock.AsyncMock(
            side_effect=nhmisc.commands.UserFeedbackCheckFailure("role hierarchy")
        )
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        cog._send_maintenance_log = mock.AsyncMock(return_value=True)
        interaction = self._interaction(guild)
        view = SimpleNamespace(
            definition=definition,
            old_role=SimpleNamespace(id=123, mention="<@&123>"),
            new_role=SimpleNamespace(id=456, mention="<@&456>"),
            old_holder_ids=(10,),
            new_holder_ids=(),
            stop=mock.Mock(),
            render_embed=mock.Mock(return_value=SimpleNamespace()),
        )

        await cog._confirm_achievement_role_replace(
            interaction,
            view,
            remove_old=True,
        )

        store.replace_role.assert_awaited_once()
        cog._send_maintenance_log.assert_awaited_once()
        self.assertIn(
            "Members skipped: 1",
            cog._send_moderation_log.await_args.args[1],
        )

    async def test_role_list_uses_non_pinging_role_mentions(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=123,
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=(definition,)),
        )
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            send=mock.AsyncMock(),
        )

        await nhmisc.NHMisc.achievement_role_list(cog, ctx)

        self.assertEqual(ctx.send.await_args.args, ("<@&123> All Quests",))
        self.assertIn("allowed_mentions", ctx.send.await_args.kwargs)

    async def test_generic_grant_stores_source_ids_and_projects_role(self):
        default_role = SimpleNamespace(id=0, position=0)
        solo_role = SimpleNamespace(
            id=nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            managed=False,
            position=2,
        )
        member = SimpleNamespace(
            id=10,
            display_name="Player",
            bot=False,
            top_role=SimpleNamespace(position=1),
            roles=[default_role],
            edit=mock.AsyncMock(),
        )
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=10),
            ),
            default_role=default_role,
            get_member=lambda user_id: member if user_id == member.id else None,
            get_role=lambda role_id: solo_role if role_id == solo_role.id else default_role,
        )
        result_message = SimpleNamespace(id=31)
        source = SimpleNamespace(
            id=30,
            channel=SimpleNamespace(id=20),
            guild=guild,
            webhook_id=None,
            author=member,
            raw_mentions=(),
            reply=mock.AsyncMock(return_value=result_message),
        )
        definition_type = type(nhmisc.SOLO_GATER_DEFINITION)
        unbound_definition = definition_type(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            display_order=100,
        )
        existing_definition = definition_type(
            key="speedrun",
            display_name="Speedrun",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            display_order=101,
        )
        definitions = (
            nhmisc.SOLO_GATER_DEFINITION,
            unbound_definition,
            existing_definition,
        )
        view = SimpleNamespace(
            source_message=source,
            selected_user_ids={member.id},
            selected_keys={definition.key for definition in definitions},
            definitions=definitions,
            candidate_ids=(member.id,),
            stop=mock.Mock(),
            render_embed=mock.Mock(),
        )
        store = SimpleNamespace(
            grant_boolean=mock.AsyncMock(
                side_effect=(
                    SimpleNamespace(created=True),
                    SimpleNamespace(created=True),
                    SimpleNamespace(created=False),
                )
            ),
            revoke_booleans=mock.AsyncMock(),
            list_definitions=mock.AsyncMock(return_value=definitions),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        interaction = self._interaction(guild)

        with mock.patch.object(
            nhmisc.discord,
            "AllowedMentions",
            side_effect=SimpleNamespace,
        ):
            await cog._confirm_achievement_grant(interaction, view)

        self.assertEqual(
            tuple(call.args[2] for call in store.grant_boolean.await_args_list),
            ("solo_gater", "all_quests", "speedrun"),
        )
        self.assertEqual(
            {role.id for role in member.edit.await_args.kwargs["roles"]},
            {solo_role.id},
        )
        interaction.delete_original_response.assert_awaited_once_with()
        cog._send_moderation_log.assert_awaited_once()
        audit = cog._send_moderation_log.await_args.args[1]
        self.assertIn("Achievements granted", audit)
        self.assertIn("Recipients: <@10>", audit)
        self.assertIn("https://discord.com/channels/1/20/30", audit)
        self.assertEqual(
            source.reply.await_args.args[0],
            "🎉 **Congratulations!**\n"
            f"<@10> <@&{solo_role.id}>, All Quests",
        )
        allowed_mentions = source.reply.await_args.kwargs["allowed_mentions"]
        self.assertTrue(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.replied_user)
        self.assertNotIn("Speedrun", source.reply.await_args.args[0])

    async def test_grant_rejects_a_role_binding_changed_during_review(self):
        member = SimpleNamespace(id=10, display_name="Player", bot=False)
        guild = SimpleNamespace(
            id=1,
            get_member=lambda user_id: member if user_id == member.id else None,
        )
        source = SimpleNamespace(
            id=30,
            channel=SimpleNamespace(id=20),
            guild=guild,
            webhook_id=None,
            author=member,
            raw_mentions=(),
        )
        changed_definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key=nhmisc.SOLO_GATER_DEFINITION.key,
            display_name=nhmisc.SOLO_GATER_DEFINITION.display_name,
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=None,
            display_order=nhmisc.SOLO_GATER_DEFINITION.display_order,
        )
        view = SimpleNamespace(
            source_message=source,
            selected_user_ids={member.id},
            selected_keys={nhmisc.SOLO_GATER_DEFINITION.key},
            definitions=(nhmisc.SOLO_GATER_DEFINITION,),
            candidate_ids=(member.id,),
            render_embed=mock.Mock(return_value=SimpleNamespace()),
        )
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(changed_definition,)),
            grant_boolean=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        interaction = self._interaction(guild)

        await cog._confirm_achievement_grant(interaction, view)

        store.grant_boolean.assert_not_awaited()
        self.assertIn(
            "configuration changed",
            view.render_embed.call_args.kwargs["notice"].lower(),
        )

    async def test_revoke_updates_database_before_removing_projected_role(self):
        effects = []

        async def record_role_edit(**_kwargs):
            effects.append("discord")

        async def record_revocation(*_args, **_kwargs):
            effects.append("database")
            return 1

        default_role = SimpleNamespace(id=0, position=0)
        solo_role = SimpleNamespace(
            id=nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            managed=False,
            position=2,
        )
        member = SimpleNamespace(
            id=10,
            top_role=SimpleNamespace(position=1),
            roles=[default_role, solo_role],
            edit=mock.AsyncMock(side_effect=record_role_edit),
        )
        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=10),
            ),
            default_role=default_role,
            get_role=lambda role_id: solo_role if role_id == solo_role.id else default_role,
        )
        store = SimpleNamespace(
            shared_boolean_keys=mock.AsyncMock(return_value=("solo_gater",)),
            revoke_booleans=mock.AsyncMock(side_effect=record_revocation),
            list_definitions=mock.AsyncMock(
                return_value=(nhmisc.SOLO_GATER_DEFINITION,)
            ),
        )
        view = SimpleNamespace(
            members=(member,),
            selected_keys={"solo_gater"},
            definitions=(nhmisc.SOLO_GATER_DEFINITION,),
            stop=mock.Mock(),
            render_embed=mock.Mock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._send_moderation_log = mock.AsyncMock(return_value=True)
        interaction = self._interaction(guild)

        await cog._confirm_achievement_revoke(interaction, view)

        self.assertEqual(member.edit.await_args.kwargs["roles"], [])
        self.assertEqual(effects, ["database", "discord"])
        store.revoke_booleans.assert_awaited_once_with(
            guild.id,
            (member.id,),
            ("solo_gater",),
        )
        interaction.delete_original_response.assert_awaited_once_with()
        cog._send_moderation_log.assert_awaited_once()
        audit = cog._send_moderation_log.await_args.args[1]
        self.assertIn("Achievements revoked", audit)
        self.assertIn("Members: <@10>", audit)

    async def test_revoke_rejects_a_role_binding_changed_during_review(self):
        guild = SimpleNamespace(id=1)
        member = SimpleNamespace(id=10)
        changed_definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key=nhmisc.SOLO_GATER_DEFINITION.key,
            display_name=nhmisc.SOLO_GATER_DEFINITION.display_name,
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=None,
            display_order=nhmisc.SOLO_GATER_DEFINITION.display_order,
        )
        store = SimpleNamespace(
            shared_boolean_keys=mock.AsyncMock(return_value=("solo_gater",)),
            list_definitions=mock.AsyncMock(return_value=(changed_definition,)),
            revoke_booleans=mock.AsyncMock(),
        )
        view = SimpleNamespace(
            members=(member,),
            selected_keys={"solo_gater"},
            definitions=(nhmisc.SOLO_GATER_DEFINITION,),
            render_embed=mock.Mock(return_value=SimpleNamespace()),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        interaction = self._interaction(guild)

        await cog._confirm_achievement_revoke(interaction, view)

        store.revoke_booleans.assert_not_awaited()
        self.assertIn(
            "configuration changed",
            view.render_embed.call_args.kwargs["notice"].lower(),
        )

    async def test_grant_keeps_review_visible_when_source_disappears(self):
        guild = SimpleNamespace(id=1)
        view = SimpleNamespace(
            source_message=SimpleNamespace(
                guild=guild,
                channel=SimpleNamespace(id=2),
                id=3,
            ),
            selected_user_ids={10},
            selected_keys={"solo_gater"},
            render_embed=mock.Mock(return_value=object()),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._fetch_gate_increment_source = mock.AsyncMock(
            side_effect=nhmisc.commands.UserFeedbackCheckFailure(
                "Source message is unavailable"
            )
        )
        interaction = self._interaction(guild)

        await cog._confirm_achievement_grant(interaction, view)

        view.render_embed.assert_called_once_with(
            notice="Source message is unavailable"
        )

    async def test_audit_notification_pings_human_actor_in_alert_channel(self):
        human = SimpleNamespace(id=42, bot=False, send=mock.AsyncMock())
        entries = (
            SimpleNamespace(
                target=SimpleNamespace(id=10),
                user=SimpleNamespace(bot=True),
                created_at=nhmisc.datetime.now(nhmisc.timezone.utc),
            ),
            SimpleNamespace(
                target=SimpleNamespace(id=10),
                user=human,
                created_at=nhmisc.datetime.now(nhmisc.timezone.utc),
            ),
        )

        async def audit_logs(**_kwargs):
            for entry in entries:
                yield entry

        guild = SimpleNamespace(
            id=1,
            me=SimpleNamespace(
                guild_permissions=SimpleNamespace(view_audit_log=True)
            ),
            audit_logs=audit_logs,
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._send_guild_alert = mock.AsyncMock(return_value=True)
        previous_action = getattr(nhmisc.discord, "AuditLogAction", None)
        nhmisc.discord.AuditLogAction = SimpleNamespace(member_role_update=object())
        try:
            with mock.patch.object(nhmisc.asyncio, "sleep", mock.AsyncMock()):
                await cog._notify_unauthorized_gate_actor(guild, 10)
        finally:
            if previous_action is None:
                del nhmisc.discord.AuditLogAction
            else:
                nhmisc.discord.AuditLogAction = previous_action

        human.send.assert_not_awaited()
        cog._send_guild_alert.assert_awaited_once_with(
            guild,
            "<@42> Gate roles must be changed through the bot. "
            "Your manual role change was automatically reverted",
            ping_user=human,
        )

    async def test_guild_alert_enables_only_requested_user_mention(self):
        guild = SimpleNamespace(id=1)
        channel = SimpleNamespace(id=2)
        user = SimpleNamespace(id=42)
        config = SimpleNamespace(alert_channel=mock.AsyncMock(return_value=2))
        cog = object.__new__(nhmisc.NHMisc)
        cog.config = SimpleNamespace(guild=mock.Mock(return_value=config))
        cog._get_log_channel = mock.Mock(return_value=channel)
        cog._send_voice_log = mock.AsyncMock(return_value=object())

        delivered = await cog._send_guild_alert(
            guild,
            "<@42> warning",
            ping_user=user,
        )

        self.assertTrue(delivered)
        allowed_mentions = cog._send_voice_log.await_args.kwargs[
            "allowed_mentions"
        ]
        self.assertEqual(allowed_mentions.users, [user])
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.replied_user)

    async def test_manual_solo_role_addition_is_reverted_from_database_state(self):
        default_role = SimpleNamespace(id=0)
        solo_role = SimpleNamespace(id=nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID)
        guild = SimpleNamespace(id=1)
        before = SimpleNamespace(
            id=10,
            bot=False,
            guild=guild,
            roles=(default_role,),
        )
        after = SimpleNamespace(
            id=10,
            bot=False,
            guild=guild,
            roles=(default_role, solo_role),
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(
                return_value=(nhmisc.SOLO_GATER_DEFINITION,)
            ),
            projected_users_for_boolean=mock.AsyncMock(return_value=()),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._authorized_gate_role_edits = {}
        cog._edit_achievement_roles = mock.AsyncMock()
        cog._send_guild_alert = mock.AsyncMock(return_value=True)

        await cog.on_achievement_member_update(before, after)

        cog._edit_achievement_roles.assert_awaited_once_with(
            guild,
            after,
            add_role_ids=(),
            remove_role_ids=(nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,),
            reason="Revert unauthorized achievement role change",
        )

    async def test_pending_solo_award_allows_gate_increment_role_event(self):
        default_role = SimpleNamespace(id=0)
        solo_role = SimpleNamespace(id=nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID)
        guild = SimpleNamespace(id=1)
        before = SimpleNamespace(
            id=10,
            bot=False,
            guild=guild,
            roles=(default_role,),
        )
        after = SimpleNamespace(
            id=10,
            bot=False,
            guild=guild,
            roles=(default_role, solo_role),
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(
                return_value=(nhmisc.SOLO_GATER_DEFINITION,)
            ),
            projected_users_for_boolean=mock.AsyncMock(return_value=(10,)),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._authorized_gate_role_edits = {}
        cog._edit_achievement_roles = mock.AsyncMock()
        cog._send_guild_alert = mock.AsyncMock(return_value=True)

        await cog.on_achievement_member_update(before, after)

        cog._edit_achievement_roles.assert_not_awaited()

    async def test_deleted_achievement_role_is_unbound_without_losing_awards(self):
        guild = SimpleNamespace(id=1)
        role = SimpleNamespace(id=123, name="All Quests", guild=guild)
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=role.id,
        )
        store = SimpleNamespace(
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            unbind_role=mock.AsyncMock(return_value=definition),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._sticky_roles = SimpleNamespace(
            get_role_state=mock.AsyncMock(return_value=(False, 0))
        )
        cog._send_maintenance_log = mock.AsyncMock(return_value=True)

        await cog.on_guild_role_delete(role)

        store.unbind_role.assert_awaited_once_with(guild.id, role.id)
        cog._send_maintenance_log.assert_awaited_once_with(
            guild,
            "Stopped tracking deleted role All Quests for All Quests",
        )


if __name__ == "__main__":
    unittest.main()
