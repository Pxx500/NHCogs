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
    def __init__(self, member_counts):
        self._roles = {
            role_id: SimpleNamespace(members=[object()] * count)
            for role_id, count in member_counts.items()
        }

    def get_role(self, role_id):
        return self._roles.get(role_id)


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


if __name__ == "__main__":
    unittest.main()
