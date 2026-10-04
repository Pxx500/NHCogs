import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_detection_cases import DetectionCaseStore


class VerificationArchiveTests(unittest.TestCase):
    def test_single_join_observation_keeps_source_and_guild_scope(self):
        with TemporaryDirectory() as directory:
            store = DetectionCaseStore(Path(directory) / "cases.sqlite")
            store.initialize()
            record = {"first_joined_at": "2026-10-01T12:00:00+00:00", "imported": True}
            store.save_joinwatch_history(10, {"observations": {"20": record}})
            self.assertEqual(store.get_joinwatch_observation(10, 20), record)
            self.assertIsNone(store.get_joinwatch_observation(10, 21))
            self.assertIsNone(store.get_joinwatch_observation(11, 20))

    def test_archive_survives_wave_cleanup_and_deduplicates_events(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cases.sqlite"
            store = DetectionCaseStore(path)
            store.initialize()
            incident = {"incident_id": "one", "guild_id": "10", "user_id": "20",
                        "source": "wave", "wave_id": "wave", "outcome": "pending",
                        "captured_at": "2026-10-04T12:00:00+00:00"}
            event = {"event_id": "enrolled", "incident_id": "one", "kind": "enrolled",
                     "occurred_at": incident["captured_at"]}
            store.save_verification_history(incident, [event])
            store.save_verification_history({**incident, "outcome": "passed"}, [event])
            store.delete_joinwatch_wave(10, "wave")
            reopened = DetectionCaseStore(path)
            result = reopened.export_verification_history(10)
            self.assertEqual(result["incidents"][0]["outcome"], "passed")
            self.assertEqual(result["events"], [event])
            self.assertEqual(reopened.export_verification_history(11)["incidents"], [])
            self.assertEqual(json.loads(json.dumps(result)), result)
            reopened.delete_verification_history(user_id=20)
            self.assertEqual(reopened.export_verification_history(10)["events"], [])
