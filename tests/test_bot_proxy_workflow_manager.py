from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PACKAGE_NAME = "nhmisc_bot_proxy_manager_test_package"
PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "nhmisc"


def _fake_discord() -> types.ModuleType:
    discord = types.ModuleType("discord")

    class View:
        def __init__(self, *, timeout=None):
            self.timeout = timeout
            self.children = []

        def add_item(self, item):
            self.children.append(item)

        def stop(self):
            return None

    class Button:
        def __init__(self, *, label, style, row=None, disabled=False):
            self.label = label
            self.style = style
            self.row = row
            self.disabled = disabled
            self.callback = None

    class Select:
        def __init__(self, *, placeholder, options, min_values=1, max_values=1, row=None):
            self.placeholder = placeholder
            self.options = options
            self.min_values = min_values
            self.max_values = max_values
            self.row = row
            self.callback = None

    class Modal:
        def __init__(self, *, title):
            self.title = title
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class TextInput:
        def __init__(self, **kwargs):
            self.value = kwargs.get("default") or ""

    class SelectOption:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Embed:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class AllowedMentions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @classmethod
        def none(cls):
            return cls()

    class TextChannel:
        pass

    class Thread:
        pass

    discord.ui = SimpleNamespace(
        View=View,
        Button=Button,
        Select=Select,
        Modal=Modal,
        TextInput=TextInput,
    )
    discord.SelectOption = SelectOption
    discord.Embed = Embed
    discord.AllowedMentions = AllowedMentions
    discord.ButtonStyle = SimpleNamespace(secondary=1, primary=2, danger=3)
    discord.TextStyle = SimpleNamespace(paragraph=1)
    discord.Color = SimpleNamespace(blue=lambda: 1)
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.TextChannel = TextChannel
    discord.Thread = Thread
    discord.ForumChannel = type("ForumChannel", (), {})
    discord.MediaChannel = type("MediaChannel", (), {})
    discord.Guild = object
    discord.Member = object
    discord.Message = object
    discord.Interaction = object
    return discord


def _load_subject():
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules[PACKAGE_NAME] = package
    discord = _fake_discord()
    previous = sys.modules.get("discord")
    sys.modules["discord"] = discord
    try:
        for name in (
            "bot_proxy_store",
            "bot_proxy",
            "bot_proxy_workflow",
            "bot_proxy_manager",
        ):
            qualified_name = f"{PACKAGE_NAME}.{name}"
            spec = importlib.util.spec_from_file_location(
                qualified_name,
                PACKAGE_PATH / f"{name}.py",
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Unable to load {name}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[qualified_name] = module
            spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = previous
    return (
        discord,
        sys.modules[f"{PACKAGE_NAME}.bot_proxy_workflow"],
        sys.modules[f"{PACKAGE_NAME}.bot_proxy_manager"],
    )


discord, workflow, manager_module = _load_subject()


class _Workspace(discord.TextChannel):
    def __init__(self, launcher):
        self.id = 30
        self.mention = "<#30>"
        self._launcher = launcher
        self.send = mock.AsyncMock(return_value=launcher)

    def permissions_for(self, subject):
        if subject == "everyone":
            return SimpleNamespace(view_channel=False)
        return SimpleNamespace(
            view_channel=True,
            send_messages=True,
            create_public_threads=True,
            send_messages_in_threads=True,
            manage_threads=True,
        )


class BotProxyWorkflowManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_session_always_uses_configured_private_workspace(self) -> None:
        dashboard = SimpleNamespace(id=60, edit=mock.AsyncMock())
        thread = SimpleNamespace(
            id=50,
            mention="<#50>",
            send=mock.AsyncMock(return_value=dashboard),
            edit=mock.AsyncMock(),
        )
        launcher = SimpleNamespace(
            id=40,
            create_thread=mock.AsyncMock(return_value=thread),
        )
        workspace = _Workspace(launcher)
        guild = SimpleNamespace(
            id=10,
            default_role="everyone",
            me="bot",
            get_channel=lambda channel_id: workspace if channel_id == 30 else None,
        )
        moderator = SimpleNamespace(id=20, mention="<@20>", display_name="Mod")
        guild_config = SimpleNamespace(
            bot_proxy_channel=mock.AsyncMock(return_value=30)
        )
        config = SimpleNamespace(guild=lambda _guild: guild_config)
        store = SimpleNamespace(
            record_active_session=mock.AsyncMock(),
            remove_active_session=mock.AsyncMock(),
        )
        manager = manager_module.BotProxyWorkflowManager(
            config=config,
            store=store,
            moderation_log=mock.AsyncMock(),
            error_reporter=mock.AsyncMock(),
        )

        session = await manager.create_session(guild, moderator)

        workspace.send.assert_awaited_once()
        launcher.create_thread.assert_awaited_once()
        self.assertEqual(
            launcher.create_thread.await_args.kwargs["name"],
            "bot-proxy-Mod-1",
        )
        store.record_active_session.assert_awaited_once()
        self.assertEqual(session.thread, thread)
        self.assertEqual(manager.registry.sessions_for(10, 20), (session.active,))

        await session.finish(workflow.SessionStatus.CANCELLED)
        store.remove_active_session.assert_awaited_once()

    async def test_public_workspace_is_rejected_before_creating_launcher(self) -> None:
        workspace = _Workspace(SimpleNamespace())
        workspace.permissions_for = lambda _subject: SimpleNamespace(
            view_channel=True
        )
        guild = SimpleNamespace(
            id=10,
            default_role="everyone",
            me="bot",
            get_channel=lambda _channel_id: workspace,
        )
        config = SimpleNamespace(
            guild=lambda _guild: SimpleNamespace(
                bot_proxy_channel=mock.AsyncMock(return_value=30)
            )
        )
        manager = manager_module.BotProxyWorkflowManager(
            config=config,
            store=SimpleNamespace(),
            moderation_log=mock.AsyncMock(),
            error_reporter=mock.AsyncMock(),
        )

        with self.assertRaisesRegex(
            workflow.WorkflowInputError,
            "visible to @everyone",
        ):
            await manager.workspace_channel(guild)

        workspace.send.assert_not_awaited()

    async def test_tracked_message_opens_lifecycle_actions_instead_of_session(self) -> None:
        record = SimpleNamespace(deleted_at=None)
        store = SimpleNamespace(
            get_message=mock.AsyncMock(return_value=record),
        )
        manager = manager_module.BotProxyWorkflowManager(
            config=SimpleNamespace(),
            store=store,
            moderation_log=mock.AsyncMock(),
            error_reporter=mock.AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            user=SimpleNamespace(id=20),
            response=SimpleNamespace(defer=mock.AsyncMock()),
            edit_original_response=mock.AsyncMock(),
        )
        source = SimpleNamespace(id=40, channel=SimpleNamespace(id=30))
        manager.create_session = mock.AsyncMock()

        await manager.route_message(interaction, source)

        manager.create_session.assert_not_awaited()
        kwargs = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(kwargs["content"], "Choose a Bot Proxy action")
        self.assertIsInstance(kwargs["view"], workflow.TrackedMessageActionsView)

    async def test_timeout_does_not_cancel_its_own_terminal_cleanup(self) -> None:
        active = manager_module.ActiveSession("session", 10, 20, 50)
        store = SimpleNamespace(remove_active_session=mock.AsyncMock())
        manager = manager_module.BotProxyWorkflowManager(
            config=SimpleNamespace(),
            store=store,
            moderation_log=mock.AsyncMock(return_value=True),
            error_reporter=mock.AsyncMock(),
        )
        manager.registry.add(active)
        dashboard = SimpleNamespace(id=60, edit=mock.AsyncMock())
        thread = SimpleNamespace(id=50, edit=mock.AsyncMock())
        session = workflow.BotProxyWorkflowSession(
            manager,
            active=active,
            guild=SimpleNamespace(id=10),
            moderator=SimpleNamespace(id=20),
            launcher=SimpleNamespace(),
            thread=thread,
            dashboard=dashboard,
            draft=manager_module.BotProxyDraft(),
        )
        manager.sessions[active.session_id] = session

        async def expire_now() -> None:
            session._timeout_task = asyncio.current_task()
            await session.finish(workflow.SessionStatus.TIMED_OUT)

        await asyncio.create_task(expire_now())

        dashboard.edit.assert_awaited_once()
        thread.edit.assert_awaited_once_with(archived=True, locked=True)
        store.remove_active_session.assert_awaited_once_with("session")

    async def test_concurrent_send_clicks_publish_only_once(self) -> None:
        active = manager_module.ActiveSession("session", 10, 20, 50)
        manager = manager_module.BotProxyWorkflowManager(
            config=SimpleNamespace(),
            store=SimpleNamespace(remove_active_session=mock.AsyncMock()),
            moderation_log=mock.AsyncMock(return_value=True),
            error_reporter=mock.AsyncMock(),
        )
        manager.registry.add(active)
        release = asyncio.Event()

        async def publish_once(**_kwargs):
            await release.wait()
            return SimpleNamespace(id=70, jump_url="https://example.invalid/message")

        manager.publisher.publish = mock.AsyncMock(side_effect=publish_once)
        manager.resolve_publish_channel = mock.AsyncMock(
            return_value=SimpleNamespace()
        )
        manager.log_publication = mock.AsyncMock()
        dashboard = SimpleNamespace(id=60, edit=mock.AsyncMock())
        thread = SimpleNamespace(id=50, edit=mock.AsyncMock())
        draft = manager_module.BotProxyDraft(
            destination=manager_module.ProxyDestination(10, 30),
            content="Hello",
        )
        session = workflow.BotProxyWorkflowSession(
            manager,
            active=active,
            guild=SimpleNamespace(id=10),
            moderator=SimpleNamespace(id=20),
            launcher=SimpleNamespace(),
            thread=thread,
            dashboard=dashboard,
            draft=draft,
        )
        manager.sessions[active.session_id] = session
        user = SimpleNamespace(id=20)
        first = SimpleNamespace(
            user=user,
            response=SimpleNamespace(is_done=lambda: True),
            edit_original_response=mock.AsyncMock(),
        )
        second = SimpleNamespace(
            user=user,
            response=SimpleNamespace(is_done=lambda: True),
            edit_original_response=mock.AsyncMock(),
        )

        first_task = asyncio.create_task(session.publish(first))
        await asyncio.sleep(0)
        second_task = asyncio.create_task(session.publish(second))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first_task, second_task)

        self.assertEqual(manager.publisher.publish.await_count, 1)

    async def test_cancel_waits_for_committed_publication_and_keeps_sent_status(
        self,
    ) -> None:
        active = manager_module.ActiveSession("session", 10, 20, 50)
        manager = manager_module.BotProxyWorkflowManager(
            config=SimpleNamespace(),
            store=SimpleNamespace(remove_active_session=mock.AsyncMock()),
            moderation_log=mock.AsyncMock(return_value=True),
            error_reporter=mock.AsyncMock(),
        )
        manager.registry.add(active)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def publish_once(**_kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(id=70, jump_url="https://example.invalid/message")

        manager.publisher.publish = mock.AsyncMock(side_effect=publish_once)
        manager.resolve_publish_channel = mock.AsyncMock(return_value=SimpleNamespace())
        manager.log_publication = mock.AsyncMock()
        session = workflow.BotProxyWorkflowSession(
            manager,
            active=active,
            guild=SimpleNamespace(id=10),
            moderator=SimpleNamespace(id=20),
            launcher=SimpleNamespace(),
            thread=SimpleNamespace(id=50, edit=mock.AsyncMock()),
            dashboard=SimpleNamespace(id=60, edit=mock.AsyncMock()),
            draft=manager_module.BotProxyDraft(
                destination=manager_module.ProxyDestination(10, 30),
                content="Hello",
            ),
        )
        manager.sessions[active.session_id] = session
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=20),
            response=SimpleNamespace(is_done=lambda: True),
            edit_original_response=mock.AsyncMock(),
        )

        publish_task = asyncio.create_task(session.publish(interaction))
        await entered.wait()
        cancel_task = asyncio.create_task(
            session.finish(workflow.SessionStatus.CANCELLED)
        )
        await asyncio.sleep(0)
        cancelled_before_publication = cancel_task.done()
        release.set()
        await asyncio.gather(publish_task, cancel_task)

        self.assertFalse(cancelled_before_publication)
        self.assertEqual(session._terminal.status, workflow.SessionStatus.SENT)

    def test_send_button_tracks_draft_validity(self) -> None:
        session = SimpleNamespace(
            opener_id=20,
            draft=manager_module.BotProxyDraft(),
        )
        empty_view = workflow.DashboardView(session)
        empty_send = next(item for item in empty_view.children if item.label == "Send")
        self.assertTrue(empty_send.disabled)

        session.draft = manager_module.BotProxyDraft(
            destination=manager_module.ProxyDestination(10, 30),
            content="Ready",
        )
        ready_view = workflow.DashboardView(session)
        ready_send = next(item for item in ready_view.children if item.label == "Send")
        self.assertFalse(ready_send.disabled)

    async def test_session_controls_recheck_manage_messages(self) -> None:
        session = SimpleNamespace(
            opener_id=20,
            draft=manager_module.BotProxyDraft(),
            touch=mock.Mock(),
        )
        view = workflow.DashboardView(session)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=20),
            permissions=SimpleNamespace(manage_messages=False),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )

        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once_with(
            "You need Manage Messages permission",
            ephemeral=True,
        )
        session.touch.assert_not_called()

    async def test_invalid_avatar_attachment_is_private_feedback_not_operational_error(
        self,
    ) -> None:
        active = manager_module.ActiveSession("session", 10, 20, 50)
        reporter = mock.AsyncMock()
        manager = manager_module.BotProxyWorkflowManager(
            config=SimpleNamespace(),
            store=SimpleNamespace(),
            moderation_log=mock.AsyncMock(return_value=True),
            error_reporter=reporter,
        )
        manager.load_avatar_attachment = mock.AsyncMock(
            side_effect=ValueError("unsupported avatar media type")
        )
        thread = SimpleNamespace(
            id=50,
            send=mock.AsyncMock(),
            permissions_for=lambda _member: SimpleNamespace(manage_messages=True),
        )
        session = workflow.BotProxyWorkflowSession(
            manager,
            active=active,
            guild=SimpleNamespace(id=10),
            moderator=SimpleNamespace(id=20),
            launcher=SimpleNamespace(),
            thread=thread,
            dashboard=SimpleNamespace(id=60, edit=mock.AsyncMock()),
            draft=manager_module.BotProxyDraft(
                identity=workflow.ProxyIdentity(
                    manager_module.IdentityType.CHARACTER,
                    display_name="Guide",
                )
            ),
        )
        session.input_mode = workflow.InputMode.AVATAR
        message = SimpleNamespace(
            author=SimpleNamespace(id=20),
            channel=thread,
            attachments=(object(),),
        )

        self.assertTrue(await session.handle_message(message))

        thread.send.assert_awaited_once()
        self.assertIn("unsupported avatar", thread.send.await_args.args[0])
        reporter.assert_not_awaited()

    async def test_undelivered_moderation_log_reports_operational_error(self) -> None:
        reporter = mock.AsyncMock()
        moderation_log = mock.AsyncMock(return_value=False)
        manager = manager_module.BotProxyWorkflowManager(
            config=SimpleNamespace(),
            store=SimpleNamespace(),
            moderation_log=moderation_log,
            error_reporter=reporter,
        )
        session = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            thread=SimpleNamespace(id=50),
            dashboard=SimpleNamespace(id=60),
            draft=manager_module.BotProxyDraft(
                destination=manager_module.ProxyDestination(10, 30),
                content="Hello",
                identity=workflow.ProxyIdentity(
                    manager_module.IdentityType.CHARACTER,
                    display_name="Guide",
                    preset_name="guide",
                    avatar_sha256="abc123",
                ),
            ),
        )
        moderator = SimpleNamespace(id=20, mention="<@20>")
        message = SimpleNamespace(
            id=70,
            jump_url="https://discord.com/channels/10/30/70",
        )

        await manager.log_publication(session, moderator, message)

        metadata = moderation_log.await_args.args[1]
        self.assertIn("Sender: character", metadata)
        self.assertIn("Character preset: guide", metadata)
        self.assertIn("Avatar digest: abc123", metadata)
        reporter.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
