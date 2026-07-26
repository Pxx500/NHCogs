import importlib
import sqlite3
import sys
import types
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

PACKAGE_NAME = "_honeypot_storage_tests"
PACKAGE_PATH = Path(__file__).resolve().parents[1] / "Honeypot"


def load_storage_module():
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules[PACKAGE_NAME] = package
    try:
        return importlib.import_module(f"{PACKAGE_NAME}.storage")
    except ModuleNotFoundError as error:
        if error.name == f"{PACKAGE_NAME}.storage":
            raise AssertionError("the shared Honeypot storage interface is missing") from error
        raise


class HoneypotStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "storage.sqlite3"

    def test_connect_configures_the_shared_sqlite_contract(self):
        storage = load_storage_module()

        with closing(storage.connect(self.database_path)) as connection:
            row = connection.execute("SELECT 1 AS value").fetchone()
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertIsInstance(row, sqlite3.Row)
        self.assertEqual(row["value"], 1)
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(busy_timeout, 5000)
        self.assertEqual(journal_mode, "wal")

    def test_failed_migration_rolls_back_only_that_step_and_keeps_its_version(self):
        storage = load_storage_module()

        def create_retained_table(connection):
            connection.execute("CREATE TABLE retained (value TEXT NOT NULL)")
            connection.execute("INSERT INTO retained VALUES ('kept')")

        def fail_after_writing(connection):
            connection.execute("CREATE TABLE rolled_back (value TEXT NOT NULL)")
            connection.execute("INSERT INTO rolled_back VALUES ('discarded')")
            raise RuntimeError("planned migration failure")

        with closing(storage.connect(self.database_path)) as connection:
            with self.assertRaisesRegex(RuntimeError, "sample storage migration 1"):
                storage.apply_migrations(
                    connection,
                    (create_retained_table, fail_after_writing),
                    label="sample storage",
                )

            version = connection.execute("PRAGMA user_version").fetchone()[0]
            retained = connection.execute("SELECT value FROM retained").fetchone()[0]
            rolled_back = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'rolled_back'"
            ).fetchone()

        self.assertEqual(version, 1)
        self.assertEqual(retained, "kept")
        self.assertIsNone(rolled_back)

    def test_migrations_reject_a_database_from_a_newer_schema(self):
        storage = load_storage_module()

        with closing(storage.connect(self.database_path)) as connection:
            connection.execute("PRAGMA user_version = 2")

            with self.assertRaisesRegex(
                ValueError, "sample storage schema version 2 is newer than supported version 1"
            ):
                storage.apply_migrations(connection, (lambda _connection: None,), label="sample storage")


if __name__ == "__main__":
    unittest.main()
