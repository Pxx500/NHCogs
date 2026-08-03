import importlib.util
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

NHMISC_PATH = Path(__file__).resolve().parents[1] / "NHMisc" / "nhmisc.py"


class _Color:
    @staticmethod
    def blue() -> int:
        return 0x3498DB


class _Embed:
    def __init__(self, *, title=None, description=None, color=None):
        self.title = title
        self.description = description
        self.color = color

    def add_field(self, *, name, value, inline=True):
        pass


class _UserFeedbackCheckFailure(Exception):
    pass


def _decorator(*args, **kwargs):
    def wrapper(function):
        function.command = _decorator
        function.group = _decorator
        return function

    return wrapper


def _permission_decorator(name, permissions):
    def wrapper(function):
        setattr(function, name, permissions)
        return function

    return wrapper


class _Cog:
    @classmethod
    def listener(cls, *args, **kwargs):
        return _decorator(*args, **kwargs)


def _load_nhmisc():
    discord = types.ModuleType("discord")
    discord.Color = _Color
    discord.Embed = _Embed

    commands = types.ModuleType("redbot.core.commands")
    commands.Cog = _Cog
    commands.Context = object
    commands.UserFeedbackCheckFailure = _UserFeedbackCheckFailure
    commands.command = _decorator
    commands.group = _decorator
    commands.guild_only = lambda: (lambda function: function)
    commands.admin_or_permissions = lambda **kwargs: (lambda function: function)
    commands.mod_or_permissions = lambda **kwargs: _permission_decorator(
        "mod_or_permissions", kwargs
    )
    commands.has_permissions = lambda **kwargs: (lambda function: function)
    commands.cooldown = _decorator
    commands.BucketType = SimpleNamespace(user="user", guild="guild")

    redbot = types.ModuleType("redbot")
    redbot_core = types.ModuleType("redbot.core")
    redbot_core.Config = SimpleNamespace(get_conf=lambda *args, **kwargs: None)
    redbot_core.commands = commands
    redbot.core = redbot_core

    data_manager = types.ModuleType("redbot.core.data_manager")
    data_manager.cog_data_path = lambda cog: Path(".")

    package_name = "_gatecount_nhmisc"
    package = types.ModuleType(package_name)
    package.__path__ = [str(NHMISC_PATH.parent)]
    module_name = f"{package_name}.nhmisc"
    spec = importlib.util.spec_from_file_location(module_name, NHMISC_PATH)
    module = importlib.util.module_from_spec(spec)

    stubs = {
        "discord": discord,
        "redbot": redbot,
        "redbot.core": redbot_core,
        "redbot.core.commands": commands,
        "redbot.core.data_manager": data_manager,
        package_name: package,
        module_name: module,
    }
    with mock.patch.dict(sys.modules, stubs):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module, discord, commands


nhmisc, discord, commands = _load_nhmisc()


class _Role:
    def __init__(self, role_id, members):
        self.id = role_id
        self.members = list(members)

    def is_default(self):
        return False


class _Guild:
    def __init__(self, role_members):
        self.id = 123
        self.default_role = SimpleNamespace(id=0)
        self._roles: dict[int, _Role] = {}
        for role_id, members in role_members.items():
            normalized_members = (
                [object() for _ in range(members)]
                if isinstance(members, int)
                else members
            )
            self._roles[role_id] = _Role(role_id, normalized_members)

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def analytics_members(self):
        memberships = {}
        for role_id, role in self._roles.items():
            for member in role.members:
                memberships.setdefault(member, set()).add(role_id)
        return [
            SimpleNamespace(
                user_id=user_id,
                is_bot=False,
                role_ids=tuple(sorted(role_ids)),
            )
            for user_id, role_ids in enumerate(memberships.values(), start=1)
        ]


TIER_COUNTS = {
    757645112267243541: 120,
    757643319265460224: 260,
    630848584539045926: 300,
    631180331839389738: 420,
    631180321727184896: 500,
    631180312906563594: 460,
    631180295252738099: 360,
    631180266982866986: 250,
    631180246837624852: 160,
    631180223928336414: 90,
    631180193960296478: 45,
    631180158262575174: 20,
    631180143385247754: 10,
    631180120782012426: 4,
    631180089782042625: 1,
}

GATE_MEMBERSHIP_COUNTS = {
    1348078501986828461: 100,
    798700443979087892: 200,
    1348078496710135888: 50,
    1004822424921055233: 100,
    1348078483384958986: 25,
    1097204292198338692: 75,
    1442209676530815076: 50,
    1442209801374269682: 75,
    1442208051212976158: 50,
    1437811360208781406: 75,
}

ALL_DISTRIBUTION_ROLE_IDS = tuple(TIER_COUNTS) + tuple(GATE_MEMBERSHIP_COUNTS)
GATE_ROLE_IDS = tuple(GATE_MEMBERSHIP_COUNTS)


class GatecountCommandTests(unittest.IsolatedAsyncioTestCase):
    async def run_gatecount(
        self, role_members, schema_state=nhmisc.SchemaState.LEGACY
    ):
        guild = _Guild(role_members)
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = nhmisc.RoleAnalyticsStore(
            Path(temp_dir.name) / "role_analytics.sqlite"
        )
        await store.initialize()
        generation = await store.next_generation(guild.id)
        members = guild.analytics_members()
        await store.write_generation(guild.id, generation, members)
        await store.activate_generation(guild.id, generation, len(members))

        cog = object.__new__(nhmisc.NHMisc)
        cog._role_analytics_store = store
        cog._gate_migration_store = SimpleNamespace(
            get_schema_state=mock.AsyncMock(return_value=schema_state)
        )
        ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())
        await nhmisc.NHMisc.gatecount(cog, ctx)
        return ctx

    async def test_current_gatecount_shows_boolean_and_highest_linear_tiers(self):
        tier_roles = tuple(nhmisc.TARGET_TIER_ROLE_IDS)
        singleplayer = object()
        boolean_only = object()
        tier_ten = object()
        members = {role_id: [] for role_id in tier_roles}
        members[nhmisc.SINGLEPLAYER_COMPLETED_ROLE_ID] = [
            singleplayer,
            boolean_only,
        ]
        members[tier_roles[0]] = [singleplayer]
        members[tier_roles[2]] = [singleplayer]
        members[tier_roles[9]] = [tier_ten]

        ctx = await self.run_gatecount(members, nhmisc.SchemaState.CURRENT)

        description = ctx.send.await_args.kwargs["embed"].description
        self.assertEqual(
            description,
            "Singleplayer completed — **2**\n"
            "Tier 1 — **0**\n"
            "Tier 2 — **0**\n"
            "Tier 3 — **1**\n"
            "Tier 4 — **0**\n"
            "Tier 5 — **0**\n"
            "Tier 6 — **0**\n"
            "Tier 7 — **0**\n"
            "Tier 8 — **0**\n"
            "Tier 9 — **0**\n"
            "Tier 10 — **1**",
        )
        self.assertNotIn("Total Gates", description)

    async def test_gatecount_is_unavailable_while_migrating(self):
        ctx = await self.run_gatecount({}, nhmisc.SchemaState.MIGRATING)

        ctx.send.assert_awaited_once_with(
            "Gate reports are unavailable during migration"
        )

    async def test_gatecount_shows_weighted_sp_mp_and_combined_totals(self):
        ctx = await self.run_gatecount(
            {
                1348078501986828461: 168,
                798700443979087892: 509,
                1348078496710135888: 16,
                1004822424921055233: 68,
                1348078483384958986: 3,
                1097204292198338692: 13,
                1442209676530815076: 0,
                1442209801374269682: 3,
                1442208051212976158: 0,
                1437811360208781406: 3,
            }
        )

        ctx.send.assert_awaited_once()
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Current Gatecount:")
        self.assertEqual(embed.color, discord.Color.blue())
        self.assertEqual(
            embed.description,
            "<:stargate:769315278953381928> — **168 SP** | **509 MP**\n"
            "<:gatefinity:1004823049037680702> — **16 SP** | **68 MP**\n"
            "<:gateforce:1097204464919773205> — **3 SP** | **13 MP**\n"
            "<:gateflower:1442240252084486286> — **0 SP** | **3 MP**\n"
            "<:gatelympics:1442208021655715961> — **0 SP** | **3 MP**\n\n"
            "**Total Gates: 209 SP + 711 MP = 920**",
        )

    async def test_gatecount_counts_each_member_only_in_their_highest_sp_and_mp_tier(
        self,
    ):
        member = object()
        role_members = dict.fromkeys(GATE_ROLE_IDS, ())
        role_members.update(
            {
                1348078501986828461: (member,),
                1348078496710135888: (member,),
                798700443979087892: (member,),
                1097204292198338692: (member,),
            }
        )

        ctx = await self.run_gatecount(role_members)

        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(
            embed.description,
            "<:stargate:769315278953381928> — **0 SP** | **0 MP**\n"
            "<:gatefinity:1004823049037680702> — **1 SP** | **0 MP**\n"
            "<:gateforce:1097204464919773205> — **0 SP** | **1 MP**\n"
            "<:gateflower:1442240252084486286> — **0 SP** | **0 MP**\n"
            "<:gatelympics:1442208021655715961> — **0 SP** | **0 MP**\n\n"
            "**Total Gates: 2 SP + 3 MP = 5**",
        )

    async def test_gatecount_reports_unavailable_role_analytics(self):
        guild = _Guild(dict.fromkeys(GATE_ROLE_IDS, ()))
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = nhmisc.RoleAnalyticsStore(
            Path(temp_dir.name) / "role_analytics.sqlite"
        )
        await store.initialize()
        cog = object.__new__(nhmisc.NHMisc)
        cog._role_analytics_store = store
        cog._gate_migration_store = SimpleNamespace(
            get_schema_state=mock.AsyncMock(
                return_value=nhmisc.SchemaState.LEGACY
            )
        )
        ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            r"^Role analytics are unavailable right now$",
        ):
            await nhmisc.NHMisc.gatecount(cog, ctx)

        ctx.send.assert_not_awaited()

    async def test_gatecount_rejects_missing_role_without_sending_an_embed(self):
        ctx = SimpleNamespace(
            guild=_Guild(
                {
                    1348078501986828461: 168,
                    798700443979087892: 509,
                    1348078496710135888: 16,
                }
            ),
            send=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._gate_migration_store = SimpleNamespace(
            get_schema_state=mock.AsyncMock(
                return_value=nhmisc.SchemaState.LEGACY
            )
        )

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            (
                r"^Gatecount is misconfigured: Gatefinity MP role "
                r"\(1004822424921055233\) was not found in this server\.$"
            ),
        ):
            await nhmisc.NHMisc.gatecount(cog, ctx)

        ctx.send.assert_not_awaited()


class TierDistributionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def run_tierdistribution(
        self, role_members, schema_state=nhmisc.SchemaState.LEGACY
    ):
        guild = _Guild(role_members)
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = nhmisc.RoleAnalyticsStore(
            Path(temp_dir.name) / "role_analytics.sqlite"
        )
        await store.initialize()
        generation = await store.next_generation(guild.id)
        members = guild.analytics_members()
        await store.write_generation(guild.id, generation, members)
        await store.activate_generation(guild.id, generation, len(members))

        cog = object.__new__(nhmisc.NHMisc)
        cog._role_analytics_store = store
        cog._gate_migration_store = SimpleNamespace(
            get_schema_state=mock.AsyncMock(return_value=schema_state)
        )
        ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())
        await nhmisc.NHMisc.tierdistribution(cog, ctx)
        return ctx

    async def test_current_tierdistribution_counts_linear_gate_roles_not_boolean(self):
        gate_member = object()
        boolean_only = object()
        members = {role_id: [] for role_id in TIER_COUNTS}
        members.update({role_id: [] for role_id in nhmisc.TARGET_TIER_ROLE_IDS})
        members[nhmisc.TARGET_TIER_ROLE_IDS[0]] = [gate_member]
        members[nhmisc.TARGET_TIER_ROLE_IDS[4]] = [gate_member]
        members[nhmisc.SINGLEPLAYER_COMPLETED_ROLE_ID] = [gate_member, boolean_only]

        ctx = await self.run_tierdistribution(
            members, nhmisc.SchemaState.CURRENT
        )

        description = ctx.send.await_args.kwargs["embed"].description
        self.assertTrue(
            description.endswith(
                "<:stargate:769315278953381928> — **1 Player** (100.0%)"
            )
        )

    async def test_tierdistribution_is_unavailable_while_migrating(self):
        ctx = await self.run_tierdistribution({}, nhmisc.SchemaState.MIGRATING)

        ctx.send.assert_awaited_once_with(
            "Gate reports are unavailable during migration"
        )

    async def test_tierdistribution_shows_ordered_player_percentages(self):
        ctx = await self.run_tierdistribution(
            {**TIER_COUNTS, **GATE_MEMBERSHIP_COUNTS}
        )

        ctx.send.assert_awaited_once()
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Current Tier Distribution:")
        self.assertEqual(embed.color, discord.Color.blue())
        self.assertEqual(
            embed.description,
            "<:stoneTier:757571320945967205> — **120 Players** (3.2%)\n"
            "<:steamTier:757571510880829540> — **260 Players** (6.8%)\n"
            "<:lvTier:757571726790885378> — **300 Players** (7.9%)\n"
            "<:mvTier:757571761159012383> — **420 Players** (11.1%)\n"
            "<:hvTier:757571801961201714> — **500 Players** (13.2%)\n"
            "<:evTier:757571842209873991> — **460 Players** (12.1%)\n"
            "<:ivTier:757571883268046908> — **360 Players** (9.5%)\n"
            "<:luvTier:757571961114066994> — **250 Players** (6.6%)\n"
            "<:zpmTier:757571992500305962> — **160 Players** (4.2%)\n"
            "<:uvTier:757572023269720078> — **90 Players** (2.4%)\n"
            "<:uhvTier:757572062058643467> — **45 Players** (1.2%)\n"
            "<:uevTier:888133083931476009> — **20 Players** (0.5%)\n"
            "<:uivTier:888133292547772467> — **10 Players** (0.3%)\n"
            "<:umvTier:888133377620852776> — **4 Players** (0.1%)\n"
            "<:uxvTier:888133463461494864> — **1 Player** (0.0%)\n"
            "<:stargate:769315278953381928> — **800 Players** (21.1%)",
        )

    async def test_tierdistribution_handles_an_empty_distribution(self):
        ctx = await self.run_tierdistribution(
            dict.fromkeys(ALL_DISTRIBUTION_ROLE_IDS, 0)
        )

        description = ctx.send.await_args.kwargs["embed"].description
        lines = description.splitlines()
        self.assertEqual(len(lines), 16)
        self.assertTrue(
            all(line.endswith("**0 Players** (0.0%)") for line in lines)
        )

    async def test_tierdistribution_rejects_a_missing_tier_role(self):
        counts = {**TIER_COUNTS, **GATE_MEMBERSHIP_COUNTS}
        del counts[757645112267243541]
        ctx = SimpleNamespace(guild=_Guild(counts), send=mock.AsyncMock())
        cog = object.__new__(nhmisc.NHMisc)
        cog._gate_migration_store = SimpleNamespace(
            get_schema_state=mock.AsyncMock(
                return_value=nhmisc.SchemaState.LEGACY
            )
        )

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            r"^Tier distribution is misconfigured: Stone role "
            r"\(757645112267243541\) was not found in this server\.$",
        ):
            await nhmisc.NHMisc.tierdistribution(cog, ctx)

        ctx.send.assert_not_awaited()

    async def test_tierdistribution_rejects_a_missing_gate_role(self):
        counts = {**TIER_COUNTS, **GATE_MEMBERSHIP_COUNTS}
        del counts[1004822424921055233]
        ctx = SimpleNamespace(guild=_Guild(counts), send=mock.AsyncMock())
        cog = object.__new__(nhmisc.NHMisc)
        cog._gate_migration_store = SimpleNamespace(
            get_schema_state=mock.AsyncMock(
                return_value=nhmisc.SchemaState.LEGACY
            )
        )

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            r"^Tier distribution is misconfigured: Gatefinity MP role "
            r"\(1004822424921055233\) was not found in this server\.$",
        ):
            await nhmisc.NHMisc.tierdistribution(cog, ctx)

        ctx.send.assert_not_awaited()

    async def test_tierdistribution_reports_unavailable_role_analytics(self):
        guild = _Guild(dict.fromkeys(ALL_DISTRIBUTION_ROLE_IDS, 0))
        cog = object.__new__(nhmisc.NHMisc)
        cog._role_analytics_store = SimpleNamespace(
            count_matching=mock.AsyncMock(
                side_effect=nhmisc.AnalyticsUnavailableError("not ready")
            )
        )
        cog._gate_migration_store = SimpleNamespace(
            get_schema_state=mock.AsyncMock(
                return_value=nhmisc.SchemaState.LEGACY
            )
        )
        ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            "^Role analytics are unavailable right now$",
        ):
            await nhmisc.NHMisc.tierdistribution(cog, ctx)

        ctx.send.assert_not_awaited()

    async def test_tierdistribution_uses_highest_tier_and_unique_gate_members(self):
        stone_player = object()
        shared_player = object()
        members = {role_id: [] for role_id in ALL_DISTRIBUTION_ROLE_IDS}
        members[757645112267243541] = [stone_player, shared_player]
        members[631180331839389738] = [shared_player]
        members[1348078501986828461] = [shared_player]
        members[798700443979087892] = [shared_player]
        ctx = await self.run_tierdistribution(members)

        description = ctx.send.await_args.kwargs["embed"].description
        self.assertIn(
            "<:stoneTier:757571320945967205> — **1 Player** (33.3%)",
            description,
        )
        self.assertIn(
            "<:mvTier:757571761159012383> — **1 Player** (33.3%)",
            description,
        )
        self.assertTrue(
            description.endswith(
                "<:stargate:769315278953381928> — **1 Player** (33.3%)"
            )
        )


if __name__ == "__main__":
    unittest.main()
