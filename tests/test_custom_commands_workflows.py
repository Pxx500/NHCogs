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

        def clear_items(self):
            self.children.clear()

        def stop(self):
            return None

    class Button:
        def __init__(self, *, label, style, row=None):
            self.label = label
            self.style = style
            self.row = row
            self.disabled = False
            self.callback = None

    class Select:
        def __init__(self, *, placeholder, options, row=None):
            self.placeholder = placeholder
            self.options = options
            self.row = row
            self.values = []
            self.disabled = False
            self.callback = None

    class SelectOption:
        def __init__(self, *, label, value, default=False):
            self.label = label
            self.value = value
            self.default = default

    class TextInput:
        def __init__(
            self,
            *,
            label,
            style=None,
            default=None,
            placeholder=None,
            required=True,
            max_length=None,
        ):
            self.label = label
            self.style = style
            self.default = default
            self.placeholder = placeholder
            self.required = required
            self.max_length = max_length
            self.value = default or ""

    class Modal:
        def __init__(self, *, title):
            self.title = title
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    class Embed:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.fields = []

        def add_field(self, **kwargs):
            self.fields.append(kwargs)

    class File:
        def __init__(self, fp, *, filename):
            self.fp = fp
            self.filename = filename

    discord.ui = types.SimpleNamespace(
        View=View,
        Button=Button,
        Select=Select,
        TextInput=TextInput,
        Modal=Modal,
    )
    discord.SelectOption = SelectOption
    discord.TextStyle = types.SimpleNamespace(paragraph=1, short=2)
    discord.ButtonStyle = types.SimpleNamespace(
        green=1,
        secondary=2,
        primary=3,
        danger=4,
    )
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.Interaction = object
    discord.Thread = object
    discord.Member = object
    discord.Message = object
    discord.Embed = Embed
    discord.File = File
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
    def test_weight_range_feedback_uses_concise_copy(self):
        draft = workflows.WorkflowDraft(
            "hello",
            responses=[catalog.ResponseDraft("response")],
        )

        with self.assertRaisesRegex(
            workflows.WorkflowInputError,
            "Weight must be between 1 and 1000",
        ):
            draft.set_weight(0, 0)

    def test_component_operations_share_one_draft_and_preserve_whitespace(self):
        draft = workflows.WorkflowDraft(
            "hello",
            responses=[catalog.ResponseDraft("first", 25, "first-id")],
        )

        added = draft.add_response("  second   response  \n")
        draft.set_weight(added, 250)
        draft.replace_response(added, "  replacement  \n")
        moved = draft.move_response(added, 0)
        removed = draft.remove_response(1)

        self.assertEqual(moved, 0)
        self.assertEqual(removed.response_id, "first-id")
        self.assertEqual(
            draft.responses,
            [catalog.ResponseDraft("  replacement  \n", 250)],
        )

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
    async def test_add_modal_preserves_content_and_refreshes_the_dashboard(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
        )
        manager.log_moderation_action = mock.AsyncMock()
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200, __str__=lambda self: "Moderator"),
            draft=workflows.WorkflowDraft("new"),
        )
        session.dashboard = SimpleNamespace(edit=mock.AsyncMock())
        open_interaction = SimpleNamespace(
            response=SimpleNamespace(send_modal=mock.AsyncMock())
        )
        add = next(item for item in session.view.children if item.label == "Add")

        await add.callback(open_interaction)

        modal = open_interaction.response.send_modal.await_args.args[0]
        modal.content.value = "  exact   response  \n"
        submit = SimpleNamespace(response=SimpleNamespace(defer=mock.AsyncMock()))
        await modal.on_submit(submit)

        self.assertEqual(
            session.draft.responses,
            [catalog.ResponseDraft("  exact   response  \n")],
        )
        self.assertEqual(session.selected_index, 0)
        session.dashboard.edit.assert_awaited_once()

    async def test_weight_move_and_confirmed_delete_use_the_selected_response(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
        )
        manager.log_moderation_action = mock.AsyncMock()
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200, __str__=lambda self: "Moderator"),
            draft=workflows.WorkflowDraft(
                "edit",
                responses=[
                    catalog.ResponseDraft("first"),
                    catalog.ResponseDraft("second"),
                    catalog.ResponseDraft("third"),
                ],
            ),
        )
        session.dashboard = SimpleNamespace(edit=mock.AsyncMock())
        session.select_response(0)
        session.view.refresh()

        weight = next(
            item
            for item in session.view.children
            if getattr(item, "label", None) == "Weight"
        )
        open_weight = SimpleNamespace(
            response=SimpleNamespace(send_modal=mock.AsyncMock())
        )
        await weight.callback(open_weight)
        weight_modal = open_weight.response.send_modal.await_args.args[0]
        weight_modal.value.value = "250"
        await weight_modal.on_submit(
            SimpleNamespace(response=SimpleNamespace(defer=mock.AsyncMock()))
        )
        self.assertEqual(session.draft.responses[0].weight, 250)

        move = next(
            item
            for item in session.view.children
            if getattr(item, "label", None) == "Move"
        )
        open_move = SimpleNamespace(response=SimpleNamespace(send_modal=mock.AsyncMock()))
        await move.callback(open_move)
        move_modal = open_move.response.send_modal.await_args.args[0]
        move_modal.value.value = "3"
        await move_modal.on_submit(
            SimpleNamespace(response=SimpleNamespace(defer=mock.AsyncMock()))
        )
        self.assertEqual(session.selected_index, 2)
        self.assertEqual(
            [response.content for response in session.draft.responses],
            ["second", "third", "first"],
        )

        delete_interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock())
        )
        delete = next(
            item
            for item in session.view.children
            if getattr(item, "label", None) == "Delete"
        )
        await delete.callback(delete_interaction)
        self.assertEqual(len(session.draft.responses), 3)
        self.assertNotIn(
            "Error",
            {field["name"] for field in session.render_embed().fields},
        )
        confirm = next(
            item
            for item in session.view.children
            if getattr(item, "label", None) == "Confirm delete"
        )
        await confirm.callback(delete_interaction)
        self.assertEqual(
            [response.content for response in session.draft.responses],
            ["second", "third"],
        )

    async def test_view_exact_sends_selected_whitespace_in_a_private_code_block(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
        )
        manager.log_moderation_action = mock.AsyncMock()
        content = "  left   right\ntrailing  "
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200, __str__=lambda self: "Moderator"),
            draft=workflows.WorkflowDraft(
                "edit",
                responses=[catalog.ResponseDraft(content)],
            ),
        )
        session.select_response(0)
        session.view.refresh()
        exact = next(
            item
            for item in session.view.children
            if getattr(item, "label", None) == "View exact"
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=mock.AsyncMock())
        )

        await exact.callback(interaction)

        sent = interaction.response.send_message.await_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(sent["embed"].kwargs["description"], f"```\n{content}\n```")

    async def test_view_exact_attaches_code_fence_content_without_rewriting_it(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
            log_moderation_action=mock.AsyncMock(),
        )
        content = "  before\n```py\nprint('exact')\n```\nafter  "
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200, __str__=lambda self: "Moderator"),
            draft=workflows.WorkflowDraft(
                "edit",
                responses=[catalog.ResponseDraft(content)],
            ),
        )
        session.select_response(0)
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=mock.AsyncMock())
        )

        await session.send_exact_response(interaction)

        sent = interaction.response.send_message.await_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(sent["file"].filename, "edit-response-1.txt")
        self.assertEqual(sent["file"].fp.getvalue(), content.encode("utf-8"))

    async def test_view_exposes_paged_response_controls_and_selection(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
        )
        manager.log_moderation_action = mock.AsyncMock()
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200, __str__=lambda self: "Moderator"),
            draft=workflows.WorkflowDraft(
                "large",
                responses=[
                    catalog.ResponseDraft(f"response {index}")
                    for index in range(7)
                ],
            ),
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock())
        )

        select = session.view.children[0]
        buttons = {item.label: item for item in session.view.children[1:]}
        self.assertEqual([option.label for option in select.options], ["#1", "#2", "#3", "#4", "#5"])
        self.assertEqual({item.row for item in session.view.children}, {0, 1, 2})
        self.assertEqual(
            set(buttons),
            {
                "Previous",
                "Next",
                "Add",
                "Edit",
                "Delete",
                "Weight",
                "Move",
                "View exact",
                "Save",
                "Cancel",
            },
        )
        for label in ("Edit", "Delete", "Weight", "Move", "View exact"):
            self.assertTrue(buttons[label].disabled)

        select.values = ["1"]
        await select.callback(interaction)
        self.assertEqual(session.selected_index, 1)

        await buttons["Next"].callback(interaction)
        self.assertEqual(session.page, 1)
        self.assertIsNone(session.selected_index)
        refreshed_select = session.view.children[0]
        self.assertEqual(
            [option.label for option in refreshed_select.options],
            ["#6", "#7"],
        )

    async def test_page_navigation_clears_pending_delete_confirmation(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
            log_moderation_action=mock.AsyncMock(),
        )
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200, __str__=lambda self: "Moderator"),
            draft=workflows.WorkflowDraft(
                "large",
                responses=[
                    catalog.ResponseDraft(f"response {index}") for index in range(6)
                ],
            ),
        )
        session.dashboard = SimpleNamespace(edit=mock.AsyncMock())
        session.select_response(0)
        session.view.refresh()
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=mock.AsyncMock())
        )

        delete = next(
            item
            for item in session.view.children
            if getattr(item, "label", None) == "Delete"
        )
        await delete.callback(interaction)
        next_button = next(
            item
            for item in session.view.children
            if getattr(item, "label", None) == "Next"
        )
        await next_button.callback(interaction)

        self.assertIsNone(session.delete_confirmation_index)
        self.assertIsNone(session.validation_error)
        self.assertNotIn("Error", {field["name"] for field in session.render_embed().fields})
    def test_dashboard_renders_only_the_current_five_response_page(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
        )
        manager.log_moderation_action = mock.AsyncMock()
        responses = [
            catalog.ResponseDraft(
                f"response {index}\nsecond   line",
                100,
            )
            for index in range(12)
        ]
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200, __str__=lambda self: "Moderator"),
            draft=workflows.WorkflowDraft("large", responses=responses),
        )
        session.set_page(1)

        embed = session.render_embed()

        self.assertEqual(embed.kwargs["title"], "large")
        self.assertEqual(embed.kwargs["description"], "Editing")
        response_field = next(
            field for field in embed.fields if field["name"].startswith("Responses")
        )
        self.assertEqual(response_field["name"], "Responses 6-10 of 12")
        self.assertIn("#6 · weight 100 · 8.3%", response_field["value"])
        self.assertIn("#10 · weight 100 · 8.3%", response_field["value"])
        self.assertIn("response 5 ↵ second   line", response_field["value"])
        self.assertNotIn("response 4 ", response_field["value"])
        self.assertNotIn("response 10 ", response_field["value"])
        self.assertEqual(
            {field["name"] for field in embed.fields},
            {"Responses 6-10 of 12"},
        )

    def test_dashboard_uses_a_singular_response_heading(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
            log_moderation_action=mock.AsyncMock(),
        )
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200),
            draft=workflows.WorkflowDraft(
                "single",
                responses=[catalog.ResponseDraft("only response")],
            ),
        )

        embed = session.render_embed()

        self.assertEqual(embed.fields[0]["name"], "Response")

    def test_dashboard_keeps_configured_cooldowns_and_arguments(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
            log_moderation_action=mock.AsyncMock(),
        )
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200),
            draft=workflows.WorkflowDraft(
                "configured",
                responses=[catalog.ResponseDraft("hello {1}")],
                cooldowns={"member": 10},
            ),
        )

        embed = session.render_embed()
        fields = {field["name"]: field["value"] for field in embed.fields}

        self.assertEqual(fields["Cooldowns"], "member: 10s")
        self.assertEqual(fields["Arguments"], "argument 1: text")

    def test_page_and_selection_follow_add_move_and_delete(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
        )
        manager.log_moderation_action = mock.AsyncMock()
        session = workflows.WorkflowSession(
            manager,
            thread=SimpleNamespace(id=10),
            opener=SimpleNamespace(id=200, __str__=lambda self: "Moderator"),
            draft=workflows.WorkflowDraft(
                "large",
                responses=[
                    catalog.ResponseDraft(f"response {index}")
                    for index in range(12)
                ],
            ),
        )

        self.assertEqual(session.visible_response_indices, tuple(range(5)))
        session.set_page(1)
        session.select_response(7)
        self.assertEqual(session.visible_response_indices, tuple(range(5, 10)))

        session.move_selected(11)
        self.assertEqual(session.selected_index, 11)
        self.assertEqual(session.page, 2)
        session.remove_selected()
        self.assertEqual(session.selected_index, 10)
        self.assertEqual(session.page, 2)
        added = session.add_response("  exact   spacing  ")
        self.assertEqual(added, 11)
        self.assertEqual(session.selected_index, 11)
        self.assertEqual(session.page, 2)
        self.assertEqual(session.draft.responses[11].content, "  exact   spacing  ")

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
        self.assertEqual(session.selected_index, 0)
        self.assertEqual(session.page, 0)

    async def test_ordinary_message_addition_selects_the_new_response_page(self):
        manager = SimpleNamespace(
            catalog=SimpleNamespace(),
            session_timeout_seconds=workflows.SESSION_TIMEOUT_SECONDS,
            remove=mock.Mock(),
            logger=mock.Mock(),
            log_moderation_action=mock.AsyncMock(),
        )
        thread = SimpleNamespace(
            id=10,
            guild=SimpleNamespace(id=100),
            mention="#thread",
            send=mock.AsyncMock(),
            edit=mock.AsyncMock(),
        )
        session = workflows.WorkflowSession(
            manager,
            thread=thread,
            opener=SimpleNamespace(id=200),
            draft=workflows.WorkflowDraft(
                "hello",
                responses=[
                    catalog.ResponseDraft(f"response {index}") for index in range(5)
                ],
            ),
        )

        await session.handle_message(
            SimpleNamespace(author=SimpleNamespace(id=200), content="sixth response")
        )

        self.assertEqual(session.selected_index, 5)
        self.assertEqual(session.page, 1)
        self.assertEqual(session.draft.responses[5].content, "sixth response")

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

    async def test_finish_removes_controls_from_the_terminal_dashboard(self):
        manager = SimpleNamespace(
            session_timeout_seconds=60,
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
            draft=workflows.WorkflowDraft(
                "hello",
                responses=[catalog.ResponseDraft("response")],
            ),
        )
        session.dashboard = SimpleNamespace(id=300, edit=mock.AsyncMock())

        await session.finish("Cancelled")

        edited = session.dashboard.edit.await_args.kwargs
        self.assertEqual(edited["embed"].kwargs["title"], "hello")
        self.assertEqual(edited["embed"].kwargs["description"], "Cancelled")
        self.assertIsNone(edited["view"])

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
