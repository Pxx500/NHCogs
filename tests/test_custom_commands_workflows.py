import asyncio
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "custom_commands"


def load_workflow_modules():
    package_name = "custom_commands_workflow_subject"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules[package_name] = package
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
        def __init__(self, *, label, style):
            self.label = label
            self.style = style
            self.disabled = False
            self.callback = None

    class Embed:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.fields = []

        def add_field(self, **kwargs):
            self.fields.append(kwargs)

    discord.ui = types.SimpleNamespace(View=View, Button=Button)
    discord.ButtonStyle = types.SimpleNamespace(green=1, secondary=2)
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.Interaction = object
    discord.Thread = object
    discord.Member = object
    discord.Message = object
    discord.Embed = Embed
    commands = types.ModuleType("redbot.core.commands")
    commands.Parameter = inspect.Parameter
    core = types.ModuleType("redbot.core")
    core.commands = commands
    temporary = {
        "discord": discord,
        "redbot": types.ModuleType("redbot"),
        "redbot.core": core,
        "redbot.core.commands": commands,
    }
    previous = {name: sys.modules.get(name) for name in temporary}
    sys.modules.update(temporary)
    try:
        for module_name in ("migration_state", "arguments", "catalog", "workflows"):
            qualified_name = f"{package_name}.{module_name}"
            spec = importlib.util.spec_from_file_location(
                qualified_name,
                PACKAGE_PATH / f"{module_name}.py",
            )
            assert spec is not None
            assert spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[qualified_name] = module
            spec.loader.exec_module(module)
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module
    return (
        sys.modules[f"{package_name}.catalog"],
        sys.modules[f"{package_name}.workflows"],
    )


catalog, workflows = load_workflow_modules()


class WorkflowDraftTests(unittest.TestCase):
    def test_messages_and_controls_update_one_ordered_draft(self):
        draft = workflows.WorkflowDraft("hello")

        self.assertEqual(draft.process_message("first response"), "added")
        self.assertEqual(draft.process_message("second response"), "added")
        self.assertEqual(draft.process_message("weight 2 250"), "weight updated")
        self.assertEqual(draft.process_message("replace 1"), "replacement requested")
        self.assertEqual(draft.process_message("replacement"), "response replaced")
        self.assertEqual(draft.process_message("remove 2"), "response removed")

        self.assertEqual(
            draft.responses,
            [catalog.ResponseDraft("replacement", 100)],
        )

    def test_move_reorders_responses_without_changing_identity_or_weight(self):
        first = catalog.ResponseDraft("first", 25, "first-id")
        second = catalog.ResponseDraft("second", 75, "second-id")
        third = catalog.ResponseDraft("third", 100, "third-id")
        draft = workflows.WorkflowDraft(
            "hello",
            responses=[first, second, third],
        )

        self.assertEqual(draft.process_message("move 3 1"), "response moved")

        self.assertEqual(draft.responses, [third, first, second])


class WorkflowSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_opener_messages_change_session_state(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
        )
        manager.log_moderation_action = mock.AsyncMock()
        thread = SimpleNamespace(
            id=10,
            guild=SimpleNamespace(id=100),
            mention="#thread",
            send=mock.AsyncMock(),
            edit=mock.AsyncMock(),
        )
        opener = SimpleNamespace(id=200, __str__=lambda self: "Moderator")
        session = workflows.WorkflowSession(
            manager,
            thread=thread,
            opener=opener,
            draft=workflows.WorkflowDraft("hello"),
        )

        await session.handle_message(
            SimpleNamespace(author=SimpleNamespace(id=300), content="ignored")
        )
        await session.handle_message(
            SimpleNamespace(author=SimpleNamespace(id=200), content="accepted")
        )

        self.assertEqual(
            session.draft.responses,
            [catalog.ResponseDraft("accepted")],
        )

    async def test_save_uses_one_catalog_operation_and_closes_thread(self):
        stored = SimpleNamespace()
        catalog_store = SimpleNamespace(create=mock.AsyncMock(return_value=stored))
        manager = SimpleNamespace(
            catalog=catalog_store,
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
            log_moderation_action=mock.AsyncMock(),
        )
        thread = SimpleNamespace(
            id=10,
            guild=SimpleNamespace(id=100),
            mention="#thread",
            edit=mock.AsyncMock(),
        )
        opener = SimpleNamespace(id=200)
        opener.__str__ = mock.Mock(return_value="Moderator")
        session = workflows.WorkflowSession(
            manager,
            thread=thread,
            opener=opener,
            draft=workflows.WorkflowDraft(
                "hello",
                responses=[catalog.ResponseDraft("response")],
            ),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()),
        )

        await session.save(interaction)

        catalog_store.create.assert_awaited_once()
        manager.log_moderation_action.assert_awaited_once()
        manager.remove.assert_called_once_with(10)
        thread.edit.assert_awaited_once_with(archived=True, locked=True)
        self.assertTrue(session.finished)

    async def test_dashboard_send_failure_archives_thread_without_registering_session(self):
        reporter = SimpleNamespace(report=mock.AsyncMock())
        manager = workflows.WorkflowManager(
            SimpleNamespace(),
            SimpleNamespace(operational_errors=reporter),
            logger=mock.Mock(),
        )
        thread = SimpleNamespace(
            id=10,
            guild=SimpleNamespace(id=100),
            send=mock.AsyncMock(side_effect=RuntimeError("send failed")),
            edit=mock.AsyncMock(),
        )
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=200),
            message=SimpleNamespace(
                create_thread=mock.AsyncMock(return_value=thread),
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await manager.open(ctx, workflows.WorkflowDraft("hello"))

        self.assertEqual(manager._sessions, {})
        thread.edit.assert_awaited_once_with(archived=True, locked=True)

    async def test_activity_replaces_the_pending_inactivity_timeout(self):
        manager = SimpleNamespace(
            session_timeout_seconds=60,
            remove=mock.Mock(),
            _report_failure=mock.AsyncMock(),
        )
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(
                id=10,
                guild=SimpleNamespace(id=100),
                edit=mock.AsyncMock(),
            ),
            opener=SimpleNamespace(id=200),
            draft=workflows.WorkflowDraft("hello"),
        )

        session.touch()
        first_timeout = session._timeout_task
        session.touch()
        second_timeout = session._timeout_task
        await asyncio.sleep(0)

        self.assertIsNot(first_timeout, second_timeout)
        self.assertTrue(first_timeout.cancelled())
        await session.finish("Cancelled")
        await asyncio.sleep(0)
        self.assertTrue(second_timeout.done())

    async def test_zero_inactivity_timeout_finishes_and_archives_session(self):
        manager = SimpleNamespace(
            session_timeout_seconds=0,
            remove=mock.Mock(),
            _report_failure=mock.AsyncMock(),
        )
        thread = SimpleNamespace(
            id=10,
            guild=SimpleNamespace(id=100),
            edit=mock.AsyncMock(),
        )
        session = workflows.WorkflowSession(
            manager,
            thread=thread,
            opener=SimpleNamespace(id=200),
            draft=workflows.WorkflowDraft("hello"),
        )

        session.touch()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertTrue(session.finished)
        self.assertEqual(session.status, "Timed out")
        thread.edit.assert_awaited_once_with(archived=True, locked=True)

    async def test_dashboard_edit_failure_is_reported_without_blocking_cleanup(self):
        manager = SimpleNamespace(
            session_timeout_seconds=0,
            remove=mock.Mock(),
            _report_failure=mock.AsyncMock(),
        )
        thread = SimpleNamespace(
            id=10,
            guild=SimpleNamespace(id=100),
            edit=mock.AsyncMock(),
        )
        session = workflows.WorkflowSession(
            manager,
            thread=thread,
            opener=SimpleNamespace(id=200),
            draft=workflows.WorkflowDraft("hello"),
        )
        failure = RuntimeError("dashboard edit failed")
        session.dashboard = SimpleNamespace(
            id=300,
            edit=mock.AsyncMock(side_effect=failure),
        )

        session.touch()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertTrue(session.finished)
        manager.remove.assert_called_once_with(10)
        thread.edit.assert_awaited_once_with(archived=True, locked=True)
        manager._report_failure.assert_awaited_once_with(
            guild_id=100,
            action="update completed custom command workflow",
            error=failure,
            thread_id=10,
            message_id=300,
        )


if __name__ == "__main__":
    unittest.main()
