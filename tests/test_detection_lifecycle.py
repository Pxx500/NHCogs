"""Detection pipeline lifecycle: cog load and unload, loop and worker
startup, guild defaults and the module surface the pipeline exposes.
"""

import asyncio
import logging
import sqlite3
import unittest
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from types import SimpleNamespace
from unittest import mock

from tests.harness import (
    EXPECTED_GUILD_DEFAULTS,
    _async_noop,
    _Bot,
    _isolated_honeypot_modules,
    _operational_support,
)


class DetectionPipelineLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_health_reports_a_stopped_required_loop(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                pending_task = asyncio.create_task(asyncio.Event().wait())

                class Loop:
                    def __init__(self, *, running=True):
                        self.running = running

                    def is_running(self):
                        return self.running

                    def failed(self):
                        return False

                    def get_task(self):
                        return pending_task

                cog = SimpleNamespace(
                    joinwatch_auto_role_loop=Loop(running=False),
                    joinwatch_wave_loop=Loop(),
                    purge_cache_cleanup_loop=Loop(),
                    firstpost_seen_flush_loop=Loop(),
                    detection_case_loop=Loop(),
                    detection_reconciliation_loop=Loop(),
                    _daily_stats_task=pending_task,
                )

                try:
                    self.assertEqual(
                        honeypot.Honeypot.runtime_health_issues(cog),
                        ("joinwatch auto role loop is not running",),
                    )
                finally:
                    pending_task.cancel()
                    await asyncio.gather(pending_task, return_exceptions=True)

    async def test_load_prunes_unknown_guild_config_keys(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                cog.config._guilds[42] = {
                    "enabled": False,
                    "honeypot_channels": [123],
                    "honeypot_channel": 456,
                    "imagescan_channel": 789,
                }
                cog._init_firstpost_seen_store = _async_noop
                cog._init_imagescan_store = _async_noop
                cog._run_detection_reconciliation = _async_noop
                cog._restore_detection_case_views = _async_noop

                await cog.cog_load()
                try:
                    self.assertEqual(
                        cog.config._guilds[42],
                        {
                            "enabled": False,
                            "honeypot_channels": [123],
                        },
                    )
                finally:
                    await cog.cog_unload()

    async def test_user_privacy_deletion_removes_only_that_users_records(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                await self._seed_retained_record(
                    honeypot, cog, guild_id=10, user_id=42, message_id=1
                )
                await self._seed_retained_record(
                    honeypot, cog, guild_id=11, user_id=99, message_id=2
                )

                await cog.red_delete_data_for_user(
                    requester="discord_deleted_user",
                    user_id=42,
                )

                self.assertEqual(
                    await cog._message_registry.recent_by_author(10, 42),
                    (),
                )
                self.assertEqual(
                    len(await cog._message_registry.recent_by_author(11, 99)),
                    1,
                )
                open_cases = await asyncio.to_thread(cog._case_store.list_open_cases)
                self.assertFalse(any(item.case.user_id == 42 for item in open_cases))
                self.assertTrue(any(item.case.user_id == 99 for item in open_cases))

    async def test_user_privacy_deletion_still_removes_cases_when_registry_fails(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                await self._seed_retained_record(
                    honeypot, cog, guild_id=10, user_id=42, message_id=1
                )
                await self._seed_retained_record(
                    honeypot, cog, guild_id=11, user_id=99, message_id=2
                )
                registry_path = Path(cog._message_registry.database_path)
                await asyncio.to_thread(registry_path.unlink)
                await asyncio.to_thread(registry_path.mkdir)

                with self.assertRaises(sqlite3.OperationalError):
                    await cog.red_delete_data_for_user(
                        requester="discord_deleted_user",
                        user_id=42,
                    )

                open_cases = await asyncio.to_thread(cog._case_store.list_open_cases)
                self.assertFalse(any(item.case.user_id == 42 for item in open_cases))
                self.assertTrue(any(item.case.user_id == 99 for item in open_cases))

    async def test_guild_removal_deletes_only_that_guilds_records(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                await self._seed_retained_record(
                    honeypot, cog, guild_id=10, user_id=42, message_id=1
                )
                await self._seed_retained_record(
                    honeypot, cog, guild_id=11, user_id=42, message_id=2
                )

                await cog.on_guild_remove(SimpleNamespace(id=10))

                self.assertEqual(
                    await cog._message_registry.recent_by_author(10, 42),
                    (),
                )
                self.assertEqual(
                    len(await cog._message_registry.recent_by_author(11, 42)),
                    1,
                )
                open_cases = await asyncio.to_thread(cog._case_store.list_open_cases)
                self.assertFalse(any(item.case.guild_id == 10 for item in open_cases))
                self.assertTrue(any(item.case.guild_id == 11 for item in open_cases))

    async def _seed_retained_record(self, honeypot, cog, *, guild_id, user_id, message_id):
        await cog._message_registry.initialize()
        await asyncio.to_thread(cog._case_store.initialize)
        created_at = datetime(2026, 7, 13, tzinfo=timezone.utc)
        await cog._message_registry.observe(
            honeypot.MessageRecord(
                message_id=message_id,
                guild_id=guild_id,
                channel_id=30,
                author_id=user_id,
                created_at=created_at,
                pinned=False,
                author_kind="member",
                fingerprint=f"fingerprint-{message_id}",
            )
        )
        await asyncio.to_thread(
            cog._case_store.append_message,
            honeypot.NewMessage(
                guild_id=guild_id,
                user_id=user_id,
                channel_id=30,
                message_id=message_id,
                content="evidence",
                created_at=created_at,
                jump_url=None,
                attachments=(),
            ),
            (
                honeypot.DetectionSignal(
                    "honeypot",
                    "bait",
                    honeypot.ActionIntent.REVIEW,
                    True,
                    {},
                ),
            ),
        )


    async def test_guild_settings_ignore_unknown_keys_and_keep_known_values(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild_settings = honeypot.GuildSettings.from_mapping(
                    {"enabled": True, "future_setting": "ignored"}
                )

                self.assertTrue(guild_settings.enabled)
                self.assertFalse(hasattr(guild_settings, "future_setting"))

    async def test_guild_settings_warn_and_default_malformed_booleans(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                with self.assertLogs("red.Honeypot", level=logging.WARNING) as captured:
                    guild_settings = honeypot.GuildSettings.from_mapping(
                        {"enabled": "false"}
                    )

                self.assertFalse(guild_settings.enabled)
                self.assertIn("enabled", "\n".join(captured.output))

    async def test_malformed_integer_and_action_fall_back_instead_of_raising(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                with self.assertLogs("red.Honeypot", level=logging.WARNING) as captured:
                    guild_settings = honeypot.GuildSettings.from_mapping(
                        {
                            "spam_window_seconds": "invalid",
                            "spam_min_channels": 7,
                            "action": "invalid",
                            "fallback_action": "invalid",
                        }
                    )

                self.assertEqual(guild_settings.spam_window_seconds, 10)
                self.assertEqual(guild_settings.spam_min_channels, 7)
                self.assertIsNone(guild_settings.action)
                self.assertIs(
                    guild_settings.fallback_action,
                    honeypot.FallbackActionOption.REVIEW,
                )
                logged = "\n".join(captured.output)
                self.assertIn("spam_window_seconds", logged)
                self.assertIn("action", logged)

    async def test_malformed_pending_map_does_not_log_other_accounts_challenge_data(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                raw = {"joinwatch_pending_roles": {
                    "account-one": {"challenge": "private-question-answer", "session_nonce": "private-session-nonce"},
                    "account-two": "malformed entry",
                }}
                with self.assertLogs("red.Honeypot", level=logging.WARNING) as captured:
                    settings = honeypot.GuildSettings.from_mapping(raw)
                self.assertEqual(settings.joinwatch_pending_roles, {})
                logged = "\n".join(captured.output)
                self.assertIn("joinwatch_pending_roles", logged)
                self.assertNotIn("private-question-answer", logged)
                self.assertNotIn("private-session-nonce", logged)
                self.assertNotIn("account-one", logged)


    async def test_guild_settings_defaults_exactly_match_registered_config(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())

                self.assertEqual(dict(honeypot.settings.DEFAULTS), EXPECTED_GUILD_DEFAULTS)
                self.assertEqual(cog.config.defaults, EXPECTED_GUILD_DEFAULTS)

    async def test_guild_settings_never_raise_for_non_mapping_config(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                with self.assertLogs("red.Honeypot", level=logging.WARNING):
                    try:
                        guild_settings = honeypot.GuildSettings.from_mapping(None)
                    except Exception as exc:
                        self.fail(f"from_mapping raised for config input: {exc!r}")

                observed = {
                    field.name: getattr(guild_settings, field.name)
                    for field in fields(guild_settings)
                }
                self.assertEqual(observed, EXPECTED_GUILD_DEFAULTS)


    async def test_load_ignores_stale_pending_reviews_when_there_are_no_open_cases(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                cog = honeypot.Honeypot(bot, _operational_support())

                class StaleConfig:
                    def __init__(self):
                        self.read_count = 0
                        self.values = {
                            1: {
                                "pending_reviews": {
                                    "99": {
                                        "target_id": 2,
                                        "review_channel_id": 3,
                                        "expires_at": "2099-01-01T00:00:00+00:00",
                                    }
                                }
                            }
                        }

                    async def all_guilds(self):
                        self.read_count += 1
                        return self.values

                    def guild_from_id(self, guild_id):
                        values = self.values[guild_id]

                        class GuildConfig:
                            async def clear_raw(self, key):
                                values.pop(key, None)

                        return GuildConfig()

                class Store:
                    def initialize(self):
                        return None

                    def reconcile_moderator_actions(self, now):
                        return ()

                    def list_open_cases(self):
                        return ()

                    def list_due_cases(self, now):
                        return ()

                    def claim_due_operations(self, now, limit, stale_before):
                        return ()

                    def list_reconcilable_cases(self, now, stale_before):
                        return ()

                    def list_planned_case_deletions(self):
                        return ()

                    def list_orphan_publications(self):
                        return ()

                stale_config = StaleConfig()
                cog.config = stale_config
                cog._case_store = Store()
                cog._init_firstpost_seen_store = _async_noop
                cog._init_imagescan_store = _async_noop
                cog._flush_firstpost_seen_authors = _async_noop

                await cog.cog_load()
                try:
                    await cog._case_restore_task
                    await asyncio.sleep(0)

                    self.assertEqual(stale_config.read_count, 1)
                    self.assertEqual(stale_config.values[1], {})
                    restored = getattr(bot, "restored_views", [])
                    self.assertEqual(len(restored), 1)
                    self.assertEqual(restored[0][0].children[0].label, "Verify")
                    self.assertIsNone(restored[0][1])
                finally:
                    await cog.cog_unload()

    async def test_load_initializes_case_storage_before_restoring_and_starts_loops(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                initialize_started = Event()
                allow_initialize_finish = Event()
                restore_called = Event()
                initialize_observations = []
                event_loop_thread_id = get_ident()

                class Store:
                    def initialize(self):
                        initialize_observations.append(
                            (get_ident(), cog._detection_case_files_path.is_dir())
                        )
                        initialize_started.set()
                        if not allow_initialize_finish.wait(timeout=2):
                            raise TimeoutError("test did not release case-store initialization")

                    def reconcile_moderator_actions(self, now):
                        return ()

                    def list_open_cases(self):
                        restore_called.set()
                        return ()

                    def list_due_cases(self, now):
                        return ()

                    def claim_due_operations(self, now, limit, stale_before):
                        return ()

                    def list_reconcilable_cases(self, now, stale_before):
                        return ()

                    def list_planned_case_deletions(self):
                        return ()

                    def list_orphan_publications(self):
                        return ()

                cog._case_store = Store()
                cog._init_firstpost_seen_store = _async_noop
                cog._init_imagescan_store = _async_noop

                self.assertEqual(cog._detection_case_db_path, data_path / "detection_cases.sqlite")
                self.assertEqual(cog._detection_case_files_path, data_path / "detection_case_files")
                self.assertEqual(
                    cog._message_registry.database_path,
                    data_path / "message_registry.sqlite",
                )
                self.assertEqual(cog._case_views, {})
                self.assertFalse(cog._detection_case_files_path.exists())

                load_task = asyncio.create_task(cog.cog_load())
                try:
                    self.assertTrue(
                        await asyncio.to_thread(initialize_started.wait, 2),
                        "case-store initialization did not start",
                    )
                    self.assertFalse(load_task.done())
                    self.assertFalse(cog.detection_case_loop.started)
                    self.assertFalse(cog.detection_reconciliation_loop.started)
                    self.assertFalse(restore_called.is_set())
                finally:
                    allow_initialize_finish.set()

                await asyncio.wait_for(load_task, timeout=2)
                await cog._case_restore_task

                self.assertEqual(len(initialize_observations), 1)
                initialize_thread_id, evidence_directory_existed = initialize_observations[0]
                self.assertNotEqual(initialize_thread_id, event_loop_thread_id)
                self.assertTrue(evidence_directory_existed)
                self.assertTrue(restore_called.is_set())
                self.assertTrue(cog._message_registry.database_path.is_file())
                for loop_name in (
                    "joinwatch_auto_role_loop",
                    "purge_cache_cleanup_loop",
                    "firstpost_seen_flush_loop",
                    "detection_case_loop",
                    "detection_reconciliation_loop",
                ):
                    self.assertTrue(getattr(cog, loop_name).started, loop_name)
                self.assertEqual(cog.detection_case_loop.options, {"minutes": 1})
                self.assertEqual(cog.detection_reconciliation_loop.options, {"seconds": 10})
                await cog.cog_unload()

    async def test_unload_cancels_case_loops_and_case_restore(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                restore_started = asyncio.Event()
                restore_cleanup_finished = asyncio.Event()

                async def restore_until_cancelled():
                    restore_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        restore_cleanup_finished.set()

                cog._init_firstpost_seen_store = _async_noop
                cog._init_imagescan_store = _async_noop
                cog._restore_detection_case_views = restore_until_cancelled
                cog._flush_firstpost_seen_authors = _async_noop

                await cog.cog_load()
                restore_task = cog._case_restore_task
                daily_stats_task = cog._daily_stats_task
                await asyncio.wait_for(restore_started.wait(), timeout=2)
                self.assertFalse(restore_task.done())
                self.assertFalse(daily_stats_task.done())

                try:
                    await cog.cog_unload()

                    for loop_name in (
                        "joinwatch_auto_role_loop",
                        "purge_cache_cleanup_loop",
                        "firstpost_seen_flush_loop",
                        "detection_case_loop",
                        "detection_reconciliation_loop",
                    ):
                        self.assertTrue(getattr(cog, loop_name).cancelled, loop_name)
                    self.assertTrue(restore_task.cancelled())
                    self.assertTrue(daily_stats_task.cancelled())
                    self.assertTrue(restore_cleanup_finished.is_set())
                    self.assertIsNone(cog._case_restore_task)
                    self.assertIsNone(cog._daily_stats_task)
                finally:
                    restore_task.cancel()
                    daily_stats_task.cancel()
                    await asyncio.gather(
                        restore_task,
                        daily_stats_task,
                        return_exceptions=True,
                    )

    async def test_unload_awaits_cancelled_background_loops(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                loop_started = asyncio.Event()
                cleanup_started = asyncio.Event()
                cleanup_release = asyncio.Event()
                cleanup_finished = asyncio.Event()

                async def loop_until_cancelled():
                    loop_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleanup_started.set()
                        await cleanup_release.wait()
                        cleanup_finished.set()

                cog._init_firstpost_seen_store = _async_noop
                cog._init_imagescan_store = _async_noop
                cog._restore_detection_case_views = _async_noop
                cog._flush_firstpost_seen_authors = _async_noop
                await cog.cog_load()
                loop_task = asyncio.create_task(loop_until_cancelled())
                cog.detection_reconciliation_loop.task = loop_task
                await asyncio.wait_for(loop_started.wait(), timeout=2)
                unload_task = asyncio.create_task(cog.cog_unload())

                try:
                    await asyncio.wait_for(cleanup_started.wait(), timeout=2)
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            asyncio.shield(unload_task),
                            timeout=0.01,
                        )
                    cleanup_release.set()
                    await unload_task
                    self.assertTrue(loop_task.done())
                    self.assertTrue(cleanup_finished.is_set())
                finally:
                    cleanup_release.set()
                    loop_task.cancel()
                    await asyncio.gather(
                        unload_task,
                        loop_task,
                        return_exceptions=True,
                    )

    async def test_failed_case_restore_is_logged_and_cleared_on_unload(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot(), _operational_support())
                restore_failed = Event()

                class Store:
                    def initialize(self):
                        return None

                    def reconcile_moderator_actions(self, now):
                        return ()

                    def list_open_cases(self):
                        restore_failed.set()
                        raise RuntimeError("restore failed")

                    def list_due_cases(self, now):
                        return ()

                    def claim_due_operations(self, now, limit, stale_before):
                        return ()

                    def list_reconcilable_cases(self, now, stale_before):
                        return ()

                    def list_planned_case_deletions(self):
                        return ()

                    def list_orphan_publications(self):
                        return ()

                cog._case_store = Store()
                cog._init_firstpost_seen_store = _async_noop
                cog._init_imagescan_store = _async_noop
                cog._flush_firstpost_seen_authors = _async_noop

                with mock.patch.object(honeypot.log, "error") as log_error:
                    await cog.cog_load()
                    self.assertTrue(
                        await asyncio.to_thread(restore_failed.wait, 2),
                        "case restoration did not fail",
                    )
                    await asyncio.gather(cog._case_restore_task, return_exceptions=True)
                    await asyncio.sleep(0)

                    log_error.assert_called_once()
                    self.assertIn("detection case", log_error.call_args.args[1])
                    await cog.cog_unload()

                self.assertIsNone(cog._case_restore_task)

    async def test_case_loops_wait_for_red_readiness(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot(ready=False)
                cog = honeypot.Honeypot(bot, _operational_support())

                waiters = [
                    asyncio.create_task(cog.detection_case_loop.wait_before_start()),
                    asyncio.create_task(cog.detection_reconciliation_loop.wait_before_start()),
                ]
                await asyncio.sleep(0)
                self.assertTrue(all(not waiter.done() for waiter in waiters))

                bot.ready.set()
                await asyncio.gather(*waiters)
