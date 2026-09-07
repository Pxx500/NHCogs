"""Review-role ownership: applying and releasing the role, the handoff
between cases, and reclaim of a stale role worker.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import CaseExpiryTestCase, _Bot, _isolated_honeypot_modules, _operational_support


class CaseRoleTests(CaseExpiryTestCase):
    async def test_reclaimed_effect_fences_the_stale_role_worker(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(
                    roles=[], add_roles=mock.AsyncMock(), id=appended.case.user_id
                )
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                cog.bot.get_guild = lambda guild_id: guild
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                stale = cog._case_store.claim_operation(operation.operation_id, now)
                self.assertTrue(
                    cog._case_store.start_operation_effect(
                        stale.operation_id, stale.claim_token, now
                    )
                )
                reclaimed = cog._case_store.claim_due_operations(
                    now + timedelta(minutes=10),
                    stale_before=now + timedelta(minutes=5),
                )[0]
                self.assertNotEqual(stale.claim_token, reclaimed.claim_token)

                await cog._execute_detection_case_operation(stale, now)

                member.add_roles.assert_not_awaited()

    async def test_role_apply_is_superseded_by_completed_moderation(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(
                    id=appended.case.user_id,
                    roles=[],
                    add_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda _user_id: member,
                    get_role=lambda _role_id: role,
                )
                cog.bot.get_guild = lambda _guild_id: guild
                cog._increment_stat = mock.AsyncMock()
                moderation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "moderator_ban",
                    f"moderator-ban:{appended.case.case_id}",
                    actor_id=99,
                )
                claimed_moderation = cog._case_store.claim_operation(
                    moderation.operation_id,
                    now,
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        claimed_moderation.operation_id,
                        claimed_moderation.claim_token,
                        now,
                        "ban",
                    )
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(completed.status.value, "succeeded")
                self.assertEqual(completed.result, "superseded_by_moderation")
                member.add_roles.assert_not_awaited()
                self.assertFalse(
                    any(
                        call.args[1] == "pending_mute_failures"
                        for call in cog._increment_stat.await_args_list
                    )
                )

    async def test_role_apply_fetches_cache_miss_and_terminalizes_not_found(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                fetch_member = mock.AsyncMock(
                    side_effect=honeypot.discord.NotFound()
                )
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda _user_id: None,
                    fetch_member=fetch_member,
                    get_role=lambda _role_id: role,
                )
                cog.bot.get_guild = lambda _guild_id: guild
                cog._increment_stat = mock.AsyncMock()
                cog._record_operational_failure = mock.AsyncMock()
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)

                await cog._execute_detection_case_operation(claimed, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                completed = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(completed.status.value, "succeeded")
                self.assertEqual(completed.result, "member_unavailable")
                fetch_member.assert_awaited_once_with(appended.case.user_id)
                cog._record_operational_failure.assert_not_awaited()
                self.assertFalse(
                    any(
                        call.args[1] == "pending_mute_failures"
                        for call in cog._increment_stat.await_args_list
                    )
                )

    async def test_current_role_apply_warning_disappears_after_recovery(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:77",
                )
                claimed = cog._case_store.claim_operation(operation.operation_id, now)
                retry_at = now + timedelta(seconds=10)
                self.assertTrue(
                    cog._case_store.fail_operation(
                        claimed.operation_id,
                        claimed.claim_token,
                        "RuntimeError: temporary failure",
                        now,
                        retry_at,
                    )
                )

                failed = cog._case_store.get_case(appended.case.case_id)
                self.assertTrue(
                    any(
                        "temporary mute" in note.lower() and "retry" in note.lower()
                        for note in honeypot.render_timeline(failed).case_notes
                    )
                )

                retry = cog._case_store.claim_operation(
                    operation.operation_id,
                    retry_at,
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        retry.operation_id,
                        retry.claim_token,
                        retry_at,
                        "already_present",
                    )
                )
                recovered = cog._case_store.get_case(appended.case.case_id)
                self.assertFalse(
                    any(
                        "temporary mute" in note.lower()
                        for note in honeypot.render_timeline(recovered).case_notes
                    )
                )

    async def test_terminal_case_fences_late_role_apply(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(
                    roles=[], add_roles=mock.AsyncMock(), id=appended.case.user_id
                )
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                cog.bot.get_guild = lambda guild_id: guild
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                running = cog._case_store.claim_operation(operation.operation_id, now)
                lease = cog._case_store.claim_resolution(
                    appended.case.case_id, now + timedelta(seconds=1)
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.EXPIRED,
                        "expired",
                        None,
                        now + timedelta(seconds=1),
                    )
                )

                await cog._execute_detection_case_operation(running, now)

                member.add_roles.assert_not_awaited()
                stored = next(
                    item
                    for item in cog._case_store.get_case(appended.case.case_id).operations
                    if item.operation_id == operation.operation_id
                )
                self.assertEqual(stored.result, "case_terminal")

    async def test_role_apply_compensates_when_case_resolves_during_discord_add(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(id=appended.case.user_id, roles=[])

                async def add_role(added, **kwargs):
                    member.roles.append(added)
                    lease = cog._case_store.claim_resolution(
                        appended.case.case_id, now + timedelta(seconds=1)
                    )
                    self.assertTrue(
                        cog._case_store.finish_resolution(
                            lease,
                            honeypot.CaseStatus.EXPIRED,
                            "expired",
                            None,
                            now + timedelta(seconds=1),
                        )
                    )

                async def remove_role(removed, **kwargs):
                    member.roles.remove(removed)

                member.add_roles = mock.AsyncMock(side_effect=add_role)
                member.remove_roles = mock.AsyncMock(side_effect=remove_role)
                guild = SimpleNamespace(
                    id=appended.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                cog.bot.get_guild = lambda guild_id: guild
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )

                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(operation.operation_id, now), now
                )

                self.assertNotIn(role, member.roles)
                member.remove_roles.assert_awaited_once()
                snapshot = cog._case_store.get_case(appended.case.case_id)
                release = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "role_release"
                )
                self.assertEqual(release.status.value, "succeeded")

    async def test_review_role_ownership_hands_off_to_the_next_case(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                first = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(id=first.case.user_id, roles=[])

                async def add_role(*args, **kwargs):
                    member.roles.append(role)

                async def remove_role(*args, **kwargs):
                    member.roles.remove(role)

                member.add_roles = mock.AsyncMock(side_effect=add_role)
                member.remove_roles = mock.AsyncMock(side_effect=remove_role)
                guild = SimpleNamespace(
                    id=first.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                cog.bot.get_guild = lambda guild_id: guild
                first_apply = cog._case_store.ensure_operation(
                    first.case.case_id,
                    "role_apply",
                    f"role-apply:{first.case.case_id}:{role.id}",
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(first_apply.operation_id, now), now
                )
                lease = cog._case_store.claim_resolution(
                    first.case.case_id, now + timedelta(seconds=1)
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.EXPIRED,
                        "expired",
                        None,
                        now + timedelta(seconds=1),
                        final_operations=(
                            (
                                "role_release",
                                f"role-release:{first.case.case_id}:{role.id}",
                            ),
                        ),
                    )
                )
                second = self._append_case(
                    honeypot, cog, now + timedelta(seconds=2), message_id=41
                )
                second_apply = cog._case_store.ensure_operation(
                    second.case.case_id,
                    "role_apply",
                    f"role-apply:{second.case.case_id}:{role.id}",
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(
                        second_apply.operation_id, now + timedelta(seconds=2)
                    ),
                    now + timedelta(seconds=2),
                )

                self.assertEqual(
                    cog._case_store.owned_role_ids(second.case.case_id), (role.id,)
                )
                release = next(
                    item
                    for item in cog._case_store.get_case(first.case.case_id).operations
                    if item.operation_type == "role_release"
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(
                        release.operation_id, now + timedelta(seconds=3)
                    ),
                    now + timedelta(seconds=3),
                )
                member.remove_roles.assert_not_awaited()
                self.assertIn(role, member.roles)

    async def test_role_release_cannot_remove_role_after_handoff_race(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                first = self._append_case(honeypot, cog, now)
                role = SimpleNamespace(id=77)
                member = SimpleNamespace(id=first.case.user_id, roles=[])

                async def add_role(added, **kwargs):
                    if added not in member.roles:
                        member.roles.append(added)

                member.add_roles = mock.AsyncMock(side_effect=add_role)
                member.remove_roles = mock.AsyncMock()
                guild = SimpleNamespace(
                    id=first.case.guild_id,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                cog.bot.get_guild = lambda guild_id: guild
                first_apply = cog._case_store.ensure_operation(
                    first.case.case_id,
                    "role_apply",
                    f"role-apply:{first.case.case_id}:{role.id}",
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(first_apply.operation_id, now), now
                )
                lease = cog._case_store.claim_resolution(
                    first.case.case_id, now + timedelta(seconds=1)
                )
                self.assertTrue(
                    cog._case_store.finish_resolution(
                        lease,
                        honeypot.CaseStatus.EXPIRED,
                        "expired",
                        None,
                        now + timedelta(seconds=1),
                        final_operations=((
                            "role_release",
                            f"role-release:{first.case.case_id}:{role.id}",
                        ),),
                    )
                )
                release = next(
                    item
                    for item in cog._case_store.get_case(first.case.case_id).operations
                    if item.operation_type == "role_release"
                )
                release_started = asyncio.Event()
                allow_release = asyncio.Event()

                async def blocked_remove(*args, **kwargs):
                    release_started.set()
                    await allow_release.wait()
                    if role in member.roles:
                        member.roles.remove(role)
                    return True

                cog._remove_review_mute_role = blocked_remove
                release_worker = asyncio.create_task(
                    cog._execute_detection_case_operation(
                        cog._case_store.claim_operation(
                            release.operation_id, now + timedelta(seconds=2)
                        ),
                        now + timedelta(seconds=2),
                    )
                )
                await asyncio.wait_for(release_started.wait(), timeout=1)

                second = self._append_case(
                    honeypot, cog, now + timedelta(seconds=3), message_id=41
                )
                second_apply = cog._case_store.ensure_operation(
                    second.case.case_id,
                    "role_apply",
                    f"role-apply:{second.case.case_id}:{role.id}",
                )
                await cog._execute_detection_case_operation(
                    cog._case_store.claim_operation(
                        second_apply.operation_id, now + timedelta(seconds=3)
                    ),
                    now + timedelta(seconds=3),
                )
                allow_release.set()
                await release_worker

                pending = next(
                    item
                    for item in cog._case_store.get_case(second.case.case_id).operations
                    if item.operation_id == second_apply.operation_id
                )
                self.assertEqual(pending.status.value, "failed")
                retry = cog._case_store.claim_operation(
                    second_apply.operation_id,
                    pending.retry_at + timedelta(seconds=1),
                )
                await cog._execute_detection_case_operation(retry, pending.retry_at)

                self.assertIn(role, member.roles)
                self.assertEqual(
                    cog._case_store.owned_role_ids(second.case.case_id), (role.id,)
                )

    async def test_role_release_treats_departed_member_as_completed(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                now = datetime.now(timezone.utc)
                role = SimpleNamespace(id=55)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: None,
                    fetch_member=mock.AsyncMock(
                        side_effect=honeypot.discord.NotFound("departed")
                    ),
                    get_role=lambda role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot, _operational_support())
                cog.config = self._config({})
                appended = self._append_case(honeypot, cog, now)
                ownership = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:{role.id}",
                )
                ownership = cog._case_store.claim_operation(
                    ownership.operation_id,
                    now,
                )
                cog._case_store.start_operation_effect(
                    ownership.operation_id,
                    ownership.claim_token,
                    now,
                )
                self.assertEqual(
                    cog._case_store.record_operation_role_ownership(
                        ownership.operation_id,
                        ownership.claim_token,
                        appended.case.case_id,
                        guild.id,
                        appended.case.user_id,
                        role_id=role.id,
                        now=now,
                    ),
                    "owned",
                )
                cog._case_store.complete_operation(
                    ownership.operation_id,
                    ownership.claim_token,
                    now,
                    "applied",
                )
                release = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_release",
                    f"role-release:{appended.case.case_id}:{role.id}",
                )
                release = cog._case_store.claim_operation(
                    release.operation_id,
                    now,
                )

                await cog._execute_detection_case_operation(release, now)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                persisted = next(
                    item
                    for item in snapshot.operations
                    if item.operation_id == release.operation_id
                )
                self.assertEqual(persisted.status.value, "succeeded")
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id),
                    (),
                )

    async def test_failed_role_release_creates_retry_operation(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)

                async def remove_roles(*args, **kwargs):
                    raise honeypot.discord.HTTPException()

                member = SimpleNamespace(
                    id=20, roles=[role], remove_roles=remove_roles
                )
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot, _operational_support())
                cog.config = self._config({"mute_role": 55})
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                ownership = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                owned_at = datetime.now(timezone.utc)
                ownership = cog._case_store.claim_operation(ownership.operation_id, owned_at)
                cog._case_store.start_operation_effect(
                    ownership.operation_id, ownership.claim_token, owned_at
                )
                cog._case_store.record_operation_role_ownership(
                    ownership.operation_id, ownership.claim_token,
                    appended.case.case_id, 10, 20, role_id=55, now=owned_at,
                )
                cog._case_store.complete_operation(
                    ownership.operation_id, ownership.claim_token, owned_at
                )
                await cog.resolve_detection_case(appended.case.case_id, "expired")
                snapshot = cog._case_store.get_case(appended.case.case_id)

                self.assertEqual(snapshot.case.status.value, "expired")
                operation = next(
                    item for item in snapshot.operations if item.operation_type == "role_release"
                )
                self.assertEqual(operation.status.value, "failed")
                self.assertIsNotNone(operation.retry_at)

    async def test_expiry_does_not_remove_preexisting_unowned_role(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(
                    id=20,
                    roles=[role],
                    remove_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot, _operational_support())
                cog.config = self._config({"mute_role": 55})
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                claimed = cog._case_store.claim_operation(
                    operation.operation_id, datetime.now(timezone.utc)
                )
                await cog._execute_detection_case_operation(
                    claimed, datetime.now(timezone.utc)
                )
                await cog.resolve_detection_case(appended.case.case_id, "expired")
                snapshot = cog._case_store.get_case(appended.case.case_id)

                member.remove_roles.assert_not_awaited()
                self.assertNotIn(
                    "role_release",
                    {item.operation_type for item in snapshot.operations},
                )

    async def test_role_added_by_case_is_removed_once_on_expiry(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(id=20, roles=[])

                async def add_roles(added, **kwargs):
                    member.roles.append(added)

                async def remove_roles(removed, **kwargs):
                    member.roles.remove(removed)

                member.add_roles = mock.AsyncMock(side_effect=add_roles)
                member.remove_roles = mock.AsyncMock(side_effect=remove_roles)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot, _operational_support())
                cog.config = self._config({"mute_role": 55})
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                claimed = cog._case_store.claim_operation(
                    operation.operation_id, datetime.now(timezone.utc)
                )

                await cog._execute_detection_case_operation(
                    claimed, datetime.now(timezone.utc)
                )
                await cog.resolve_detection_case(appended.case.case_id, "expired")
                await cog.resolve_detection_case(appended.case.case_id, "expired")

                member.add_roles.assert_awaited_once()
                member.remove_roles.assert_awaited_once()
                self.assertNotIn(role, member.roles)

    async def test_role_apply_retry_observes_external_state_without_repeating_add(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(id=20, roles=[])

                async def add_roles(added, **kwargs):
                    member.roles.append(added)

                member.add_roles = mock.AsyncMock(side_effect=add_roles)
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot, _operational_support())
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                now = datetime.now(timezone.utc)
                first = cog._case_store.claim_operation(operation.operation_id, now)
                real_record = cog._case_store.record_operation_role_ownership
                cog._case_store.record_operation_role_ownership = mock.Mock(
                    side_effect=RuntimeError("crash after Discord")
                )

                await cog._execute_detection_case_operation(first, now)
                cog._case_store.record_operation_role_ownership = real_record
                failed = cog._case_store.get_case(appended.case.case_id)
                retry_at = next(
                    item.retry_at for item in failed.operations
                    if item.operation_id == operation.operation_id
                )
                second = cog._case_store.claim_operation(
                    operation.operation_id, retry_at + timedelta(seconds=1)
                )
                await cog._execute_detection_case_operation(second, retry_at)

                member.add_roles.assert_awaited_once()
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), ()
                )

    async def test_role_apply_reclaim_does_not_own_ambiguously_present_role(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                role = SimpleNamespace(id=55)
                member = SimpleNamespace(
                    id=20,
                    roles=[],
                    add_roles=mock.AsyncMock(),
                    remove_roles=mock.AsyncMock(),
                )
                guild = SimpleNamespace(
                    id=10,
                    get_member=lambda user_id: member,
                    get_role=lambda role_id: role,
                )
                member.guild = guild
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot, _operational_support())
                cog.config = self._config({"mute_role": 55})
                cog._is_joinwatch_active_role = mock.AsyncMock(return_value=False)
                now = datetime.now(timezone.utc)
                appended = self._append_case(honeypot, cog, now)
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                first = cog._case_store.claim_operation(operation.operation_id, now)
                self.assertTrue(
                    cog._case_store.start_operation_effect(
                        first.operation_id, first.claim_token, now
                    )
                )
                self.assertTrue(
                    cog._case_store.fail_operation(
                        first.operation_id,
                        first.claim_token,
                        "crash before Discord add",
                        now,
                        now + timedelta(seconds=1),
                    )
                )
                member.roles.append(role)
                second = cog._case_store.claim_operation(
                    operation.operation_id, now + timedelta(seconds=1)
                )

                await cog._execute_detection_case_operation(
                    second, now + timedelta(seconds=1)
                )
                await cog.resolve_detection_case(appended.case.case_id, "expired")

                member.add_roles.assert_not_awaited()
                member.remove_roles.assert_not_awaited()
                snapshot = cog._case_store.get_case(appended.case.case_id)
                role_operation = next(
                    item for item in snapshot.operations
                    if item.operation_id == operation.operation_id
                )
                projection = honeypot.render_case(snapshot)
                self.assertEqual(role_operation.result, "ambiguous_role_ownership")
                self.assertTrue(snapshot.case.needs_attention)
                self.assertTrue(projection.needs_attention)
                self.assertTrue(
                    any(
                        "could not confirm that this case applied the temporary mute role"
                        in field.value.lower()
                        for page in projection.pages
                        for field in page
                    )
                )
                self.assertEqual(cog._case_store.owned_role_ids(appended.case.case_id), ())
                self.assertIn(role, member.roles)

    async def test_stale_operation_token_cannot_start_role_effect(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                now = datetime.now(timezone.utc)
                old = cog._case_store.claim_operation(operation.operation_id, now)
                new = cog._case_store.claim_due_operations(
                    now + timedelta(minutes=6),
                    stale_before=now + timedelta(minutes=5),
                )[0]

                started = cog._case_store.start_operation_effect(
                    operation.operation_id, old.claim_token, now
                )

                self.assertFalse(started)
                self.assertFalse(
                    cog._case_store.operation_effect_started(operation.operation_id)
                )
                self.assertNotEqual(old.claim_token, new.claim_token)

    async def test_stale_worker_cannot_record_role_ownership_after_reclaim(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                appended = self._append_case(honeypot, cog, datetime.now(timezone.utc))
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "role_apply",
                    f"role-apply:{appended.case.case_id}:55",
                )
                now = datetime.now(timezone.utc)
                old = cog._case_store.claim_operation(operation.operation_id, now)
                self.assertTrue(
                    cog._case_store.start_operation_effect(
                        operation.operation_id, old.claim_token, now
                    )
                )
                new = cog._case_store.claim_due_operations(
                    now + timedelta(minutes=6),
                    stale_before=now + timedelta(minutes=5),
                )[0]

                recorded = cog._case_store.record_operation_role_ownership(
                    operation.operation_id,
                    old.claim_token,
                    appended.case.case_id,
                    10,
                    20,
                    role_id=55,
                    now=now,
                )

                self.assertFalse(recorded)
                self.assertEqual(
                    cog._case_store.owned_role_ids(appended.case.case_id), ()
                )
                self.assertNotEqual(old.claim_token, new.claim_token)
