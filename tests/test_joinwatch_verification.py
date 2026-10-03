from __future__ import annotations

import copy
import importlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules
from tests.operations.test_role_apply import RoleApplyHandlerTests


class VerificationTimerTests(unittest.TestCase):
    def test_test_and_passed_entries_never_become_ban_work(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                entry = {"role_id": 5, "expires_at": (now - timedelta(days=1)).isoformat()}
                selected = honeypot.joinwatch_state.select_due_joinwatch_assignments(
                    now=now,
                    assignments_enabled=True,
                    pending_assignments={},
                    pending_roles={
                        "1": {**entry, "test": True},
                        "2": {**entry, "verification_state": "release_pending"},
                        "3": entry,
                    },
                )
                self.assertEqual([action.member_id for action in selected.role_actions], [3])


class _Value:
    def __init__(self, values):
        self.values = values

    def __await__(self):
        async def read():
            return copy.deepcopy(self.values)

        return read().__await__()

    async def __aenter__(self):
        return self.values

    async def __aexit__(self, *_args):
        return False


def _runtime(honeypot):
    module = importlib.import_module(f"{honeypot.__package__}.joinwatch_verification")
    role = SimpleNamespace(id=51)
    guild = SimpleNamespace(id=10, get_role=lambda value: role if value == 51 else None)
    member = SimpleNamespace(
        id=20,
        guild=guild,
        roles=[role],
        bot=False,
        created_at=datetime.now(timezone.utc) - timedelta(days=10),
        joined_at=datetime.now(timezone.utc),
        display_name="Member",
        display_avatar=None,
        mention="<@20>",
    )
    member.add_roles = mock.AsyncMock(side_effect=lambda role, **_kw: member.roles.append(role))
    member.remove_roles = mock.AsyncMock(side_effect=lambda role, **_kw: member.roles.remove(role))
    guild.get_member = lambda value: member if value == 20 else None
    now = datetime.now(timezone.utc)
    challenge = {
        "target": "circles",
        "count": 2,
        "answer": 2,
        "tiles": [["circles"] * count for count in (0, 1, 2, 3, 4, 5)],
    }
    raw = {
        "joinwatch_auto_role_id": 51,
        "joinwatch_auto_role_timer_minutes": 60,
        "joinwatch_pending_role_assignments": {},
        "joinwatch_verified_members": {},
        "joinwatch_pending_roles": {
            "20": {
                "incident_id": "incident",
                "role_id": 51,
                "role_owned": True,
                "captcha_enabled": True,
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "verification_state": "active",
                "failures": 0,
                "stage": 0,
                "challenge": [challenge, copy.deepcopy(challenge)],
            }
        },
    }
    config = SimpleNamespace(all=mock.AsyncMock(side_effect=lambda: copy.deepcopy(raw)))

    async def get_raw(*path, default=None):
        value = raw
        for key in path:
            if not isinstance(value, dict) or key not in value:
                return copy.deepcopy(default)
            value = value[key]
        return copy.deepcopy(value)

    config.get_raw = get_raw
    config.joinwatch_group_admission_times = SimpleNamespace(
        set=mock.AsyncMock(
            side_effect=lambda value: raw.update(joinwatch_group_admission_times=value)
        )
    )
    for key in (
        "joinwatch_pending_roles",
        "joinwatch_pending_role_assignments",
        "joinwatch_verified_members",
    ):
        setattr(config, key, lambda key=key: _Value(raw[key]))
    cog = SimpleNamespace(
        config=SimpleNamespace(guild=lambda _guild: config),
        bot=SimpleNamespace(guilds=[guild]),
        _is_protected_member=mock.AsyncMock(return_value=False),
        _punitive_effect_allowed=mock.AsyncMock(return_value=True),
        _missing_role_assignment_permission=mock.Mock(return_value=None),
        _get_text_channel_or_thread=mock.Mock(return_value=None),
        _record_operational_failure=mock.AsyncMock(),
    )
    return SimpleNamespace(
        module=module,
        cog=cog,
        raw=raw,
        member=member,
        role=role,
        owner=module.JoinwatchVerification(cog),
        now=now,
    )


def _remove_role(member, role):
    member.roles.remove(role)
    return True


async def _question(runtime, *, now=None):
    result = await runtime.owner.start(runtime.member, now=now)
    if result.status == "preparing":
        await runtime.owner.preparation.wait()
        result = await runtime.owner.start(runtime.member, now=now)
    return result


class VerificationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_enable_adopts_existing_timer_but_startup_does_not(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            entry = runtime.raw["joinwatch_pending_roles"]["20"]
            entry.update({"captcha_enabled": False, "failures": 1, "stage": 1})
            deadline = entry["expires_at"]
            original = copy.deepcopy(entry["challenge"])
            try:
                await runtime.owner.restore()
                self.assertEqual((await runtime.owner.start(runtime.member)).status, "unavailable")
                self.assertEqual(await runtime.owner.enable_existing(runtime.member.guild), 1)
                question = await _question(runtime)
                self.assertEqual(question.stage, 1)
                self.assertEqual(question.attempts_remaining, 1)
                self.assertEqual(
                    runtime.raw["joinwatch_pending_roles"]["20"]["challenge"], original
                )
                self.assertEqual(
                    runtime.raw["joinwatch_pending_roles"]["20"]["expires_at"], deadline
                )
                runtime.member.add_roles.assert_not_awaited()
            finally:
                await runtime.owner.close()

    async def test_group_limit_reserves_assignments_and_survives_owner_restart(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            runtime.raw["joinwatch_pending_roles"].clear()
            runtime.member.roles.clear()
            runtime.raw.update(
                joinwatch_groups_enabled=True,
                joinwatch_groups_max_active=1,
                joinwatch_groups_per_minute=1,
                joinwatch_group_admission_times=[],
            )
            try:
                self.assertEqual(
                    (await runtime.owner.schedule_group(runtime.member)).status, "scheduled"
                )
                assignment = runtime.raw["joinwatch_pending_role_assignments"]["20"]
                self.assertEqual(assignment["source"], "group")
                self.assertTrue(assignment["captcha_enabled"])
                runtime.raw["joinwatch_pending_role_assignments"].clear()
                await runtime.owner.close()
                runtime.owner = runtime.module.JoinwatchVerification(runtime.cog)
                self.assertEqual(
                    (await runtime.owner.schedule_group(runtime.member)).status, "capacity"
                )
                self.assertEqual(runtime.raw["joinwatch_pending_role_assignments"], {})
                runtime.member.add_roles.assert_not_awaited()
            finally:
                await runtime.owner.close()

    async def test_dryrun_answer_cannot_queue_later_role_release(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            try:
                question = await _question(runtime)
                runtime.cog._punitive_effect_allowed.return_value = False
                result = await runtime.owner.submit(runtime.member, question.session_id, 0, 2)
                self.assertEqual(result.status, "dry_run")
                self.assertEqual(
                    runtime.raw["joinwatch_pending_roles"]["20"]["verification_state"], "active"
                )
                self.assertEqual(runtime.raw["joinwatch_verified_members"], {})
                runtime.member.remove_roles.assert_not_awaited()
            finally:
                await runtime.owner.close()

    async def test_pending_case_inherits_joinwatch_role_and_both_release_orders_are_safe(self):

        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            runtime.cog._case_store = honeypot.DetectionCaseStore(
                Path(directory) / "shared-role.sqlite"
            )
            runtime.cog.bot.get_guild = lambda _guild_id: runtime.member.guild
            store = runtime.cog._case_store
            store.initialize()
            appended = store.append_message(
                honeypot.NewMessage(
                    guild_id=10,
                    user_id=20,
                    channel_id=30,
                    message_id=40,
                    content="evidence",
                    created_at=runtime.now,
                    jump_url="https://discord.test/40",
                    attachments=(),
                ),
                (),
            )
            operation = store.ensure_operation(
                appended.case.case_id,
                honeypot.OperationType.ROLE_APPLY,
                f"role-apply:{appended.case.case_id}:51",
            )
            claimed = store.claim_operation(operation.operation_id, runtime.now)
            context = RoleApplyHandlerTests._handler_context(
                honeypot, runtime.cog, claimed, runtime.now
            )
            handler = importlib.import_module(f"{honeypot.__package__}.operations.role_apply")
            try:
                await handler.role_apply_handler(runtime.cog, context)
                store.complete_operation(claimed.operation_id, claimed.claim_token, runtime.now)
                self.assertTrue(store.role_required_by_case(10, 20, 51))
                self.assertEqual(store.owned_role_ids(appended.case.case_id), (51,))
                question = await _question(runtime)
                second = await runtime.owner.submit(runtime.member, question.session_id, 0, 2)
                result = await runtime.owner.submit(runtime.member, second.session_id, 1, 2)
                self.assertEqual(result.status, "complete_restricted")
                self.assertIn(runtime.role, runtime.member.roles)
                self.assertNotIn("20", runtime.raw["joinwatch_pending_roles"])

                # A later case release is now responsible for the last shared reason.
                release = store.ensure_operation(
                    appended.case.case_id,
                    honeypot.OperationType.ROLE_RELEASE,
                    f"role-release:{appended.case.case_id}:51",
                )
                claimed = store.claim_operation(release.operation_id, runtime.now)
                context = RoleApplyHandlerTests._handler_context(
                    honeypot, runtime.cog, claimed, runtime.now
                )
                release_handler = importlib.import_module(
                    f"{honeypot.__package__}.operations.role_release"
                )
                runtime.cog._remove_review_mute_role = mock.AsyncMock(
                    side_effect=lambda member, role, reason: _remove_role(member, role)
                )
                await release_handler.role_release_handler(runtime.cog, context)
                self.assertEqual(runtime.member.roles, [])
            finally:
                await runtime.owner.close()

    async def test_case_release_retains_active_joinwatch_role(self):

        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            runtime.cog._case_store = honeypot.DetectionCaseStore(
                Path(directory) / "case-first.sqlite"
            )
            runtime.cog.bot.get_guild = lambda _guild_id: runtime.member.guild
            store = runtime.cog._case_store
            store.initialize()
            appended = store.append_message(
                honeypot.NewMessage(
                    guild_id=10,
                    user_id=20,
                    channel_id=30,
                    message_id=40,
                    content="evidence",
                    created_at=runtime.now,
                    jump_url="https://discord.test/40",
                    attachments=(),
                ),
                (),
            )
            RoleApplyHandlerTests._record_role_ownership(
                honeypot, runtime.cog, appended.case.case_id, runtime.now, role_id=51
            )
            release = store.ensure_operation(
                appended.case.case_id,
                honeypot.OperationType.ROLE_RELEASE,
                f"role-release:{appended.case.case_id}:51",
            )
            claimed = store.claim_operation(release.operation_id, runtime.now)
            context = RoleApplyHandlerTests._handler_context(
                honeypot, runtime.cog, claimed, runtime.now
            )
            handler = importlib.import_module(f"{honeypot.__package__}.operations.role_release")
            try:
                await handler.role_release_handler(runtime.cog, context)
                self.assertIn(runtime.role, runtime.member.roles)
                self.assertIn("20", runtime.raw["joinwatch_pending_roles"])
                self.assertEqual(store.owned_role_ids(appended.case.case_id), ())
            finally:
                await runtime.owner.close()

    async def test_two_errors_lock_same_incident_across_restart_without_resetting_deadline(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            try:
                deadline = runtime.raw["joinwatch_pending_roles"]["20"]["expires_at"]
                question = await _question(runtime)
                failed = await runtime.owner.submit(runtime.member, question.session_id, 0, 1)
                self.assertEqual(failed.status, "incorrect")
                await runtime.owner.close()
                runtime.owner = runtime.module.JoinwatchVerification(runtime.cog)
                await runtime.owner.restore()
                question = await _question(runtime)
                descriptor = runtime.raw["joinwatch_pending_roles"]["20"]["challenge"][0]
                wrong = (descriptor["answer"] + 1) % 6
                failed = await runtime.owner.submit(runtime.member, question.session_id, 0, wrong)
                self.assertEqual(failed.status, "locked")
                self.assertEqual((await runtime.owner.start(runtime.member)).status, "locked")
                self.assertEqual(
                    runtime.raw["joinwatch_pending_roles"]["20"]["expires_at"], deadline
                )
                runtime.member.remove_roles.assert_not_awaited()
            finally:
                await runtime.owner.close()

    async def test_success_retries_release_without_resolving_again(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            try:
                question = await _question(runtime)
                second = await runtime.owner.submit(runtime.member, question.session_id, 0, 2)
                self.assertEqual(second.stage, 1)
                duplicate = await runtime.owner.submit(runtime.member, question.session_id, 0, 2)
                self.assertEqual(duplicate.status, "stale")
                runtime.member.remove_roles.side_effect = honeypot.discord.HTTPException(
                    "temporary failure"
                )
                passed = await runtime.owner.submit(runtime.member, second.session_id, 1, 2)
                self.assertEqual(passed.status, "release_pending")
                runtime.member.remove_roles.side_effect = lambda role, **_kw: (
                    runtime.member.roles.remove(role)
                )
                self.assertEqual((await runtime.owner.start(runtime.member)).status, "complete")
                self.assertNotIn("20", runtime.raw["joinwatch_pending_roles"])
                self.assertIn("20", runtime.raw["joinwatch_verified_members"])
                self.assertEqual(runtime.member.roles, [])
            finally:
                await runtime.owner.close()

    async def test_timeout_and_restart_keep_second_stage_and_questions(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            try:
                question = await _question(runtime, now=runtime.now)
                second = await runtime.owner.submit(
                    runtime.member, question.session_id, 0, 2, now=runtime.now
                )
                await runtime.owner.close()
                runtime.owner = runtime.module.JoinwatchVerification(runtime.cog)
                await runtime.owner.restore()
                reopened = await _question(runtime, now=runtime.now + timedelta(minutes=6))
                self.assertEqual(reopened.stage, 1)
                self.assertEqual(reopened.question, second.question)
                self.assertNotEqual(reopened.session_id, second.session_id)
                self.assertEqual(
                    (await runtime.owner.submit(runtime.member, second.session_id, 1, 2)).status,
                    "stale",
                )
            finally:
                await runtime.owner.close()

    async def test_manual_reason_retains_shared_role_but_settles_joinwatch_timer(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            runtime.raw["joinwatch_pending_roles"]["20"]["manual_role_reasons"] = [51]
            try:
                question = await _question(runtime)
                second = await runtime.owner.submit(runtime.member, question.session_id, 0, 2)
                self.assertEqual(
                    (await runtime.owner.submit(runtime.member, second.session_id, 1, 2)).status,
                    "complete_restricted",
                )
                self.assertIn(runtime.role, runtime.member.roles)
                self.assertNotIn("20", runtime.raw["joinwatch_pending_roles"])
            finally:
                await runtime.owner.close()

    async def test_role_without_timer_and_pending_assignment_never_authorize_a_challenge(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            runtime.raw["joinwatch_pending_roles"].clear()
            runtime.raw["joinwatch_pending_role_assignments"]["20"] = {"role_id": 51}
            try:
                self.assertEqual((await runtime.owner.start(runtime.member)).status, "unavailable")
                runtime.member.remove_roles.assert_not_awaited()
            finally:
                await runtime.owner.close()

    async def test_replaced_incident_rejects_old_session(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            try:
                question = await _question(runtime)
                runtime.raw["joinwatch_pending_roles"]["20"] = {
                    "incident_id": "replacement",
                    "role_id": 51,
                    "expires_at": (runtime.now + timedelta(days=1)).isoformat(),
                }
                self.assertEqual(
                    (await runtime.owner.submit(runtime.member, question.session_id, 0, 2)).status,
                    "stale",
                )
                runtime.member.remove_roles.assert_not_awaited()
            finally:
                await runtime.owner.close()

    async def test_explicit_test_restarts_after_lockout_without_touching_real_timer(self):
        with (
            TemporaryDirectory() as directory,
            _isolated_honeypot_modules(Path(directory)) as honeypot,
        ):
            runtime = _runtime(honeypot)
            entry = runtime.raw["joinwatch_pending_roles"]["20"]
            entry.update({"test": True, "failures": 2})
            try:
                result = await runtime.owner.enroll_test(runtime.member)
                self.assertEqual(result.status, "enrolled")
                fresh = runtime.raw["joinwatch_pending_roles"]["20"]
                self.assertEqual(fresh["failures"], 0)
                self.assertTrue(fresh["test"])
                self.assertNotEqual(fresh["incident_id"], "incident")
            finally:
                await runtime.owner.close()
