import types
import unittest
from unittest import mock

from NHCogsMigrator.red_runtime import RedRuntime, RedRuntimeError


class PackageValue:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self

    def __await__(self):
        async def read():
            return list(self.values)

        return read().__await__()

    async def __aenter__(self):
        return self.values

    async def __aexit__(self, *_args):
        return None


class FakeCogManager:
    def __init__(self):
        self.specs = {"NHCogs": types.SimpleNamespace(name="NHCogs")}

    async def find_cog(self, name):
        return self.specs.get(name)


class FakeBot:
    def __init__(self):
        self._config = types.SimpleNamespace(
            packages=PackageValue(["NHMisc", "OtherCog", "Honeypot"])
        )
        self._cog_mgr = FakeCogManager()
        self.extensions = {}
        self.cogs = {}
        self.loaded_specs = []
        self.unloaded_extensions = []

    async def load_extension(self, spec):
        self.loaded_specs.append(spec)
        self.extensions[spec.name] = types.SimpleNamespace(__name__=spec.name)

    async def unload_extension(self, name):
        self.unloaded_extensions.append(name)
        self.extensions.pop(name, None)

    def get_cog(self, name):
        return self.cogs.get(name)


class RedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_handle_does_not_require_a_loaded_cog(self):
        bot = FakeBot()
        runtime = RedRuntime(bot)
        handle = object()
        config_type = types.SimpleNamespace(get_conf=mock.Mock(return_value=handle))
        redbot = types.ModuleType("redbot")
        core = types.ModuleType("redbot.core")
        core.Config = config_type
        redbot.core = core

        with mock.patch.dict(
            "sys.modules",
            {"redbot": redbot, "redbot.core": core},
        ):
            observed = runtime.config_for_cog("NHMisc", 123)

        self.assertIs(observed, handle)
        config_type.get_conf.assert_called_once_with(
            None,
            123,
            cog_name="NHMisc",
        )

    async def test_direct_load_and_unload_do_not_change_persisted_packages(self):
        bot = FakeBot()
        runtime = RedRuntime(bot)

        extension_key = await runtime.load_extension("NHCogs")
        bot.cogs["NHMisc"] = types.SimpleNamespace(
            __class__=types.SimpleNamespace(__module__="NHCogs.nhmisc.nhmisc")
        )
        await runtime.unload_extension(extension_key)

        self.assertEqual(extension_key, "NHCogs")
        self.assertEqual(bot.loaded_specs[0].name, "NHCogs")
        self.assertEqual(bot.unloaded_extensions, ["NHCogs"])
        self.assertEqual(
            await runtime.persisted_packages(),
            ("NHMisc", "OtherCog", "Honeypot"),
        )

    async def test_package_replacement_is_ordered_and_compare_and_swap(self):
        bot = FakeBot()
        runtime = RedRuntime(bot)
        original = ("NHMisc", "OtherCog", "Honeypot")
        replacement = ("NHCogs", "OtherCog")

        await runtime.replace_persisted_packages(original, replacement)

        self.assertEqual(await runtime.persisted_packages(), replacement)
        with self.assertRaises(RedRuntimeError):
            await runtime.replace_persisted_packages(original, ("NHCogs",))
        self.assertEqual(await runtime.persisted_packages(), replacement)

    async def test_extension_key_is_derived_from_cog_module_origin(self):
        bot = FakeBot()
        runtime = RedRuntime(bot)
        root = types.SimpleNamespace(__name__="NHCogs")
        bot.extensions["NHCogs"] = root

        class NestedCog:
            pass

        NestedCog.__module__ = "NHCogs.nhmisc.nhmisc"
        bot.cogs["NHMisc"] = NestedCog()

        self.assertEqual(runtime.extension_key_for_cog("NHMisc"), "NHCogs")

    async def test_background_health_uses_each_cog_owned_snapshot(self):
        bot = FakeBot()
        runtime = RedRuntime(bot)

        class HealthyCog:
            RUNTIME_HEALTH_VERSION = 1

            def runtime_health_issues(self):
                return ()

        class UnhealthyCog:
            RUNTIME_HEALTH_VERSION = 1

            def runtime_health_issues(self):
                return ("daily worker failed",)

        bot.cogs["NHMisc"] = HealthyCog()
        bot.cogs["Honeypot"] = UnhealthyCog()

        self.assertEqual(
            runtime.background_health_issues(("NHMisc", "Honeypot")),
            ("Honeypot: daily worker failed",),
        )
