import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

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

    def get_guild(self, guild_id):
        return self.guild_map.get(guild_id)

    async def fetch_channel(self, _channel_id):
        self.fetch_calls += 1
        raise AssertionError("startup must not fetch Discord objects")


class FakeInteractionResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))


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
            await cog.config.guild_from_id(10).set_raw(
                "participant_role_ids",
                value=[99],
            )
            await cog.cog_load()
            try:
                dashboard_command = bot.tree.get_command(
                    "github-tickets",
                    type="chat_input",
                )
                profile_command = bot.tree.get_command(
                    "Developer Profile",
                    type="user",
                )
                self.assertIs(dashboard_command, cog._dashboard_command)
                self.assertIs(profile_command, cog._developer_profile_command)
                self.assertTrue(dashboard_command.guild_only)
                self.assertTrue(profile_command.guild_only)

                rejected = FakeInteraction()
                await dashboard_command.callback(rejected)
                self.assertEqual(
                    rejected.response.messages[0][0],
                    modules.presentation.CANNOT_USE_ACTION,
                )
                self.assertTrue(rejected.response.messages[0][1]["ephemeral"])

                accepted = FakeInteraction(role_ids=(99,))
                await dashboard_command.callback(accepted)
                self.assertEqual(
                    accepted.response.messages[0][0],
                    modules.presentation.DASHBOARD_TITLE,
                )
                self.assertTrue(accepted.response.messages[0][1]["ephemeral"])

                target = SimpleNamespace(id=500)
                profile_interaction = FakeInteraction(manage_messages=True)
                await profile_command.callback(profile_interaction, target)
                self.assertEqual(
                    profile_interaction.response.messages[0][0],
                    modules.presentation.NO_PROFILE,
                )
            finally:
                await cog.cog_unload()

            self.assertIsNone(
                bot.tree.get_command("github-tickets", type="chat_input")
            )
            self.assertIsNone(
                bot.tree.get_command("Developer Profile", type="user")
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

    async def test_channel_role_and_guild_deletions_cleanup_only_their_scopes(self):
        with isolated_githubtickets_modules(self.data_path) as modules:
            bot = FakeBot(ready=False)
            cog = modules.githubtickets.GitHubTickets(bot)
            guild_config = cog.config.guild_from_id(10)
            await guild_config.set_raw("ticket_channel_id", value=20)
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
            self.assertEqual(cleanup.author_id, 0)
            self.assertEqual(cleanup.pr_title, "")
            self.assertEqual(cleanup.pr_url, "")
            self.assertEqual(cleanup.category_display, "")
            self.assertEqual(cleanup.message_id, 40)
            self.assertEqual(cleanup.thread_id, 50)
            self.assertEqual(channel.message.delete_calls, 0)
            self.assertEqual(bot.fetch_calls, 0)

            thread = CachedThread(50)
            bot.channels[50] = thread
            result = await cog.coordinator.recover_projection_cleanup(
                created.ticket_id
            )

            self.assertTrue(result.success)
            self.assertIsNone(await cog.store.get_ticket(created.ticket_id))
            self.assertEqual(thread.delete_calls, 1)
            self.assertEqual(channel.message.delete_calls, 1)
            self.assertEqual(bot.fetch_calls, 0)
