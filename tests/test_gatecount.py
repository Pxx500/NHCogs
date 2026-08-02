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


GATE_ROLE_IDS = (
    1348078501986828461,
    798700443979087892,
    1348078496710135888,
    1004822424921055233,
    1348078483384958986,
    1097204292198338692,
    1442209676530815076,
    1442209801374269682,
    1442208051212976158,
    1437811360208781406,
)


class GatecountCommandTests(unittest.IsolatedAsyncioTestCase):
    async def run_gatecount(self, role_members):
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
        await nhmisc.NHMisc.gatecount(cog, ctx)
        return ctx

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

        with self.assertRaisesRegex(
            commands.UserFeedbackCheckFailure,
            (
                r"^Gatecount is misconfigured: Gatefinity MP role "
                r"\(1004822424921055233\) was not found in this server\.$"
            ),
        ):
            await nhmisc.NHMisc.gatecount(None, ctx)

        ctx.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
