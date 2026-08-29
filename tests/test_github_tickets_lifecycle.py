import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.githubtickets_loader import isolated_githubtickets_modules
from tests.harness import _Bot


class FakeBot(_Bot):
    def __init__(self, *, ready=False):
        super().__init__(ready=ready)
        self.channels = {}
        self.guild_map = {}
        self.fetch_calls = 0

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_partial_messageable(self, channel_id):
        return self.channels[channel_id]

    def get_guild(self, guild_id):
        return self.guild_map.get(guild_id)

    async def fetch_channel(self, _channel_id):
        self.fetch_calls += 1
        raise AssertionError("startup must not fetch Discord objects")


class FakeInteractionResponse:
    def __init__(self):
        self.messages = []
        self.modals = []

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modals.append(modal)


class FakeInteraction:
    def __init__(self, *, user_id=100, role_ids=(), manage_messages=False):
        self.guild_id = 10
        self.user = SimpleNamespace(
            id=user_id,
            roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
            guild_permissions=SimpleNamespace(manage_messages=manage_messages),
        )
        self.response = FakeInteractionResponse()


class CachedMessage:
    def __init__(self, message_id=40):
        self.id = message_id
        self.edit_calls = []
        self.delete_calls = 0

    async def edit(self, **kwargs):
        self.edit_calls.append(kwargs)

    async def delete(self):
        self.delete_calls += 1


class CachedChannel:
    def __init__(self, channel_id=20):
        self.id = channel_id
        self.message = CachedMessage()

    def get_partial_message(self, message_id):
        self.message.id = message_id
        return self.message


class CachedThread:
    def __init__(self, thread_id=50):
        self.id = thread_id
        self.send_calls = []
        self.delete_calls = 0

    async def send(self, content, **kwargs):
        self.send_calls.append((content, kwargs))

    async def delete(self):
        self.delete_calls += 1


class CachedLogChannel:
    def __init__(self, *, error=None):
        self.send_calls = []
        self.error = error

    async def send(self, content, **kwargs):
        self.send_calls.append((content, kwargs))
        if self.error is not None:
            raise self.error


class GitHubTicketsLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_path = Path(self.temporary_directory.name)

    async def test_startup_restores_views_locally_then_starts_deadline_scheduler(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.cog_load()
            now = datetime.now(timezone.utc)
            ticket = await cog.store.create_ticket(
                modules.models.NewTicket(
                    guild_id=10,
                    channel_id=20,
                    author_id=30,
                    pr_title="Improve rendering",
                    pr_url="https://example.test/pull/1",
                    category_display="",
                    routing_mode=modules.models.RoutingMode.NONE,
                    direct_target_id=None,
                    category_ids=(),
                    created_at=now,
                )
            )
            await cog.store.activate_ticket(
                ticket.ticket_id,
                message_id=40,
                thread_id=50,
                protection_until=now,
                next_action=None,
                next_action_at=None,
                updated_at=now,
            )
            await cog.config.guild_from_id(10).set_raw("max_pings", value=0)
            overdue = await cog.store.create_ticket(
                modules.models.NewTicket(
                    guild_id=10,
                    channel_id=20,
                    author_id=31,
                    pr_title="Overdue routing",
                    pr_url="https://example.test/pull/2",
                    category_display="",
                    routing_mode=modules.models.RoutingMode.AUTOMATIC,
                    direct_target_id=None,
                    category_ids=(),
                    created_at=now,
                )
            )
            await cog.store.activate_ticket(
                overdue.ticket_id,
                message_id=41,
                thread_id=51,
                protection_until=now,
                next_action=modules.models.NextAction.AUTOMATIC_PING,
                next_action_at=now,
                updated_at=now,
            )

            try:
                self.assertFalse(hasattr(bot, "restored_views"))
                bot.ready.set()
                for _attempt in range(50):
                    if (
                        getattr(bot, "restored_views", None)
                        and cog.scheduler._task is not None
                    ):
                        break
                    await asyncio.sleep(0.01)

                self.assertEqual(len(bot.restored_views), 2)
                self.assertEqual(
                    [message_id for _view, message_id in bot.restored_views],
                    [40, 41],
                )
                self.assertTrue(
                    all(view.timeout is None for view, _message_id in bot.restored_views)
                )
                self.assertIsNone(
                    (await cog.store.get_ticket(overdue.ticket_id)).next_action
                )
                self.assertEqual(bot.fetch_calls, 0)
                self.assertIsNotNone(cog.scheduler._task)
            finally:
                await cog.cog_unload()
            self.assertIsNone(cog.scheduler._task)
            await asyncio.sleep(0.01)

    async def test_application_commands_are_guild_only_and_runtime_authorized(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            self.assertFalse(hasattr(cog._new_ticket_command, "type"))
            self.assertFalse(hasattr(cog._developer_profile_slash_command, "type"))
            await cog.config.guild_from_id(10).set_raw(
                "participant_role_ids",
                value=[99],
            )
            await cog.cog_load()
            try:
                new_ticket_command = bot.tree.get_command(
                    "newticket",
                    type="chat_input",
                )
                developer_profile_command = bot.tree.get_command(
                    "developerprofile",
                    type="chat_input",
                )
                context_profile_command = bot.tree.get_command(
                    "Developer Profile",
                    type="user",
                )
                self.assertIsNotNone(new_ticket_command)
                self.assertIsNotNone(developer_profile_command)
                self.assertIsNotNone(context_profile_command)
                self.assertIsNone(
                    bot.tree.get_command("github-tickets", type="chat_input")
                )
                self.assertEqual(
                    new_ticket_command.description,
                    "Create a new GitHub ticket",
                )
                self.assertEqual(
                    developer_profile_command.description,
                    "Manage your developer profile",
                )
                self.assertTrue(new_ticket_command.guild_only)
                self.assertTrue(developer_profile_command.guild_only)
                self.assertTrue(context_profile_command.guild_only)

                rejected = FakeInteraction()
                await new_ticket_command.callback(rejected)
                self.assertEqual(
                    rejected.response.messages[0][0],
                    modules.presentation.CANNOT_USE_ACTION,
                )
                self.assertTrue(rejected.response.messages[0][1]["ephemeral"])

                accepted = FakeInteraction(role_ids=(99,))
                await new_ticket_command.callback(accepted)
                self.assertIsInstance(
                    accepted.response.modals[0],
                    modules.dashboard.NewTicketModal,
                )

                profile_dashboard_interaction = FakeInteraction(role_ids=(99,))
                await developer_profile_command.callback(profile_dashboard_interaction)
                self.assertEqual(
                    profile_dashboard_interaction.response.messages[0][0],
                    modules.presentation.DEVELOPER_PROFILE_COMMAND,
                )
                self.assertTrue(
                    profile_dashboard_interaction.response.messages[0][1]["ephemeral"]
                )

                target = SimpleNamespace(id=500)
                profile_interaction = FakeInteraction(manage_messages=True)
                await context_profile_command.callback(profile_interaction, target)
                self.assertEqual(
                    profile_interaction.response.messages[0][0],
                    modules.presentation.NO_PROFILE,
                )
            finally:
                await cog.cog_unload()

            self.assertIsNone(
                bot.tree.get_command("newticket", type="chat_input")
            )
            self.assertIsNone(
                bot.tree.get_command("developerprofile", type="chat_input")
            )
            self.assertIsNone(
                bot.tree.get_command("Developer Profile", type="user")
            )

    async def test_application_commands_restore_displaced_commands_on_unload(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)

            async def chat_callback(_interaction):
                return None

            async def user_callback(
                _interaction,
                _member: modules.githubtickets.discord.Member,
            ):
                return None

            previous_commands = (
                modules.githubtickets.discord.app_commands.Command(
                    name="newticket",
                    description="Previous new ticket command",
                    callback=chat_callback,
                ),
                modules.githubtickets.discord.app_commands.Command(
                    name="developerprofile",
                    description="Previous developer profile command",
                    callback=chat_callback,
                ),
                modules.githubtickets.discord.app_commands.ContextMenu(
                    name="Developer Profile",
                    callback=user_callback,
                ),
            )
            previous_commands[-1].type = (
                modules.githubtickets.discord.AppCommandType.user
            )
            for command in previous_commands:
                bot.tree.add_command(command)
            cog = modules.githubtickets.GitHubTickets(bot)

            await cog.cog_load()
            try:
                for command in previous_commands:
                    command_type = getattr(command, "type", "chat_input")
                    self.assertIsNot(
                        bot.tree.get_command(command.name, type=command_type),
                        command,
                    )
            finally:
                await cog.cog_unload()

            for command in previous_commands:
                command_type = getattr(command, "type", "chat_input")
                self.assertIs(
                    bot.tree.get_command(command.name, type=command_type),
                    command,
                )

    async def test_raw_message_and_thread_deletions_cleanup_without_fetches(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=True)
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.cog_load()
            now = datetime.now(timezone.utc)

            class CachedThread:
                def __init__(self):
                    self.delete_calls = 0

                async def delete(self):
                    self.delete_calls += 1

            class PartialMessage:
                def __init__(self):
                    self.delete_calls = 0

                async def delete(self):
                    self.delete_calls += 1

            class CachedChannel:
                def __init__(self):
                    self.message = PartialMessage()

                def get_partial_message(self, _message_id):
                    return self.message

            channel = CachedChannel()
            thread = CachedThread()
            bot.channels = {20: channel, 50: thread}

            async def active(message_id, thread_id):
                created = await cog.store.create_ticket(
                    modules.models.NewTicket(
                        guild_id=10,
                        channel_id=20,
                        author_id=30,
                        pr_title="Improve rendering",
                        pr_url="https://example.test/pull/1",
                        category_display="",
                        routing_mode=modules.models.RoutingMode.NONE,
                        direct_target_id=None,
                        category_ids=(),
                        created_at=now,
                    )
                )
                await cog.store.activate_ticket(
                    created.ticket_id,
                    message_id=message_id,
                    thread_id=thread_id,
                    protection_until=now,
                    next_action=None,
                    next_action_at=None,
                    updated_at=now,
                )
                return created.ticket_id

            first_id = await active(40, 50)
            await cog.on_raw_message_delete(SimpleNamespace(message_id=40))
            self.assertIsNone(await cog.store.get_ticket(first_id))
            self.assertEqual(thread.delete_calls, 1)

            second_id = await active(41, 51)
            await cog.on_thread_delete(SimpleNamespace(id=51))
            self.assertIsNone(await cog.store.get_ticket(second_id))
            self.assertEqual(channel.message.delete_calls, 1)
            self.assertEqual(bot.fetch_calls, 0)

            third_id = await active(42, 52)
            fourth_id = await active(43, 53)
            bot.channels[52] = CachedThread()
            bot.channels[53] = CachedThread()
            await cog.on_raw_bulk_message_delete(
                SimpleNamespace(message_ids={42, 43})
            )
            self.assertIsNone(await cog.store.get_ticket(third_id))
            self.assertIsNone(await cog.store.get_ticket(fourth_id))

            retained_id = await active(44, 54)
            await cog.on_raw_message_delete(SimpleNamespace(message_id=44))
            retained = await cog.store.get_ticket(retained_id)
            self.assertEqual(retained.state, modules.models.TicketState.FINISHING)
            bot.channels[54] = CachedThread()
            recovered = await cog.coordinator.recover_projection_cleanup(retained_id)
            self.assertTrue(recovered.success)
            self.assertIsNone(await cog.store.get_ticket(retained_id))

            await cog.cog_unload()

    async def test_automatic_routing_uses_cached_members_and_persisted_history(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.config.guild_from_id(10).set_raw(
                "participant_role_ids",
                value=[99],
            )
            await cog.cog_load()
            now = datetime.now(timezone.utc)
            category = await cog.store.add_category(10, "rendering", now)
            for user_id in (200, 201):
                await cog.store.save_profile(
                    guild_id=10,
                    user_id=user_id,
                    github_username=None,
                    category_ids=(category.category_id,),
                    automatic_pings=True,
                    updated_at=now,
                )
            ticket = await cog.store.create_ticket(
                modules.models.NewTicket(
                    guild_id=10,
                    channel_id=20,
                    author_id=30,
                    pr_title="Improve rendering",
                    pr_url="https://example.test/pull/1",
                    category_display="rendering",
                    routing_mode=modules.models.RoutingMode.AUTOMATIC,
                    direct_target_id=None,
                    category_ids=(category.category_id,),
                    created_at=now,
                )
            )
            await cog.store.activate_ticket(
                ticket.ticket_id,
                message_id=40,
                thread_id=50,
                protection_until=now,
                next_action=modules.models.NextAction.AUTOMATIC_PING,
                next_action_at=now,
                updated_at=now,
            )
            role = SimpleNamespace(id=99)
            members = [
                SimpleNamespace(
                    id=200,
                    roles=[role],
                    guild_permissions=SimpleNamespace(manage_messages=False),
                    status="idle",
                ),
                SimpleNamespace(
                    id=201,
                    roles=[role],
                    guild_permissions=SimpleNamespace(manage_messages=False),
                    status="online",
                ),
            ]
            bot.guild_map[10] = SimpleNamespace(id=10, members=members)
            channel = CachedChannel()
            thread = CachedThread()
            bot.channels = {20: channel, 50: thread}
            try:
                result = await cog.coordinator.process_due(ticket.ticket_id)

                self.assertTrue(result.success)
                self.assertEqual(
                    thread.send_calls[0][0],
                    modules.presentation.automatic_review_notification("<@201>"),
                )
                current = await cog.store.get_ticket(ticket.ticket_id)
                self.assertEqual(current.current_target_id, 201)
                self.assertEqual(current.ping_count, 1)
                self.assertEqual(len(channel.message.edit_calls), 1)
                self.assertEqual(bot.fetch_calls, 0)
            finally:
                await cog.cog_unload()

    async def test_automatic_candidate_count_uses_strict_cached_policy_and_excludes_author(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.config.guild_from_id(10).set_raw(
                "participant_role_ids",
                value=[99],
            )
            await cog.cog_load()
            now = datetime.now(timezone.utc)
            rendering = await cog.store.add_category(10, "rendering", now)
            mixins = await cog.store.add_category(10, "mixins", now)
            both_categories = (rendering.category_id, mixins.category_id)
            profiles = (
                (30, both_categories, True),
                (200, both_categories, True),
                (201, (rendering.category_id,), True),
                (202, both_categories, False),
                (203, both_categories, True),
                (204, both_categories, True),
            )
            for user_id, category_ids, automatic_pings in profiles:
                await cog.store.save_profile(
                    guild_id=10,
                    user_id=user_id,
                    github_username=None,
                    category_ids=category_ids,
                    automatic_pings=automatic_pings,
                    updated_at=now,
                )
            participant = SimpleNamespace(id=99)
            no_permissions = SimpleNamespace(manage_messages=False)
            bot.guild_map[10] = SimpleNamespace(
                id=10,
                members=[
                    SimpleNamespace(id=30, roles=[participant], guild_permissions=no_permissions),
                    SimpleNamespace(id=200, roles=[participant], guild_permissions=no_permissions),
                    SimpleNamespace(id=201, roles=[participant], guild_permissions=no_permissions),
                    SimpleNamespace(id=202, roles=[participant], guild_permissions=no_permissions),
                    SimpleNamespace(id=203, roles=[], guild_permissions=no_permissions),
                    SimpleNamespace(
                        id=204,
                        roles=[],
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                ],
            )
            try:
                count = await cog._count_automatic_candidates(
                    10,
                    both_categories,
                    frozenset({30, 200}),
                )

                self.assertEqual(count, 1)
                self.assertEqual(bot.fetch_calls, 0)
            finally:
                await cog.cog_unload()

    async def test_member_remove_deletes_only_the_departed_guild_profile(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            cog = modules.githubtickets.GitHubTickets(FakeBot(ready=False))
            delete_profile = mock.AsyncMock(
                side_effect=[None, RuntimeError("database unavailable")]
            )
            cog.store.delete_profile = delete_profile

            await cog.on_member_remove(
                SimpleNamespace(id=200, guild=SimpleNamespace(id=10))
            )
            with self.assertLogs(modules.githubtickets.log, level="ERROR") as logs:
                await cog.on_member_remove(
                    SimpleNamespace(id=300, guild=SimpleNamespace(id=11))
                )

            self.assertEqual(
                delete_profile.await_args_list,
                [mock.call(10, 200), mock.call(11, 300)],
            )
            self.assertTrue(
                any("member profile deletion failed" in message for message in logs.output)
            )

    async def test_channel_role_and_guild_deletions_cleanup_only_their_scopes(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            guild_config = cog.config.guild_from_id(10)
            await guild_config.set_raw("ticket_channel_id", value=20)
            await guild_config.set_raw("log_channel_id", value=20)
            await guild_config.set_raw("participant_role_ids", value=[99, 100])
            await cog.cog_load()
            now = datetime.now(timezone.utc)
            category = await cog.store.add_category(10, "rendering", now)
            await cog.store.save_profile(
                guild_id=10,
                user_id=200,
                github_username=None,
                category_ids=(category.category_id,),
                automatic_pings=True,
                updated_at=now,
            )
            ticket = await cog.store.create_ticket(
                modules.models.NewTicket(
                    guild_id=10,
                    channel_id=20,
                    author_id=30,
                    pr_title="Improve rendering",
                    pr_url="https://example.test/pull/1",
                    category_display="rendering",
                    routing_mode=modules.models.RoutingMode.NONE,
                    direct_target_id=None,
                    category_ids=(category.category_id,),
                    created_at=now,
                )
            )
            await cog.store.activate_ticket(
                ticket.ticket_id,
                message_id=40,
                thread_id=50,
                protection_until=now,
                next_action=None,
                next_action_at=None,
                updated_at=now,
            )
            guild = SimpleNamespace(id=10)
            try:
                await cog.on_guild_role_delete(SimpleNamespace(id=99, guild=guild))
                self.assertEqual(
                    await guild_config.get_raw("participant_role_ids"),
                    [100],
                )

                await cog.on_guild_channel_delete(
                    SimpleNamespace(id=20, guild=guild)
                )
                self.assertIsNone(await guild_config.get_raw("ticket_channel_id"))
                self.assertIsNone(await guild_config.get_raw("log_channel_id"))
                self.assertIsNone(await cog.store.get_ticket(ticket.ticket_id))
                self.assertIsNotNone(await cog.store.get_profile(10, 200))

                await cog.on_guild_remove(guild)
                self.assertIsNone(await cog.store.get_profile(10, 200))
                self.assertEqual(await cog.store.list_categories(10), ())
                self.assertEqual(
                    await guild_config.get_raw("participant_role_ids"),
                    [],
                )
            finally:
                await cog.cog_unload()

    async def test_deleted_log_channel_is_cleared_even_when_ticket_cleanup_fails(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            guild_config = cog.config.guild_from_id(10)
            await guild_config.set_raw("log_channel_id", value=20)
            await cog.cog_load()

            async def fail_cleanup(_guild_id, _channel_id):
                raise RuntimeError("database unavailable")

            cog.store.delete_tickets_for_channel = fail_cleanup
            try:
                with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                    await cog._handle_guild_channel_delete(
                        SimpleNamespace(id=20, guild=SimpleNamespace(id=10))
                    )
                self.assertIsNone(await guild_config.get_raw("log_channel_id"))
            finally:
                await cog.cog_unload()

    async def test_deleted_channel_ticket_cleanup_runs_even_when_config_fails(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.cog_load()
            now = datetime.now(timezone.utc)
            created = await cog.store.create_ticket(
                modules.models.NewTicket(
                    guild_id=10,
                    channel_id=20,
                    author_id=30,
                    pr_title="Improve rendering",
                    pr_url="https://example.test/pull/1",
                    category_display="",
                    routing_mode=modules.models.RoutingMode.NONE,
                    direct_target_id=None,
                    category_ids=(),
                    created_at=now,
                )
            )

            class FailingGuildConfig:
                async def get_raw(self, _key, *, default=None):
                    del default
                    raise RuntimeError("config unavailable")

            cog.config.guild_from_id = lambda _guild_id: FailingGuildConfig()
            try:
                with self.assertRaisesRegex(RuntimeError, "config unavailable"):
                    await cog._handle_guild_channel_delete(
                        SimpleNamespace(id=20, guild=SimpleNamespace(id=10))
                    )
                self.assertIsNone(await cog.store.get_ticket(created.ticket_id))
            finally:
                await cog.cog_unload()

    async def test_mark_finished_writes_the_exact_best_effort_audit_log(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.cog_load()
            now = datetime.now(timezone.utc)
            created = await cog.store.create_ticket(
                modules.models.NewTicket(
                    guild_id=10,
                    channel_id=20,
                    author_id=30,
                    pr_title="Improve rendering",
                    pr_url="https://github.com/example/repository/pull/123",
                    category_display="rendering",
                    routing_mode=modules.models.RoutingMode.NONE,
                    direct_target_id=None,
                    category_ids=(),
                    created_at=now,
                )
            )
            await cog.store.activate_ticket(
                created.ticket_id,
                message_id=40,
                thread_id=50,
                protection_until=now,
                next_action=None,
                next_action_at=None,
                updated_at=now,
            )
            original_mark_finished = cog.coordinator.mark_finished

            async def claim_then_finish(ticket_id, actor):
                claimed = await cog.store.claim(
                    ticket_id,
                    assignee_id=200,
                    protection_until=now,
                    updated_at=now,
                )
                self.assertTrue(claimed)
                return await original_mark_finished(ticket_id, actor)

            cog.coordinator.mark_finished = claim_then_finish
            log_channel = CachedLogChannel()
            bot.channels = {
                20: CachedChannel(),
                50: CachedThread(),
                60: log_channel,
            }
            await cog.config.guild_from_id(10).set_raw("log_channel_id", value=60)

            try:
                result = await cog._finish_ticket(
                    created.public_token,
                    modules.coordinator.TicketActor(
                        user_id=30,
                        is_participant=False,
                        can_manage_messages=False,
                    ),
                )
            finally:
                await cog.cog_unload()

            self.assertTrue(result.success)
            self.assertEqual(
                log_channel.send_calls[0][0],
                "[Improve rendering](<https://github.com/example/repository/pull/123>)\n"
                "Finished by <@30> | Author <@30> | Reviewer <@200>",
            )
            allowed_mentions = log_channel.send_calls[0][1]["allowed_mentions"]
            self.assertFalse(allowed_mentions.users)
            self.assertFalse(allowed_mentions.roles)
            self.assertFalse(allowed_mentions.everyone)
            self.assertEqual(bot.fetch_calls, 0)

    async def test_mark_finished_logging_never_changes_the_action_result(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.cog_load()
            now = datetime.now(timezone.utc)

            async def finish(message_id, thread_id, *, log_channel_id=None):
                created = await cog.store.create_ticket(
                    modules.models.NewTicket(
                        guild_id=10,
                        channel_id=20,
                        author_id=30,
                        pr_title="Small fix",
                        pr_url="https://github.com/example/repository/pull/124",
                        category_display="",
                        routing_mode=modules.models.RoutingMode.NONE,
                        direct_target_id=None,
                        category_ids=(),
                        created_at=now,
                    )
                )
                await cog.store.activate_ticket(
                    created.ticket_id,
                    message_id=message_id,
                    thread_id=thread_id,
                    protection_until=now,
                    next_action=None,
                    next_action_at=None,
                    updated_at=now,
                )
                if log_channel_id is None:
                    await cog.config.guild_from_id(10).clear_raw("log_channel_id")
                else:
                    await cog.config.guild_from_id(10).set_raw(
                        "log_channel_id", value=log_channel_id
                    )
                return await cog._finish_ticket(
                    created.public_token,
                    modules.coordinator.TicketActor(
                        user_id=30,
                        is_participant=False,
                        can_manage_messages=False,
                    ),
                )

            ticket_channel = CachedChannel()
            bot.channels = {
                20: ticket_channel,
                50: CachedThread(),
                51: CachedThread(),
                52: CachedThread(),
                61: CachedLogChannel(error=RuntimeError("send failed")),
            }
            try:
                unset = await finish(40, 50)
                cache_miss = await finish(41, 51, log_channel_id=62)
                send_failure = await finish(42, 52, log_channel_id=61)
            finally:
                await cog.cog_unload()

            self.assertTrue(unset.success)
            self.assertTrue(cache_miss.success)
            self.assertTrue(send_failure.success)
            self.assertEqual(bot.fetch_calls, 0)

    async def test_failed_mark_finished_does_not_write_an_audit_log(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.cog_load()
            now = datetime.now(timezone.utc)
            created = await cog.store.create_ticket(
                modules.models.NewTicket(
                    guild_id=10,
                    channel_id=20,
                    author_id=30,
                    pr_title="Improve rendering",
                    pr_url="https://github.com/example/repository/pull/123",
                    category_display="",
                    routing_mode=modules.models.RoutingMode.NONE,
                    direct_target_id=None,
                    category_ids=(),
                    created_at=now,
                )
            )
            await cog.store.activate_ticket(
                created.ticket_id,
                message_id=40,
                thread_id=50,
                protection_until=now,
                next_action=None,
                next_action_at=None,
                updated_at=now,
            )
            log_channel = CachedLogChannel()
            bot.channels = {
                20: CachedChannel(),
                50: CachedThread(),
                60: log_channel,
            }
            await cog.config.guild_from_id(10).set_raw("log_channel_id", value=60)

            try:
                result = await cog._finish_ticket(
                    created.public_token,
                    modules.coordinator.TicketActor(
                        user_id=999,
                        is_participant=True,
                        can_manage_messages=False,
                    ),
                )
            finally:
                await cog.cog_unload()

            self.assertFalse(result.success)
            self.assertEqual(log_channel.send_calls, [])

    async def test_red_privacy_deletion_removes_authored_projection_and_reopens_assignments(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.cog_load()
            now = datetime.now(timezone.utc)
            channel = CachedChannel()
            authored_thread = CachedThread(50)
            assigned_thread = CachedThread(51)
            bot.channels = {20: channel, 50: authored_thread, 51: assigned_thread}

            async def active(
                author_id,
                message_id,
                thread_id,
                *,
                direct_target_id=None,
            ):
                created = await cog.store.create_ticket(
                    modules.models.NewTicket(
                        guild_id=10,
                        channel_id=20,
                        author_id=author_id,
                        pr_title="Improve rendering",
                        pr_url="https://example.test/pull/1",
                        category_display="",
                        routing_mode=modules.models.RoutingMode.AUTOMATIC,
                        direct_target_id=direct_target_id,
                        category_ids=(),
                        created_at=now,
                    )
                )
                await cog.store.activate_ticket(
                    created.ticket_id,
                    message_id=message_id,
                    thread_id=thread_id,
                    protection_until=now,
                    next_action=None,
                    next_action_at=None,
                    updated_at=now,
                )
                return await cog.store.get_ticket(created.ticket_id)

            authored = await active(500, 40, 50)
            assigned = await active(30, 41, 51)
            await cog.store.claim(assigned.ticket_id, 500, now, now)
            obsolete_direct = await active(
                30,
                42,
                52,
                direct_target_id=500,
            )
            await cog.store.claim(obsolete_direct.ticket_id, 600, now, now)
            await cog.store.save_profile(
                guild_id=10,
                user_id=500,
                github_username="private-name",
                category_ids=(),
                automatic_pings=False,
                updated_at=now,
            )
            try:
                await cog.red_delete_data_for_user(requester="user", user_id=500)

                self.assertIsNone(await cog.store.get_ticket(authored.ticket_id))
                reopened = await cog.store.get_ticket(assigned.ticket_id)
                self.assertEqual(reopened.state, modules.models.TicketState.OPEN)
                self.assertIsNone(reopened.assignee_id)
                self.assertEqual(
                    reopened.next_action,
                    modules.models.NextAction.AUTOMATIC_PING,
                )
                unchanged = await cog.store.get_ticket(obsolete_direct.ticket_id)
                self.assertEqual(unchanged.state, modules.models.TicketState.CLAIMED)
                self.assertEqual(unchanged.assignee_id, 600)
                self.assertIsNone(unchanged.direct_target_id)
                self.assertIsNone(await cog.store.get_profile(10, 500))
                self.assertEqual(await cog.store.user_reference_guild_ids(500), ())
                self.assertEqual(authored_thread.delete_calls, 1)
                self.assertEqual(channel.message.delete_calls, 1)
                self.assertEqual(len(channel.message.edit_calls), 1)
                self.assertEqual(bot.fetch_calls, 0)
            finally:
                await cog.cog_unload()

    async def test_red_privacy_deletion_keeps_existing_finishing_cleanup_ids(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            channel = CachedChannel()
            bot.channels = {20: channel}
            cog = modules.githubtickets.GitHubTickets(bot)
            await cog.store.initialize()
            now = datetime.now(timezone.utc)
            created = await cog.store.create_ticket(
                modules.models.NewTicket(
                    guild_id=10,
                    channel_id=20,
                    author_id=500,
                    pr_title="private title",
                    pr_url="https://example.test/private",
                    category_display="private category",
                    routing_mode=modules.models.RoutingMode.AUTOMATIC,
                    direct_target_id=501,
                    category_ids=(),
                    created_at=now,
                )
            )
            await cog.store.activate_ticket(
                created.ticket_id,
                message_id=40,
                thread_id=50,
                protection_until=now,
                next_action=None,
                next_action_at=None,
                updated_at=now,
            )
            self.assertTrue(await cog.store.begin_finishing(created.ticket_id, now))

            await cog.red_delete_data_for_user(requester="user", user_id=500)

            cleanup = await cog.store.get_ticket(created.ticket_id)
            self.assertEqual(cleanup.state, modules.models.TicketState.FINISHING)
            self.assertIsNone(cleanup.author_id)
            self.assertEqual(cleanup.pr_title, "")
            self.assertEqual(cleanup.pr_url, "")
            self.assertEqual(cleanup.category_display, "")
            self.assertEqual(cleanup.message_id, 40)
            self.assertEqual(cleanup.thread_id, 50)
            self.assertEqual(channel.message.delete_calls, 0)
            self.assertEqual(bot.fetch_calls, 1)

            thread = CachedThread(50)
            bot.channels[50] = thread
            result = await cog.coordinator.recover_projection_cleanup(
                created.ticket_id
            )

            self.assertTrue(result.success)
            self.assertIsNone(await cog.store.get_ticket(created.ticket_id))
            self.assertEqual(thread.delete_calls, 1)
            self.assertEqual(channel.message.delete_calls, 1)
            self.assertEqual(bot.fetch_calls, 1)
