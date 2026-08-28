from __future__ import annotations

import types
import unittest
from unittest import mock

from tests.test_chatchart import load_nhmisc_module

nhmisc = load_nhmisc_module()


class _ConfigValue:
    def __init__(self, values, key):
        self.values = values
        self.key = key

    async def __call__(self):
        return self.values.get(self.key)

    async def set(self, value):
        self.values[self.key] = value

    async def clear(self):
        self.values[self.key] = None


class _Config:
    def __init__(self):
        self.values = {
            "bot_proxy_channel": None,
            "bot_proxy_delete_closed_sessions": False,
            "bot_proxy_enabled": True,
        }

    def guild(self, _guild):
        return types.SimpleNamespace(
            bot_proxy_channel=_ConfigValue(self.values, "bot_proxy_channel"),
            bot_proxy_delete_closed_sessions=_ConfigValue(
                self.values,
                "bot_proxy_delete_closed_sessions",
            ),
            bot_proxy_enabled=_ConfigValue(self.values, "bot_proxy_enabled"),
        )

    def guild_from_id(self, _guild_id):
        return self.guild(None)


class BotProxyCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_setting_shows_state_and_disables_through_manager(
        self,
    ) -> None:
        guild = types.SimpleNamespace(id=10)
        ctx = types.SimpleNamespace(guild=guild, send=mock.AsyncMock())
        manager = types.SimpleNamespace(
            enabled=mock.AsyncMock(return_value=False),
            set_enabled=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._ensure_bot_proxy = mock.Mock(return_value=manager)

        await nhmisc.NHMisc.botproxy_enabled.callback(cog, ctx, False)

        manager.set_enabled.assert_awaited_once_with(guild, False)
        ctx.send.assert_not_awaited()

        await nhmisc.NHMisc.botproxy_enabled.callback(cog, ctx, None)

        manager.enabled.assert_awaited_once_with(guild)
        self.assertIn("disabled", ctx.send.await_args.args[0])

    async def test_create_reports_preflight_failure_directly_without_deleting_command(
        self,
    ) -> None:
        guild = types.SimpleNamespace(id=10)
        ctx = types.SimpleNamespace(
            guild=guild,
            channel=types.SimpleNamespace(id=30),
            author=types.SimpleNamespace(id=20),
            message=types.SimpleNamespace(id=40, delete=mock.AsyncMock()),
            send=mock.AsyncMock(),
        )
        manager = types.SimpleNamespace(
            require_enabled=mock.AsyncMock(),
            workspace_channel=mock.AsyncMock(
                side_effect=ValueError("Bot Proxy channel is unavailable")
            ),
            create_session=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._ensure_bot_proxy = mock.Mock(return_value=manager)

        await nhmisc.NHMisc.botproxy_create.callback(cog, ctx)

        ctx.send.assert_awaited_once()
        self.assertIn("unavailable", ctx.send.await_args.args[0])
        ctx.message.delete.assert_not_awaited()
        manager.create_session.assert_not_awaited()

    async def test_deleteclosed_configuration_can_be_shown_and_changed(self) -> None:
        config = _Config()
        ctx = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=10),
            channel=types.SimpleNamespace(id=20),
            send=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog.config = config
        cog._channel_allows_everyone = mock.Mock(return_value=False)

        await nhmisc.NHMisc.botproxy_deleteclosed.callback(cog, ctx, True)
        self.assertTrue(config.values["bot_proxy_delete_closed_sessions"])

        await nhmisc.NHMisc.botproxy_deleteclosed.callback(cog, ctx, None)
        self.assertIn("enabled", ctx.send.await_args.args[0])

    async def test_create_deletes_invocation_and_always_opens_new_session(self) -> None:
        channel = types.SimpleNamespace(id=30, mention="<#30>")
        guild = types.SimpleNamespace(id=10)
        author = types.SimpleNamespace(id=20)
        events = []
        delete = mock.AsyncMock(side_effect=lambda: events.append("delete"))
        ctx = types.SimpleNamespace(
            guild=guild,
            channel=channel,
            author=author,
            message=types.SimpleNamespace(id=40, delete=delete),
        )
        manager = types.SimpleNamespace(
            require_enabled=mock.AsyncMock(),
            workspace_channel=mock.AsyncMock(return_value=channel),
            create_session=mock.AsyncMock(
                side_effect=lambda *_args: events.append("create")
            ),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._ensure_bot_proxy = mock.Mock(return_value=manager)

        await nhmisc.NHMisc.botproxy_create.callback(cog, ctx)

        ctx.message.delete.assert_awaited_once()
        manager.create_session.assert_awaited_once_with(guild, author)
        self.assertEqual(events, ["create", "delete"])

    async def test_channel_set_requires_private_channel_and_clear_disables_it(self) -> None:
        config = _Config()
        guild = types.SimpleNamespace(id=10, me=object(), default_role=object())
        invocation_channel = types.SimpleNamespace(id=30)
        ctx = types.SimpleNamespace(
            guild=guild,
            channel=invocation_channel,
            send=mock.AsyncMock(),
        )
        permissions = types.SimpleNamespace(
            view_channel=True,
            send_messages=True,
            create_public_threads=True,
            send_messages_in_threads=True,
            manage_threads=True,
            manage_messages=True,
            manage_webhooks=True,
        )
        channel = types.SimpleNamespace(
            id=30,
            mention="<#30>",
            permissions_for=lambda _member: permissions,
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog.config = config
        cog._channel_allows_everyone = mock.Mock(return_value=False)

        await nhmisc.NHMisc.botproxy_channel.callback(cog, ctx, channel)
        self.assertEqual(config.values["bot_proxy_channel"], 30)

        await nhmisc.NHMisc.botproxy_channel.callback(cog, ctx, "clear")
        self.assertIsNone(config.values["bot_proxy_channel"])

    async def test_channel_configuration_never_reveals_workspace_publicly(self) -> None:
        config = _Config()
        guild = types.SimpleNamespace(id=10, me=object(), default_role=object())
        ctx = types.SimpleNamespace(
            guild=guild,
            channel=types.SimpleNamespace(id=99),
            send=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog.config = config
        cog._channel_allows_everyone = mock.Mock(return_value=True)

        with self.assertRaisesRegex(
            nhmisc.commands.UserFeedbackCheckFailure,
            "private moderator channel",
        ):
            await nhmisc.NHMisc.botproxy_channel.callback(cog, ctx, None)

        ctx.send.assert_not_awaited()

    async def test_recovery_deletes_thread_and_launcher_when_configured(self) -> None:
        record = types.SimpleNamespace(
            session_id="session",
            guild_id=10,
            launcher_channel_id=30,
            launcher_message_id=35,
            thread_id=40,
            dashboard_message_id=50,
        )
        thread = types.SimpleNamespace(delete=mock.AsyncMock())
        launcher = types.SimpleNamespace(delete=mock.AsyncMock())
        launcher_channel = types.SimpleNamespace(
            fetch_message=mock.AsyncMock(return_value=launcher)
        )
        store = types.SimpleNamespace(
            list_active_sessions=mock.AsyncMock(return_value=(record,)),
            remove_active_session=mock.AsyncMock(),
        )
        config = _Config()
        config.values["bot_proxy_delete_closed_sessions"] = True
        cog = object.__new__(nhmisc.NHMisc)
        cog._bot_proxy_store = store
        cog.config = config
        cog.bot = types.SimpleNamespace(
            get_channel=lambda channel_id: (
                thread if channel_id == 40 else launcher_channel
            ),
            fetch_channel=mock.AsyncMock(),
        )
        cog.report_operational_error = mock.AsyncMock()

        await nhmisc.NHMisc._recover_bot_proxy_sessions(cog)

        thread.delete.assert_awaited_once()
        launcher.delete.assert_awaited_once()
        store.remove_active_session.assert_awaited_once_with("session")
        cog.report_operational_error.assert_not_awaited()

    async def test_failed_startup_cleanup_keeps_session_for_next_recovery(self) -> None:
        record = types.SimpleNamespace(
            session_id="session",
            guild_id=10,
            launcher_channel_id=30,
            thread_id=40,
            dashboard_message_id=50,
        )
        dashboard = types.SimpleNamespace(
            edit=mock.AsyncMock(side_effect=RuntimeError("Discord unavailable"))
        )
        thread = types.SimpleNamespace(
            fetch_message=mock.AsyncMock(return_value=dashboard),
            edit=mock.AsyncMock(),
        )
        store = types.SimpleNamespace(
            list_active_sessions=mock.AsyncMock(return_value=(record,)),
            remove_active_session=mock.AsyncMock(),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog._bot_proxy_store = store
        cog.config = _Config()
        cog.bot = types.SimpleNamespace(
            get_channel=lambda _channel_id: thread,
            fetch_channel=mock.AsyncMock(),
        )
        cog.report_operational_error = mock.AsyncMock()

        await nhmisc.NHMisc._recover_bot_proxy_sessions(cog)

        store.remove_active_session.assert_not_awaited()
        cog.report_operational_error.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
