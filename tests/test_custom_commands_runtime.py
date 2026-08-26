import asyncio
import importlib.util
import inspect
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "custom_commands"


def load_runtime_modules():
    package_name = "custom_commands_runtime_subject"
    package = types.ModuleType(package_name)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules[package_name] = package
    discord = types.ModuleType("discord")
    discord.member = types.ModuleType("discord.member")
    discord.Member = type("Member", (), {})
    discord.PartialMessageable = type("PartialMessageable", (), {})
    commands = types.ModuleType("redbot.core.commands")
    commands.Parameter = inspect.Parameter
    commands.MemberConverter = object

    class DynamicCommand:
        def __init__(self, callback):
            self.callback = callback
            self.params = {}
            self.requires = SimpleNamespace(
                ready_event=SimpleNamespace(set=lambda: None)
            )

    def command_decorator(**_attrs):
        def decorate(callback):
            return DynamicCommand(callback)

        return decorate

    commands.command = command_decorator
    core = types.ModuleType("redbot.core")
    core.commands = commands
    formatting = types.ModuleType("redbot.core.utils.chat_formatting")

    def humanize_list(values):
        return " and ".join(values)

    formatting.humanize_list = humanize_list
    temporary_modules = {
        "discord": discord,
        "redbot": types.ModuleType("redbot"),
        "redbot.core": core,
        "redbot.core.commands": commands,
        "redbot.core.utils": types.ModuleType("redbot.core.utils"),
        "redbot.core.utils.chat_formatting": formatting,
    }
    previous = {name: sys.modules.get(name) for name in temporary_modules}
    sys.modules.update(temporary_modules)
    try:
        for module_name in ("migration_state", "arguments", "catalog", "runtime"):
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
        sys.modules[f"{package_name}.runtime"],
    )


catalog, runtime = load_runtime_modules()


def command_with_weights(*weights):
    now = datetime.now(timezone.utc)
    return catalog.CustomCommand(
        guild_id=100,
        name="weighted",
        author_id=200,
        author_name="Creator",
        created_at=now,
        edited_at=None,
        revision=1,
        responses=tuple(
            catalog.CustomResponse(
                response_id=f"response-{index}",
                display_order=index,
                content=f"response {index}",
                weight=weight,
            )
            for index, weight in enumerate(weights)
        ),
        cooldowns={},
        editors=(),
    )


class CustomCommandWhitespaceRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_read_back_and_runtime_keep_response_whitespace_exact(self):
        content = "  leading   spaces\nsecond line  "
        with TemporaryDirectory() as directory:
            store = catalog.CustomCommandCatalog(Path(directory) / "commands.sqlite")
            await store.initialize()
            await store.create(
                guild_id=100,
                name="spacing",
                author_id=200,
                author_name="Creator",
                responses=(catalog.ResponseDraft(content),),
                cooldowns={},
            )
            stored = await store.get(100, "spacing")

        rendered = runtime.CustomCommandRuntime.render_response(
            SimpleNamespace(
                author=SimpleNamespace(),
                channel=SimpleNamespace(),
                guild=SimpleNamespace(),
            ),
            stored.responses[0].content,
            (),
        )

        self.assertEqual(stored.responses[0].content, content)
        self.assertEqual(rendered, content)


class CustomCommandRuntimeTests(unittest.TestCase):
    def test_cooldown_feedback_uses_concise_singular_and_plural_copy(self):
        async def run():
            engine = runtime.CustomCommandRuntime(
                object(),
                object(),
                object(),
                logger=mock.Mock(),
            )
            ctx = SimpleNamespace(send=mock.AsyncMock())

            await engine._send_cooldown_feedback(ctx, 0.1)
            ctx.send.assert_awaited_once_with("Try again in 1 second")

            ctx.send.reset_mock()
            await engine._send_cooldown_feedback(ctx, 14.1)
            ctx.send.assert_awaited_once_with("Try again in 15 seconds")

        asyncio.run(run())

    def test_prepare_args_prefers_member_class_over_lowercase_module(self):
        parameters = runtime.CustomCommandRuntime.prepare_args("{1:Member}")

        self.assertEqual(tuple(parameters), ("member_final",))
        self.assertIs(parameters["member_final"].annotation, runtime.discord.Member)

    def test_weighted_selection_uses_exact_integer_boundaries(self):
        command = command_with_weights(99, 1)

        self.assertEqual(
            runtime.CustomCommandRuntime.select_response(command, 0).response_id,
            "response-0",
        )
        self.assertEqual(
            runtime.CustomCommandRuntime.select_response(command, 98).response_id,
            "response-0",
        )
        self.assertEqual(
            runtime.CustomCommandRuntime.select_response(command, 99).response_id,
            "response-1",
        )

    def test_prepare_args_preserves_red_converter_and_final_text_contract(self):
        parameters = runtime.CustomCommandRuntime.prepare_args(
            "{1:Member.name} {2:int} {3}"
        )

        self.assertEqual(
            tuple(parameters),
            ("member_0", "int_1", "text_final"),
        )
        self.assertEqual(parameters["member_0"].annotation.__name__, "Member")
        self.assertEqual(
            parameters["text_final"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )

    def test_prepare_args_rejects_gaps_and_more_than_ten_positions(self):
        with self.assertRaisesRegex(runtime.RuntimeArgumentError, "Missing"):
            runtime.CustomCommandRuntime.prepare_args("{1} {3}")
        with self.assertRaisesRegex(runtime.RuntimeArgumentError, "Too many"):
            runtime.CustomCommandRuntime.prepare_args("{1} {11}")

    def test_render_response_supports_context_attributes_and_collections(self):
        message = SimpleNamespace(
            author=SimpleNamespace(mention="@user"),
            channel=SimpleNamespace(name="general"),
            guild=SimpleNamespace(name="Guild"),
        )
        member = SimpleNamespace(display_name="Player")
        rendered = runtime.CustomCommandRuntime.render_response(
            message,
            "{author.mention}: {1.display_name} used {2}",
            (member, ("one", "two")),
        )

        self.assertEqual(rendered, "@user: Player used one and two")

    def test_parser_supports_zero_based_converter_suffix_and_query(self):
        parameters = runtime.CustomCommandRuntime.prepare_args(
            "{0:MemberConverter.display_name} {1:query}"
        )

        self.assertEqual(tuple(parameters), ("member_0", "quote_plus_final"))
        self.assertEqual(parameters["member_0"].annotation.__name__, "Member")

    def test_renderer_preserves_unknown_private_and_nested_placeholders(self):
        message = SimpleNamespace(
            author=SimpleNamespace(name="User", _secret="hidden"),
            channel=SimpleNamespace(name="general"),
            guild=SimpleNamespace(name="Guild"),
        )
        argument = SimpleNamespace(name="Value", _secret="hidden")

        rendered = runtime.CustomCommandRuntime.render_response(
            message,
            "{server.name} {missing} {author._secret} "
            "{author.profile.name} {1._secret} {1.profile.name}",
            (argument,),
        )

        self.assertEqual(
            rendered,
            "Guild {missing} {author._secret} "
            "{author.profile.name} {1._secret} {1.profile.name}",
        )

    def test_cooldown_scopes_are_evaluated_before_any_deadline_changes(self):
        command = command_with_weights(100)
        command = catalog.CustomCommand(
            **{
                **command.__dict__,
                "cooldowns": {"member": 10, "channel": 20},
            }
        )
        subject = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
            author=SimpleNamespace(id=300),
        )
        engine = runtime.CustomCommandRuntime(
            object(),
            object(),
            object(),
            logger=mock.Mock(),
        )
        with mock.patch.object(runtime.time, "monotonic", return_value=1_000):
            engine.check_cooldowns(command, subject)
        with mock.patch.object(runtime.time, "monotonic", return_value=1_005):
            with self.assertRaises(runtime.CustomCommandOnCooldown) as caught:
                engine.check_cooldowns(command, subject)

        self.assertEqual(int(caught.exception.retry_after), 15)


class CustomCommandInvocationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def invocation_context(
        *,
        command_name="weighted",
        channel_id=200,
        author_id=300,
    ):
        ctx = SimpleNamespace(
            prefix="!",
            invoked_with=command_name,
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=channel_id),
            author=SimpleNamespace(id=author_id),
            args=[],
            kwargs={},
            command_failed=False,
            send=mock.AsyncMock(),
        )
        message = SimpleNamespace(
            guild=ctx.guild,
            author=SimpleNamespace(bot=False),
            channel=ctx.channel,
            content=f"!{command_name}",
            id=400,
        )
        ctx.message = message
        ctx.args = [ctx]
        return ctx, message

    async def test_runtime_invocation_preserves_public_mention_content(self):
        command = command_with_weights(100)
        command = catalog.CustomCommand(
            **{
                **command.__dict__,
                "responses": (
                    catalog.CustomResponse(
                        "response-0",
                        0,
                        "Hello <@123>",
                        100,
                    ),
                ),
            }
        )
        store = SimpleNamespace(get=mock.AsyncMock(return_value=command))
        ctx = SimpleNamespace(
            prefix="!",
            invoked_with="weighted",
            guild=SimpleNamespace(id=100),
            channel=SimpleNamespace(id=200),
            author=SimpleNamespace(id=300),
            args=[],
            kwargs={},
            command_failed=False,
            send=mock.AsyncMock(),
        )
        ctx.message = None
        bot = SimpleNamespace(
            get_context=mock.AsyncMock(return_value=ctx),
            invoke=mock.AsyncMock(),
        )
        reporter = SimpleNamespace(report=mock.AsyncMock())
        engine = runtime.CustomCommandRuntime(
            bot,
            store,
            reporter,
            random_index=lambda _total: 0,
            logger=mock.Mock(),
        )
        message = SimpleNamespace(
            guild=ctx.guild,
            author=SimpleNamespace(bot=False),
            channel=ctx.channel,
            content="!weighted",
        )
        ctx.message = message
        ctx.args = [ctx]

        await engine.handle_message(message)

        bot.invoke.assert_awaited_once_with(ctx)
        ctx.send.assert_awaited_once_with("Hello <@123>")
        reporter.report.assert_not_awaited()

    async def test_repeated_command_is_silent_until_five_seconds_have_elapsed(self):
        first_ctx, first_message = self.invocation_context(author_id=300)
        second_ctx, second_message = self.invocation_context(author_id=301)
        command = command_with_weights(100)
        bot = SimpleNamespace(
            get_context=mock.AsyncMock(
                side_effect=(first_ctx, second_ctx, second_ctx)
            ),
            invoke=mock.AsyncMock(),
        )
        reporter = SimpleNamespace(report=mock.AsyncMock())
        engine = runtime.CustomCommandRuntime(
            bot,
            SimpleNamespace(get=mock.AsyncMock(return_value=command)),
            reporter,
            random_index=lambda _total: 0,
            logger=mock.Mock(),
        )

        with mock.patch.object(
            runtime.time,
            "monotonic",
            side_effect=(1_000, 1_000, 1_004.999, 1_005, 1_005),
        ):
            await engine.handle_message(first_message)
            await engine.handle_message(second_message)
            await engine.handle_message(second_message)

        self.assertEqual(bot.invoke.await_count, 2)
        first_ctx.send.assert_awaited_once_with("response 0")
        second_ctx.send.assert_awaited_once_with("response 0")
        reporter.report.assert_not_awaited()

    async def test_rejected_member_cooldown_does_not_reserve_the_channel(self):
        first_ctx, first_message = self.invocation_context(author_id=300)
        blocked_ctx, blocked_message = self.invocation_context(author_id=300)
        other_ctx, other_message = self.invocation_context(author_id=301)
        command = command_with_weights(100)
        command = catalog.CustomCommand(
            **{**command.__dict__, "cooldowns": {"member": 60}}
        )
        bot = SimpleNamespace(
            get_context=mock.AsyncMock(
                side_effect=(first_ctx, blocked_ctx, other_ctx)
            ),
            invoke=mock.AsyncMock(),
        )
        reporter = SimpleNamespace(report=mock.AsyncMock())
        engine = runtime.CustomCommandRuntime(
            bot,
            SimpleNamespace(get=mock.AsyncMock(return_value=command)),
            reporter,
            random_index=lambda _total: 0,
            logger=mock.Mock(),
        )

        with mock.patch.object(
            runtime.time,
            "monotonic",
            side_effect=(0, 0, 6, 6, 7, 7),
        ):
            await engine.handle_message(first_message)
            await engine.handle_message(blocked_message)
            await engine.handle_message(other_message)

        self.assertEqual(bot.invoke.await_count, 2)
        first_ctx.send.assert_awaited_once_with("response 0")
        blocked_ctx.send.assert_awaited_once_with("Try again in 54 seconds")
        other_ctx.send.assert_awaited_once_with("response 0")
        reporter.report.assert_not_awaited()

    async def test_invocation_cooldown_is_scoped_to_command_and_channel(self):
        first_ctx, first_message = self.invocation_context()
        other_channel_ctx, other_channel_message = self.invocation_context(
            channel_id=201
        )
        other_command_ctx, other_command_message = self.invocation_context(
            command_name="other"
        )
        other_command = command_with_weights(100)
        other_command = catalog.CustomCommand(
            **{**other_command.__dict__, "name": "other"}
        )
        bot = SimpleNamespace(
            get_context=mock.AsyncMock(
                side_effect=(first_ctx, other_channel_ctx, other_command_ctx)
            ),
            invoke=mock.AsyncMock(),
        )
        reporter = SimpleNamespace(report=mock.AsyncMock())
        engine = runtime.CustomCommandRuntime(
            bot,
            SimpleNamespace(
                get=mock.AsyncMock(
                    side_effect=(
                        command_with_weights(100),
                        command_with_weights(100),
                        other_command,
                    )
                )
            ),
            reporter,
            random_index=lambda _total: 0,
            logger=mock.Mock(),
        )

        with mock.patch.object(runtime.time, "monotonic", return_value=1_000):
            await engine.handle_message(first_message)
            await engine.handle_message(other_channel_message)
            await engine.handle_message(other_command_message)

        self.assertEqual(bot.invoke.await_count, 3)
        first_ctx.send.assert_awaited_once_with("response 0")
        other_channel_ctx.send.assert_awaited_once_with("response 0")
        other_command_ctx.send.assert_awaited_once_with("response 0")
        reporter.report.assert_not_awaited()

    async def test_uppercase_invocation_does_not_match_lowercase_custom_command(self):
        store = SimpleNamespace(get=mock.AsyncMock())
        ctx = SimpleNamespace(prefix="!", invoked_with="Weighted")
        bot = SimpleNamespace(get_context=mock.AsyncMock(return_value=ctx))
        engine = runtime.CustomCommandRuntime(
            bot,
            store,
            SimpleNamespace(report=mock.AsyncMock()),
            logger=mock.Mock(),
        )
        message = SimpleNamespace(
            guild=SimpleNamespace(id=100),
            author=SimpleNamespace(bot=False),
            channel=SimpleNamespace(id=200),
            content="!Weighted",
        )

        await engine.handle_message(message)

        store.get.assert_not_awaited()

    async def test_catalog_read_failure_is_reported_privately(self):
        ctx, message = self.invocation_context()
        store = SimpleNamespace(get=mock.AsyncMock(side_effect=OSError("sqlite failed")))
        reporter = SimpleNamespace(report=mock.AsyncMock())
        engine = runtime.CustomCommandRuntime(
            SimpleNamespace(get_context=mock.AsyncMock(return_value=ctx)),
            store,
            reporter,
            logger=mock.Mock(),
        )

        await engine.handle_message(message)

        reporter.report.assert_awaited_once()
        self.assertEqual(
            reporter.report.await_args.kwargs["action"],
            "read stored custom command",
        )

    async def test_render_failure_is_reported_privately(self):
        ctx, message = self.invocation_context()
        command = command_with_weights(100)
        command = catalog.CustomCommand(
            **{
                **command.__dict__,
                "responses": (
                    catalog.CustomResponse("response-0", 0, "{1.name}", 100),
                ),
            }
        )
        reporter = SimpleNamespace(report=mock.AsyncMock())
        bot = SimpleNamespace(
            get_context=mock.AsyncMock(return_value=ctx),
            invoke=mock.AsyncMock(),
        )
        engine = runtime.CustomCommandRuntime(
            bot,
            SimpleNamespace(get=mock.AsyncMock(return_value=command)),
            reporter,
            random_index=lambda _total: 0,
            logger=mock.Mock(),
        )

        await engine.handle_message(message)

        reporter.report.assert_awaited_once()
        self.assertEqual(
            reporter.report.await_args.kwargs["action"],
            "render stored custom command",
        )
        ctx.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
