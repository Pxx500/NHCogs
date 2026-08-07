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
        self.fields = []
        self.footer = None

    def add_field(self, *, name, value, inline=True):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))

    def set_footer(self, *, text):
        self.footer = SimpleNamespace(text=text)


class _AllowedMentions:
    def __init__(self, **kwargs):
        self.users = kwargs.get("users")
        self.everyone = kwargs.get("everyone")
        self.roles = kwargs.get("roles")
        self.replied_user = kwargs.get("replied_user")

    @staticmethod
    def none():
        return "no-mentions"


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
    discord.AllowedMentions = _AllowedMentions

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
    commands.has_permissions = lambda **kwargs: _permission_decorator(
        "has_permissions", kwargs
    )
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
GATE_MEMBERSHIP_COUNTS = dict(
    zip(
        nhmisc.GATE_TIER_ROLE_IDS,
        (200, 100, 75, 75, 75, 0),
        strict=True,
    )
)
ALL_DISTRIBUTION_ROLE_IDS = tuple(TIER_COUNTS) + tuple(GATE_MEMBERSHIP_COUNTS)


class RoleAnalyticsCommandTestCase(unittest.IsolatedAsyncioTestCase):
    async def run_command(self, command, role_members):
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
        ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())
        await command(cog, ctx)
        return ctx


class GatecountCommandTests(RoleAnalyticsCommandTestCase):
    async def test_gatecount_shows_role_mentions_and_highest_linear_tiers(self):
        tier_roles = tuple(nhmisc.GATE_TIER_ROLE_IDS)
        singleplayer = object()
        boolean_only = object()
        tier_six = object()
        members = {role_id: [] for role_id in tier_roles}
        members[nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID] = [
            singleplayer,
            boolean_only,
        ]
        members[tier_roles[0]] = [singleplayer]
        members[tier_roles[2]] = [singleplayer]
        members[tier_roles[5]] = [tier_six]

        ctx = await self.run_command(nhmisc.NHMisc.gatecount, members)

        send_kwargs = ctx.send.await_args.kwargs
        self.assertEqual(
            send_kwargs["embed"].description,
            f"<@&{nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID}> — **2 players**\n"
            f"<@&{tier_roles[0]}> — **0 players**\n"
            f"<@&{tier_roles[1]}> — **0 players**\n"
            f"<@&{tier_roles[2]}> — **1 player**\n"
            f"<@&{tier_roles[3]}> — **0 players**\n"
            f"<@&{tier_roles[4]}> — **0 players**\n"
            f"<@&{tier_roles[5]}> — **1 player**\n\n"
            "**Total Gates: 9**",
        )
        self.assertEqual(send_kwargs["allowed_mentions"], "no-mentions")
        self.assertNotIn("Tier", send_kwargs["embed"].description)

    async def test_gatecount_reports_unavailable_role_analytics(self):
        role_ids = (
            nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            *nhmisc.GATE_TIER_ROLE_IDS,
        )
        guild = _Guild(dict.fromkeys(role_ids, ()))
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = nhmisc.RoleAnalyticsStore(
            Path(temp_dir.name) / "role_analytics.sqlite"
        )
        await store.initialize()
        cog = object.__new__(nhmisc.NHMisc)
        cog._role_analytics_store = store
        ctx = SimpleNamespace(guild=guild, send=mock.AsyncMock())

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            r"^Role analytics are unavailable right now$",
        ):
            await nhmisc.NHMisc.gatecount(cog, ctx)

        ctx.send.assert_not_awaited()

    async def test_gatecount_rejects_a_missing_gate_role(self):
        counts = dict.fromkeys(nhmisc.GATE_TIER_ROLE_IDS, 0)
        counts[nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID] = 0
        missing_role_id = nhmisc.GATE_TIER_ROLE_IDS[1]
        del counts[missing_role_id]
        ctx = SimpleNamespace(guild=_Guild(counts), send=mock.AsyncMock())
        cog = object.__new__(nhmisc.NHMisc)

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            rf"^Gatecount is misconfigured: Tier 2 role \({missing_role_id}\) "
            r"was not found in this server\.$",
        ):
            await nhmisc.NHMisc.gatecount(cog, ctx)

        ctx.send.assert_not_awaited()


class TierDistributionCommandTests(RoleAnalyticsCommandTestCase):
    async def test_tierdistribution_counts_linear_gate_roles_not_boolean(self):
        gate_member = object()
        boolean_only = object()
        members = {role_id: [] for role_id in ALL_DISTRIBUTION_ROLE_IDS}
        members[nhmisc.GATE_TIER_ROLE_IDS[0]] = [gate_member]
        members[nhmisc.GATE_TIER_ROLE_IDS[4]] = [gate_member]
        members[nhmisc.SINGLEPLAYER_GATE_COMPLETED_ROLE_ID] = [
            gate_member,
            boolean_only,
        ]

        ctx = await self.run_command(nhmisc.NHMisc.tierdistribution, members)

        self.assertTrue(
            ctx.send.await_args.kwargs["embed"].description.endswith(
                "<:stargate:769315278953381928> — **1 Player** (100.0%)"
            )
        )

    async def test_tierdistribution_uses_highest_tier_and_unique_gate_members(self):
        stone_player = object()
        shared_player = object()
        members = {role_id: [] for role_id in ALL_DISTRIBUTION_ROLE_IDS}
        members[757645112267243541] = [stone_player, shared_player]
        members[631180331839389738] = [shared_player]
        members[nhmisc.GATE_TIER_ROLE_IDS[0]] = [shared_player]
        members[nhmisc.GATE_TIER_ROLE_IDS[2]] = [shared_player]

        ctx = await self.run_command(nhmisc.NHMisc.tierdistribution, members)

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

    async def test_tierdistribution_handles_an_empty_distribution(self):
        ctx = await self.run_command(
            nhmisc.NHMisc.tierdistribution,
            dict.fromkeys(ALL_DISTRIBUTION_ROLE_IDS, 0),
        )

        lines = ctx.send.await_args.kwargs["embed"].description.splitlines()
        self.assertEqual(len(lines), 16)
        self.assertTrue(
            all(line.endswith("**0 Players** (0.0%)") for line in lines)
        )

    async def test_tierdistribution_rejects_a_missing_gate_role(self):
        counts = dict.fromkeys(ALL_DISTRIBUTION_ROLE_IDS, 0)
        missing_role_id = nhmisc.GATE_TIER_ROLE_IDS[1]
        del counts[missing_role_id]
        ctx = SimpleNamespace(guild=_Guild(counts), send=mock.AsyncMock())
        cog = object.__new__(nhmisc.NHMisc)

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            rf"^Tier distribution is misconfigured: Gate Tier 2 role "
            rf"\({missing_role_id}\) was not found in this server\.$",
        ):
            await nhmisc.NHMisc.tierdistribution(cog, ctx)

        ctx.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
