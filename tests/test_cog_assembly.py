"""Assembly and contract guards for the Honeypot cog.

These do not exercise the detection pipeline: they pin the shape of the cog as
it is assembled - the command, listener and loop inventory against
tests/honeypot_command_contract.json, the README divergence that contract
records, the Phase 5 domain-shell delegation, and the runtime help and
info.json metadata.
"""

import ast
import json
import re
import unittest
from hashlib import sha256
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
            "errors",
            "manual-evidence",
            "joinwatch",
            "bait-role",
            "gif-debug",
            "mement-notifications",
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
                self.assertEqual(len(registered), contract["command_count"])
                structure = sorted(
                    (
                        {
                            "kind": command.kind,
                            "name": command.qualified_name,
                            "parent": (
                                command.parent.qualified_name
                                if command.parent is not None
                                else None
                            ),
                        }
                        for command in registered
                    ),
                    key=lambda item: item["name"],
                )
                encoded = json.dumps(
                    structure,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()

                self.assertEqual(
                    sha256(encoded).hexdigest(),
                    contract["command_structure_sha256"],
                )
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
                        self.assertEqual(command.callback.__module__, "Honeypot.honeypot")
                        self.assertTrue(
                            command.callback.__qualname__.startswith("Honeypot.")
                        )
                        self.assertEqual(
                            isinstance(command, honeypot.commands.Group),
                            command.kind == "group",
                        )

    def test_readme_command_divergence_matches_the_contract(self):
        """Phase 5 rail 2, as an assertion rather than a claim in the ledger.

        The plan wanted `inventory - allowlist == readme_rows`. That is not
        reachable: the README documents whole sections under command paths that
        do not exist (`honeypot core ...` for the `honeypot honeypot ...` group,
        `honeypot bait ...` for `bait_role`), and several real commands have no
        row at all. Both sets are therefore frozen exactly, so a split that
        loses a command, or a README edit, has to face this test.
        """
        contract = json.loads(
            (Path(__file__).with_name("honeypot_command_contract.json")).read_text(
                encoding="utf-8"
            )
        )
        expected = contract["readme"]
        readme_row = re.compile(r"^\|\s*`([^`]+)`\s*\|")
        rows = set()
        readme_path = PACKAGE_DIR / "README.md"
        for line in readme_path.read_text(encoding="utf-8").splitlines():
            match = readme_row.match(line)
            if match is None:
                continue
            command = match.group(1).strip()
            if command.startswith("!"):
                command = command[1:]
            elif command.startswith("[p]"):
                command = command[3:]
            else:
                continue
            tokens = []
            for token in command.split():
                if token.startswith(("<", "[")):
                    break
                tokens.append(token)
            rows.add(" ".join(tokens))

        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                leaves = {
                    command.qualified_name
                    for command in honeypot.Honeypot.__cog_commands__
                    if command.kind == "command"
                }

        self.assertEqual(len(rows), expected["row_count"])
        self.assertEqual(len(leaves), expected["leaf_count"])
        self.assertEqual(
            sorted(leaves - rows),
            expected["undocumented_commands"],
            "a command gained or lost its README row; fix the README or update "
            "the contract in the same commit",
        )
        self.assertEqual(
            sorted(rows - leaves),
            expected["rows_without_command"],
            "the README documents a command path that does not exist, or a "
            "documented command disappeared from the cog",
        )
        self.assertLessEqual(
            set(contract["intentionally_undocumented_debug_commands"]),
            leaves - rows,
            "the debug allow-list must stay a subset of the undocumented set",
        )

    def test_domain_shells_delegate_to_a_matching_twin(self):
        """Structural guard for the Phase 5 fallback across every domain module.

        A shell that delegates to the wrong twin - `imagescan_remove` calling
        `imagescan.imagescan_add` - renders no test failure for the commands the
        suite does not drive. Counts are exact so a lost delegation fails too;
        a new split row updates them deliberately.
        """
        expected_counts = {
            "channel_routing": 27,
            "detection": 73,
            "diagnostics": 12,
            "gif_detector": 9,
            "imagescan": 21,
            "joinwatch": 15,
            "review_publication": 13,
        }
        tree = ast.parse((PACKAGE_DIR / "honeypot.py").read_text(encoding="utf-8"))
        cog_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Honeypot"
        )
        # A seam can also be re-exported as `name = staticmethod(module.name)`.
        # That is an Assign, invisible to the delegation scan, so it is counted
        # and name-checked separately rather than silently escaping the guard.
        expected_static_reexports = {"detection": 4, "review_publication": 2}
        static_reexports = {}
        delegations = {}
        for node in cog_class.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                is_staticmethod = (
                    isinstance(call.func, ast.Name)
                    and call.func.id == "staticmethod"
                    and len(call.args) == 1
                    and isinstance(call.args[0], ast.Attribute)
                    and isinstance(call.args[0].value, ast.Name)
                )
                if is_staticmethod and len(node.targets) == 1:
                    target = node.targets[0]
                    owner = call.args[0].value.id
                    if isinstance(target, ast.Name) and owner in expected_counts:
                        static_reexports.setdefault(owner, []).append(
                            (target.id, call.args[0].attr)
                        )
                continue
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = [
                statement
                for statement in node.body
                if not (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                )
            ]
            if len(body) != 1 or not isinstance(body[0], ast.Return):
                continue
            call = body[0].value
            if isinstance(call, ast.Await):
                call = call.value
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            owner = call.func.value
            if not isinstance(owner, ast.Name) or owner.id not in expected_counts:
                continue
            delegations.setdefault(owner.id, []).append(
                (node.name, call.func.attr, call.args[0] if call.args else None)
            )

        self.assertEqual(
            {name: len(items) for name, items in sorted(delegations.items())},
            expected_counts,
        )
        self.assertEqual(
            {name: len(items) for name, items in sorted(static_reexports.items())},
            expected_static_reexports,
        )
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                for module_name, items in sorted(static_reexports.items()):
                    module = getattr(honeypot, module_name, None)
                    self.assertIsNotNone(module, f"{module_name} is not importable")
                    for attribute_name, target_name in items:
                        with self.subTest(reexport=f"{module_name}.{attribute_name}"):
                            self.assertEqual(
                                target_name,
                                attribute_name,
                                "re-export points at a differently named twin",
                            )
                            self.assertIs(
                                getattr(honeypot.Honeypot, attribute_name),
                                getattr(module, target_name),
                            )
                for module_name, items in sorted(delegations.items()):
                    module = getattr(honeypot, module_name, None)
                    self.assertIsNotNone(module, f"{module_name} is not importable")
                    for method_name, target_name, first_argument in items:
                        with self.subTest(shell=f"{module_name}.{method_name}"):
                            if module_name == "channel_routing":
                                self.assertIn(
                                    target_name,
                                    {
                                        "add_multiple",
                                        "configure_single",
                                        "list_multiple",
                                        "remove_multiple",
                                        "send_overview",
                                    },
                                )
                            else:
                                self.assertEqual(
                                    target_name,
                                    method_name,
                                    "shell delegates to a differently named twin",
                                )
                            self.assertTrue(
                                isinstance(first_argument, ast.Name)
                                and first_argument.id == "self",
                                "delegation must pass the cog as its first argument",
                            )
                            self.assertTrue(
                                callable(getattr(module, target_name, None)),
                                f"{module_name}.{target_name} is missing",
                            )
