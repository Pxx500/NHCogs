import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.test_chatchart import nhmisc

MODULE_PATH = (
    Path(__file__).parents[1] / "NHCogs" / "operational_errors.py"
)


def load_operational_errors_module():
    module_name = "test_operational_errors_subject"
    discord = types.ModuleType("discord")

    class AllowedMentions:
        def __init__(self, **values):
            self.__dict__.update(values)

    class File:
        def __init__(self, fp, *, filename):
            self.fp = fp
            self.filename = filename

    discord.AllowedMentions = AllowedMentions
    discord.File = File
    discord.Object = lambda *, id: SimpleNamespace(id=id)
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    old_discord = sys.modules.get("discord")
    sys.modules[module_name] = module
    sys.modules["discord"] = discord
    try:
        spec.loader.exec_module(module)
    finally:
        if old_discord is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = old_discord
    return module


operational_errors = load_operational_errors_module()


class _Setting:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        async def read():
            return self.value

        return read()


class _Config:
    def __init__(self, *, channel_id, maintainer_id):
        self._guild = SimpleNamespace(
            error_channel=_Setting(channel_id),
            error_maintainer_id=_Setting(maintainer_id),
        )

    def guild_from_id(self, _guild_id):
        return self._guild


class _Channel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.send = mock.AsyncMock()

    @staticmethod
    def permissions_for(_role):
        return SimpleNamespace(view_channel=False)


class _Guild:
    def __init__(self, channel, maintainer):
        self.default_role = object()
        self._channel = channel
        self._maintainer = maintainer

    def get_channel(self, channel_id):
        return self._channel if channel_id == self._channel.id else None

    def get_member(self, member_id):
        return self._maintainer if member_id == self._maintainer.id else None


class _Bot:
    def __init__(self, guild_id, guild):
        self._guild_id = guild_id
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if guild_id == self._guild_id else None


class OperationalErrorCommandTests(unittest.TestCase):
    def test_nhmisc_exposes_error_configuration_group(self):
        self.assertTrue(hasattr(nhmisc.NHMisc, "nhmisc_errors"))


class OperationalErrorReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_persists_occurrences_and_alerts_each_time(self):
        guild_id = 100
        maintainer = SimpleNamespace(id=300, mention="<@300>")
        channel = _Channel(200)
        bot = _Bot(guild_id, _Guild(channel, maintainer))
        config = _Config(channel_id=channel.id, maintainer_id=maintainer.id)

        with TemporaryDirectory() as directory:
            reporter = operational_errors.OperationalErrorReporter(
                bot,
                config,
                Path(directory) / "operational_errors.sqlite",
                logger=logging.getLogger("test.operational-errors"),
            )
            await reporter.initialize()
            try:
                raise ValueError("Discord rejected the message")
            except ValueError as error:
                first = await reporter.report(
                    guild_id=guild_id,
                    source="CustomCommands",
                    action="send response",
                    error=error,
                    channel_id=400,
                    message_id=500,
                )
                second = await reporter.report(
                    guild_id=guild_id,
                    source="CustomCommands",
                    action="send response",
                    error=error,
                    channel_id=400,
                    message_id=500,
                )

        self.assertIsNotNone(first)
        self.assertEqual(first.occurrences, 1)
        self.assertEqual(second.occurrences, 2)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(channel.send.await_count, 2)

    async def test_recovery_closes_the_active_fingerprint(self):
        guild_id = 100
        maintainer = SimpleNamespace(id=300, mention="<@300>")
        channel = _Channel(200)
        with TemporaryDirectory() as directory:
            reporter = operational_errors.OperationalErrorReporter(
                _Bot(guild_id, _Guild(channel, maintainer)),
                _Config(channel_id=channel.id, maintainer_id=maintainer.id),
                Path(directory) / "operational_errors.sqlite",
                logger=logging.getLogger("test.operational-errors"),
            )
            await reporter.initialize()
            failure = await reporter.report(
                guild_id=guild_id,
                source="NHMisc",
                action="daily reconciliation",
                error=RuntimeError("failed"),
            )

            self.assertEqual(await reporter.active_count(guild_id), 1)
            self.assertTrue(
                await reporter.mark_recovered(
                    guild_id=guild_id,
                    fingerprint=failure.fingerprint,
                )
            )
            self.assertEqual(await reporter.active_count(guild_id), 0)

    async def test_action_recovery_closes_all_active_fingerprints(self):
        guild_id = 100
        channel = _Channel(200)
        with TemporaryDirectory() as directory:
            reporter = operational_errors.OperationalErrorReporter(
                _Bot(guild_id, _Guild(channel, None)),
                _Config(channel_id=None, maintainer_id=None),
                Path(directory) / "operational_errors.sqlite",
                logger=logging.getLogger("test.operational-errors"),
            )
            await reporter.initialize()
            for summary in ("first failure", "second failure"):
                await reporter.report(
                    guild_id=guild_id,
                    source="NHModeration",
                    action="weekly reconciliation",
                    error=RuntimeError(summary),
                )

            self.assertEqual(await reporter.active_count(guild_id), 2)
            self.assertEqual(
                await reporter.mark_action_recovered(
                    guild_id=guild_id,
                    source="NHModeration",
                    action="weekly reconciliation",
                ),
                2,
            )
            self.assertEqual(await reporter.active_count(guild_id), 0)

    async def test_alert_failure_stays_persisted_and_is_logged(self):
        guild_id = 100
        channel = _Channel(200)
        channel.send.side_effect = RuntimeError("Discord unavailable")
        logger = mock.Mock()
        with TemporaryDirectory() as directory:
            reporter = operational_errors.OperationalErrorReporter(
                _Bot(guild_id, _Guild(channel, None)),
                _Config(channel_id=channel.id, maintainer_id=None),
                Path(directory) / "operational_errors.sqlite",
                logger=logger,
            )
            await reporter.initialize()

            await reporter.report(
                guild_id=guild_id,
                source="NHModeration",
                action="weekly reconciliation",
                error=RuntimeError("failed"),
            )

            self.assertEqual(await reporter.active_count(guild_id), 1)
            logger.exception.assert_called_once()

    async def test_achievement_interaction_failure_is_reported(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog._send_achievement_interaction_error = mock.AsyncMock()
        cog.report_operational_error = mock.AsyncMock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel_id=200,
            user=SimpleNamespace(id=300),
        )
        error = RuntimeError("database unavailable")

        await nhmisc.NHMisc._handle_achievement_interaction_failure(
            cog,
            interaction,
            "load profile",
            error,
            public_defer=False,
        )

        cog.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="NHMisc",
            action="load profile",
            error=error,
            channel_id=200,
        )

    async def test_unexpected_prefix_command_failure_is_reported(self):
        cog = object.__new__(nhmisc.NHMisc)
        cog.report_operational_error = mock.AsyncMock()
        error = RuntimeError("send failed")
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
            message=SimpleNamespace(id=300),
            command=SimpleNamespace(qualified_name="nhmisc log voice"),
        )

        await nhmisc.NHMisc.cog_command_error(cog, ctx, error)

        cog.report_operational_error.assert_awaited_once_with(
            guild_id=100,
            source="NHMisc",
            action="nhmisc log voice",
            error=error,
            channel_id=200,
            message_id=300,
        )


if __name__ == "__main__":
    unittest.main()
