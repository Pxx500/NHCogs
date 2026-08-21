import unittest

from NHCogsMigrator.inventory import (
    snapshot_global_inventory,
    snapshot_suite_inventory,
)


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

    def application_callback(self):
        return None


class FakeController:
    def __init__(self, cog):
        self.cog = cog

    def application_callback(self):
        return None


class FakeApplicationCommand:
    def __init__(self, name, command_type, callback):
        self.name = name
        self.type = command_type
        self.callback = callback


class FakeView:
    def __init__(self, cog, custom_id):
        self.cog = cog
        self.children = [FakeViewItem(custom_id), FakeViewItem(None)]


class FakeViewItem:
    def __init__(self, custom_id):
        self.custom_id = custom_id


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
        honeypot_controller = FakeController(self.cogs["Honeypot"])
        unrelated_cog = FakeCog([], [])
        self.tree = FakeTree(
            [
                FakeApplicationCommand(
                    "View Achievements",
                    2,
                    self.cogs["NHMisc"].application_callback,
                ),
                FakeApplicationCommand(
                    "Punish",
                    3,
                    honeypot_controller.application_callback,
                ),
                FakeApplicationCommand(
                    "Unrelated command",
                    1,
                    unrelated_cog.application_callback,
                ),
            ]
        )
        self.persistent_views = [
            FakeView(self.cogs["Honeypot"], "case:resolve"),
            FakeView(unrelated_cog, "unrelated:view"),
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

    def test_unrelated_application_commands_and_views_do_not_change_inventory(self):
        bot = FakeBot()
        before = snapshot_suite_inventory(bot, ("NHMisc", "Honeypot"))
        unrelated_cog = FakeCog([], [])
        bot.tree._commands.append(
            FakeApplicationCommand(
                "Another unrelated command",
                1,
                unrelated_cog.application_callback,
            )
        )
        bot.persistent_views.append(FakeView(unrelated_cog, "unrelated:second"))

        after = snapshot_suite_inventory(bot, ("NHMisc", "Honeypot"))

        self.assertEqual(after, before)

    def test_global_snapshot_preserves_pre_scope_inventory_format(self):
        inventory = snapshot_global_inventory(FakeBot(), ("NHMisc", "Honeypot"))

        self.assertIn("1:Unrelated command", inventory.application_commands)
        self.assertIn("unrelated:view", inventory.persistent_view_custom_ids)

    def test_missing_target_cog_is_blocking(self):
        bot = FakeBot()
        bot.cogs.pop("Honeypot")

        with self.assertRaisesRegex(RuntimeError, "Honeypot"):
            snapshot_suite_inventory(bot, ("NHMisc", "Honeypot"))
