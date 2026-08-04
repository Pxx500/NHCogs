import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

from tests.test_gatecount import nhmisc


class AchievementProfileRenderingTests(unittest.TestCase):
    def test_profile_orders_gate_proofs_before_boolean_achievements(self):
        profile = nhmisc.AchievementProfile(
            stargate_count=4,
            stargate_proofs=(
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
            tuple(field.name for field in embed.fields),
            ("Stargates completed", "Proofs", "Achievements"),
        )
        self.assertEqual(embed.fields[0].value, "4")
        self.assertIn("Stargate 4", embed.fields[1].value)
        self.assertIn(
            "https://discord.com/channels/10/20/30",
            embed.fields[1].value,
        )
        self.assertEqual(embed.fields[2].value, "Solo Gater")

    def test_empty_profile_is_explicit(self):
        profile = nhmisc.AchievementProfile(0, (), ())

        embed = nhmisc.NHMisc._build_achievements_embed(
            10,
            SimpleNamespace(display_name="Player"),
            profile,
            (),
        )

        self.assertEqual(embed.description, "No achievements recorded")


class AchievementWorkflowTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _interaction(guild):
        return SimpleNamespace(
            guild=guild,
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )

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
        cog._send_guild_alert = mock.AsyncMock(return_value=True)

        await cog._role_analytics_startup_reconcile()

        store.bootstrap_guild.assert_not_awaited()
        cog._send_guild_alert.assert_awaited_once_with(
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
        definition = SimpleNamespace(display_name="All Quests")
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            unbind_role=mock.AsyncMock(return_value=definition),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._reconcile_achievement_roles_for_guild = mock.AsyncMock()
        role = SimpleNamespace(id=123, mention="<@&123>")
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            send=mock.AsyncMock(),
        )

        await nhmisc.NHMisc.achievement_role_unbind(cog, ctx, role)

        store.unbind_role.assert_awaited_once_with(1, 123)
        cog._reconcile_achievement_roles_for_guild.assert_not_awaited()
        ctx.send.assert_awaited_once()

    async def test_role_replace_imports_new_holders_after_confirmation(self):
        definition = type(nhmisc.SOLO_GATER_DEFINITION)(
            key="all_quests",
            display_name="All Quests",
            kind=nhmisc.SOLO_GATER_DEFINITION.kind,
            role_id=123,
        )
        result = SimpleNamespace(
            definition=type(definition)(
                key=definition.key,
                display_name=definition.display_name,
                kind=definition.kind,
                role_id=456,
            ),
            imported_count=2,
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=True),
            list_definitions=mock.AsyncMock(return_value=(definition,)),
            replace_role=mock.AsyncMock(return_value=result),
        )
        guild = SimpleNamespace(id=1)
        channel = SimpleNamespace(id=2)
        author = SimpleNamespace(id=3)
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            side_effect=(((10, 11),), ((10, 11),))
        )
        cog._reconcile_achievement_roles_for_guild = mock.AsyncMock()
        cog.bot = SimpleNamespace(wait_for=mock.AsyncMock())
        ctx = SimpleNamespace(
            guild=guild,
            channel=channel,
            author=author,
            send=mock.AsyncMock(),
        )
        old_role = SimpleNamespace(id=123, mention="<@&123>")
        new_role = SimpleNamespace(id=456, mention="<@&456>")

        await nhmisc.NHMisc.achievement_role_replace(
            cog,
            ctx,
            old_role,
            new_role,
        )

        store.replace_role.assert_awaited_once_with(
            1,
            old_role_id=123,
            new_role_id=456,
            user_ids=(10, 11),
        )
        cog._reconcile_achievement_roles_for_guild.assert_awaited_once_with(guild)

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
        source = SimpleNamespace(
            id=30,
            channel=SimpleNamespace(id=20),
            guild=guild,
            webhook_id=None,
            author=member,
            raw_mentions=(),
        )
        view = SimpleNamespace(
            source_message=source,
            selected_user_ids={member.id},
            selected_keys={"solo_gater"},
            definitions=(nhmisc.SOLO_GATER_DEFINITION,),
            candidate_ids=(member.id,),
            stop=mock.Mock(),
            render_embed=mock.Mock(),
        )
        store = SimpleNamespace(
            grant_boolean=mock.AsyncMock(
                return_value=SimpleNamespace(created=True)
            ),
            revoke_booleans=mock.AsyncMock(),
            list_definitions=mock.AsyncMock(
                return_value=(nhmisc.SOLO_GATER_DEFINITION,)
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._fetch_gate_increment_source = mock.AsyncMock(return_value=source)
        interaction = self._interaction(guild)

        await cog._confirm_achievement_grant(interaction, view)

        store.grant_boolean.assert_awaited_once_with(
            guild.id,
            member.id,
            "solo_gater",
            source_channel_id=source.channel.id,
            source_message_id=source.id,
        )
        self.assertEqual(
            {role.id for role in member.edit.await_args.kwargs["roles"]},
            {solo_role.id},
        )
        self.assertIn(
            "Granted 1 achievements",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

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
        interaction = self._interaction(guild)

        await cog._confirm_achievement_revoke(interaction, view)

        self.assertEqual(member.edit.await_args.kwargs["roles"], [])
        self.assertEqual(effects, ["database", "discord"])
        store.revoke_booleans.assert_awaited_once_with(
            guild.id,
            (member.id,),
            ("solo_gater",),
        )
        self.assertIn(
            "Revoked 1 achievements",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

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

    async def test_audit_notification_skips_bot_restore_entry(self):
        human = SimpleNamespace(bot=False, send=mock.AsyncMock())
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

        human.send.assert_awaited_once()

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
        cog._send_guild_alert = mock.AsyncMock(return_value=True)

        await cog.on_guild_role_delete(role)

        store.unbind_role.assert_awaited_once_with(guild.id, role.id)
        cog._send_guild_alert.assert_awaited_once_with(
            guild,
            "Stopped tracking deleted role All Quests for All Quests",
        )


if __name__ == "__main__":
    unittest.main()
