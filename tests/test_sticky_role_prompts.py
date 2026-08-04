import asyncio
import types
import unittest
from unittest import mock

from tests.test_chatchart import load_nhmisc_module

nhmisc = load_nhmisc_module()


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []
        self._message_sent = asyncio.Event()

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))
        self._message_sent.set()

    async def wait_until_sent(self, count):
        while len(self.sent) < count:
            self._message_sent.clear()
            if len(self.sent) < count:
                await self._message_sent.wait()


class FakeBot:
    def __init__(self):
        self.waiters = []
        self._waiter_added = asyncio.Event()

    async def wait_for(self, event, *, check, timeout):
        if event != "message":
            raise AssertionError(f"Unexpected event: {event}")
        future = asyncio.get_running_loop().create_future()
        waiter = (check, future, timeout)
        self.waiters.append(waiter)
        self._waiter_added.set()
        try:
            return await future
        finally:
            self.waiters.remove(waiter)

    async def wait_until_listening(self, count=1):
        while len(self.waiters) < count:
            self._waiter_added.clear()
            if len(self.waiters) < count:
                await self._waiter_added.wait()

    def dispatch_message(self, message):
        for check, future, _timeout in list(self.waiters):
            if not future.done() and check(message):
                future.set_result(message)


class StickyRolePromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_prompts_apply_different_decisions_only_to_their_target_roles(self):
        bot = FakeBot()
        channel = FakeChannel(44)
        guild = types.SimpleNamespace(id=55)
        requester = types.SimpleNamespace(id=66)
        sticky_roles = types.SimpleNamespace(
            remove_sticky_role=mock.AsyncMock(return_value=(True, 3)),
            unconfigure_sticky_role=mock.AsyncMock(return_value=True),
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = bot
        cog._sticky_roles = sticky_roles

        async def prompt(role_id):
            await cog._prompt_sticky_role_db_action(
                guild=guild,
                channel=channel,
                role_id=role_id,
                role_name=f"Role {role_id}",
                config_exists=True,
                saved_rows=3,
                reason="Discord role deletion event",
                requester=requester,
            )

        first = asyncio.create_task(prompt(101))
        second = asyncio.create_task(prompt(202))
        try:
            await bot.wait_until_listening(2)

            prompt_text = "\n".join(content for content, _kwargs in channel.sent)
            self.assertIn("`remove 101`", prompt_text)
            self.assertIn("`keep 202`", prompt_text)
            self.assertIn("`change 101 <role mention or ID>`", prompt_text)

            author = types.SimpleNamespace(id=requester.id, bot=False)
            bot.dispatch_message(
                types.SimpleNamespace(channel=channel, author=author, content="remove 101")
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            self.assertTrue(first.done(), "the targeted prompt should complete")
            self.assertFalse(second.done(), "the other prompt must remain pending")
            sticky_roles.remove_sticky_role.assert_awaited_once_with(guild.id, 101)
            sticky_roles.unconfigure_sticky_role.assert_not_awaited()

            bot.dispatch_message(
                types.SimpleNamespace(channel=channel, author=author, content="keep 202")
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            self.assertTrue(second.done(), "the second targeted prompt should complete")
            sticky_roles.unconfigure_sticky_role.assert_awaited_once_with(guild.id, 202)
        finally:
            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

    async def test_change_command_routes_old_and_replacement_role_ids(self):
        class FakeRole:
            def __init__(self, role_id):
                self.id = role_id
                self.mention = f"<@&{role_id}>"
                self.managed = False

            def is_default(self):
                return False

            def __lt__(self, _other):
                return True

        bot = FakeBot()
        channel = FakeChannel(44)
        requester = types.SimpleNamespace(id=66)
        replacement = FakeRole(404)
        guild = types.SimpleNamespace(
            id=55,
            get_role=lambda role_id: replacement if role_id == replacement.id else None,
            me=types.SimpleNamespace(
                guild_permissions=types.SimpleNamespace(manage_roles=True),
                top_role=object(),
            ),
        )
        sticky_roles = types.SimpleNamespace(
            replace_sticky_role=mock.AsyncMock(return_value=(True, 3, 3))
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = bot
        cog._sticky_roles = sticky_roles
        task = asyncio.create_task(
            cog._prompt_sticky_role_db_action(
                guild=guild,
                channel=channel,
                role_id=303,
                role_name="Deleted role",
                config_exists=True,
                saved_rows=3,
                reason="Discord role deletion event",
                requester=requester,
            )
        )
        try:
            await bot.wait_until_listening()
            author = types.SimpleNamespace(id=requester.id, bot=False)
            bot.dispatch_message(
                types.SimpleNamespace(
                    channel=channel,
                    author=author,
                    content="change 303 <@&404>",
                )
            )
            await task

            sticky_roles.replace_sticky_role.assert_awaited_once_with(guild.id, 303, 404)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unqualified_decision_is_ignored_until_a_targeted_decision_arrives(self):
        bot = FakeBot()
        channel = FakeChannel(44)
        guild = types.SimpleNamespace(id=55)
        requester = types.SimpleNamespace(id=66)
        sticky_roles = types.SimpleNamespace(
            remove_sticky_role=mock.AsyncMock(return_value=(True, 0))
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = bot
        cog._sticky_roles = sticky_roles
        task = asyncio.create_task(
            cog._prompt_sticky_role_db_action(
                guild=guild,
                channel=channel,
                role_id=505,
                role_name="Deleted role",
                config_exists=True,
                saved_rows=0,
                reason="Discord role deletion event",
                requester=requester,
            )
        )
        try:
            await bot.wait_until_listening()
            author = types.SimpleNamespace(id=requester.id, bot=False)
            bot.dispatch_message(
                types.SimpleNamespace(channel=channel, author=author, content="remove")
            )
            await asyncio.sleep(0)

            self.assertFalse(task.done())
            sticky_roles.remove_sticky_role.assert_not_awaited()

            bot.dispatch_message(
                types.SimpleNamespace(channel=channel, author=author, content="remove 505")
            )
            await task
            sticky_roles.remove_sticky_role.assert_awaited_once_with(guild.id, 505)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_malformed_targeted_decision_gets_one_prompt_specific_usage_error(self):
        bot = FakeBot()
        channel = FakeChannel(44)
        guild = types.SimpleNamespace(id=55)
        requester = types.SimpleNamespace(id=66)
        sticky_roles = types.SimpleNamespace(
            remove_sticky_role=mock.AsyncMock(return_value=(True, 0))
        )
        cog = object.__new__(nhmisc.NHMisc)
        cog.bot = bot
        cog._sticky_roles = sticky_roles
        task = asyncio.create_task(
            cog._prompt_sticky_role_db_action(
                guild=guild,
                channel=channel,
                role_id=606,
                role_name="Deleted role",
                config_exists=True,
                saved_rows=0,
                reason="Discord role deletion event",
                requester=requester,
            )
        )
        try:
            await bot.wait_until_listening()
            author = types.SimpleNamespace(id=requester.id, bot=False)
            bot.dispatch_message(
                types.SimpleNamespace(channel=channel, author=author, content="remove 606 extra")
            )
            await channel.wait_until_sent(2)

            errors = [content for content, _kwargs in channel.sent if "Invalid response" in content]
            self.assertEqual(
                errors,
                [
                    "Invalid response. Use `remove 606`, `keep 606`, or "
                    "`change 606 <role mention or ID>`."
                ],
            )
            sticky_roles.remove_sticky_role.assert_not_awaited()

            await bot.wait_until_listening()
            bot.dispatch_message(
                types.SimpleNamespace(channel=channel, author=author, content="remove 606")
            )
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
