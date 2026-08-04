import unittest
from types import SimpleNamespace
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
            candidate_ids=(member.id,),
            stop=mock.Mock(),
            render_embed=mock.Mock(),
        )
        store = SimpleNamespace(
            grant_boolean=mock.AsyncMock(
                return_value=SimpleNamespace(created=True)
            ),
            revoke_booleans=mock.AsyncMock(),
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

    async def test_revoke_removes_projected_role_before_revoking_award(self):
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
            edit=mock.AsyncMock(),
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
            revoke_booleans=mock.AsyncMock(return_value=0),
        )
        view = SimpleNamespace(
            members=(member,),
            selected_keys={"solo_gater"},
            stop=mock.Mock(),
            render_embed=mock.Mock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        interaction = self._interaction(guild)

        await cog._confirm_achievement_revoke(interaction, view)

        self.assertEqual(member.edit.await_args.kwargs["roles"], [])
        store.revoke_booleans.assert_awaited_once_with(
            guild.id,
            (member.id,),
            ("solo_gater",),
        )
        self.assertIn(
            "Revoked 1 achievements",
            interaction.edit_original_response.await_args.kwargs["content"],
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

    async def test_bootstrap_uses_highest_duplicate_gate_tier(self):
        users_by_role = (
            (10,),
            (),
            (10, 11),
            (),
            (),
            (),
            (11,),
        )
        store = SimpleNamespace(
            is_bootstrapped=mock.AsyncMock(return_value=False),
            bootstrap_guild=mock.AsyncMock(return_value=True),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store
        cog._send_guild_alert = mock.AsyncMock(return_value=True)
        cog._role_analytics_users_with_roles = mock.AsyncMock(
            return_value=users_by_role
        )
        guild = SimpleNamespace(id=1)

        self.assertTrue(await cog._bootstrap_achievements_for_guild(guild))

        store.bootstrap_guild.assert_awaited_once_with(
            guild.id,
            gate_tiers={10: 3, 11: 3},
            boolean_users={"solo_gater": (11,)},
        )
        cog._send_guild_alert.assert_awaited_once_with(
            guild,
            "Achievement database initialized from current roles. "
            "Gate holders: 2; Solo Gater holders: 1",
        )

    async def test_manual_solo_role_addition_creates_proofless_award(self):
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
            grant_boolean=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._achievement_store = store

        await cog.on_achievement_member_update(before, after)

        store.grant_boolean.assert_awaited_once_with(
            guild.id,
            after.id,
            "solo_gater",
        )


if __name__ == "__main__":
    unittest.main()
