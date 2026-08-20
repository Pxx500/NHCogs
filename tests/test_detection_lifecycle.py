"""Detection pipeline lifecycle: cog load and unload, loop and worker
startup, guild defaults and the module surface the pipeline exposes.
"""

import asyncio
import logging
import sys
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
from types import ModuleType, SimpleNamespace
from unittest import mock

from tests.harness import (
    _MISSING,
    EXPECTED_GUILD_DEFAULTS,
    _async_noop,
    _Bot,
    _isolated_honeypot_modules,
)


class DetectionPipelineLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_prunes_unknown_guild_config_keys(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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

    async def test_user_privacy_deletion_attempts_cases_after_registry_failure(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._message_registry.forget_user = mock.AsyncMock(
                    side_effect=RuntimeError("registry unavailable")
                )
                delete_cases = mock.AsyncMock()

                with mock.patch.object(
                    honeypot.review_publication,
                    "_delete_detection_case_scope",
                    new=delete_cases,
                ):
                    with self.assertRaises(RuntimeError):
                        await cog.red_delete_data_for_user(
                            requester="discord_deleted_user",
                            user_id=42,
                        )

                delete_cases.assert_awaited_once_with(
                    cog,
                    cog._case_store.plan_user_case_deletion,
                    42,
                )

    async def test_guild_privacy_deletion_attempts_cases_after_registry_failure(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._message_registry.forget_guild = mock.AsyncMock(
                    side_effect=RuntimeError("registry unavailable")
                )
                delete_cases = mock.AsyncMock()

                with mock.patch.object(
                    honeypot.review_publication,
                    "_delete_detection_case_scope",
                    new=delete_cases,
                ):
                    with self.assertRaises(RuntimeError):
                        await cog.on_guild_remove(SimpleNamespace(id=84))

                delete_cases.assert_awaited_once_with(
                    cog,
                    cog._case_store.plan_guild_case_deletion,
                    84,
                )

    async def test_gateway_delete_and_pin_events_synchronize_message_registry(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._message_registry = SimpleNamespace(
                    forget=mock.AsyncMock(),
                    forget_many=mock.AsyncMock(),
                    set_pinned=mock.AsyncMock(),
                    forget_channel=mock.AsyncMock(),
                )
                honeypot.channel_routing.clear_deleted_channel = mock.AsyncMock()
                honeypot.manual_punishment.clear_deleted_channel = mock.AsyncMock()
                honeypot.manual_punishment.clear_deleted_role = mock.AsyncMock()

                await cog.on_raw_message_delete(SimpleNamespace(message_id=10))
                await cog.on_raw_bulk_message_delete(
                    SimpleNamespace(message_ids={11, 12})
                )
                await cog.on_raw_message_edit(
                    SimpleNamespace(message_id=13, data={"pinned": True})
                )
                channel = SimpleNamespace(id=14, guild=SimpleNamespace(id=15))
                await cog.on_guild_channel_delete(channel)
                role = SimpleNamespace(id=16, guild=channel.guild)
                await cog.on_guild_role_delete(role)

                cog._message_registry.forget.assert_awaited_once_with(10)
                cog._message_registry.forget_many.assert_awaited_once_with({11, 12})
                cog._message_registry.set_pinned.assert_awaited_once_with(13, True)
                cog._message_registry.forget_channel.assert_awaited_once_with(15, 14)
                honeypot.channel_routing.clear_deleted_channel.assert_awaited_once_with(
                    cog, channel
                )
                honeypot.manual_punishment.clear_deleted_channel.assert_awaited_once_with(
                    cog, channel
                )
                honeypot.manual_punishment.clear_deleted_role.assert_awaited_once_with(
                    cog, role
                )

    def test_fallback_keeps_diagnostic_commands_on_cog_and_exposes_implementations(self):
        implementation_names = (
            "config_dump",
            "console_dump",
            "honeypot_doctor",
            "honeypot_errors",
            "honeypot_errors_clear",
            "honeypot_mod_stats",
            "honeypot_reset_stats",
            "honeypot_stats",
            "review_dump",
        )
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                diagnostics = getattr(honeypot, "diagnostics", None)
                self.assertIsNotNone(diagnostics)
                for name in implementation_names:
                    with self.subTest(command=name):
                        command = getattr(honeypot.Honeypot, name)
                        self.assertEqual(command.callback.__module__, "Honeypot.honeypot")
                        self.assertTrue(callable(getattr(diagnostics, name, None)))

    async def test_cog_after_invoke_keeps_group_cleanup_override(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = object.__new__(honeypot.Honeypot)
                ctx = SimpleNamespace(
                    command=honeypot.Honeypot.debug,
                    invoked_subcommand=object(),
                )
                self.assertTrue(hasattr(ctx.command, "invoke_without_command"))

                result = await cog.cog_after_invoke(ctx)

                self.assertIsNone(result)

    async def test_configuration_option_enums_preserve_public_values(self):
        expected_options = (
            ("CoreActionOption", "CORE_ACTION_OPTIONS", ("kick", "ban", "review", "none")),
            ("FallbackActionOption", "FALLBACK_ACTION_OPTIONS", ("review", "kick", "ban", "none")),
            ("WhitelistModeOption", "WHITELIST_MODE_OPTIONS", ("bypass", "review", "fallback", "none")),
            ("JoinwatchAutoRoleActionOption", "JOINWATCH_AUTO_ROLE_ACTION_OPTIONS", ("none", "kick", "ban")),
            ("BaitActionOption", "BAIT_ACTION_OPTIONS", ("kick", "ban")),
            ("ImageScanDetectorActionOption", "IMAGE_SCAN_DETECTOR_ACTION_OPTIONS", ("none", "review", "kick", "ban")),
            ("ReviewKickFailWarningMode", "REVIEW_KICK_FAIL_WARNING_MODES", ("false", "true", "manual")),
        )
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                for enum_name, tuple_name, expected in expected_options:
                    with self.subTest(enum_name=enum_name):
                        enum_type = getattr(honeypot, enum_name, None)
                        self.assertIsNotNone(enum_type)
                        self.assertEqual(
                            tuple(member.value for member in enum_type),
                            expected,
                        )
                        self.assertEqual(getattr(honeypot, tuple_name), expected)

    async def test_empty_guild_settings_use_registered_defaults(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                settings_type = getattr(honeypot, "GuildSettings", None)
                self.assertIsNotNone(settings_type)

                guild_settings = settings_type.from_mapping({})

                observed = {
                    field.name: getattr(guild_settings, field.name)
                    for field in fields(guild_settings)
                }
                self.assertEqual(observed, EXPECTED_GUILD_DEFAULTS)

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

    async def test_guild_settings_coerce_integer_fields_independently(self):
        raw = {
            "purge_backward_seconds": 90,
            "purge_forward_seconds": 20,
            "spam_window_seconds": "invalid",
            "spam_min_channels": 3,
            "imagescan_detector_threshold": 12,
            "joinwatch_min_age_hours": 48,
            "joinwatch_auto_role_timer_minutes": 60,
            "joinwatch_auto_role_random_delay_min_minutes": 2,
            "joinwatch_auto_role_random_delay_max_minutes": 8,
        }
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                with self.assertLogs("red.Honeypot", level=logging.WARNING) as captured:
                    guild_settings = honeypot.GuildSettings.from_mapping(raw)

                self.assertEqual(guild_settings.purge_backward_seconds, 90)
                self.assertEqual(guild_settings.purge_forward_seconds, 20)
                self.assertEqual(guild_settings.spam_window_seconds, 10)
                self.assertEqual(guild_settings.spam_min_channels, 3)
                self.assertEqual(guild_settings.imagescan_detector_threshold, 12)
                self.assertEqual(guild_settings.joinwatch_min_age_hours, 48)
                self.assertEqual(guild_settings.joinwatch_auto_role_timer_minutes, 60)
                self.assertEqual(guild_settings.joinwatch_auto_role_random_delay_min_minutes, 2)
                self.assertEqual(guild_settings.joinwatch_auto_role_random_delay_max_minutes, 8)
                self.assertIn("spam_window_seconds", "\n".join(captured.output))

    async def test_guild_settings_preserve_every_boolean_toggle(self):
        raw = {
            "enabled": True,
            "dry_run": True,
            "firstpost_collect_enabled": True,
            "firstpost_enabled": True,
            "spam_enabled": True,
            "imagescan_detector_enabled": True,
            "review_enabled": True,
            "automated_kick_fail_warning": True,
            "joinwatch_enabled": True,
            "joinwatch_alert_enabled": False,
            "joinwatch_auto_role_enabled": True,
            "joinwatch_auto_role_random_delay_enabled": True,
            "baitrole_enabled": True,
        }
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild_settings = honeypot.GuildSettings.from_mapping(raw)

                for key, expected in raw.items():
                    with self.subTest(key=key):
                        self.assertIs(getattr(guild_settings, key), expected)

    async def test_guild_settings_coerce_optional_discord_ids(self):
        raw = {
            "errors_channel": 11,
            "mute_role": "invalid",
            "review_channel": 44,
            "joinwatch_channel": 55,
            "joinwatch_auto_role_id": 66,
            "baitrole_id": 77,
        }
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                with self.assertLogs("red.Honeypot", level=logging.WARNING) as captured:
                    guild_settings = honeypot.GuildSettings.from_mapping(raw)

                self.assertEqual(guild_settings.errors_channel, 11)
                self.assertIsNone(guild_settings.mute_role)
                self.assertEqual(guild_settings.review_channel, 44)
                self.assertEqual(guild_settings.joinwatch_channel, 55)
                self.assertEqual(guild_settings.joinwatch_auto_role_id, 66)
                self.assertEqual(guild_settings.baitrole_id, 77)
                self.assertIn("mute_role", "\n".join(captured.output))

    async def test_guild_settings_copy_and_validate_list_and_set_values(self):
        raw = {
            "honeypot_channels": {10, 20},
            "whitelisted_roles": (30, 40),
            "scam_keywords": {"alpha", "beta"},
            "attachment_patterns": [1],
        }
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                with self.assertLogs("red.Honeypot", level=logging.WARNING) as captured:
                    guild_settings = honeypot.GuildSettings.from_mapping(raw)

                self.assertEqual(set(guild_settings.honeypot_channels), {10, 20})
                self.assertEqual(guild_settings.whitelisted_roles, [30, 40])
                self.assertEqual(set(guild_settings.scam_keywords), {"alpha", "beta"})
                self.assertEqual(
                    guild_settings.attachment_patterns,
                    EXPECTED_GUILD_DEFAULTS["attachment_patterns"],
                )
                self.assertIn("attachment_patterns", "\n".join(captured.output))

    async def test_guild_settings_copy_and_validate_mapping_values(self):
        stats = {"detections": 5}
        assignments = {"7": {"role_id": 9, "retry_count": 1}}
        raw = {
            "stats": stats,
            "joinwatch_pending_role_assignments": assignments,
            "joinwatch_pending_roles": ["invalid"],
        }
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                with self.assertLogs("red.Honeypot", level=logging.WARNING) as captured:
                    guild_settings = honeypot.GuildSettings.from_mapping(raw)

                self.assertEqual(guild_settings.stats, stats)
                self.assertEqual(
                    guild_settings.joinwatch_pending_role_assignments,
                    assignments,
                )
                self.assertIsNot(guild_settings.joinwatch_pending_role_assignments, assignments)
                self.assertEqual(guild_settings.joinwatch_pending_roles, {})
                self.assertIn("joinwatch_pending_roles", "\n".join(captured.output))

    async def test_guild_settings_coerce_option_values_to_phase_one_enums(self):
        raw = {
            "action": "ban",
            "fallback_action": "kick",
            "firstpost_action": "none",
            "spam_action": "ban",
            "imagescan_detector_action": "kick",
            "review_kick_fail_warning": "manual",
            "whitelist_mode": "fallback",
            "joinwatch_auto_role_action": "ban",
            "baitrole_action": "kick",
        }
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild_settings = honeypot.GuildSettings.from_mapping(raw)

                self.assertIs(guild_settings.action, honeypot.CoreActionOption.BAN)
                self.assertIs(
                    guild_settings.fallback_action,
                    honeypot.FallbackActionOption.KICK,
                )
                self.assertIs(
                    guild_settings.firstpost_action,
                    honeypot.CoreActionOption.NONE,
                )
                self.assertIs(guild_settings.spam_action, honeypot.CoreActionOption.BAN)
                self.assertIs(
                    guild_settings.imagescan_detector_action,
                    honeypot.ImageScanDetectorActionOption.KICK,
                )
                self.assertIs(
                    guild_settings.review_kick_fail_warning,
                    honeypot.ReviewKickFailWarningMode.MANUAL,
                )
                self.assertIs(
                    guild_settings.whitelist_mode,
                    honeypot.WhitelistModeOption.FALLBACK,
                )
                self.assertIs(
                    guild_settings.joinwatch_auto_role_action,
                    honeypot.JoinwatchAutoRoleActionOption.BAN,
                )
                self.assertIs(
                    guild_settings.baitrole_action,
                    honeypot.BaitActionOption.KICK,
                )

                with self.assertLogs("red.Honeypot", level=logging.WARNING) as captured:
                    malformed = honeypot.GuildSettings.from_mapping(
                        {"action": "invalid", "fallback_action": "invalid"}
                    )

                self.assertIsNone(malformed.action)
                self.assertIs(
                    malformed.fallback_action,
                    honeypot.FallbackActionOption.REVIEW,
                )
                self.assertIn("action", "\n".join(captured.output))

    async def test_guild_settings_copy_canonical_honeypot_channels(self):
        canonical = [10, 20]
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild_settings = honeypot.GuildSettings.from_mapping(
                    {
                        "honeypot_channel": 30,
                        "honeypot_channels": canonical,
                    }
                )

                self.assertEqual(guild_settings.honeypot_channels, [10, 20])
                self.assertIsNot(guild_settings.honeypot_channels, canonical)

    async def test_guild_settings_are_frozen_snapshots(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                guild_settings = honeypot.GuildSettings.from_mapping({})

                with self.assertRaises(FrozenInstanceError):
                    guild_settings.enabled = True

    async def test_guild_settings_defaults_exactly_match_registered_config(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())

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

    async def test_isolation_removes_new_nested_honeypot_module(self):
        module_name = "Honeypot.operations.source_delete"
        sys.modules.pop(module_name, None)
        try:
            with TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)):
                    sys.modules[module_name] = ModuleType(module_name)

            self.assertNotIn(module_name, sys.modules)
        finally:
            sys.modules.pop(module_name, None)

    async def test_isolation_restores_preexisting_nested_honeypot_module(self):
        module_name = "Honeypot.operations.source_delete"
        previous = sys.modules.get(module_name, _MISSING)
        sentinel = ModuleType(module_name)
        sys.modules[module_name] = sentinel
        try:
            with TemporaryDirectory() as directory:
                with _isolated_honeypot_modules(Path(directory)):
                    pass

            self.assertIs(sys.modules.get(module_name), sentinel)
        finally:
            if previous is _MISSING:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous

    async def test_each_isolated_load_owns_one_detection_view_identity(self):
        module_name = "Honeypot.views"
        previous = sys.modules.get(module_name, _MISSING)

        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as first_honeypot:
                self.assertTrue(module_name in sys.modules)
                first_view = first_honeypot.DetectionCaseView
                self.assertIs(
                    first_view,
                    sys.modules[module_name].DetectionCaseView,
                )

            self.assertIs(sys.modules.get(module_name, _MISSING), previous)

            with _isolated_honeypot_modules(Path(directory)) as second_honeypot:
                self.assertTrue(module_name in sys.modules)
                second_view = second_honeypot.DetectionCaseView
                self.assertIs(
                    second_view,
                    sys.modules[module_name].DetectionCaseView,
                )

            self.assertIs(sys.modules.get(module_name, _MISSING), previous)

        self.assertIsNot(first_view, second_view)

    async def test_isolated_load_keeps_one_identity_per_shared_symbol(self):
        # A stale load order let a module be imported before its dependency, so
        # the dependency was created twice and half the package saw the other
        # copy. Nothing raises when that happens; identity checks are the only
        # way to see it.
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                for symbol in (
                    "GuildSettings",
                    "OperationType",
                    "DetectionCaseStore",
                    "DeleteStatus",
                ):
                    with self.subTest(symbol=symbol):
                        owners = {}
                        for name, module in list(sys.modules.items()):
                            if not name.startswith("Honeypot."):
                                continue
                            value = getattr(module, symbol, None)
                            if value is not None:
                                owners.setdefault(id(value), []).append(name)

                        self.assertEqual(
                            len(owners),
                            1,
                            f"{symbol} exists as {len(owners)} distinct objects: "
                            f"{[sorted(names) for names in owners.values()]}",
                        )

    async def test_load_ignores_stale_pending_reviews_when_there_are_no_open_cases(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                bot = _Bot()
                cog = honeypot.Honeypot(bot)

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
                    self.assertEqual(getattr(bot, "restored_views", []), [])
                finally:
                    await cog.cog_unload()

    async def test_load_initializes_case_storage_before_restoring_and_starts_loops(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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
                root_logger = logging.getLogger()
                self.assertEqual(root_logger.handlers.count(cog._console_log_buffer), 1)

                await cog.cog_unload()
                self.assertNotIn(cog._console_log_buffer, root_logger.handlers)

    async def test_unload_cancels_case_loops_and_case_restore(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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

    async def test_failed_case_restore_is_logged_and_cleared_on_unload(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
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
                cog = honeypot.Honeypot(bot)

                waiters = [
                    asyncio.create_task(cog.detection_case_loop.wait_before_start()),
                    asyncio.create_task(cog.detection_reconciliation_loop.wait_before_start()),
                ]
                await asyncio.sleep(0)
                self.assertTrue(all(not waiter.done() for waiter in waiters))

                bot.ready.set()
                await asyncio.gather(*waiters)
