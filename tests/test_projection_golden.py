import json
import unittest
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.detection_case_fixtures import DetectionCaseBuilder
from tests.test_detection_cases import detection_cases_under_test as cases
from tests.test_pending_review_merge import case_review

GOLDEN_DIR = Path(__file__).with_name("golden")
CASE_ID_PLACEHOLDER = "{{CASE_ID}}"


def _json_value(value):
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _projection_bytes(snapshot):
    value = {
        "case": _json_value(case_review.render_case(snapshot)),
        "timeline": _json_value(case_review.render_timeline(snapshot)),
    }
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


class ProjectionGoldenTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.builder = DetectionCaseBuilder(
            cases,
            Path(self.temp_dir.name) / "cases.sqlite3",
        )

    def assertGolden(self, name):
        snapshot = self.builder.snapshot()
        actual = _projection_bytes(snapshot)
        path = GOLDEN_DIR / f"{name}.json"
        expected = path.read_bytes().replace(
            CASE_ID_PLACEHOLDER.encode(),
            snapshot.case.case_id.encode(),
        )
        self.assertEqual(actual, expected)
        self.assertNotIn(snapshot.case.case_id, case_review.render_case(snapshot).description)
        self.assertNotIn(b"internal-stack-token", actual)

    def test_pending_one_message_without_attachments(self):
        self.builder.add_message(
            content="pending text only",
            signals=(self.builder.signal("spam", "Repeated message"),),
        )

        self.assertGolden("pending_one_message")

    def test_multi_message_multi_channel_signal_grouping(self):
        self.builder.add_message(
            channel_id=300,
            content="first signal",
            signals=(
                self.builder.signal("spam", "Repeated message\nRepeated message"),
                self.builder.signal("links", "Suspicious domain"),
            ),
            delete_status=cases.DeleteStatus.DELETED,
        )
        self.builder.add_message(
            channel_id=301,
            content="second signal",
            signals=(self.builder.signal("firstpost", "New account"),),
            delete_status=cases.DeleteStatus.ALREADY_GONE,
        )

        self.assertGolden("multi_channel_signals")

    def test_successful_captured_and_published_attachments(self):
        result = self.builder.add_message(
            content="image evidence",
            attachments=(
                self.builder.attachment(0, "one.png", description="first image"),
                self.builder.attachment(1, "two.png", spoiler=True),
            ),
            signals=(self.builder.signal("image", "Known suspicious image match"),),
            delete_status=cases.DeleteStatus.DELETED,
        )
        self.builder.capture(result.message.sequence, 0, "evidence/one.png")
        self.builder.capture(result.message.sequence, 1, "evidence/two.png")
        snapshot = self.builder.snapshot()
        attachment_keys = tuple(attachment.key for attachment in snapshot.attachments)
        self.builder.publish(attachment_keys)
        publications = self.builder.snapshot().publications
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0].attachment_keys, attachment_keys)

        self.assertGolden("captured_published_attachments")

    def test_capture_failed_evidence_missing(self):
        result = self.builder.add_message(
            content="missing image evidence",
            attachments=(
                self.builder.attachment(0, "publish-failed.png"),
                self.builder.attachment(1, "missing.png"),
            ),
            signals=(self.builder.signal("image", "Image could not be read"),),
        )
        self.builder.capture(
            result.message.sequence,
            0,
            "evidence/publish-failed.png",
        )
        self.assertTrue(
            self.builder.store.update_attachment_publication_error(
                self.builder.case_id,
                result.message.sequence,
                0,
                "evidence upload failed",
            )
        )
        self.builder.fail_capture(result.message.sequence, "internal capture exception")

        self.assertGolden("capture_failed")

    def test_pending_image_review_awaiting_classification(self):
        result = self.builder.add_message(
            content="classification pending",
            attachments=(self.builder.attachment(0, "matched.png"),),
            signals=(self.builder.signal("image", "Known suspicious image match"),),
        )
        self.builder.capture(result.message.sequence, 0, "evidence/matched.png")
        self.builder.scan(result.message.sequence, 0, matched=True)
        self.builder.complete_operation("moderation_action", "banned")

        self.assertGolden("awaiting_classification")

    def test_failed_operation_warning(self):
        self.builder.add_message(
            content="publication will fail",
            signals=(self.builder.signal("spam", "Repeated message"),),
        )
        self.builder.fail_operation("review_publish")

        self.assertGolden("failed_operation_warning")

    def test_resolved_terminal_with_moderator_attribution(self):
        self.builder.add_message(
            content="resolved case",
            signals=(self.builder.signal("spam", "Repeated message"),),
            delete_status=cases.DeleteStatus.DELETED,
        )
        self.builder.resolve("kick", 9001)

        self.assertGolden("resolved_moderator")

    def test_long_content_pagination_boundary(self):
        result = self.builder.add_message(
            channel_id=300,
            content="A" * 4500,
            attachments=tuple(
                self.builder.attachment(position, f"large-{position}.png")
                for position in range(3)
            ),
            signals=(
                self.builder.signal("spam", "Long detector detail " + "x" * 800),
                self.builder.signal("links", "Second detector detail " + "y" * 800),
            ),
        )
        for position in range(3):
            self.builder.capture(
                result.message.sequence,
                position,
                f"evidence/large-{position}.png",
            )
            self.assertTrue(
                self.builder.store.update_attachment_publication_error(
                    self.builder.case_id,
                    result.message.sequence,
                    position,
                    "review destination upload limit: " + "z" * 400,
                )
            )
        for operation_type in ("review_publish", "role_apply", "moderation_action"):
            self.builder.fail_operation(operation_type)
        self.assertTrue(self.builder.store.mark_case_needs_attention(self.builder.case_id))
        for channel_id in range(301, 305):
            self.builder.add_message(
                channel_id=channel_id,
                content=f"follow-up in {channel_id}",
                signals=(self.builder.signal("spam", f"Signal in {channel_id}"),),
            )

        self.assertGolden("long_content_pagination")
