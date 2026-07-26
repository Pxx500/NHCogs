import json
from contextlib import closing
from datetime import datetime, timedelta, timezone


_TEST_EVIDENCE_LIMIT = 1 << 40


class DetectionCaseBuilder:
    """Build persisted projection fixtures through ``DetectionCaseStore`` APIs."""

    def __init__(self, cases, database_path):
        self.cases = cases
        self.store = cases.DetectionCaseStore(database_path)
        self.store.initialize()
        self.now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
        self.case_id = None
        self._message_id = 1000

    def attachment(
        self,
        position,
        filename,
        *,
        size=128,
        content_type="image/png",
        description=None,
        spoiler=False,
    ):
        return self.cases.NewAttachment(
            position,
            filename,
            size,
            content_type,
            64,
            64,
            f"https://cdn.example.test/{filename}",
            description,
            spoiler,
        )

    def signal(self, detector, reason, *, action="review", matched=True, metadata=None):
        return self.cases.DetectionSignal(
            detector,
            reason,
            self.cases.ActionIntent(action),
            matched,
            metadata or {},
        )

    def add_message(
        self,
        *,
        channel_id=300,
        content="suspicious message",
        attachments=(),
        signals=(),
        delete_status=None,
        jump_url=None,
    ):
        self._message_id += 1
        result = self.store.append_message(
            self.cases.NewMessage(
                guild_id=100,
                user_id=200,
                channel_id=channel_id,
                message_id=self._message_id,
                content=content,
                created_at=self.now + timedelta(seconds=self._message_id - 1001),
                jump_url=jump_url,
                attachments=tuple(attachments),
                display_name="Fixture User",
                avatar_url="https://cdn.example.test/avatar.png",
                account_created_at=self.now - timedelta(days=7),
                guild_joined_at=self.now - timedelta(days=1),
            ),
            tuple(signals),
        )
        if self.case_id is None:
            self.case_id = result.case.case_id
        if delete_status is not None:
            self.store.update_message_delete(
                self.case_id,
                result.message.sequence,
                delete_status,
                None,
                delete_status is self.cases.DeleteStatus.FORBIDDEN,
            )
        return result

    def capture(self, message_sequence, position, filename):
        if not capture_attachment(
            self.store,
            self.case_id,
            message_sequence,
            position,
            filename,
        ):
            raise AssertionError("fixture attachment capture failed")

    def publish(self, attachment_keys=()):
        if not publish_primary(self.store, self.case_id, 400, 500):
            raise AssertionError("fixture primary publication failed")
        if attachment_keys and not publish_evidence(
            self.store,
            self.case_id,
            0,
            401,
            501,
            tuple(attachment_keys),
        ):
            raise AssertionError("fixture evidence publication failed")

    def scan(self, message_sequence, position, *, matched):
        if not self.store.update_attachment_scan(
            self.case_id,
            message_sequence,
            position,
            "fixture-sha256",
            "fixture-phash",
            match_metadata={"matched": matched, "distance": 2 if matched else 12},
            error=None,
        ):
            raise AssertionError("fixture attachment scan failed")

    def fail_capture(self, message_sequence, error="fixture capture failure"):
        changed = self.store.fail_pending_attachment_captures(
            self.case_id,
            message_sequence,
            error,
        )
        if not changed:
            raise AssertionError("fixture had no pending captures to fail")

    def complete_operation(self, operation_type, result=None):
        operation = self.store.ensure_operation(
            self.case_id,
            operation_type,
            f"fixture:{operation_type}:complete",
        )
        claimed = self.store.claim_operation(operation.operation_id, self.now)
        if claimed is None or not self.store.complete_operation(
            claimed.operation_id,
            claimed.claim_token,
            self.now,
            result,
        ):
            raise AssertionError("fixture operation completion failed")

    def fail_operation(self, operation_type, error="internal-stack-token"):
        operation = self.store.ensure_operation(
            self.case_id,
            operation_type,
            f"fixture:{operation_type}:failed",
        )
        claimed = self.store.claim_operation(operation.operation_id, self.now)
        if claimed is None or not self.store.fail_operation(
            claimed.operation_id,
            claimed.claim_token,
            error,
            self.now,
            None,
        ):
            raise AssertionError("fixture operation failure failed")

    def resolve(self, resolution, moderator_id):
        lease = self.store.claim_resolution(self.case_id, self.now)
        if lease is None or not self.store.finish_resolution(
            lease,
            self.cases.CaseStatus.RESOLVED,
            resolution,
            moderator_id,
            self.now + timedelta(minutes=5),
        ):
            raise AssertionError("fixture resolution failed")

    def snapshot(self):
        snapshot = self.store.get_case(self.case_id)
        if snapshot is None:
            raise AssertionError("fixture case is missing")
        return snapshot


def capture_attachment(store, case_id, message_sequence, position, evidence_path):
    snapshot = store.get_case(case_id)
    attachment = next(
        item
        for item in snapshot.attachments
        if item.message_sequence == message_sequence and item.position == position
    )
    now = datetime.now(timezone.utc)
    reservation = store.reserve_attachment_capture(
        case_id,
        message_sequence,
        position,
        attachment.size,
        now,
        stale_before=now - timedelta(minutes=5),
        max_attachment_bytes=_TEST_EVIDENCE_LIMIT,
        max_case_bytes=_TEST_EVIDENCE_LIMIT,
    )
    if reservation.status != "claimed":
        return False
    return store.complete_attachment_capture(
        case_id,
        message_sequence,
        position,
        reservation.claim_token,
        attachment.size,
        evidence_path=str(evidence_path),
        now=now,
        max_attachment_bytes=_TEST_EVIDENCE_LIMIT,
        max_case_bytes=_TEST_EVIDENCE_LIMIT,
    ) == "captured"


def publish_primary(store, case_id, channel_id, message_id):
    token = store.claim_publication(case_id, "primary", datetime.now(timezone.utc))
    return token is not None and store.complete_primary_publication(
        case_id, token, channel_id, message_id
    )


def publish_evidence(
    store, case_id, batch_index, channel_id, message_id, attachment_keys=()
):
    encoded_keys = json.dumps(
        [[key.message_sequence, key.position] for key in attachment_keys],
        separators=(",", ":"),
    )
    with closing(store._connect()) as connection, connection:
        result = connection.execute(
            """INSERT OR IGNORE INTO detection_evidence_publications
               (case_id, batch_index, channel_id, message_id, attachment_keys)
               VALUES (?, ?, ?, ?, ?)""",
            (case_id, batch_index, channel_id, message_id, encoded_keys),
        )
        return result.rowcount == 1
