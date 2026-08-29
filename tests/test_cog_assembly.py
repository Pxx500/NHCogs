"""Assembly and contract guards for the Honeypot cog.

These do not exercise the detection pipeline. They cover runtime command,
listener, and loop assembly plus help and info.json metadata.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from tests.harness import PACKAGE_DIR, _isolated_honeypot_modules, _LoopStub


class HoneypotMetadataTests(unittest.TestCase):
    def test_runtime_help_identifies_current_cog_and_repository(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                cog.__version__ = "3.5.0"

                help_text = cog.format_help_for_context(SimpleNamespace())

        self.assertIn(
            "Detect and review suspicious activity with honeypot channels, "
            "image scanning, and join monitoring.",
            help_text,
        )
        self.assertIn("Author: Pxx500", help_text)
        self.assertIn("Cog version: 3.5.0", help_text)
        self.assertIn("Repo name: NHCogs", help_text)
        self.assertIn("Repository: https://github.com/Pxx500/NHCogs", help_text)
        self.assertNotIn("AAA3A", help_text)
        self.assertNotIn("readthedocs", help_text.lower())
        self.assertNotIn("crowdin", help_text.lower())
        self.assertNotIn("commit", help_text.lower())

    def test_info_metadata_describes_current_maintainer_and_scope(self):
        metadata = json.loads((PACKAGE_DIR / "info.json").read_text(encoding="utf-8"))

        self.assertEqual(metadata["author"], ["Pxx500"])
        self.assertEqual(
            metadata["short"],
            "Detect and review suspicious activity with honeypot channels, manual "
            "evidence capture, image scanning, and join monitoring.",
        )
        self.assertEqual(
            metadata["description"],
            "Protect a server with honeypot channels and join monitoring, capture "
            "manual or automated moderation evidence, scan images, and execute "
            "automatic or moderator-approved actions.",
        )


class CogAssemblyContractTests(unittest.TestCase):

    def test_channel_configuration_exposes_central_semantic_categories(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                command_names = {
                    command.qualified_name
                    for command in honeypot.Honeypot.__cog_commands__
                }

        self.assertIn("honeypot channels", command_names)
        self.assertNotIn("honeypot channel", command_names)
        self.assertNotIn("honeypot channel logs", command_names)
        for category in (
            "review",
            "manual-evidence",
            "joinwatch",
            "bait-role",
            "gif-debug",
        ):
            with self.subTest(category=category):
                self.assertIn(f"honeypot channels {category}", command_names)

    def test_every_registered_channel_category_has_declared_command_paths(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                command_names = {
                    command.qualified_name
                    for command in honeypot.Honeypot.__cog_commands__
                }
                categories = honeypot.channel_routing.CHANNEL_CATEGORIES

        for category in categories:
            with self.subTest(category=category.key, path="central"):
                self.assertIsNotNone(category.central_command)
                self.assertIn(
                    f"honeypot channels {category.central_command}",
                    command_names,
                    f"Channel category {category.key} is missing its central command",
                )
            if category.module_command is not None:
                with self.subTest(category=category.key, path="module"):
                    self.assertIn(
                        f"honeypot {category.module_command}",
                        command_names,
                        f"Channel category {category.key} is missing its module command",
                    )

    def test_gif_debug_exposes_runtime_toggle(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                command_names = {
                    command.qualified_name
                    for command in honeypot.Honeypot.__cog_commands__
                }

        self.assertIn("honeypot gifdetector debug toggle", command_names)

    def test_command_listener_and_loop_assembly_matches_contract(self):
        contract = json.loads(
            (Path(__file__).with_name("honeypot_command_contract.json")).read_text(
                encoding="utf-8"
            )
        )
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                registered = getattr(honeypot.Honeypot, "__cog_commands__", ())
                self.assertEqual(
                    sorted(honeypot.Honeypot.__cog_listeners__),
                    contract["listeners"],
                )
                self.assertEqual(
                    sorted(
                        name
                        for name, value in honeypot.Honeypot.__dict__.items()
                        if isinstance(value, _LoopStub)
                    ),
                    contract["loops"],
                )
                for command in registered:
                    with self.subTest(command=command.qualified_name):
                        self.assertEqual(command.callback.__module__, "NHCogs.honeypot.honeypot")
                        self.assertTrue(
                            command.callback.__qualname__.startswith("Honeypot.")
                        )
                        self.assertEqual(
                            isinstance(command, honeypot.commands.Group),
                            command.kind == "group",
                        )
