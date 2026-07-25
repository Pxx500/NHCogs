import importlib
import sqlite3
import sys
import types
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


PACKAGE_NAME = "_honeypot_firstpost_store_tests"
PACKAGE_PATH = Path(__file__).resolve().parents[1] / "Honeypot"


def load_firstpost_store_module():
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules[PACKAGE_NAME] = package
    try:
        return importlib.import_module(f"{PACKAGE_NAME}.firstpost_store")
    except ModuleNotFoundError as error:
        if error.name == f"{PACKAGE_NAME}.firstpost_store":
            raise AssertionError("the firstpost store interface is missing") from error
        raise


class FirstPostStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "firstpost_seen.sqlite"

    def store(self):
        module = load_firstpost_store_module()
        return module.FirstPostStore(self.database_path)

    def test_initialize_creates_an_empty_versioned_store(self):
        store = self.store()

        store.initialize()

        self.assertEqual(store.load_guild(10), set())
        self.assertEqual(store.count(10), 0)
        with closing(sqlite3.connect(self.database_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)

    def test_flush_accumulates_authors_without_overwriting_first_seen_time(self):
        store = self.store()
        store.initialize()

        store.flush(10, {20, 30})
        with closing(sqlite3.connect(self.database_path)) as connection:
            original_first_seen_at = connection.execute(
                """SELECT first_seen_at FROM firstpost_seen_authors
                   WHERE guild_id = '10' AND user_id = '20'"""
            ).fetchone()[0]

        store.flush(10, {20, 40})

        self.assertEqual(store.load_guild(10), {20, 30, 40})
        self.assertEqual(store.count(10), 3)
        with closing(sqlite3.connect(self.database_path)) as connection:
            first_seen_at = connection.execute(
                """SELECT first_seen_at FROM firstpost_seen_authors
                   WHERE guild_id = '10' AND user_id = '20'"""
            ).fetchone()[0]
        self.assertEqual(first_seen_at, original_first_seen_at)

    def test_initialize_preserves_an_existing_unversioned_database(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                """CREATE TABLE firstpost_seen_authors (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    first_seen_at INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )"""
            )
            connection.execute(
                "INSERT INTO firstpost_seen_authors VALUES ('10', '20', 123)"
            )

        store = self.store()
        store.initialize()

        self.assertEqual(store.load_guild(10), {20})
        self.assertEqual(store.count(10), 1)
        with closing(sqlite3.connect(self.database_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            first_seen_at = connection.execute(
                "SELECT first_seen_at FROM firstpost_seen_authors"
            ).fetchone()[0]
        self.assertEqual(version, 1)
        self.assertEqual(first_seen_at, 123)


if __name__ == "__main__":
    unittest.main()
