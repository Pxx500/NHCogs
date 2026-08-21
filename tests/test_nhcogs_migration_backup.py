import asyncio
import hashlib
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from NHCogsMigrator import backup as backup_module
from NHCogsMigrator.backup import (
    BackupError,
    create_verified_backup,
    restore_verified_backup,
)


class SimulatedProcessStop(BaseException):
    pass


class VerifiedBackupTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_backup_uses_sqlite_snapshot_without_transient_sidecars(self):
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

            result = await create_verified_backup(
                "run-wal",
                data_directories={
                    "NHMisc": self.nhmisc,
                    "Honeypot": self.honeypot,
                },
                backup_root=self.backups,
                config_exports={},
                metadata={},
            )
        finally:
            connection.close()

        data_database = result.path / "data/Honeypot/message_registry.sqlite"
        data_exists, wal_exists, shm_exists = await asyncio.to_thread(
            lambda: (
                data_database.is_file(),
                Path(f"{data_database}-wal").exists(),
                Path(f"{data_database}-shm").exists(),
            )
        )
        self.assertTrue(data_exists)
        self.assertFalse(wal_exists)
        self.assertFalse(shm_exists)
        sqlite_database = result.path / "sqlite/Honeypot/message_registry.sqlite"
        with closing(sqlite3.connect(sqlite_database)) as restored:
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        manifest = json.loads((result.path / "manifest.json").read_text("utf-8"))
        self.assertFalse(
            any(path.endswith(("-wal", "-shm", "-journal")) for path in manifest["files"])
        )

    async def test_backup_copies_data_and_verifies_sqlite_and_manifest(self):
        database = self.nhmisc / "achievements.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE achievements (id INTEGER PRIMARY KEY, proof BLOB)"
            )
            connection.execute(
                "INSERT INTO achievements (proof) VALUES (?)",
                (b"proof-bytes",),
            )
            connection.commit()
        evidence = self.honeypot / "detection_case_files" / "case-1"
        evidence.mkdir(parents=True)
        (evidence / "capture.bin").write_bytes(b"capture")

        result = await create_verified_backup(
            "run-1",
            data_directories={"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
            backup_root=self.backups,
            config_exports={"NHMisc": {"guilds": {"1": {"enabled": True}}}},
            metadata={
                "original_packages": ["NHMisc", "Honeypot", "OtherCog"],
                "source_commit": "abc123",
            },
        )

        self.assertEqual(result.path, self.backups / "run-1")
        self.assertTrue((result.path / "data/NHMisc/achievements.sqlite").is_file())
        self.assertEqual(
            (result.path / "data/Honeypot/detection_case_files/case-1/capture.bin").read_bytes(),
            b"capture",
        )
        sqlite_backup = result.path / "sqlite/NHMisc/achievements.sqlite"
        with closing(sqlite3.connect(sqlite_backup)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM achievements").fetchone()[0], 1)
        manifest = json.loads((result.path / "manifest.json").read_text("utf-8"))
        for relative_path, entry in manifest["files"].items():
            artifact = result.path / relative_path
            self.assertEqual(artifact.stat().st_size, entry["size"])
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), entry["sha256"])
        self.assertEqual(result.manifest_sha256, hashlib.sha256((result.path / "manifest.json").read_bytes()).hexdigest())

    async def test_corrupt_sqlite_never_publishes_final_backup(self):
        (self.honeypot / "detection_cases.sqlite").write_bytes(b"not sqlite")

        with self.assertRaises(BackupError):
            await create_verified_backup(
                "run-1",
                data_directories={"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
                backup_root=self.backups,
                config_exports={},
                metadata={},
            )

        self.assertFalse((self.backups / "run-1").exists())
        self.assertFalse((self.backups / ".run-1.tmp").exists())

    async def test_restore_tree_uses_the_verified_sqlite_backup(self):
        database = self.nhmisc / "achievements.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE achievements (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO achievements DEFAULT VALUES")
            connection.commit()

        original_copy = backup_module._copy_file
        corrupted = False

        def corrupt_raw_database_copy(source, destination):
            nonlocal corrupted
            destination_path = Path(destination)
            if Path(source).suffix.casefold() == ".sqlite" and not corrupted:
                corrupted = True
                destination_path.write_bytes(b"corrupt raw copy")
                return str(destination_path)
            return original_copy(source, destination)

        with mock.patch.object(
            backup_module,
            "_copy_file",
            side_effect=corrupt_raw_database_copy,
        ):
            result = await create_verified_backup(
                "run-1",
                data_directories={
                    "NHMisc": self.nhmisc,
                    "Honeypot": self.honeypot,
                },
                backup_root=self.backups,
                config_exports={},
                metadata={},
            )

        database.write_bytes(b"mutated live database")
        await restore_verified_backup(
            result.path,
            {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
        )

        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM achievements").fetchone()[0],
                1,
            )

    async def test_interrupted_copy_never_publishes_final_backup(self):
        (self.nhmisc / "one.bin").write_bytes(b"one")
        (self.nhmisc / "two.bin").write_bytes(b"two")

        with mock.patch(
            "NHCogsMigrator.backup.shutil.copyfileobj",
            side_effect=OSError("copy interrupted"),
        ):
            with self.assertRaises(BackupError):
                await create_verified_backup(
                    "run-1",
                    data_directories={
                        "NHMisc": self.nhmisc,
                        "Honeypot": self.honeypot,
                    },
                    backup_root=self.backups,
                    config_exports={},
                    metadata={},
                )

        self.assertFalse((self.backups / "run-1").exists())
        self.assertFalse((self.backups / ".run-1.tmp").exists())

    async def test_restore_replaces_mutated_data_with_verified_snapshot(self):
        (self.nhmisc / "state.bin").write_bytes(b"before")
        result = await create_verified_backup(
            "run-1",
            data_directories={"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
            backup_root=self.backups,
            config_exports={},
            metadata={},
        )
        (self.nhmisc / "state.bin").write_bytes(b"after")
        (self.nhmisc / "new.bin").write_bytes(b"new")

        await restore_verified_backup(
            result.path,
            {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
        )

        self.assertEqual((self.nhmisc / "state.bin").read_bytes(), b"before")
        self.assertFalse((self.nhmisc / "new.bin").exists())

    async def test_failed_old_data_cleanup_does_not_undo_completed_restore(self):
        state = self.nhmisc / "state.bin"
        state.write_bytes(b"before")
        result = await create_verified_backup(
            "run-1",
            data_directories={"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
            backup_root=self.backups,
            config_exports={},
            metadata={},
        )
        state.write_bytes(b"after")
        original_rmtree = backup_module.shutil.rmtree

        def fail_old_data_cleanup(path, *args, **kwargs):
            if ".NHMisc.pre-restore-run-1" in str(path):
                raise OSError("cleanup denied")
            return original_rmtree(path, *args, **kwargs)

        with mock.patch.object(
            backup_module.shutil,
            "rmtree",
            side_effect=fail_old_data_cleanup,
        ):
            with self.assertRaises(BackupError):
                await restore_verified_backup(
                    result.path,
                    {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
                )

        self.assertEqual(state.read_bytes(), b"before")

    async def test_restore_resumes_after_process_stop_between_directory_swaps(self):
        nhmisc_state = self.nhmisc / "state.bin"
        honeypot_state = self.honeypot / "state.bin"
        nhmisc_state.write_bytes(b"nhmisc before")
        honeypot_state.write_bytes(b"honeypot before")
        result = await create_verified_backup(
            "run-1",
            data_directories={"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
            backup_root=self.backups,
            config_exports={},
            metadata={},
        )
        nhmisc_state.write_bytes(b"nhmisc after")
        honeypot_state.write_bytes(b"honeypot after")

        def stop_after_first_swap(data_directories, staged, previous, moved):
            name = next(iter(data_directories))
            target = data_directories[name].resolve()
            target.replace(previous[name])
            moved.append(name)
            staged[name].replace(target)
            raise SimulatedProcessStop()

        with mock.patch.object(
            backup_module,
            "_swap_data_restore",
            side_effect=stop_after_first_swap,
        ):
            with self.assertRaises(SimulatedProcessStop):
                await restore_verified_backup(
                    result.path,
                    {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
                )

        self.assertEqual(nhmisc_state.read_bytes(), b"nhmisc before")
        self.assertEqual(honeypot_state.read_bytes(), b"honeypot after")

        await restore_verified_backup(
            result.path,
            {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
        )

        self.assertEqual(nhmisc_state.read_bytes(), b"nhmisc before")
        self.assertEqual(honeypot_state.read_bytes(), b"honeypot before")

    async def test_restore_resumes_after_process_stop_between_target_renames(self):
        state = self.nhmisc / "state.bin"
        state.write_bytes(b"before")
        result = await create_verified_backup(
            "run-1",
            data_directories={"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
            backup_root=self.backups,
            config_exports={},
            metadata={},
        )
        state.write_bytes(b"after")

        def stop_after_moving_live_data(data_directories, staged, previous, moved):
            name = next(iter(data_directories))
            data_directories[name].resolve().replace(previous[name])
            moved.append(name)
            raise SimulatedProcessStop()

        with mock.patch.object(
            backup_module,
            "_swap_data_restore",
            side_effect=stop_after_moving_live_data,
        ):
            with self.assertRaises(SimulatedProcessStop):
                await restore_verified_backup(
                    result.path,
                    {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
                )

        self.assertFalse(self.nhmisc.exists())
        await restore_verified_backup(
            result.path,
            {"NHMisc": self.nhmisc, "Honeypot": self.honeypot},
        )
        self.assertEqual(state.read_bytes(), b"before")
