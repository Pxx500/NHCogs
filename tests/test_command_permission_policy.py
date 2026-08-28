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
    "commands.has_guild_permissions",
    "commands.is_owner",
    "commands.mod_or_permissions",
}

DIRECT_CALLER_PERMISSION_DECORATORS = {
    "app_commands.default_permissions",
    "app_commands.checks.has_permissions",
    "commands.has_permissions",
    "discord.app_commands.default_permissions",
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


def _permission_arguments(call: ast.Call) -> dict[str, bool] | None:
    if call.args:
        return None
    permissions: dict[str, bool] = {}
    for keyword in call.keywords:
        if keyword.arg is None or not isinstance(keyword.value, ast.Constant):
            return None
        if not isinstance(keyword.value.value, bool):
            return None
        permissions[keyword.arg] = keyword.value.value
    return permissions


def _authorization_violations(tree: ast.AST, source: str) -> list[str]:
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for decorator in node.decorator_list:
            for call in (
                child for child in ast.walk(decorator) if isinstance(child, ast.Call)
            ):
                decorator_name = _dotted_name(call.func)

                if decorator_name in FORBIDDEN_CALLER_DECORATORS:
                    violations.append(
                        f"{source}:{call.lineno}: {decorator_name}"
                    )

                if decorator_name in DIRECT_CALLER_PERMISSION_DECORATORS:
                    permissions = _permission_arguments(call)
                    if permissions != {"manage_messages": True}:
                        violations.append(
                            f"{source}:{call.lineno}: {decorator_name} must require "
                            f"only manage_messages=True, got {permissions!r}"
                        )

                if decorator_name not in CUSTOM_CALLER_CHECK_DECORATORS:
                    continue

                referenced_names = {
                    child.attr
                    for child in ast.walk(call)
                    if isinstance(child, ast.Attribute)
                }
                for check_name in sorted(
                    referenced_names & FORBIDDEN_CUSTOM_CHECK_REFERENCES
                ):
                    violations.append(
                        f"{source}:{call.lineno}: forbidden caller check {check_name}"
                    )

                for attribute in (
                    child for child in ast.walk(call) if isinstance(child, ast.Attribute)
                ):
                    parts = _dotted_name(attribute).split(".")
                    if len(parts) < 2 or parts[-2] != "guild_permissions":
                        continue
                    permission = parts[-1]
                    if permission != "manage_messages":
                        violations.append(
                            f"{source}:{call.lineno}: custom caller check uses "
                            f"{permission}"
                        )

    return violations


class CommandPermissionPolicyTests(unittest.TestCase):
    def test_commands_do_not_use_forbidden_caller_authorization(self) -> None:
        violations: list[str] = []

        for path in sorted(PACKAGE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            relative_path = path.relative_to(PACKAGE_ROOT.parent)

            violations.extend(_authorization_violations(tree, str(relative_path)))

        self.assertEqual(
            violations,
            [],
            "Forbidden command authorization found:\n" + "\n".join(violations),
        )

    def test_policy_rejects_wrong_and_composed_caller_permissions(self) -> None:
        source = """
@commands.has_permissions(manage_guild=True)
async def manage_guild_command(ctx):
    pass

@commands.has_guild_permissions(manage_messages=True)
async def guild_permission_command(ctx):
    pass

@commands.check_any(
    commands.has_permissions(manage_messages=True),
    commands.has_permissions(administrator=True),
)
async def composed_command(ctx):
    pass

@commands.permissions_check(lambda ctx: ctx.author.guild_permissions.ban_members)
async def custom_permission_command(ctx):
    pass
"""

        violations = _authorization_violations(ast.parse(source), "example.py")

        self.assertTrue(any("manage_guild" in violation for violation in violations))
        self.assertTrue(
            any("has_guild_permissions" in violation for violation in violations)
        )
        self.assertTrue(any("administrator" in violation for violation in violations))
        self.assertTrue(any("ban_members" in violation for violation in violations))

    def test_policy_allows_manage_messages_custom_roles_and_bot_permissions(self) -> None:
        source = """
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_webhooks=True)
async def moderator_command(ctx):
    pass

@commands.check(lambda ctx: any(role.id == 10 for role in ctx.author.roles))
async def custom_role_command(ctx):
    pass
"""

        self.assertEqual(
            _authorization_violations(ast.parse(source), "example.py"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
