import importlib.util
import sys
import types
import unittest
from pathlib import Path
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


class _Guild:
    def __init__(self, role_members):
        self._roles = {}
        for role_id, members in role_members.items():
            if isinstance(members, int):
                members = [object()] * members
            self._roles[role_id] = SimpleNamespace(members=list(members))

    def get_role(self, role_id):
        return self._roles.get(role_id)


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


class GatecountCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_gatecount_shows_weighted_sp_mp_and_combined_totals(self):
        ctx = SimpleNamespace(
            guild=_Guild(
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
            ),
            send=mock.AsyncMock(),
        )

        await nhmisc.NHMisc.gatecount(None, ctx)

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

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            (
                r"^Gatecount is misconfigured: Gatefinity MP role "
                r"\(1004822424921055233\) was not found in this server\.$"
            ),
        ):
            await nhmisc.NHMisc.gatecount(None, ctx)

        ctx.send.assert_not_awaited()


class TierDistributionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_tierdistribution_shows_ordered_player_percentages(self):
        ctx = SimpleNamespace(
            guild=_Guild({**TIER_COUNTS, **GATE_MEMBERSHIP_COUNTS}),
            send=mock.AsyncMock(),
        )

        await nhmisc.NHMisc.tierdistribution(None, ctx)

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
            "Gate — **800 Players** (21.1%)",
        )

    async def test_tierdistribution_handles_an_empty_distribution(self):
        ctx = SimpleNamespace(
            guild=_Guild({role_id: 0 for role_id in ALL_DISTRIBUTION_ROLE_IDS}),
            send=mock.AsyncMock(),
        )

        await nhmisc.NHMisc.tierdistribution(None, ctx)

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

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            r"^Tier distribution is misconfigured: Stone role "
            r"\(757645112267243541\) was not found in this server\.$",
        ):
            await nhmisc.NHMisc.tierdistribution(None, ctx)

        ctx.send.assert_not_awaited()

    async def test_tierdistribution_rejects_a_missing_gate_role(self):
        counts = {**TIER_COUNTS, **GATE_MEMBERSHIP_COUNTS}
        del counts[1004822424921055233]
        ctx = SimpleNamespace(guild=_Guild(counts), send=mock.AsyncMock())

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            r"^Tier distribution is misconfigured: Gatefinity MP role "
            r"\(1004822424921055233\) was not found in this server\.$",
        ):
            await nhmisc.NHMisc.tierdistribution(None, ctx)

        ctx.send.assert_not_awaited()

    async def test_tierdistribution_counts_duplicate_role_memberships(self):
        shared_player = object()
        members = {role_id: [] for role_id in ALL_DISTRIBUTION_ROLE_IDS}
        members[757645112267243541] = [shared_player]
        members[1348078501986828461] = [shared_player]
        members[798700443979087892] = [shared_player]
        ctx = SimpleNamespace(guild=_Guild(members), send=mock.AsyncMock())

        await nhmisc.NHMisc.tierdistribution(None, ctx)

        description = ctx.send.await_args.kwargs["embed"].description
        self.assertIn(
            "<:stoneTier:757571320945967205> — **1 Player** (33.3%)",
            description,
        )
        self.assertTrue(description.endswith("Gate — **2 Players** (66.7%)"))


if __name__ == "__main__":
    unittest.main()
