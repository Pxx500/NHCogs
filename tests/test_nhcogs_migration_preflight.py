import asyncio
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from NHCogsMigrator.preflight import inspect_persisted_data


class PersistedDataPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.nhmisc = self.root / "NHMisc"
        self.honeypot = self.root / "Honeypot"
        self.backups = self.root / "backups"
        self.nhmisc.mkdir()
        self.honeypot.mkdir()
        self.backups.mkdir()

    async def test_sqlite_wal_and_shared_memory_are_not_persisted_files(self):
        database = self.honeypot / "message_registry.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO messages DEFAULT VALUES")
            connection.commit()
            wal_exists, shm_exists = await asyncio.to_thread(
                lambda: (
                    Path(f"{database}-wal").is_file(),
                    Path(f"{database}-shm").is_file(),
                )
            )
            self.assertTrue(wal_exists)
            self.assertTrue(shm_exists)

            report = await inspect_persisted_data(
                {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
                backup_root=self.backups,
            )
        finally:
            connection.close()

        self.assertEqual(report.blocking_issues, ())
        self.assertEqual(report.database_count, 1)
        self.assertEqual(report.file_count, 1)

    async def test_inspection_reports_sqlite_tables_files_and_space(self):
        database = self.nhmisc / "achievements.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE achievements (id INTEGER PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO achievements (id) VALUES (?)",
                ((1,), (2,)),
            )
            connection.commit()
        evidence = self.honeypot / "detection_case_files" / "case-1"
        evidence.mkdir(parents=True)
        (evidence / "proof.bin").write_bytes(b"proof")

        report = await inspect_persisted_data(
            {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
            backup_root=self.backups,
        )

        self.assertEqual(report.blocking_issues, ())
        self.assertEqual(report.database_count, 1)
        self.assertEqual(report.file_count, 2)
        database_report = report.databases[0]
        self.assertEqual(database_report.integrity_result, "ok")
        self.assertEqual(database_report.table_rows, {"achievements": 2})
        self.assertGreaterEqual(report.required_backup_bytes, report.total_bytes)
        self.assertGreater(report.free_bytes, report.required_backup_bytes)

    async def test_corrupt_database_and_missing_data_directory_are_blocking(self):
        (self.honeypot / "detection_cases.sqlite").write_bytes(b"not sqlite")
        missing_nhmisc = self.root / "missing-nhmisc"

        report = await inspect_persisted_data(
            {"NHMisc": missing_nhmisc, "Honeypot": self.honeypot},
            backup_root=self.backups,
        )

        self.assertEqual(report.database_count, 1)
        self.assertTrue(
            any("NHMisc data directory is missing" in issue for issue in report.blocking_issues)
        )
        self.assertTrue(
            any("detection_cases.sqlite" in issue for issue in report.blocking_issues)
        )

    async def test_insufficient_backup_space_is_blocking(self):
        (self.nhmisc / "activity.sqlite").write_bytes(b"x" * 100)
        disk_usage = mock.Mock(free=1)

        with mock.patch(
            "NHCogsMigrator.preflight.shutil.disk_usage",
            return_value=disk_usage,
        ):
            report = await inspect_persisted_data(
                {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
                backup_root=self.backups,
            )

        self.assertTrue(
            any("Insufficient backup space" in issue for issue in report.blocking_issues)
        )
