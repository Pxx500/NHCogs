import io
import json
import unittest
import zipfile

from tests.storage_loader import load_shared_storage

load_shared_storage()

from NHCogs.nhmoderation import history_export  # noqa: E402


class ModerationHistoryExportTests(unittest.TestCase):
    def test_archive_keeps_all_rows_ids_and_explicit_coverage(self):
        destination = io.BytesIO()
        rows = ({"user_id": str(1400000000000000000 + i), "outcome": "passed"} for i in range(3))
        history_export.write_archive(
            destination,
            manifest={"schema_version": 1, "guild_id": "123", "coverage": {"historical_gap": True}},
            datasets={"joinwatch_incidents": rows, "moderation_actions": []},
            max_bytes=100000,
        )
        with zipfile.ZipFile(destination) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            exported = [json.loads(line) for line in archive.read("joinwatch_incidents.jsonl").splitlines()]
            self.assertEqual(manifest["counts"], {"joinwatch_incidents": 3, "moderation_actions": 0})
            self.assertTrue(manifest["coverage"]["historical_gap"])
            self.assertEqual([row["user_id"] for row in exported], [str(1400000000000000000 + i) for i in range(3)])
            self.assertEqual(archive.read("moderation_actions.jsonl"), b"")

    def test_oversize_archive_is_rejected_instead_of_returning_truncated_data(self):
        with self.assertRaisesRegex(ValueError, "upload limit"):
            history_export.write_archive(
                io.BytesIO(), manifest={"schema_version": 1},
                datasets={"moderation_actions": [{"user_id": "123"}]}, max_bytes=1,
            )
