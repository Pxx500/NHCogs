import importlib.util
import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

SETTINGS_PATH = (
    Path(__file__).parents[1] / "NHCogs" / "githubtickets" / "settings.py"
)
INFO_PATH = Path(__file__).parents[1] / "NHCogs" / "githubtickets" / "info.json"


def load_settings_module():
    name = "githubtickets_settings_test"
    spec = importlib.util.spec_from_file_location(name, SETTINGS_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(name, None)


class GitHubTicketsSettingsTests(unittest.TestCase):
    def test_metadata_uses_the_accepted_cog_name_and_copy(self):
        metadata = json.loads(INFO_PATH.read_text("utf-8"))

        self.assertEqual(metadata["name"], "GitHubTickets")
        self.assertEqual(
            metadata["install_msg"],
            "GitHubTickets installed\nLoad it with `[p]load NHCogs.githubtickets`",
        )
        self.assertEqual(
            metadata["short"],
            "Create pull request review tickets and route reviewers",
        )
        self.assertEqual(
            metadata["description"],
            "Publish pull request tickets, manage developer expertise profiles, "
            "and route review requests through Discord",
        )

    def test_registered_defaults_match_the_accepted_configuration(self):
        settings = load_settings_module()

        self.assertEqual(
            settings.DEFAULTS,
            {
                "ticket_channel_id": None,
                "participant_role_ids": [],
                "protection_seconds": 10,
                "volunteer_seconds": 2 * 60 * 60,
                "online_response_seconds": 2 * 60 * 60,
                "idle_response_seconds": 4 * 60 * 60,
                "dnd_response_seconds": 6 * 60 * 60,
                "offline_response_seconds": 24 * 60 * 60,
                "direct_response_seconds": 24 * 60 * 60,
                "max_pings": 3,
            },
        )
        snapshot = settings.GuildSettings.from_mapping({})
        observed = asdict(snapshot)
        observed["participant_role_ids"] = list(snapshot.participant_role_ids)
        self.assertEqual(observed, settings.DEFAULTS)

    def test_mapping_normalizes_ids_and_preserves_zero_timing_values(self):
        settings = load_settings_module()

        snapshot = settings.GuildSettings.from_mapping(
            {
                "ticket_channel_id": "42",
                "participant_role_ids": [3, "2", 3, -1, None],
                "protection_seconds": 0,
                "volunteer_seconds": 0,
                "max_pings": 0,
                "future_setting": "ignored",
            }
        )

        self.assertEqual(snapshot.ticket_channel_id, 42)
        self.assertEqual(snapshot.participant_role_ids, (3, 2))
        self.assertEqual(snapshot.protection_seconds, 0)
        self.assertEqual(snapshot.volunteer_seconds, 0)
        self.assertEqual(snapshot.max_pings, 0)

    def test_malformed_or_negative_values_fall_back_independently(self):
        settings = load_settings_module()

        snapshot = settings.GuildSettings.from_mapping(
            {
                "ticket_channel_id": "not-an-id",
                "participant_role_ids": "not-a-list",
                "protection_seconds": -1,
                "volunteer_seconds": "later",
                "online_response_seconds": True,
                "idle_response_seconds": -2,
                "dnd_response_seconds": None,
                "offline_response_seconds": [],
                "direct_response_seconds": {},
                "max_pings": -4,
            }
        )

        self.assertEqual(snapshot, settings.GuildSettings.from_mapping({}))

    def test_duration_parser_accepts_compact_units_and_distinguishes_negatives(self):
        settings = load_settings_module()

        self.assertEqual(settings.parse_duration("10s"), 10)
        self.assertEqual(settings.parse_duration("2m"), 120)
        self.assertEqual(settings.parse_duration("24h"), 24 * 60 * 60)
        self.assertEqual(settings.parse_duration("0"), 0)
        with self.assertRaises(settings.NegativeDuration):
            settings.parse_duration("-1h")
        with self.assertRaises(settings.InvalidDuration):
            settings.parse_duration("later")

    def test_duration_parser_rejects_values_that_cannot_form_a_deadline(self):
        settings = load_settings_module()

        with self.assertRaises(settings.InvalidDuration):
            settings.parse_duration("100000000000000000000h")


if __name__ == "__main__":
    unittest.main()
