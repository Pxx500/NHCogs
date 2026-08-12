"""Detection diagnostics: the doctor report, the review dump and the
imagescan sample bookkeeping they read.
"""

import base64
import json
import shutil
import sqlite3
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import get_ident
from types import SimpleNamespace
from unittest import mock

from tests.detection_case_fixtures import capture_attachment
from tests.harness import _Bot, _isolated_honeypot_modules
from tests.test_chatchart import load_nhmisc_module


class DetectionDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_imagescan_dump_exports_dated_samples_and_archive_paths(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._imagescan_store.initialize()
                sample_file = (
                    cog._imagescan_files_path
                    / "10"
                    / "samples"
                    / "imports"
                    / "sample.png"
                )
                sample_file.parent.mkdir(parents=True)
                sample_file.write_bytes(b"sample")
                sample = {
                    "sample_id": "sample-1",
                    "guild_id": "10",
                    "decision": "true_positive",
                    "sha256": "a" * 64,
                    "phash": "1" * 16,
                    "dhash": "2" * 16,
                    "ahash": "3" * 16,
                    "source_message_id": "20",
                    "source_channel_id": "30",
                    "source_jump_url": None,
                    "file_path": str(sample_file),
                    "file_size_bytes": 6,
                    "created_at": 1786554102,
                    "moderator_id": "40",
                }
                cog._imagescan_store.insert(sample)
                missing = dict(sample)
                missing.update(
                    sample_id="sample-2",
                    sha256="b" * 64,
                    file_path=str(sample_file.with_name("missing.png")),
                )
                cog._imagescan_store.insert(missing)
                cog._imagescan_store.deactivate(10, "sample-2")

                temp_root, archives = await honeypot.imagescan._imagescan_create_dump_archives(
                    cog, 10
                )
                try:
                    with zipfile.ZipFile(archives[0]) as archive:
                        rows = [
                            json.loads(line)
                            for line in archive.read("samples.jsonl")
                            .decode("utf-8")
                            .splitlines()
                        ]
                        self.assertIn(
                            "files/10/samples/imports/sample.png",
                            archive.namelist(),
                        )
                finally:
                    shutil.rmtree(temp_root, ignore_errors=True)

                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["created_at_iso"], "2026-08-12T17:01:42Z")
                self.assertEqual(
                    rows[0]["file"], "files/10/samples/imports/sample.png"
                )
                self.assertTrue(rows[0]["active"])
                self.assertIsNone(rows[1]["file"])
                self.assertFalse(rows[1]["active"])

    async def test_honeypot_errors_uses_persisted_operation_value(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                occurred_at = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                cog._case_store.record_operational_failure(
                    guild_id=10,
                    source=honeypot.OperationType.ROLE_APPLY,
                    summary="temporary",
                    occurred_at=occurred_at,
                )
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=10),
                    send=mock.AsyncMock(),
                )

                await cog.honeypot_errors(ctx)

                ctx.send.assert_awaited_once_with(
                    "**Honeypot operational errors:**\n"
                    f"- <t:{int(occurred_at.timestamp())}:R> "
                    "`role_apply` (active, x1): temporary"
                )

    @staticmethod
    def _append_case(
        honeypot,
        cog,
        *,
        guild_id=10,
        user_id=20,
        message_id=40,
        attachments=(),
        created_at=None,
    ):
        cog._case_store.initialize()
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=guild_id,
                user_id=user_id,
                channel_id=30,
                message_id=message_id,
                content="evidence",
                created_at=created_at or datetime.now(timezone.utc),
                jump_url=f"https://discord.test/messages/{message_id}",
                attachments=attachments,
            ),
            (),
        )

    @staticmethod
    def _doctor_context(*, role_ids=()):
        permissions = SimpleNamespace(
            kick_members=True,
            ban_members=True,
            manage_roles=True,
        )
        top_role = mock.MagicMock()
        top_role.__gt__.return_value = True
        member = SimpleNamespace(guild_permissions=permissions, top_role=top_role)
        roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
        guild = SimpleNamespace(
            id=10,
            me=member,
            channels=[],
            threads=[],
            get_channel=lambda channel_id: None,
            get_thread=lambda channel_id: None,
            get_role=roles.get,
        )
        return SimpleNamespace(guild=guild, send=mock.AsyncMock())

    @staticmethod
    def _doctor_config(**overrides):
        config = {
            "enabled": False,
            "action": "none",
            "fallback_action": "none",
            "whitelist_mode": "bypass",
        }
        config.update(overrides)
        return config

    async def test_nhmisc_exposes_configured_sticky_roles_as_a_read_only_snapshot(self):
        with TemporaryDirectory() as directory:
            nhmisc = load_nhmisc_module()
            sticky_roles = nhmisc.StickyRoleStore(Path(directory) / "sticky_roles.sqlite")
            await sticky_roles.initialize()
            await sticky_roles.add_sticky_role(10, 41)
            await sticky_roles.add_sticky_role(10, 42)
            cog = object.__new__(nhmisc.NHMisc)
            cog._sticky_roles = sticky_roles

            role_ids = await cog.configured_sticky_role_ids(10)

            self.assertEqual(role_ids, frozenset({41, 42}))

    async def test_doctor_warns_when_bait_role_is_the_mute_role(self):
        role_id = 41
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value=self._doctor_config(
                                baitrole_id=role_id,
                                mute_role=role_id,
                            )
                        )
                    )
                )
                ctx = self._doctor_context(role_ids=(role_id,))

                await cog.honeypot_doctor(ctx)

                report = "\n".join(call.args[0] for call in ctx.send.await_args_list)
                collision_line = next(
                    line
                    for line in report.splitlines()
                    if "bait role" in line.lower() and "mute role" in line.lower()
                )
                self.assertIn("bait role", report.lower())
                self.assertIn("mute role", report.lower())
                self.assertNotIn("❌", collision_line)

    async def test_doctor_warns_when_bait_role_is_the_joinwatch_auto_role(self):
        role_id = 42
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value=self._doctor_config(
                                baitrole_id=role_id,
                                joinwatch_auto_role_enabled=True,
                                joinwatch_auto_role_id=role_id,
                            )
                        )
                    )
                )
                ctx = self._doctor_context(role_ids=(role_id,))

                await cog.honeypot_doctor(ctx)

                report = "\n".join(call.args[0] for call in ctx.send.await_args_list)
                collision_line = next(
                    line
                    for line in report.splitlines()
                    if "bait role" in line.lower()
                    and "joinwatch auto-role" in line.lower()
                )
                self.assertIn("bait role", report.lower())
                self.assertIn("joinwatch auto-role", report.lower())
                self.assertNotIn("❌", collision_line)

    async def test_doctor_warns_when_bait_role_is_an_nhmisc_sticky_role(self):
        role_id = 43
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                bot.get_cog = mock.Mock(
                    return_value=SimpleNamespace(
                        configured_sticky_role_ids=mock.AsyncMock(
                            return_value=frozenset({role_id})
                        )
                    )
                )
                cog = honeypot.Honeypot(bot)
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value=self._doctor_config(baitrole_id=role_id)
                        )
                    )
                )
                ctx = self._doctor_context(role_ids=(role_id,))

                await cog.honeypot_doctor(ctx)

                report = "\n".join(call.args[0] for call in ctx.send.await_args_list)
                collision_line = next(
                    line
                    for line in report.splitlines()
                    if "bait role" in line.lower() and "sticky role" in line.lower()
                )
                self.assertIn("bait role", report.lower())
                self.assertIn("sticky role", report.lower())
                self.assertNotIn("❌", collision_line)

    async def test_doctor_treats_unloaded_nhmisc_as_an_unavailable_optional_check(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                bot.get_cog = mock.Mock(return_value=None)
                cog = honeypot.Honeypot(bot)
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value=self._doctor_config(baitrole_id=43)
                        )
                    )
                )
                ctx = self._doctor_context(role_ids=(43,))

                await cog.honeypot_doctor(ctx)

                report = "\n".join(call.args[0] for call in ctx.send.await_args_list)
                sticky_role_lines = [
                    line for line in report.splitlines() if "sticky role" in line.lower()
                ]
                self.assertTrue(
                    all("❌" not in line for line in sticky_role_lines),
                    report,
                )

    async def test_doctor_command_output_matches_golden(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "enabled": False,
                                "action": "invalid",
                                "fallback_action": "review",
                                "whitelist_mode": "bypass",
                            }
                        )
                    )
                )
                ctx = self._doctor_context()

                await cog.honeypot_doctor(ctx)

                actual = "".join(call.args[0] for call in ctx.send.await_args_list)
                expected = (
                    Path(__file__).with_name("golden") / "honeypot_doctor.txt"
                ).read_text(encoding="utf-8").removesuffix("\n")
                self.assertEqual(actual, expected)

    async def test_logs_rejects_thread_destination(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                text_channel_type = type("TextChannel", (), {})
                thread_type = type("Thread", (), {})
                honeypot.discord.TextChannel = text_channel_type
                honeypot.discord.Thread = thread_type
                target = thread_type()
                target.id = 55
                target.mention = "#thread"
                target.permissions_for = lambda member: SimpleNamespace(
                    view_channel=True,
                    send_messages=True,
                )
                logs_setting = SimpleNamespace(set=mock.AsyncMock())
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(logs_channel=logs_setting)
                )
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=10, me=SimpleNamespace()),
                    send=mock.AsyncMock(),
                )

                with self.assertRaises(honeypot.commands.UserFeedbackCheckFailure):
                    await cog.logs(ctx, target)

                logs_setting.set.assert_not_awaited()

    async def test_doctor_reports_thread_logs_destination_as_invalid(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                text_channel_type = type("TextChannel", (), {})
                thread_type = type("Thread", (), {})
                honeypot.discord.TextChannel = text_channel_type
                honeypot.discord.Thread = thread_type
                permissions = SimpleNamespace(
                    view_channel=True,
                    send_messages=True,
                )
                target = thread_type()
                target.id = 55
                target.mention = "#thread"
                target.permissions_for = lambda member: permissions
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "enabled": False,
                                "action": "none",
                                "fallback_action": "none",
                                "whitelist_mode": "bypass",
                                "logs_channel": target.id,
                            }
                        )
                    )
                )
                ctx = self._doctor_context()
                ctx.guild.get_thread = lambda channel_id: target
                ctx.guild.threads = [target]

                await cog.honeypot_doctor(ctx)

                report = "\n".join(call.args[0] for call in ctx.send.await_args_list)
                self.assertIn("Logs channel must be a normal text channel", report)

    async def test_doctor_checks_thread_permissions_on_logs_fallback_destination(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                text_channel_type = type("TextChannel", (), {})
                honeypot.discord.TextChannel = text_channel_type
                permissions = SimpleNamespace(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    create_public_threads=False,
                    send_messages_in_threads=True,
                    embed_links=True,
                    attach_files=True,
                    manage_threads=True,
                )
                target = text_channel_type()
                target.id = 55
                target.mention = "#logs"
                target.permissions_for = lambda member: permissions
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "enabled": False,
                                "action": "none",
                                "fallback_action": "none",
                                "whitelist_mode": "bypass",
                                "logs_channel": target.id,
                                "review_channel": None,
                            }
                        )
                    )
                )
                ctx = self._doctor_context()
                ctx.guild.get_channel = lambda channel_id: target
                ctx.guild.channels = [target]

                await cog.honeypot_doctor(ctx)

                report = "\n".join(call.args[0] for call in ctx.send.await_args_list)
                self.assertIn("Logs channel cannot host case threads", report)
                self.assertIn("Create Public Threads", report)

    async def test_case_database_healthcheck_rejects_read_only_main_database(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                database_path = data_path / "health.sqlite"
                writable_store = honeypot.DetectionCaseStore(database_path)
                writable_store.initialize()
                writable_store.verify_read_write()

                def read_only_connection(_database_path, **kwargs):
                    return sqlite3.connect(
                        f"file:{database_path.as_posix()}?mode=ro",
                        uri=True,
                        **kwargs,
                    )

                read_only_store = honeypot.DetectionCaseStore(
                    database_path, connection_factory=read_only_connection
                )

                with self.assertRaises(sqlite3.OperationalError):
                    read_only_store.verify_read_write()

                with closing(sqlite3.connect(database_path)) as connection:
                    persistent_probes = connection.execute(
                        """SELECT case_id FROM detection_cases
                           WHERE case_id LIKE 'healthcheck:%'"""
                    ).fetchall()
                self.assertEqual(persistent_probes, [])

    async def test_duplicate_file_sample_keeps_existing_record_and_canonical_file(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await cog._init_imagescan_store()
                source = data_path / "source.png"
                source.write_bytes(
                    base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                        "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                    )
                )

                first_status, first = await cog._imagescan_add_file_sample(
                    10, source, "true_positive", 99
                )
                canonical = Path(first["file_path"])
                canonical_bytes = canonical.read_bytes()
                second_status, _second = await cog._imagescan_add_file_sample(
                    10, source, "true_positive", 99
                )
                with closing(sqlite3.connect(cog._imagescan_db_path)) as connection:
                    row_count = connection.execute(
                        """SELECT COUNT(*) FROM imagescan_samples
                           WHERE guild_id = '10' AND active = 1"""
                    ).fetchone()[0]

                self.assertEqual((first_status, second_status), ("inserted", "duplicate"))
                self.assertEqual(row_count, 1)
                self.assertTrue(canonical.exists())
                self.assertEqual(canonical.read_bytes(), canonical_bytes)

    async def test_file_sample_commit_failure_removes_new_canonical_file(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await cog._init_imagescan_store()
                source = data_path / "source.png"
                source.write_bytes(
                    base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                        "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                    )
                )
                real_connect = honeypot.imagescan.sqlite3.connect
                canonical_was_published = []

                class CommitFailingConnection:
                    def __init__(self, connection):
                        object.__setattr__(self, "_connection", connection)

                    def __getattr__(self, name):
                        return getattr(self._connection, name)

                    def __setattr__(self, name, value):
                        setattr(self._connection, name, value)

                    def __enter__(self):
                        return self

                    def commit(self):
                        imports = cog._imagescan_files_path / "10" / "samples" / "imports"
                        canonical_was_published.append(
                            any(
                                path.is_file() and not path.name.startswith(".sample-")
                                for path in imports.glob("*")
                            )
                        )
                        self._connection.rollback()
                        raise sqlite3.OperationalError("injected commit failure")

                    def __exit__(self, exc_type, exc, traceback):
                        if exc_type is not None:
                            return self._connection.__exit__(exc_type, exc, traceback)
                        self.commit()

                    def close(self):
                        self._connection.close()

                def commit_failing_connect(*args, **kwargs):
                    return CommitFailingConnection(real_connect(*args, **kwargs))

                try:
                    with mock.patch.object(
                        honeypot.imagescan.sqlite3,
                        "connect",
                        side_effect=commit_failing_connect,
                    ):
                        status, _sample = await cog._imagescan_add_file_sample(
                            10, source, "true_positive", 99
                        )
                except sqlite3.OperationalError:
                    status = "raised"

                with closing(real_connect(cog._imagescan_db_path)) as connection:
                    row_count = connection.execute(
                        "SELECT COUNT(*) FROM imagescan_samples WHERE guild_id = '10'"
                    ).fetchone()[0]
                imports = cog._imagescan_files_path / "10" / "samples" / "imports"
                canonical_files = [path for path in imports.glob("*") if path.is_file()]

                self.assertEqual(canonical_was_published, [True])
                self.assertEqual(row_count, 0)
                self.assertEqual(canonical_files, [])
                self.assertEqual(status, "error")

    async def test_file_sample_does_not_overwrite_untracked_canonical_file(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await cog._init_imagescan_store()
                payload = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
                source = data_path / "source.png"
                source.write_bytes(payload)
                imports = cog._imagescan_files_path / "10" / "samples" / "imports"
                imports.mkdir(parents=True)
                canonical = imports / f"{sha256(payload).hexdigest()[:12]}-source.png"
                canonical.write_bytes(b"pre-existing canonical")

                status, _sample = await cog._imagescan_add_file_sample(
                    10, source, "true_positive", 99
                )

                with closing(sqlite3.connect(cog._imagescan_db_path)) as connection:
                    row_count = connection.execute(
                        "SELECT COUNT(*) FROM imagescan_samples WHERE guild_id = '10'"
                    ).fetchone()[0]
                self.assertEqual(status, "conflict")
                self.assertEqual(row_count, 0)
                self.assertEqual(canonical.read_bytes(), b"pre-existing canonical")

    async def test_doctor_hides_healthy_operational_details(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "enabled": False,
                                "action": "none",
                                "fallback_action": "none",
                                "whitelist_mode": "bypass",
                            }
                        )
                    )
                )
                ctx = self._doctor_context()

                await cog.honeypot_doctor(ctx)

                report = ctx.send.await_args.args[0]
                self.assertIn("⚠️ Honeypot is disabled", report)
                self.assertNotIn("Active detection cases", report)
                self.assertNotIn("Due detection cases: 0", report)
                self.assertNotIn("Stale resolving cases: 0", report)
                self.assertNotIn("Failed containment cases: 0", report)
                self.assertNotIn("Outstanding durable operations", report)

    async def test_doctor_reports_active_operational_failures(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                cog._case_store.record_operational_failure(
                    guild_id=10,
                    source="review_publish",
                    summary="Could not create the case thread",
                    occurred_at=datetime.now(timezone.utc),
                )
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "enabled": False,
                                "action": "none",
                                "fallback_action": "none",
                                "whitelist_mode": "bypass",
                            }
                        )
                    )
                )
                ctx = self._doctor_context()

                await cog.honeypot_doctor(ctx)

                report = "\n".join(call.args[0] for call in ctx.send.await_args_list)
                self.assertIn("Active operational failures: 1", report)
                self.assertIn("honeypot errors", report)

    async def test_doctor_checks_evidence_directory_off_event_loop_thread(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "enabled": False,
                                "action": "none",
                                "fallback_action": "none",
                                "whitelist_mode": "bypass",
                            }
                        )
                    )
                )
                ctx = self._doctor_context()
                event_loop_thread = get_ident()
                probe_threads = []
                named_temporary_file = honeypot.diagnostics.tempfile.NamedTemporaryFile

                def record_probe_thread(*args, **kwargs):
                    probe_threads.append(get_ident())
                    return named_temporary_file(*args, **kwargs)

                with mock.patch.object(
                    honeypot.diagnostics.tempfile,
                    "NamedTemporaryFile",
                    side_effect=record_probe_thread,
                ):
                    await cog.honeypot_doctor(ctx)

                self.assertEqual(len(probe_threads), 1)
                self.assertNotEqual(probe_threads[0], event_loop_thread)

    async def test_doctor_cleans_probe_after_evidence_directory_read_failure(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "enabled": False,
                                "action": "none",
                                "fallback_action": "none",
                                "whitelist_mode": "bypass",
                            }
                        )
                    )
                )
                ctx = self._doctor_context()

                with mock.patch.object(
                    honeypot.Path,
                    "read_bytes",
                    side_effect=OSError("injected probe read failure"),
                ):
                    await cog.honeypot_doctor(ctx)

                report = "\n".join(call.args[0] for call in ctx.send.await_args_list)
                self.assertIn("❌ Detection case evidence directory", report)
                self.assertEqual(
                    list(cog._detection_case_files_path.glob(".doctor-*")),
                    [],
                )

    async def test_doctor_paginates_every_visible_channel_permission_failure(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "enabled": False,
                                "action": "none",
                                "fallback_action": "none",
                                "whitelist_mode": "bypass",
                            }
                        )
                    )
                )
                ctx = self._doctor_context()
                channels = []
                for index in range(80):
                    permissions = SimpleNamespace(
                        view_channel=True,
                        read_message_history=True,
                        manage_messages=False,
                    )
                    channels.append(
                        SimpleNamespace(
                            mention=f"<#channel-{index:03d}-{'x' * 30}>",
                            purge=lambda: None,
                            permissions_for=lambda member, value=permissions: value,
                        )
                    )
                ctx.guild.channels = channels

                await cog.honeypot_doctor(ctx)

                pages = [call.args[0] for call in ctx.send.await_args_list]
                self.assertGreater(len(pages), 1)
                self.assertTrue(all(len(page) <= 2000 for page in pages))
                report = "\n".join(pages)
                positions = [report.index(channel.mention) for channel in channels]
                self.assertEqual(positions, sorted(positions))

    async def test_status_counts_open_cases_from_sqlite(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                self._append_case(honeypot, cog)
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "stats": {},
                                "joinwatch_pending_role_assignments": {},
                                "joinwatch_pending_roles": {},
                            }
                        )
                    )
                )
                ctx = SimpleNamespace(guild=SimpleNamespace(id=10), send=mock.AsyncMock())

                await cog.config_stats(ctx)

                report = ctx.send.await_args.args[0]
                self.assertIn("Active detection cases: 1", report)

    async def test_review_config_reports_fixed_case_lifetime_not_stale_timeout(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "review_enabled": True,
                                "review_channel": None,
                                "review_timeout_minutes": 5,
                                "review_kick_fail_warning": "false",
                            }
                        )
                    )
                )
                cog._send_config_dump = mock.AsyncMock()
                ctx = SimpleNamespace(
                    guild=SimpleNamespace(id=10, get_channel=lambda channel_id: None)
                )

                await cog.config_review(ctx)

                settings = cog._send_config_dump.await_args.args[2]
                labels = [label for label, _value in settings]
                values = dict(settings)
                self.assertNotIn("Timeout", labels)
                self.assertEqual(values["Case lifetime"], "24 hours (fixed)")

    async def test_status_reports_due_stale_and_outstanding_durable_work(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                now = datetime.now(timezone.utc)
                due = self._append_case(
                    honeypot,
                    cog,
                    user_id=20,
                    message_id=40,
                    created_at=now - timedelta(hours=25),
                )
                stale = self._append_case(
                    honeypot, cog, user_id=21, message_id=41, created_at=now
                )
                cog._case_store.claim_resolution(
                    stale.case.case_id, now - timedelta(minutes=10)
                )
                cog._case_store.ensure_operation(
                    due.case.case_id,
                    "review_publish",
                    f"review-publish:{due.case.case_id}",
                )
                cog.config = SimpleNamespace(
                    guild=lambda guild: SimpleNamespace(
                        all=mock.AsyncMock(
                            return_value={
                                "stats": {},
                                "joinwatch_pending_role_assignments": {},
                                "joinwatch_pending_roles": {},
                            }
                        )
                    )
                )
                ctx = SimpleNamespace(guild=SimpleNamespace(id=10), send=mock.AsyncMock())

                await cog.config_stats(ctx)

                report = ctx.send.await_args.args[0]
                self.assertIn("Due detection cases: 1", report)
                self.assertIn("Stale resolving cases: 1", report)
                self.assertIn("Outstanding durable operations: 1", report)

    async def test_forbidden_delete_is_visible_in_stats(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog)
                cog._case_store.update_message_delete(
                    appended.case.case_id,
                    appended.message.sequence,
                    honeypot.DeleteStatus.FORBIDDEN,
                    "manage messages denied",
                    True,
                )
                guild_config = SimpleNamespace(
                    stats=mock.AsyncMock(return_value={}),
                    joinwatch_pending_role_assignments=mock.AsyncMock(return_value={}),
                    joinwatch_pending_roles=mock.AsyncMock(return_value={}),
                )
                cog.config = SimpleNamespace(guild=lambda guild: guild_config)
                ctx = SimpleNamespace(guild=SimpleNamespace(id=10), send=mock.AsyncMock())

                await cog.honeypot_mod_stats(ctx)

                report = ctx.send.await_args.args[0]
                self.assertIn("Forbidden message deletes: 1", report)

    async def test_terminal_case_is_not_current_failed_containment(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(honeypot, cog)
                cog._case_store.update_message_delete(
                    appended.case.case_id,
                    appended.message.sequence,
                    honeypot.DeleteStatus.FORBIDDEN,
                    "manage messages denied",
                    True,
                )
                cog._case_review_rerender = mock.AsyncMock()
                await cog.resolve_detection_case(
                    appended.case.case_id, "expired"
                )
                guild_config = SimpleNamespace(
                    stats=mock.AsyncMock(return_value={}),
                    joinwatch_pending_role_assignments=mock.AsyncMock(return_value={}),
                    joinwatch_pending_roles=mock.AsyncMock(return_value={}),
                )
                cog.config = SimpleNamespace(guild=lambda guild: guild_config)
                ctx = SimpleNamespace(guild=SimpleNamespace(id=10), send=mock.AsyncMock())

                await cog.honeypot_mod_stats(ctx)

                report = ctx.send.await_args.args[0]
                self.assertIn("Failed containment cases: 0", report)
                self.assertIn("Forbidden message deletes: 0", report)

    async def test_resolved_case_copies_samples_before_evidence_cleanup(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                attachments = tuple(
                    honeypot.NewAttachment(
                        position,
                        filename,
                        5,
                        "image/png",
                        None,
                        None,
                        f"https://cdn.test/{filename}",
                    )
                    for position, filename in enumerate(("tp.png", "fp.png"))
                )
                appended = self._append_case(
                    honeypot, cog, attachments=attachments
                )
                case_directory = (
                    cog._detection_case_files_path
                    / str(appended.case.guild_id)
                    / appended.case.case_id
                    / str(appended.message.sequence)
                )
                case_directory.mkdir(parents=True)
                evidence_paths = []
                for position, filename in enumerate(("tp.png", "fp.png")):
                    evidence = case_directory / filename
                    evidence.write_bytes(filename.encode())
                    evidence_paths.append(evidence)
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        position,
                        evidence,
                    )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                tp = next(item for item in snapshot.attachments if item.position == 0)
                fp = next(item for item in snapshot.attachments if item.position == 1)
                cog._case_store.apply_attachment_decisions(
                    appended.case.case_id,
                    {tp.key: "true_positive", fp.key: "false_positive"},
                    99,
                    datetime.now(timezone.utc),
                )
                copied = []

                async def copy_sample(guild_id, source_path, decision, moderator_id):
                    self.assertTrue(source_path.exists())
                    copied.append((source_path.read_bytes(), decision, moderator_id))
                    return "inserted", {}

                cog._imagescan_add_file_sample = copy_sample
                cog._case_review_rerender = mock.AsyncMock()

                await cog.resolve_detection_case(
                    appended.case.case_id, "ban", moderator_id=99
                )

                self.assertCountEqual(
                    copied,
                    [
                        (b"tp.png", "true_positive", 99),
                        (b"fp.png", "false_positive", 99),
                    ],
                )
                self.assertTrue(all(not path.exists() for path in evidence_paths))
                compacted = cog._case_store.get_case(appended.case.case_id)
                self.assertEqual(compacted.messages, ())
                self.assertEqual(compacted.attachments, ())
                self.assertEqual(compacted.operations, ())

    async def test_user_data_deletion_removes_cases_and_case_files(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await cog._message_registry.initialize()
                target = self._append_case(honeypot, cog, user_id=20, message_id=40)
                retained = self._append_case(honeypot, cog, user_id=21, message_id=41)
                target_directory = (
                    cog._detection_case_files_path
                    / str(target.case.guild_id)
                    / target.case.case_id
                )
                retained_directory = (
                    cog._detection_case_files_path
                    / str(retained.case.guild_id)
                    / retained.case.case_id
                )
                target_directory.mkdir(parents=True)
                retained_directory.mkdir(parents=True)
                (target_directory / "target.png").write_bytes(b"target")
                retained_evidence = retained_directory / "retained.png"
                retained_evidence.write_bytes(b"retained")

                delete_user_data = getattr(cog, "red_delete_data_for_user", None)
                if delete_user_data is None:
                    self.fail("Red user-data deletion hook is missing")
                await delete_user_data(
                    requester="discord_deleted_user", user_id=20
                )

                self.assertIsNone(cog._case_store.get_case(target.case.case_id))
                self.assertIsNotNone(cog._case_store.get_case(retained.case.case_id))
                self.assertFalse(target_directory.exists())
                self.assertTrue(retained_evidence.exists())

    async def test_user_data_deletion_removes_the_remote_case_workspace(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                cog = honeypot.Honeypot(bot)
                await cog._message_registry.initialize()
                target = self._append_case(honeypot, cog, user_id=20, message_id=40)
                cog._case_store.activate_projection_endpoint(
                    target.case.case_id,
                    parent_channel_id=50,
                    summary_message_id=60,
                    thread_id=60,
                    projected_revision=1,
                    verified_at=datetime.now(timezone.utc),
                )
                thread = SimpleNamespace(delete=mock.AsyncMock())
                summary = SimpleNamespace(
                    fetch_thread=mock.AsyncMock(return_value=thread),
                    delete=mock.AsyncMock(),
                )
                parent = SimpleNamespace(
                    fetch_message=mock.AsyncMock(return_value=summary)
                )
                guild = SimpleNamespace(
                    get_channel=lambda channel_id: parent if channel_id == 50 else None,
                    get_thread=lambda _channel_id: None,
                )
                bot.get_guild = lambda guild_id: guild if guild_id == 10 else None

                await cog.red_delete_data_for_user(
                    requester="discord_deleted_user", user_id=20
                )

                thread.delete.assert_awaited_once()
                summary.delete.assert_awaited_once()
                self.assertIsNone(cog._case_store.get_case(target.case.case_id))

    async def test_remote_deletion_failure_keeps_only_a_minimal_retry_job(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                cog = honeypot.Honeypot(bot)
                await cog._message_registry.initialize()
                target = self._append_case(honeypot, cog, user_id=20, message_id=40)
                cog._case_store.activate_projection_endpoint(
                    target.case.case_id,
                    parent_channel_id=50,
                    summary_message_id=60,
                    thread_id=60,
                    projected_revision=1,
                    verified_at=datetime.now(timezone.utc),
                )
                thread = SimpleNamespace(
                    delete=mock.AsyncMock(
                        side_effect=[honeypot.discord.Forbidden(), None]
                    )
                )
                summary = SimpleNamespace(
                    fetch_thread=mock.AsyncMock(return_value=thread),
                    delete=mock.AsyncMock(),
                )
                parent = SimpleNamespace(
                    fetch_message=mock.AsyncMock(return_value=summary)
                )
                guild = SimpleNamespace(
                    get_channel=lambda channel_id: parent if channel_id == 50 else None,
                    get_thread=lambda _channel_id: None,
                )
                bot.get_guild = lambda guild_id: guild if guild_id == 10 else None

                with self.assertRaises(honeypot.discord.Forbidden):
                    await cog.red_delete_data_for_user(
                        requester="discord_deleted_user", user_id=20
                    )

                self.assertIsNone(cog._case_store.get_case(target.case.case_id))
                job = cog._case_store.get_case_deletion_job(target.case.case_id)
                self.assertIsNotNone(job)
                self.assertFalse(job.remote_deleted)
                self.assertTrue(job.local_deleted)
                self.assertTrue(job.rows_deleted)
                self.assertFalse(hasattr(job, "user_id"))
                counts = cog._case_store.operational_counts(
                    10,
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                )
                self.assertEqual(counts["privacy_deletion_jobs"], 1)

                await honeypot.review_publication._retry_detection_case_deletions(cog)

                self.assertEqual(thread.delete.await_count, 2)
                summary.delete.assert_awaited_once()
                self.assertIsNone(
                    cog._case_store.get_case_deletion_job(target.case.case_id)
                )
                counts = cog._case_store.operational_counts(
                    10,
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc) - timedelta(minutes=5),
                )
                self.assertEqual(counts["privacy_deletion_jobs"], 0)

    async def test_user_data_deletion_retries_filesystem_before_removing_personal_rows(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await cog._message_registry.initialize()
                target = self._append_case(honeypot, cog, user_id=20, message_id=40)
                target_directory = (
                    cog._detection_case_files_path
                    / str(target.case.guild_id)
                    / target.case.case_id
                )
                target_directory.mkdir(parents=True)
                evidence = target_directory / "target.png"
                evidence.write_bytes(b"target")

                with mock.patch.object(
                    honeypot.review_publication.shutil, "rmtree", side_effect=OSError("busy")
                ):
                    with self.assertRaises(OSError):
                        await cog.red_delete_data_for_user(
                            requester="discord_deleted_user", user_id=20
                        )

                self.assertIsNotNone(cog._case_store.get_case(target.case.case_id))
                job = cog._case_store.get_case_deletion_job(target.case.case_id)
                self.assertIsNotNone(job)
                self.assertFalse(job.local_deleted)
                self.assertFalse(job.rows_deleted)
                self.assertTrue(evidence.exists())

                await cog.red_delete_data_for_user(
                    requester="discord_deleted_user", user_id=20
                )

                self.assertIsNone(cog._case_store.get_case(target.case.case_id))
                self.assertFalse(target_directory.exists())
                self.assertIsNone(
                    cog._case_store.get_case_deletion_job(target.case.case_id)
                )

    async def test_guild_data_deletion_removes_only_that_guilds_cases_and_files(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                await cog._message_registry.initialize()
                target = self._append_case(
                    honeypot, cog, guild_id=10, user_id=20, message_id=40
                )
                retained = self._append_case(
                    honeypot, cog, guild_id=11, user_id=20, message_id=41
                )
                target_directory = (
                    cog._detection_case_files_path
                    / str(target.case.guild_id)
                    / target.case.case_id
                )
                retained_directory = (
                    cog._detection_case_files_path
                    / str(retained.case.guild_id)
                    / retained.case.case_id
                )
                target_directory.mkdir(parents=True)
                retained_directory.mkdir(parents=True)
                (target_directory / "target.png").write_bytes(b"target")
                retained_evidence = retained_directory / "retained.png"
                retained_evidence.write_bytes(b"retained")

                guild_remove_listener = getattr(cog, "on_guild_remove", None)
                if guild_remove_listener is None:
                    self.fail("Guild removal listener is missing")
                self.assertTrue(
                    getattr(type(cog).on_guild_remove, "__cog_listener__", False)
                )
                await guild_remove_listener(SimpleNamespace(id=10))

                self.assertIsNone(cog._case_store.get_case(target.case.case_id))
                self.assertIsNotNone(cog._case_store.get_case(retained.case.case_id))
                self.assertFalse(target_directory.exists())
                self.assertTrue(retained_evidence.exists())
