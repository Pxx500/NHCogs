from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "NHCogs"

FORBIDDEN_CALLER_DECORATORS = {
    "commands.admin",
    "commands.admin_or_permissions",
    "commands.guildowner",
    "commands.guildowner_or_permissions",
    "commands.is_owner",
    "commands.mod_or_permissions",
}

FORBIDDEN_CALLER_PERMISSIONS = {
    "administrator",
    "manage_webhooks",
}

CALLER_PERMISSION_DECORATORS = {
    "app_commands.checks.has_permissions",
    "commands.has_guild_permissions",
    "commands.has_permissions",
    "discord.app_commands.checks.has_permissions",
}

CUSTOM_CALLER_CHECK_DECORATORS = {
    "commands.check",
    "commands.permissions_check",
}

FORBIDDEN_CUSTOM_CHECK_REFERENCES = {
    "administrator",
    "is_admin",
    "is_owner",
    "manage_webhooks",
    "owner_id",
    "owner_ids",
}


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _enabled_keywords(call: ast.Call) -> set[str]:
    enabled: set[str] = set()
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        if isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
            enabled.add(keyword.arg)
    return enabled


class CommandPermissionPolicyTests(unittest.TestCase):
    def test_commands_do_not_use_forbidden_caller_authorization(self) -> None:
        violations: list[str] = []

        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            relative_path = path.relative_to(PACKAGE_ROOT.parent)

            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue

                for decorator in node.decorator_list:
                    call = decorator if isinstance(decorator, ast.Call) else None
                    target = call.func if call is not None else decorator
                    decorator_name = _dotted_name(target)

                    if decorator_name in FORBIDDEN_CALLER_DECORATORS:
                        violations.append(
                            f"{relative_path}:{decorator.lineno}: {decorator_name}"
                        )

                    if (
                        decorator_name in CALLER_PERMISSION_DECORATORS
                        and call is not None
                    ):
                        forbidden_permissions = (
                            _enabled_keywords(call) & FORBIDDEN_CALLER_PERMISSIONS
                        )
                        for permission in sorted(forbidden_permissions):
                            violations.append(
                                f"{relative_path}:{decorator.lineno}: "
                                f"caller permission {permission}"
                            )

                    if decorator_name in CUSTOM_CALLER_CHECK_DECORATORS:
                        referenced_names = {
                            child.attr
                            for child in ast.walk(decorator)
                            if isinstance(child, ast.Attribute)
                        }
                        for check_name in sorted(
                            referenced_names & FORBIDDEN_CUSTOM_CHECK_REFERENCES
                        ):
                            violations.append(
                                f"{relative_path}:{decorator.lineno}: "
                                f"Red bot check {check_name}"
                            )

        self.assertEqual(
            violations,
            [],
            "Forbidden command authorization found:\n" + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
