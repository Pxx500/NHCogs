import importlib.util
import sys
import unittest
from pathlib import Path

PRESENTATION_PATH = (
    Path(__file__).parents[1] / "NHCogs" / "githubtickets" / "presentation.py"
)


def load_presentation_module():
    name = "githubtickets_presentation_test"
    spec = importlib.util.spec_from_file_location(name, PRESENTATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(name, None)


class GitHubTicketsPresentationTests(unittest.TestCase):
    def test_ticket_message_uses_every_accepted_compact_variant(self):
        presentation = load_presentation_module()
        base = {
            "title": "Fix shader cache invalidation",
            "url": "https://github.com/example/repository/pull/123",
            "author_mention": "@Mira",
        }

        self.assertEqual(
            presentation.ticket_message(
                **base,
                categories=("rendering", "performance"),
                reviewer_mention="@Nova",
                reviewer_github="nova-dev",
            ),
            "[Fix shader cache invalidation]"
            "(<https://github.com/example/repository/pull/123>)\n"
            "Author: @Mira | rendering, performance | Reviewer: @Nova | nova-dev",
        )
        self.assertEqual(
            presentation.ticket_message(
                **base,
                categories=("rendering", "performance"),
                reviewer_mention="@Nova",
            ).splitlines()[1],
            "Author: @Mira | rendering, performance | Reviewer: @Nova",
        )
        self.assertEqual(
            presentation.ticket_message(
                **base,
                categories=("rendering", "performance"),
            ).splitlines()[1],
            "Author: @Mira | rendering, performance",
        )
        self.assertEqual(
            presentation.ticket_message(**base).splitlines()[1],
            "Author: @Mira",
        )
        self.assertEqual(
            presentation.ticket_message(
                **base,
                reviewer_mention="@Nova",
                reviewer_github="nova-dev",
            ).splitlines()[1],
            "Author: @Mira | Reviewer: @Nova | nova-dev",
        )

    def test_routing_notifications_only_contain_the_target_mention(self):
        presentation = load_presentation_module()

        self.assertEqual(
            presentation.direct_review_notification("@Nova"),
            "@Nova was directly requested for review",
        )
        self.assertEqual(
            presentation.automatic_review_notification("@Nova"),
            "@Nova was automatically selected for review",
        )

    def test_developer_profile_uses_no_profile_and_optional_lines(self):
        presentation = load_presentation_module()

        self.assertEqual(
            presentation.developer_profile(
                mention="@Mira",
                has_profile=False,
            ),
            "No profile",
        )
        self.assertEqual(
            presentation.developer_profile(
                mention="@Mira",
                has_profile=True,
                github_username="mira-dev",
                categories=("rendering", "performance"),
            ),
            "@Mira | mira-dev\nrendering, performance",
        )
        self.assertEqual(
            presentation.developer_profile(
                mention="@Mira",
                has_profile=True,
                categories=("rendering",),
            ),
            "@Mira\nrendering",
        )

    def test_category_page_is_flat_and_only_shows_pagination_when_needed(self):
        presentation = load_presentation_module()

        self.assertEqual(
            presentation.category_page(
                category="rendering",
                users=("@Alice | alice-gh", "@Bob"),
                page=1,
                page_count=3,
            ),
            "rendering\n@Alice | alice-gh\n@Bob\nPage 1 of 3",
        )
        self.assertEqual(
            presentation.category_page(
                category="rendering",
                users=("@Bob",),
                page=1,
                page_count=1,
            ),
            "rendering\n@Bob",
        )
        self.assertEqual(
            presentation.category_page(
                category="rendering",
                users=(),
                page=1,
                page_count=1,
            ),
            "rendering\nNo users found",
        )

    def test_thread_name_uses_title_and_truncates_without_an_ellipsis(self):
        presentation = load_presentation_module()

        self.assertEqual(
            presentation.thread_name("Fix shader cache invalidation"),
            "Fix shader cache invalidation",
        )
        long_title = "x" * 120
        self.assertEqual(presentation.thread_name(long_title), "x" * 100)

    def test_finished_ticket_log_omits_a_missing_reviewer(self):
        presentation = load_presentation_module()

        self.assertEqual(
            presentation.finished_ticket_log(
                title="Improve rendering",
                url="https://github.com/example/repository/pull/123",
                actor_id=40,
                author_id=30,
                reviewer_id=None,
            ),
            "[Improve rendering](<https://github.com/example/repository/pull/123>)\n"
            "Finished by <@40> | Author <@30>",
        )

    def test_finished_ticket_log_handles_github_finish_without_discord_author(self):
        presentation = load_presentation_module()

        self.assertEqual(
            presentation.finished_ticket_log(
                title="Improve rendering",
                url="https://github.com/example/repository/pull/123",
                actor_id=None,
                author_id=None,
                reviewer_id=50,
            ),
            "[Improve rendering](<https://github.com/example/repository/pull/123>)\n"
            "Finished from GitHub | Reviewer <@50>",
        )

    def test_configuration_overview_uses_the_accepted_labels(self):
        presentation = load_presentation_module()

        overview = presentation.configuration_overview(
            ticket_channel="#github-tickets",
            log_channel="#github-ticket-logs",
            participant_roles=("@GT:NH Devs", "@GT:NH Contributor"),
            categories=("rendering", "mixins", "performance"),
            max_pings=3,
            protection_seconds=10,
            volunteer_seconds=2 * 60 * 60,
            online_response_seconds=2 * 60 * 60,
            idle_response_seconds=4 * 60 * 60,
            dnd_response_seconds=6 * 60 * 60,
            offline_response_seconds=24 * 60 * 60,
            direct_response_seconds=24 * 60 * 60,
        )

        self.assertEqual(
            overview,
            "Ticket channel: #github-tickets\n"
            "Log channel: #github-ticket-logs\n"
            "Participant roles: @GT:NH Devs, @GT:NH Contributor\n"
            "Categories: rendering, mixins, performance\n"
            "Maximum pings: 3\n"
            "Protection period: 10 seconds\n"
            "Initial volunteer window: 2 hours\n"
            "Online response time: 2 hours\n"
            "Idle response time: 4 hours\n"
            "Do Not Disturb response time: 6 hours\n"
            "Offline response time: 24 hours\n"
            "Direct response time: 24 hours",
        )

    def test_configuration_overview_marks_missing_resources(self):
        presentation = load_presentation_module()

        overview = presentation.configuration_overview(
            ticket_channel=None,
            log_channel=None,
            participant_roles=(),
            categories=(),
            max_pings=0,
            protection_seconds=0,
            volunteer_seconds=0,
            online_response_seconds=0,
            idle_response_seconds=0,
            dnd_response_seconds=0,
            offline_response_seconds=0,
            direct_response_seconds=0,
        )

        self.assertIn("Ticket channel: Not set", overview)
        self.assertIn("Log channel: Not set", overview)
        self.assertIn("Participant roles: None", overview)
        self.assertIn("Categories: None", overview)
        self.assertIn("Maximum pings: 0", overview)

    def test_long_configuration_overview_splits_without_losing_content(self):
        presentation = load_presentation_module()
        overview = presentation.configuration_overview(
            ticket_channel="#github-tickets",
            log_channel="#github-ticket-logs",
            participant_roles=tuple(f"@role-{index}-" + "x" * 80 for index in range(50)),
            categories=tuple(f"category-{index}-" + "x" * 80 for index in range(25)),
            max_pings=3,
            protection_seconds=10,
            volunteer_seconds=7200,
            online_response_seconds=7200,
            idle_response_seconds=14400,
            dnd_response_seconds=21600,
            offline_response_seconds=86400,
            direct_response_seconds=86400,
        )

        chunks = getattr(presentation, "message_chunks", lambda value: (value,))(
            overview
        )

        self.assertGreater(len(chunks), 1)
        self.assertTrue(
            all(len(chunk) <= presentation.DISCORD_MESSAGE_LIMIT for chunk in chunks)
        )
        self.assertEqual("".join(chunks), overview)

    def test_configuration_confirmations_use_the_resulting_value(self):
        presentation = load_presentation_module()

        self.assertEqual(
            presentation.ticket_channel_set("#github-tickets"),
            "Ticket channel set to #github-tickets",
        )
        self.assertEqual(
            presentation.participant_role_added("@GT:NH Devs"),
            "Participant role added: @GT:NH Devs",
        )
        self.assertEqual(
            presentation.category_added("rendering"),
            "Category added: rendering",
        )
        self.assertEqual(
            presentation.maximum_pings_set(3),
            "Maximum pings set to 3",
        )
        self.assertEqual(
            presentation.timing_set("Protection period", 10),
            "Protection period set to 10 seconds",
        )
        self.assertEqual(
            presentation.profile_cleared(123456789),
            "Profile cleared: 123456789",
        )

    def test_fixed_copy_has_no_prohibited_punctuation(self):
        presentation = load_presentation_module()

        for value in presentation.FIXED_COPY:
            with self.subTest(value=value):
                self.assertNotIn("—", value)
                self.assertNotIn(";", value)
                self.assertFalse(value.endswith("."))


if __name__ == "__main__":
    unittest.main()
