from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "NHCogs"
PACKAGE_PATH = ROOT / PACKAGE_NAME
GITHUBTICKETS_PACKAGE_NAME = f"{PACKAGE_NAME}.githubtickets"
GITHUBTICKETS_PACKAGE_PATH = PACKAGE_PATH / "githubtickets"


def _install_discord_stub():
    discord = types.ModuleType("discord")

    class NotFound(Exception):
        pass

    class AllowedMentions:
        def __init__(
            self,
            *,
            everyone=True,
            users=True,
            roles=True,
            replied_user=True,
        ):
            self.everyone = everyone
            self.users = users
            self.roles = roles
            self.replied_user = replied_user

        @classmethod
        def none(cls):
            return cls(everyone=False, users=False, roles=False, replied_user=False)

    class Object:
        def __init__(self, *, id):
            self.id = id

    ui = types.ModuleType("discord.ui")
    ui.View = type("View", (), {})
    discord.ui = ui
    discord.NotFound = NotFound
    discord.AllowedMentions = AllowedMentions
    discord.Object = Object
    sys.modules["discord"] = discord
    sys.modules["discord.ui"] = ui
    return discord


discord = _install_discord_stub()


def _load_modules():
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules[PACKAGE_NAME] = package

    githubtickets_package = sys.modules.get(GITHUBTICKETS_PACKAGE_NAME)
    if githubtickets_package is None:
        githubtickets_package = types.ModuleType(GITHUBTICKETS_PACKAGE_NAME)
        githubtickets_package.__path__ = [str(GITHUBTICKETS_PACKAGE_PATH)]
        sys.modules[GITHUBTICKETS_PACKAGE_NAME] = githubtickets_package

    models = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.models")
    presentation = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.presentation")
    projection = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.projection")
    adapter = importlib.import_module(f"{GITHUBTICKETS_PACKAGE_NAME}.discord_projection")
    return models, presentation, projection, adapter


models, presentation, projection_module, adapter_module = _load_modules()


def ticket(**changes):
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    values = {
        "ticket_id": 7,
        "guild_id": 10,
        "channel_id": 20,
        "message_id": None,
        "thread_id": None,
        "author_id": 30,
        "pr_title": "Fix shader cache invalidation",
        "pr_url": "https://github.com/example/repository/pull/123",
        "category_display": "rendering, performance",
        "routing_mode": models.RoutingMode.AUTOMATIC,
        "state": models.TicketState.CREATING,
        "direct_target_id": None,
        "current_target_id": None,
        "assignee_id": None,
        "ping_count": 0,
        "protection_until": None,
        "next_action": None,
        "next_action_at": None,
        "pending_target_id": None,
        "pending_presence_tier": None,
        "pending_ping_automatic": None,
        "pending_response_deadline": None,
        "created_at": now,
        "updated_at": now,
        "transition_version": 0,
        "category_ids": (1, 2),
    }
    values.update(changes)
    return models.Ticket(**values)


class FakeThread:
    def __init__(self, thread_id):
        self.id = thread_id
        self.send_calls = []
        self.delete_calls = 0
        self.error = None

    async def send(self, content, **kwargs):
        if self.error is not None:
            raise self.error
        self.send_calls.append((content, kwargs))

    async def delete(self):
        if self.error is not None:
            raise self.error
        self.delete_calls += 1


class FakeMessage:
    def __init__(self, message_id=55):
        self.id = message_id
        self.created_thread_names = []
        self.edit_calls = []
        self.delete_calls = 0
        self.error = None

    async def create_thread(self, *, name):
        if self.error is not None:
            raise self.error
        self.created_thread_names.append(name)
        return FakeThread(66)

    async def edit(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.edit_calls.append(kwargs)

    async def delete(self):
        if self.error is not None:
            raise self.error
        self.delete_calls += 1


class FakeChannel:
    def __init__(self, channel_id=20):
        self.id = channel_id
        self.message = FakeMessage()
        self.send_calls = []
        self.fetch_calls = 0
        self.partial_calls = []
        self.error = None
        self.history_messages = []
        self.history_after = []
        self.fetched_message = None

    async def send(self, content, **kwargs):
        if self.error is not None:
            raise self.error
        self.send_calls.append((content, kwargs))
        return self.message

    def get_partial_message(self, message_id):
        self.partial_calls.append(message_id)
        self.message.id = message_id
        return self.message

    async def fetch_message(self, _message_id):
        self.fetch_calls += 1
        if self.fetched_message is not None:
            return self.fetched_message
        raise AssertionError("fetch_message must not be called")

    async def history(self, *, after, oldest_first, limit):
        self.history_after.append(after)
        for message in self.history_messages[:limit]:
            if message.created_at > after:
                yield message


class FakeBot:
    def __init__(self, channels):
        self.channels = channels
        self.fetch_calls = 0
        self.partial_messageables = {}
        self.fetched_channels = {}
        self.user = types.SimpleNamespace(id=999)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_partial_messageable(self, channel_id):
        if channel_id in self.channels:
            return self.channels[channel_id]
        return self.partial_messageables.setdefault(channel_id, FakeThread(channel_id))

    async def fetch_channel(self, channel_id):
        self.fetch_calls += 1
        if channel_id in self.fetched_channels:
            return self.fetched_channels[channel_id]
        raise AssertionError("fetch_channel must not be called")


class DiscordTicketProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_history_uses_lookback_but_keeps_exact_correlations(self):
        now = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
        channel = FakeChannel()
        thread = FakeChannel(66)
        bot = FakeBot({channel.id: channel, thread.id: thread})
        adapter = adapter_module.DiscordTicketProjection(bot, lambda _ticket: object())
        current = ticket(created_at=now, public_token="opaque-token")
        main = types.SimpleNamespace(
            id=77,
            created_at=now - timedelta(seconds=2),
            author=types.SimpleNamespace(id=bot.user.id),
            components=(
                types.SimpleNamespace(
                    children=(
                        types.SimpleNamespace(
                            custom_id="githubtickets:opaque-token:claim"
                        ),
                    )
                ),
            ),
            content="unrelated visible copy",
        )
        ping = types.SimpleNamespace(
            id=78,
            created_at=now - timedelta(seconds=2),
            author=types.SimpleNamespace(id=bot.user.id),
            components=(),
            content=presentation.automatic_review_notification("<@41>"),
        )
        channel.history_messages = [main]
        thread.history_messages = [ping]

        message_id = await adapter.find_ticket_message(current)
        ping_at = await adapter.find_ping(66, 41, True, now)

        self.assertEqual(message_id, 77)
        self.assertEqual(ping_at, ping.created_at)
        self.assertLess(channel.history_after[0], now)
        self.assertLess(thread.history_after[0], now)

    async def test_thread_recovery_fetches_saved_message_only_in_recovery_path(self):
        channel = FakeChannel()
        channel.fetched_message = types.SimpleNamespace(
            thread=types.SimpleNamespace(id=88)
        )
        adapter = adapter_module.DiscordTicketProjection(
            FakeBot({channel.id: channel}),
            lambda _ticket: object(),
        )

        thread_id = await adapter.find_ticket_thread(ticket(message_id=55))

        self.assertEqual(thread_id, 88)
        self.assertEqual(channel.fetch_calls, 1)

    async def test_send_and_thread_creation_use_exact_projection_without_fetches(self):
        channel = FakeChannel()
        bot = FakeBot({channel.id: channel})
        view = object()
        view_tickets = []

        def view_factory(current):
            view_tickets.append((current.ticket_id, current.state is models.TicketState.CLAIMED))
            return view

        adapter = adapter_module.DiscordTicketProjection(bot, view_factory)
        current = ticket(pr_title="x" * 101)

        message_id = await adapter.send_ticket(current)
        thread_id = await adapter.create_thread(current, message_id)

        self.assertEqual(message_id, 55)
        self.assertEqual(thread_id, 66)
        self.assertEqual(view_tickets, [(current.ticket_id, False)])
        self.assertEqual(len(channel.send_calls), 1)
        content, kwargs = channel.send_calls[0]
        self.assertEqual(
            content,
            presentation.ticket_message(
                title=current.pr_title,
                url=current.pr_url,
                author_mention="<@30>",
                categories=("rendering, performance",),
            ),
        )
        self.assertIs(kwargs["view"], view)
        allowed_mentions = kwargs["allowed_mentions"]
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.replied_user)
        self.assertEqual(channel.message.created_thread_names, [presentation.thread_name(current.pr_title)])
        self.assertEqual(channel.fetch_calls, 0)
        self.assertEqual(bot.fetch_calls, 0)

    async def test_edit_uses_one_partial_message_mutation_with_claimed_copy_and_view(self):
        channel = FakeChannel()
        bot = FakeBot({channel.id: channel})
        view = object()
        view_tickets = []

        def view_factory(current):
            view_tickets.append((current.ticket_id, current.state is models.TicketState.CLAIMED))
            return view

        adapter = adapter_module.DiscordTicketProjection(bot, view_factory)
        current = ticket(
            message_id=55,
            state=models.TicketState.CLAIMED,
            assignee_id=40,
        )

        await adapter.edit_ticket(current, reviewer_github="nova-dev")

        self.assertEqual(channel.partial_calls, [55])
        self.assertEqual(len(channel.message.edit_calls), 1)
        self.assertEqual(
            channel.message.edit_calls[0]["content"],
            presentation.ticket_message(
                    title=current.pr_title,
                    url=current.pr_url,
                    author_mention="<@30>",
                    categories=("rendering, performance",),
                    reviewer_mention="<@40>",
                    reviewer_github="nova-dev",
            ),
        )
        self.assertIs(channel.message.edit_calls[0]["view"], view)
        allowed_mentions = channel.message.edit_calls[0]["allowed_mentions"]
        self.assertFalse(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.everyone)
        self.assertEqual(view_tickets, [(current.ticket_id, True)])
        self.assertEqual(channel.fetch_calls, 0)
        self.assertEqual(bot.fetch_calls, 0)

    async def test_open_ticket_edit_displays_the_current_target_and_github_username(self):
        channel = FakeChannel()
        bot = FakeBot({channel.id: channel})
        adapter = adapter_module.DiscordTicketProjection(
            bot,
            lambda _ticket: object(),
        )
        current = ticket(
            message_id=55,
            state=models.TicketState.OPEN,
            current_target_id=41,
        )

        await adapter.edit_ticket(current, reviewer_github="reviewer-gh")

        self.assertEqual(
            channel.message.edit_calls[0]["content"],
            presentation.ticket_message(
                title=current.pr_title,
                url=current.pr_url,
                author_mention="<@30>",
                categories=("rendering, performance",),
                reviewer_mention="<@41>",
                reviewer_github="reviewer-gh",
            ),
        )

    async def test_ping_uses_exact_copy_and_allows_only_the_new_target_mention(self):
        thread = FakeThread(66)
        bot = FakeBot({thread.id: thread})
        adapter = adapter_module.DiscordTicketProjection(bot, lambda _ticket: object())

        await adapter.ping_reviewer(66, 41, False)
        await adapter.ping_reviewer(66, 42, True)

        self.assertEqual(
            [content for content, _kwargs in thread.send_calls],
            [
                presentation.direct_review_notification("<@41>"),
                presentation.automatic_review_notification("<@42>"),
            ],
        )
        for expected_id, (_content, kwargs) in zip((41, 42), thread.send_calls, strict=True):
            allowed_mentions = kwargs["allowed_mentions"]
            self.assertFalse(allowed_mentions.everyone)
            self.assertFalse(allowed_mentions.roles)
            self.assertFalse(allowed_mentions.replied_user)
            self.assertEqual([user.id for user in allowed_mentions.users], [expected_id])
        self.assertEqual(bot.fetch_calls, 0)

    async def test_deletes_use_saved_ids_partial_message_and_cached_thread(self):
        channel = FakeChannel()
        thread = FakeThread(66)
        bot = FakeBot({channel.id: channel, thread.id: thread})
        adapter = adapter_module.DiscordTicketProjection(bot, lambda _ticket: object())

        await adapter.delete_message(channel.id, 55)
        await adapter.delete_thread(thread.id)

        self.assertEqual(channel.partial_calls, [55])
        self.assertEqual(channel.message.delete_calls, 1)
        self.assertEqual(thread.delete_calls, 1)
        self.assertEqual(channel.fetch_calls, 0)
        self.assertEqual(bot.fetch_calls, 0)

    async def test_required_mutation_not_found_is_mapped_to_projection_not_found(self):
        error = discord.NotFound("controlled absence")

        with self.subTest(operation="send"):
            channel = FakeChannel()
            channel.error = error
            adapter = adapter_module.DiscordTicketProjection(
                FakeBot({channel.id: channel}), lambda _ticket: object()
            )
            with self.assertRaises(projection_module.ProjectionNotFound):
                await adapter.send_ticket(ticket())

        for operation in ("create_thread", "edit_ticket", "delete_message"):
            with self.subTest(operation=operation):
                channel = FakeChannel()
                channel.message.error = error
                adapter = adapter_module.DiscordTicketProjection(
                    FakeBot({channel.id: channel}), lambda _ticket: object()
                )
                if operation == "create_thread":
                    with self.assertRaises(projection_module.ProjectionNotFound):
                        await adapter.create_thread(ticket(), 55)
                elif operation == "edit_ticket":
                    with self.assertRaises(projection_module.ProjectionNotFound):
                        await adapter.edit_ticket(ticket(message_id=55))
                else:
                    with self.assertRaises(projection_module.ProjectionNotFound):
                        await adapter.delete_message(channel.id, 55)

        for operation in ("ping_reviewer", "delete_thread"):
            with self.subTest(operation=operation):
                thread = FakeThread(66)
                thread.error = error
                adapter = adapter_module.DiscordTicketProjection(
                    FakeBot({thread.id: thread}), lambda _ticket: object()
                )
                if operation == "ping_reviewer":
                    with self.assertRaises(projection_module.ProjectionNotFound):
                        await adapter.ping_reviewer(thread.id, 41, True)
                else:
                    with self.assertRaises(projection_module.ProjectionNotFound):
                        await adapter.delete_thread(thread.id)

    async def test_cache_miss_uses_partial_ping_and_targeted_thread_delete_fetch(self):
        bot = FakeBot({})
        archived = FakeThread(66)
        bot.fetched_channels[66] = archived
        adapter = adapter_module.DiscordTicketProjection(bot, lambda _ticket: object())

        with self.assertRaises(projection_module.ProjectionUnavailable):
            await adapter.send_ticket(ticket())
        await adapter.ping_reviewer(67, 41, True)
        await adapter.delete_thread(66)

        self.assertEqual(len(bot.partial_messageables[67].send_calls), 1)
        self.assertEqual(archived.delete_calls, 1)
        self.assertEqual(bot.fetch_calls, 1)
