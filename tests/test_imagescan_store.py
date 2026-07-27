import importlib
import sqlite3
import sys
import types
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

PACKAGE_NAME = "_honeypot_imagescan_store_tests"
PACKAGE_PATH = Path(__file__).resolve().parents[1] / "Honeypot"


def load_imagescan_store_module():
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules[PACKAGE_NAME] = package
    try:
        return importlib.import_module(f"{PACKAGE_NAME}.imagescan_store")
    except ModuleNotFoundError as error:
        if error.name == f"{PACKAGE_NAME}.imagescan_store":
            raise AssertionError("the imagescan store interface is missing") from error
        raise


class ImageScanStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.data_path = Path(self.directory.name)
        self.database_path = self.data_path / "imagescan.sqlite"
        self.files_path = self.data_path / "imagescan_files"

    def store(self, **kwargs):
        module = load_imagescan_store_module()
        return module.ImageScanStore(self.database_path, self.files_path, **kwargs)

    @staticmethod
    def sample(
        sample_id="sample-1",
        *,
        decision="true_positive",
        sha256="a" * 64,
        file_path=None,
        file_size_bytes=0,
    ):
        return {
            "sample_id": sample_id,
            "guild_id": "10",
            "decision": decision,
            "sha256": sha256,
            "phash": "1" * 16,
            "dhash": "2" * 16,
            "ahash": "3" * 16,
            "source_message_id": "20",
            "source_channel_id": "30",
            "source_jump_url": "https://discord.test/messages/20",
            "file_path": file_path,
            "file_size_bytes": file_size_bytes,
            "created_at": 100,
            "moderator_id": "40",
        }

    def test_initialize_creates_an_empty_versioned_store_and_file_root(self):
        store = self.store()

        store.initialize()

        self.assertEqual(store.load_active(10), [])
        self.assertEqual(store.rows(10), [])
        self.assertEqual(store.export_rows(10), [])
        self.assertEqual(store.stored_size(10), 0)
        self.assertTrue(self.files_path.is_dir())
        with closing(sqlite3.connect(self.database_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)

    def test_insert_reports_duplicate_and_conflicting_active_hashes(self):
        store = self.store()
        store.initialize()

        inserted = store.insert(self.sample())
        duplicate = store.insert(self.sample("sample-2"))
        conflict = store.insert(
            self.sample("sample-3", decision="false_positive")
        )

        self.assertEqual((inserted, duplicate, conflict), ("inserted", "duplicate", "conflict"))
        self.assertEqual([sample.sample_id for sample in store.load_active(10)], ["sample-1"])

    def test_update_deactivate_and_delete_preserve_sample_result_semantics(self):
        store = self.store()
        store.initialize()
        store.insert(self.sample())

        store.update_file(10, "sample-1", "samples/sample-1.png", 128)

        self.assertEqual(store.stored_size(10), 128)
        self.assertEqual(store.rows(10)[0]["file_path"], "samples/sample-1.png")
        store.deactivate(10, "sample-1")
        self.assertEqual(store.load_active(10), [])
        self.assertEqual(store.rows(10), [])
        self.assertEqual(store.rows(10, include_inactive=True)[0]["active"], 0)
        store.delete(10, "sample-1")
        self.assertEqual(store.rows(10, include_inactive=True), [])

    def test_profile_and_model_verification_use_persisted_samples(self):
        store = self.store()
        store.initialize()
        store.insert(self.sample(file_size_bytes=64))

        store.increment_profile(
            10,
            {"messages_scanned": 2, "hash_ms_total": 25, "unknown": 99},
        )
        profile = store.profile(10)
        state = store.verify(10, 20)

        self.assertEqual(profile["messages_scanned"], 2)
        self.assertEqual(profile["hash_ms_total"], 25)
        self.assertNotIn("unknown", profile)
        self.assertEqual(state["sample_count_tp"], 1)
        self.assertEqual(state["sample_count_fp"], 0)
        self.assertEqual(state["stored_size_bytes"], 64)

    def test_publish_file_sample_commits_file_and_row_then_detects_duplicate(self):
        store = self.store()
        store.initialize()
        path = self.files_path / "10" / "samples" / "sample.png"
        path.parent.mkdir(parents=True)
        sample = self.sample(file_path=str(path), file_size_bytes=7)

        inserted = store.publish_file_sample(sample, b"payload", path)
        duplicate = store.publish_file_sample(self.sample("sample-2"), b"changed", path)

        self.assertEqual((inserted, duplicate), ("inserted", "duplicate"))
        self.assertEqual(path.read_bytes(), b"payload")
        self.assertEqual(store.rows(10)[0]["sample_id"], "sample-1")

    def test_publish_file_sample_rejects_an_untracked_canonical_path(self):
        store = self.store()
        store.initialize()
        path = self.files_path / "10" / "samples" / "sample.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"pre-existing")

        status = store.publish_file_sample(self.sample(), b"payload", path)

        self.assertEqual(status, "conflict")
        self.assertEqual(path.read_bytes(), b"pre-existing")
        self.assertEqual(store.rows(10), [])

    def test_publish_file_sample_rolls_back_when_temp_write_fails(self):
        store = self.store()
        store.initialize()
        path = self.files_path / "missing" / "sample.png"

        with self.assertRaises(FileNotFoundError):
            store.publish_file_sample(self.sample(), b"payload", path)

        self.assertEqual(store.rows(10), [])
        self.assertFalse(path.exists())

    def test_publish_file_sample_removes_row_file_and_temp_after_commit_failure(self):
        store = self.store()
        store.initialize()
        path = self.files_path / "10" / "samples" / "sample.png"
        path.parent.mkdir(parents=True)
        published_before_commit = []

        class CommitFailingConnection:
            def __init__(self, connection):
                object.__setattr__(self, "_connection", connection)

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def __setattr__(self, name, value):
                setattr(self._connection, name, value)

            def commit(self):
                published_before_commit.append(path.exists())
                self._connection.rollback()
                raise sqlite3.OperationalError("injected commit failure")

            def close(self):
                self._connection.close()

        def commit_failing_connect(*args, **kwargs):
            return CommitFailingConnection(sqlite3.connect(*args, **kwargs))

        failing_store = self.store(connection_factory=commit_failing_connect)

        with self.assertRaisesRegex(sqlite3.OperationalError, "injected commit failure"):
            failing_store.publish_file_sample(self.sample(), b"payload", path)

        self.assertEqual(published_before_commit, [True])
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.glob(".sample-*.tmp")), [])
        self.assertEqual(store.rows(10), [])

    def test_initialize_preserves_existing_rows_and_far_export_data(self):
        store = self.store()
        store.initialize()
        store.insert(self.sample())
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                """INSERT INTO imagescan_events (
                    event_id, guild_id, user_id, channel_id, message_id,
                    message_jump_url, created_at, image_count
                ) VALUES ('event-1', '10', '20', '30', '40',
                          'https://discord.test/messages/40', 100, 1)"""
            )
            connection.execute(
                """INSERT INTO imagescan_files (
                    event_id, file_index, filename, path, size, sha256
                ) VALUES ('event-1', 0, 'proof.png', 'events/proof.png', 128, 'abc')"""
            )
            connection.execute("PRAGMA user_version = 0")

        store.initialize()

        exported = store.export_rows(10)
        self.assertEqual(store.rows(10)[0]["sample_id"], "sample-1")
        self.assertEqual(exported[0]["event_id"], "event-1")
        self.assertEqual(exported[0]["files"][0]["filename"], "proof.png")
        with closing(sqlite3.connect(self.database_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)


if __name__ == "__main__":
    unittest.main()
