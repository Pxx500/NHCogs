import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "NHMisc" / "role_expression.py"
SPEC = importlib.util.spec_from_file_location("nhmisc_role_expression_test", MODULE_PATH)
role_expression = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = role_expression
SPEC.loader.exec_module(role_expression)


class RoleExpressionTests(unittest.TestCase):
    def test_operator_precedence_is_not_then_and_then_or(self):
        expression = role_expression.parse_role_expression(
            "11 OR 22 AND NOT 33"
        )

        self.assertEqual(
            role_expression.render_role_expression(expression),
            "<@&11> OR <@&22> AND NOT <@&33>",
        )

    def test_compiler_emits_fixed_exists_predicates_and_parameters(self):
        expression = role_expression.parse_role_expression(
            "<@&11> AND NOT 22"
        )

        sql, parameters = role_expression.compile_role_expression(expression)

        self.assertEqual(
            sql,
            "(EXISTS (SELECT 1 FROM role_analytics_memberships AS membership "
            "WHERE membership.guild_id = member.guild_id "
            "AND membership.generation = member.generation "
            "AND membership.user_id = member.user_id "
            "AND membership.role_id = ?) AND "
            "(NOT EXISTS (SELECT 1 FROM role_analytics_memberships AS membership "
            "WHERE membership.guild_id = member.guild_id "
            "AND membership.generation = member.generation "
            "AND membership.user_id = member.user_id "
            "AND membership.role_id = ?)))",
        )
        self.assertEqual(parameters, (11, 22))

    def test_input_longer_than_one_thousand_characters_is_rejected(self):
        with self.assertRaises(role_expression.RoleExpressionLimitError):
            role_expression.parse_role_expression("1" + " " * 1000)

    def test_more_than_sixty_four_tokens_is_rejected(self):
        expression = " OR ".join(str(role_id) for role_id in range(1, 34))

        with self.assertRaises(role_expression.RoleExpressionLimitError):
            role_expression.parse_role_expression(expression)

    def test_more_than_twenty_unique_roles_is_rejected(self):
        expression = " OR ".join(str(role_id) for role_id in range(1, 22))

        with self.assertRaises(role_expression.RoleExpressionLimitError):
            role_expression.parse_role_expression(expression)

    def test_parentheses_nested_deeper_than_sixteen_are_rejected(self):
        expression = "(" * 17 + "1" + ")" * 17

        with self.assertRaises(role_expression.RoleExpressionLimitError):
            role_expression.parse_role_expression(expression)


if __name__ == "__main__":
    unittest.main()
