"""Guard against rendering `str`-Enum members instead of their values.

`class X(str, Enum)` members format differently across the supported
interpreters: Python 3.10 renders the value, 3.11+ renders
``ClassName.MEMBER``. CI runs 3.10 while production runs newer versions, so an
interpolation without ``.value`` is invisible to both the suite and CI, and can
reach moderator-facing text. The refactor plan required this check for
`OperationType`; it is enforced here for every `str`-Enum in the package.

Deliberately conservative to stay free of false positives:

* a bare name (``f"{action}"``) carries no reliable type information, so it is
  never flagged - the store boundary is what guarantees enum decoding;
* an attribute name annotated as an enum in one place and as a plain type in
  another is ambiguous and is skipped. ``delete_status`` is exactly that case:
  `detection_cases.MessageRecord.delete_status` is a `DeleteStatus`, while
  `case_review.CaseTimelineMessage.delete_status` is the already-rendered
  human label that the timeline card interpolates.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "NHCogs" / "honeypot"


def _source_files() -> list[Path]:
    return sorted(PACKAGE_DIR.rglob("*.py"))


def _annotations(tree: ast.Module) -> list[tuple[str, str]]:
    pairs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            pairs.append((node.target.id, ast.unparse(node.annotation)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = list(node.args.args) + list(node.args.kwonlyargs)
            pairs.extend(
                (argument.arg, ast.unparse(argument.annotation))
                for argument in arguments
                if argument.annotation is not None
            )
    return pairs


def _str_enum_names(trees: dict[Path, ast.Module]) -> set[str]:
    names = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {ast.unparse(base) for base in node.bases}
            if "Enum" in bases and ("str" in bases or "StrEnum" in bases):
                names.add(node.name)
    return names


def _unambiguous_enum_fields(
    trees: dict[Path, ast.Module], enum_names: set[str]
) -> set[str]:
    """Names annotated as a `str`-Enum everywhere they are annotated at all."""
    enum_typed: set[str] = set()
    other_typed: set[str] = set()
    for tree in trees.values():
        for name, annotation in _annotations(tree):
            if any(enum_name in annotation for enum_name in enum_names):
                enum_typed.add(name)
            else:
                other_typed.add(name)
    return enum_typed - other_typed


def _interpolated_expressions(tree: ast.Module) -> list[tuple[int, ast.expr]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    found.append((value.lineno, value.value))
        elif isinstance(node, ast.Call):
            rendered = ast.unparse(node.func)
            if rendered == "str" or rendered.endswith(".format"):
                found.extend((node.lineno, argument) for argument in node.args)
    return found


def _violations(
    trees: dict[Path, ast.Module], enum_names: set[str], enum_fields: set[str]
) -> list[str]:
    violations = []
    for path, tree in trees.items():
        for lineno, expression in _interpolated_expressions(tree):
            if not isinstance(expression, ast.Attribute):
                continue
            rendered = ast.unparse(expression)
            if rendered.endswith(".value"):
                continue
            owner = expression.value
            direct_member = isinstance(owner, ast.Name) and owner.id in enum_names
            if direct_member or expression.attr in enum_fields:
                violations.append(f"{path.name}:{lineno}: {rendered}")
    return sorted(violations)


class EnumRenderingGuardTests(unittest.TestCase):

    def setUp(self):
        self.trees = {
            path: ast.parse(path.read_text(encoding="utf-8"))
            for path in _source_files()
        }
        self.enum_names = _str_enum_names(self.trees)
        self.enum_fields = _unambiguous_enum_fields(self.trees, self.enum_names)

    def test_scanner_sees_the_package(self):
        self.assertIn("honeypot.py", {path.name for path in self.trees})
        self.assertIn("OperationType", self.enum_names)
        self.assertIn("DeleteStatus", self.enum_names)
        self.assertIn("operation_type", self.enum_fields)

    def test_ambiguous_field_names_are_not_flagged(self):
        # delete_status is DeleteStatus on the store record and a rendered label
        # on the timeline projection; flagging it produced a false positive once.
        self.assertNotIn("delete_status", self.enum_fields)

    def test_scanner_detects_a_synthetic_violation(self):
        """Keeps the guard from passing vacuously if the rules stop matching."""
        synthetic = ast.parse(
            'alert = f"failed {operation.operation_type}"\n'
            'label = f"{DeleteStatus.DELETED}"\n'
            'safe = f"failed {operation.operation_type.value}"\n'
        )

        violations = _violations(
            {Path("synthetic.py"): synthetic}, self.enum_names, self.enum_fields
        )

        self.assertEqual(
            violations,
            [
                "synthetic.py:1: operation.operation_type",
                "synthetic.py:2: DeleteStatus.DELETED",
            ],
        )

    def test_no_str_enum_is_rendered_without_value(self):
        violations = _violations(self.trees, self.enum_names, self.enum_fields)

        self.assertEqual(
            violations,
            [],
            "str-Enum interpolated without .value; renders as ClassName.MEMBER on "
            "Python 3.11+ but as the value on the 3.10 CI target",
        )


if __name__ == "__main__":
    unittest.main()
