import unittest

from tests.storage_loader import load_shared_storage

load_shared_storage()

from NHCogs.nhmoderation.command_inputs import (  # noqa: E402
    BanChartArguments,
    parse_banchart_arguments,
)


class BanChartArgumentTests(unittest.TestCase):
    def test_empty_arguments_use_accepted_defaults(self):
        self.assertEqual(
            parse_banchart_arguments(""),
            BanChartArguments(days=30, amount=10, include_automation=False),
        )

    def test_all_amount_and_automation_can_be_combined(self):
        self.assertEqual(
            parse_banchart_arguments("all 15 --automation"),
            BanChartArguments(days=None, amount=15, include_automation=True),
        )

    def test_unknown_flag_returns_useful_error(self):
        with self.assertRaisesRegex(ValueError, "Unknown option: --source"):
            parse_banchart_arguments("30 10 --source")

    def test_days_and_amount_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "Days must be at least 1"):
            parse_banchart_arguments("0")
        with self.assertRaisesRegex(ValueError, "Amount must be between 1 and 20"):
            parse_banchart_arguments("30 21")
