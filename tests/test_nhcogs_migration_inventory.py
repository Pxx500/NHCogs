import types
import unittest

from NHCogsMigrator.inventory import snapshot_suite_inventory


class FakeCommand:
    def __init__(self, name, aliases=()):
        self.qualified_name = name
        self.aliases = list(aliases)


class FakeCog:
    def __init__(self, commands, listeners):
        self._commands = commands
        self._listeners = listeners

    def walk_commands(self):
        return iter(self._commands)

    def get_listeners(self):
        return list(self._listeners)


class FakeTree:
    def __init__(self, commands):
        self._commands = commands

    def get_commands(self):
        return list(self._commands)


class FakeBot:
    def __init__(self):
        def callback():
            return None

        self.cogs = {
            "NHMisc": FakeCog(
                [FakeCommand("nhmisc log moderation", aliases=("modlog",))],
                [("on_member_update", callback)],
            ),
            "Honeypot": FakeCog(
                [FakeCommand("honeypot errors")],
                [("on_message", lambda: None)],
            ),
        }
        self.tree = FakeTree(
            [
                types.SimpleNamespace(name="View Achievements", type=2),
                types.SimpleNamespace(name="Punish", type=3),
            ]
        )
        self.persistent_views = [
            types.SimpleNamespace(
                children=[
                    types.SimpleNamespace(custom_id="case:resolve"),
                    types.SimpleNamespace(custom_id=None),
                ]
            )
        ]

    def get_cog(self, name):
        return self.cogs.get(name)


class SuiteInventoryTests(unittest.TestCase):
    def test_snapshot_is_stable_across_module_relocation(self):
        first = snapshot_suite_inventory(FakeBot(), ("NHMisc", "Honeypot"))
        second = snapshot_suite_inventory(FakeBot(), ("NHMisc", "Honeypot"))

        self.assertEqual(first, second)
        self.assertEqual(
            first.prefix_commands,
            (
                "Honeypot:honeypot errors:",
                "NHMisc:nhmisc log moderation:modlog",
            ),
        )
        self.assertEqual(
            first.application_commands,
            ("2:View Achievements", "3:Punish"),
        )
        self.assertEqual(first.persistent_view_custom_ids, ("case:resolve",))

    def test_missing_target_cog_is_blocking(self):
        bot = FakeBot()
        bot.cogs.pop("Honeypot")

        with self.assertRaisesRegex(RuntimeError, "Honeypot"):
            snapshot_suite_inventory(bot, ("NHMisc", "Honeypot"))
