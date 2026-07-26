import asyncio
import os
import subprocess
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tests.detection_case_fixtures import capture_attachment, publish_primary
from tests.test_detection_pipeline import _Bot, _isolated_honeypot_modules


class EvidenceCleanupHandlerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _append_case(honeypot, cog, created_at, *, attachments=()):
        cog._case_store.initialize()
        return cog._case_store.append_message(
            honeypot.NewMessage(
                guild_id=10,
                user_id=20,
                channel_id=30,
                message_id=40,
                content="evidence",
                created_at=created_at,
                jump_url="https://discord.test/messages/40",
                attachments=attachments,
            ),
            (),
        )

    @staticmethod
    def _case_root(honeypot, cog, appended):
        return (
            cog._detection_case_files_path
            / str(appended.case.guild_id)
            / appended.case.case_id
        )

    @staticmethod
    def _handler(honeypot, cog):
        handler = cog._detection_operation_handlers.resolve(
            honeypot.OperationType.EVIDENCE_CLEANUP
        )
        if handler is None:
            raise AssertionError("evidence cleanup handler is not registered")
        return handler

    @staticmethod
    def _cleanup_operation(honeypot, cog, case_id):
        return cog._case_store.ensure_operation(
            case_id,
            honeypot.OperationType.EVIDENCE_CLEANUP,
            f"evidence-cleanup:{case_id}",
        )

    @staticmethod
    def _context(honeypot, cog, operation, now):
        snapshot = cog._case_store.get_case(operation.case_id)
        return honeypot.OperationContext(
            operation=operation,
            snapshot=snapshot,
            lease=honeypot.OperationLease(
                operation_id=operation.operation_id,
                claim_token=operation.claim_token,
            ),
            now=now,
        )

    async def test_registered_handler_removes_case_tree_and_completes_with_none(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                handler = self._handler(honeypot, cog)
                appended = self._append_case(honeypot, cog, now)
                case_root = self._case_root(honeypot, cog, appended)
                nested = case_root / "nested"
                nested.mkdir(parents=True)
                (nested / "proof.png").write_bytes(b"proof")
                operation = self._cleanup_operation(
                    honeypot, cog, appended.case.case_id
                )
                claimed = cog._case_store.claim_operation(
                    operation.operation_id, now
                )

                self.assertIsNotNone(handler)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertFalse(case_root.exists())
                self.assertIs(completed.status, honeypot.OperationStatus.SUCCEEDED)
                self.assertIsNone(completed.result)

    async def test_review_dependency_uses_message_and_first_update_status(self):
        scenarios = (
            ("published_pending", True, "pending", True),
            ("published_missing", True, None, False),
            ("published_succeeded", True, "succeeded", False),
            ("unpublished_pending", False, "pending", False),
        )
        for name, published, update_status, blocked in scenarios:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)) as honeypot:
                    now = datetime.now(timezone.utc)
                    cog = honeypot.Honeypot(_Bot())
                    handler = self._handler(honeypot, cog)
                    appended = self._append_case(honeypot, cog, now)
                    if published:
                        self.assertTrue(
                            publish_primary(
                                cog._case_store,
                                appended.case.case_id,
                                90,
                                91,
                            )
                        )
                    if update_status is not None:
                        review_update = cog._case_store.ensure_operation(
                            appended.case.case_id,
                            honeypot.OperationType.REVIEW_UPDATE,
                            f"review-update:{appended.case.case_id}",
                        )
                        if update_status == "succeeded":
                            claimed_update = cog._case_store.claim_operation(
                                review_update.operation_id, now
                            )
                            self.assertTrue(
                                cog._case_store.complete_operation(
                                    claimed_update.operation_id,
                                    claimed_update.claim_token,
                                    now,
                                )
                            )
                    cleanup = self._cleanup_operation(
                        honeypot, cog, appended.case.case_id
                    )
                    case_root = self._case_root(honeypot, cog, appended)
                    case_root.mkdir(parents=True)
                    evidence = case_root / "proof.png"
                    evidence.write_bytes(b"proof")
                    context = self._context(honeypot, cog, cleanup, now)

                    if blocked:
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "^terminal review projection is not complete$",
                        ):
                            await handler(cog, context)
                        self.assertTrue(evidence.exists())
                    else:
                        outcome = await handler(cog, context)
                        self.assertEqual(outcome, honeypot.OperationOutcome())
                        self.assertFalse(case_root.exists())

    async def test_preflight_escape_prevents_every_copy_and_delete(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                handler = self._handler(honeypot, cog)
                attachments = (
                    honeypot.NewAttachment(
                        0,
                        "inside.png",
                        6,
                        "image/png",
                        None,
                        None,
                        "https://cdn.test/inside.png",
                    ),
                    honeypot.NewAttachment(
                        1,
                        "outside.pdf",
                        7,
                        "application/pdf",
                        None,
                        None,
                        "https://cdn.test/outside.pdf",
                    ),
                )
                appended = self._append_case(
                    honeypot, cog, now, attachments=attachments
                )
                case_root = self._case_root(honeypot, cog, appended)
                case_root.mkdir(parents=True)
                inside = case_root / "inside.png"
                outside = data_path / "outside.pdf"
                inside.write_bytes(b"inside")
                outside.write_bytes(b"outside")
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        inside,
                    )
                )
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        1,
                        outside,
                    )
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                cog._case_store.apply_attachment_decisions(
                    appended.case.case_id,
                    {snapshot.attachments[0].key: "true_positive"},
                    99,
                    now,
                )
                cleanup = self._cleanup_operation(
                    honeypot, cog, appended.case.case_id
                )
                copied = []

                async def copy_sample(guild_id, source_path, decision, moderator_id):
                    copied.append((guild_id, source_path, decision, moderator_id))
                    return "inserted", object()

                cog._imagescan_add_file_sample = copy_sample

                with self.assertRaisesRegex(
                    RuntimeError,
                    "^detection case evidence path escapes case root$",
                ):
                    await handler(
                        cog, self._context(honeypot, cog, cleanup, now)
                    )

                self.assertEqual(copied, [])
                self.assertTrue(inside.exists())
                self.assertTrue(outside.exists())

    async def test_copy_selects_only_existing_decided_images_and_accepts_duplicates(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                handler = self._handler(honeypot, cog)
                definitions = (
                    ("fp.png", "image/png"),
                    ("tp.png", "image/png"),
                    ("undecided.png", "image/png"),
                    ("report.pdf", "application/pdf"),
                    ("missing.png", "image/png"),
                    ("uncaptured.png", "image/png"),
                )
                attachments = tuple(
                    honeypot.NewAttachment(
                        position,
                        filename,
                        len(filename),
                        content_type,
                        None,
                        None,
                        f"https://cdn.test/{filename}",
                    )
                    for position, (filename, content_type) in enumerate(definitions)
                )
                appended = self._append_case(
                    honeypot, cog, now, attachments=attachments
                )
                cleanup = self._cleanup_operation(
                    honeypot, cog, appended.case.case_id
                )
                case_root = self._case_root(honeypot, cog, appended)
                message_root = case_root / str(appended.message.sequence)
                message_root.mkdir(parents=True)
                evidence_paths = {}
                for position, (filename, _content_type) in enumerate(definitions[:-1]):
                    path = message_root / filename
                    evidence_paths[filename] = path
                    if filename != "missing.png":
                        path.write_bytes(filename.encode())
                    self.assertTrue(
                        capture_attachment(
                            cog._case_store,
                            appended.case.case_id,
                            appended.message.sequence,
                            position,
                            path,
                        )
                    )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                by_name = {item.filename: item for item in snapshot.attachments}
                self.assertTrue(
                    cog._case_store.apply_attachment_decisions(
                        appended.case.case_id,
                        {
                            by_name["tp.png"].key: "true_positive",
                            by_name["fp.png"].key: "false_positive",
                            by_name["report.pdf"].key: "true_positive",
                            by_name["missing.png"].key: "true_positive",
                        },
                        99,
                        now,
                        resolution="images:reviewed",
                    )
                )
                copied = []

                async def copy_sample(guild_id, source_path, decision, moderator_id):
                    copied.append((guild_id, source_path.name, decision, moderator_id))
                    result = "inserted" if source_path.name == "tp.png" else "duplicate"
                    return result, object()

                cog._imagescan_add_file_sample = copy_sample

                outcome = await handler(
                    cog, self._context(honeypot, cog, cleanup, now)
                )

                self.assertCountEqual(
                    copied,
                    [
                        (10, "tp.png", "true_positive", 99),
                        (10, "fp.png", "false_positive", 99),
                    ],
                )
                self.assertEqual(outcome, honeypot.OperationOutcome())
                self.assertFalse(case_root.exists())

    async def test_unexpected_copy_result_retries_with_none_and_preserves_evidence(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                attachment = honeypot.NewAttachment(
                    0,
                    "proof.png",
                    5,
                    "image/png",
                    None,
                    None,
                    "https://cdn.test/proof.png",
                )
                appended = self._append_case(
                    honeypot, cog, now, attachments=(attachment,)
                )
                case_root = self._case_root(honeypot, cog, appended)
                case_root.mkdir(parents=True)
                evidence = case_root / "proof.png"
                evidence.write_bytes(b"proof")
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        evidence,
                    )
                )
                captured = cog._case_store.get_case(appended.case.case_id)
                lease = cog._case_store.claim_resolution(
                    appended.case.case_id, now
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.RESOLVED,
                        "images:reviewed",
                        99,
                        now,
                        decisions={
                            captured.attachments[0].key: "true_positive"
                        },
                        final_operations=(
                            (
                                "evidence_cleanup",
                                f"evidence-cleanup:{appended.case.case_id}",
                            ),
                        ),
                    )
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                cleanup = next(iter(snapshot.operations))
                claimed = cog._case_store.claim_operation(
                    cleanup.operation_id, now
                )

                async def reject_sample(guild_id, source_path, decision, moderator_id):
                    return "rejected", None

                cog._imagescan_add_file_sample = reject_sample

                await cog._execute_detection_case_operation(claimed, now)

                failed_snapshot = cog._case_store.get_case(
                    appended.case.case_id
                )
                failed = next(iter(failed_snapshot.operations))
                self.assertIs(failed.status, honeypot.OperationStatus.FAILED)
                self.assertIsNone(failed.result)
                self.assertIsNotNone(failed.retry_at)
                self.assertEqual(
                    failed.last_error,
                    "RuntimeError: failed to copy detection evidence into learning "
                    "samples: rejected",
                )
                self.assertTrue(evidence.exists())

    async def test_handler_propagates_copy_exception_unchanged(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                handler = self._handler(honeypot, cog)
                attachment = honeypot.NewAttachment(
                    0,
                    "proof.png",
                    5,
                    "image/png",
                    None,
                    None,
                    "https://cdn.test/proof.png",
                )
                appended = self._append_case(
                    honeypot, cog, now, attachments=(attachment,)
                )
                cleanup = self._cleanup_operation(
                    honeypot, cog, appended.case.case_id
                )
                case_root = self._case_root(honeypot, cog, appended)
                case_root.mkdir(parents=True)
                evidence = case_root / "proof.png"
                evidence.write_bytes(b"proof")
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        evidence,
                    )
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                cog._case_store.apply_attachment_decisions(
                    appended.case.case_id,
                    {snapshot.attachments[0].key: "true_positive"},
                    99,
                    now,
                )
                copy_error = RuntimeError("imagescan database unavailable")

                async def fail_copy(guild_id, source_path, decision, moderator_id):
                    raise copy_error

                cog._imagescan_add_file_sample = fail_copy

                with self.assertRaises(RuntimeError) as captured:
                    await handler(
                        cog, self._context(honeypot, cog, cleanup, now)
                    )

                self.assertIs(captured.exception, copy_error)
                self.assertTrue(evidence.exists())

    async def test_storage_root_escape_is_rejected(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                handler = self._handler(honeypot, cog)
                appended = self._append_case(honeypot, cog, now)
                cleanup = self._cleanup_operation(
                    honeypot, cog, appended.case.case_id
                )
                escaping_operation = replace(
                    cleanup,
                    case_id=str(Path("..") / ".." / "escaped-case"),
                )
                context = honeypot.OperationContext(
                    operation=escaping_operation,
                    snapshot=cog._case_store.get_case(appended.case.case_id),
                    lease=honeypot.OperationLease(
                        operation_id=escaping_operation.operation_id,
                        claim_token=escaping_operation.claim_token,
                    ),
                    now=now,
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "^detection case evidence path escapes storage root$",
                ):
                    await handler(cog, context)

    async def test_deletion_walk_rejects_resolved_path_outside_case_root(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                handler = self._handler(honeypot, cog)
                appended = self._append_case(honeypot, cog, now)
                cleanup = self._cleanup_operation(
                    honeypot, cog, appended.case.case_id
                )
                case_root = self._case_root(honeypot, cog, appended)
                case_root.mkdir(parents=True)
                outside = data_path / "outside"
                outside.mkdir()
                marker = outside / "marker.txt"
                marker.write_text("safe", encoding="utf-8")
                link = case_root / "outside-link"
                if os.name == "nt":
                    subprocess.run(
                        ("cmd", "/c", "mklink", "/J", str(link), str(outside)),
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    link.symlink_to(outside, target_is_directory=True)

                with self.assertRaisesRegex(
                    RuntimeError,
                    "^detection case evidence path escapes case root$",
                ):
                    await handler(
                        cog, self._context(honeypot, cog, cleanup, now)
                    )

                self.assertEqual(marker.read_text(encoding="utf-8"), "safe")
                self.assertTrue(case_root.exists())

    async def test_cancellation_propagates_and_preserves_evidence(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                attachment = honeypot.NewAttachment(
                    0,
                    "proof.png",
                    5,
                    "image/png",
                    None,
                    None,
                    "https://cdn.test/proof.png",
                )
                appended = self._append_case(
                    honeypot, cog, now, attachments=(attachment,)
                )
                cleanup = self._cleanup_operation(
                    honeypot, cog, appended.case.case_id
                )
                case_root = self._case_root(honeypot, cog, appended)
                case_root.mkdir(parents=True)
                evidence = case_root / "proof.png"
                evidence.write_bytes(b"proof")
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        evidence,
                    )
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                cog._case_store.apply_attachment_decisions(
                    appended.case.case_id,
                    {snapshot.attachments[0].key: "true_positive"},
                    99,
                    now,
                )
                claimed = cog._case_store.claim_operation(
                    cleanup.operation_id, now
                )

                async def cancel_copy(guild_id, source_path, decision, moderator_id):
                    raise asyncio.CancelledError

                cog._imagescan_add_file_sample = cancel_copy

                with self.assertRaises(asyncio.CancelledError):
                    await cog._execute_detection_case_operation(claimed, now)

                self.assertTrue(evidence.exists())

    async def test_compaction_runs_when_case_deletion_loses_the_completion_fence(
        self,
    ):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot())
                attachment = honeypot.NewAttachment(
                    0,
                    "proof.png",
                    5,
                    "image/png",
                    None,
                    None,
                    "https://cdn.test/proof.png",
                )
                appended = self._append_case(
                    honeypot, cog, now, attachments=(attachment,)
                )
                case_id = appended.case.case_id
                case_root = self._case_root(honeypot, cog, appended)
                case_root.mkdir(parents=True)
                evidence = case_root / "proof.png"
                evidence.write_bytes(b"proof")
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        case_id,
                        appended.message.sequence,
                        0,
                        evidence,
                    )
                )
                snapshot = cog._case_store.get_case(case_id)
                self.assertTrue(
                    cog._case_store.apply_attachment_decisions(
                        case_id,
                        {snapshot.attachments[0].key: "true_positive"},
                        99,
                        now,
                    )
                )
                lease = cog._case_store.claim_resolution(
                    case_id, now
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.EXPIRED,
                        "expired",
                        99,
                        now,
                        final_operations=(
                            (
                                "evidence_cleanup",
                                f"evidence-cleanup:{case_id}",
                            ),
                        ),
                    )
                )
                terminal = cog._case_store.get_case(case_id)
                cleanup = next(iter(terminal.operations))
                claimed = cog._case_store.claim_operation(
                    cleanup.operation_id, now
                )
                sample_attempts = []
                compaction_attempts = []
                real_compact = cog._case_store.compact_terminal_case

                async def delete_case_on_sample_copy(
                    guild_id, source_path, decision, moderator_id
                ):
                    sample_attempts.append(
                        (guild_id, source_path.name, decision, moderator_id)
                    )
                    await cog.red_delete_data_for_user(
                        requester="discord_deleted_user",
                        user_id=appended.case.user_id,
                    )
                    return "inserted", object()

                def record_compaction(compacted_case_id):
                    compaction_attempts.append(compacted_case_id)
                    return real_compact(compacted_case_id)

                cog._imagescan_add_file_sample = delete_case_on_sample_copy
                with mock.patch.object(
                    cog._case_store,
                    "compact_terminal_case",
                    side_effect=record_compaction,
                ):
                    await cog._execute_detection_case_operation(claimed, now)

                self.assertEqual(
                    sample_attempts,
                    [(10, "proof.png", "true_positive", 99)],
                )
                self.assertIn(case_id, compaction_attempts)
                self.assertFalse(case_root.exists())
                self.assertIsNone(cog._case_store.get_case(case_id))
                self.assertIsNone(cog._case_store.get_case_deletion_job(case_id))
