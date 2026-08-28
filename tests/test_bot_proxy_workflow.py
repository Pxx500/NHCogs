from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = Path(__file__).parents[1] / "NHCogs" / "nhmisc" / "bot_proxy.py"
SPEC = importlib.util.spec_from_file_location("nhmisc_bot_proxy_workflow_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Bot Proxy workflow")
bot_proxy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bot_proxy
SPEC.loader.exec_module(bot_proxy)

ActiveSession = bot_proxy.ActiveSession
BotProxyDraft = bot_proxy.BotProxyDraft
BotProxySession = bot_proxy.BotProxySession
IdentityType = bot_proxy.IdentityType
ProxyDestination = bot_proxy.ProxyDestination
ProxyIdentity = bot_proxy.ProxyIdentity
SessionRegistry = bot_proxy.SessionRegistry
SessionStatus = bot_proxy.SessionStatus


class BotProxyDraftTests(unittest.TestCase):
    def test_character_identity_cannot_send_a_native_reply(self) -> None:
        draft = BotProxyDraft(
            destination=ProxyDestination(guild_id=10, channel_id=20, message_id=30),
            content="Reply content",
            identity=ProxyIdentity(IdentityType.CHARACTER, display_name="Narrator"),
        )

        self.assertEqual(
            draft.validation_errors(),
            ("Characters cannot reply to an existing message",),
        )

    def test_complete_bot_reply_is_sendable(self) -> None:
        draft = BotProxyDraft(
            destination=ProxyDestination(guild_id=10, channel_id=20, message_id=30),
            content="Reply content",
        )

        self.assertEqual(draft.validation_errors(), ())


class BotProxySessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_finish_is_idempotent_and_closes_durable_and_discord_state(self) -> None:
        registry = SessionRegistry()
        active = ActiveSession("session", 10, 20, 30)
        registry.add(active)
        store = SimpleNamespace(remove_active_session=mock.AsyncMock())
        thread = SimpleNamespace(edit=mock.AsyncMock())
        dashboard = SimpleNamespace(edit=mock.AsyncMock())
        session = BotProxySession(
            active=active,
            registry=registry,
            store=store,
            thread=thread,
            dashboard=dashboard,
        )

        await session.finish(SessionStatus.CANCELLED)
        await session.finish(SessionStatus.TIMED_OUT)

        self.assertEqual(session.status, SessionStatus.CANCELLED)
        self.assertEqual(registry.sessions_for(10, 20), ())
        store.remove_active_session.assert_awaited_once_with("session")
        dashboard.edit.assert_awaited_once_with(
            content="Bot Proxy session: Cancelled",
            view=None,
        )
        thread.edit.assert_awaited_once_with(archived=True, locked=True)

    async def test_failed_discord_cleanup_keeps_durable_recovery_record(self) -> None:
        registry = SessionRegistry()
        active = ActiveSession("session", 10, 20, 30)
        registry.add(active)
        store = SimpleNamespace(remove_active_session=mock.AsyncMock())
        thread = SimpleNamespace(edit=mock.AsyncMock())
        dashboard = SimpleNamespace(
            edit=mock.AsyncMock(side_effect=RuntimeError("Discord unavailable"))
        )
        session = BotProxySession(
            active=active,
            registry=registry,
            store=store,
            thread=thread,
            dashboard=dashboard,
        )

        with self.assertRaisesRegex(RuntimeError, "Discord unavailable"):
            await session.finish(SessionStatus.TIMED_OUT)

        store.remove_active_session.assert_not_awaited()
        thread.edit.assert_awaited_once_with(archived=True, locked=True)

    async def test_delete_mode_removes_launcher_instead_of_archiving_thread(self) -> None:
        registry = SessionRegistry()
        active = ActiveSession("session", 10, 20, 30)
        registry.add(active)
        store = SimpleNamespace(remove_active_session=mock.AsyncMock())
        thread = SimpleNamespace(edit=mock.AsyncMock(), delete=mock.AsyncMock())
        dashboard = SimpleNamespace(edit=mock.AsyncMock())
        launcher = SimpleNamespace(delete=mock.AsyncMock())
        session = BotProxySession(
            active=active,
            registry=registry,
            store=store,
            thread=thread,
            dashboard=dashboard,
        )

        await session.finish(
            SessionStatus.CANCELLED,
            launcher=launcher,
            delete=True,
        )

        launcher.delete.assert_awaited_once()
        thread.delete.assert_awaited_once()
        thread.edit.assert_not_awaited()
        dashboard.edit.assert_not_awaited()
        store.remove_active_session.assert_awaited_once_with("session")


if __name__ == "__main__":
    unittest.main()
