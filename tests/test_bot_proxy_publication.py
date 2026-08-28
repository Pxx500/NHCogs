from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PACKAGE_NAME = "nhmisc_bot_proxy_publication_test_package"
PACKAGE_PATH = Path(__file__).parents[1] / "NHCogs" / "nhmisc"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package


def load_module(name: str):
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
    return module


bot_proxy_store = load_module("bot_proxy_store")
bot_proxy = load_module("bot_proxy")

BotProxyDraft = bot_proxy.BotProxyDraft
IdentityType = bot_proxy.IdentityType
ProxyDestination = bot_proxy.ProxyDestination
ProxyIdentity = bot_proxy.ProxyIdentity
ProxySender = bot_proxy_store.ProxySender


class FakeAllowedMentions:
    def __init__(
        self,
        *,
        everyone: bool,
        users: bool,
        roles: bool,
        replied_user: bool,
    ) -> None:
        self.everyone = everyone
        self.users = users
        self.roles = roles
        self.replied_user = replied_user

    @classmethod
    def none(cls):
        return cls(everyone=False, users=False, roles=False, replied_user=False)


def discord_module() -> types.ModuleType:
    module = types.ModuleType("discord")
    module.AllowedMentions = FakeAllowedMentions
    return module


def assert_user_only_mentions(test: unittest.TestCase, mentions: object) -> None:
    test.assertIsInstance(mentions, FakeAllowedMentions)
    test.assertFalse(mentions.everyone)
    test.assertTrue(mentions.users)
    test.assertFalse(mentions.roles)
    test.assertFalse(mentions.replied_user)


class BotProxyPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_preview_is_exact_untracked_and_suppresses_mentions(self) -> None:
        preview = SimpleNamespace(id=300)
        channel = SimpleNamespace(send=mock.AsyncMock(return_value=preview))
        store = SimpleNamespace(record_message=mock.AsyncMock())
        publisher = bot_proxy.BotProxyPublisher(store)
        draft = BotProxyDraft(
            destination=ProxyDestination(guild_id=100, channel_id=200),
            content="Hello <@123> @everyone",
        )

        with mock.patch.dict(sys.modules, {"discord": discord_module()}):
            result = await publisher.preview(draft=draft, channel=channel)

        self.assertIs(result, preview)
        kwargs = channel.send.await_args.kwargs
        self.assertEqual(kwargs["content"], draft.content)
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].users)
        store.record_message.assert_not_awaited()

    async def test_character_preview_uses_name_avatar_and_is_not_tracked(self) -> None:
        preview = SimpleNamespace(id=300)
        webhook = SimpleNamespace(id=900)
        webhook.edit = mock.AsyncMock(return_value=webhook)
        webhook.send = mock.AsyncMock(return_value=preview)
        parent = SimpleNamespace(id=200, webhooks=mock.AsyncMock(return_value=[webhook]))
        channel = SimpleNamespace(
            id=201,
            parent=parent,
            guild=SimpleNamespace(id=100),
        )
        store = SimpleNamespace(
            get_webhook_id=mock.AsyncMock(return_value=900),
            record_message=mock.AsyncMock(),
        )
        publisher = bot_proxy.BotProxyPublisher(store)
        draft = BotProxyDraft(
            destination=ProxyDestination(guild_id=100, channel_id=400),
            content="Character preview",
            identity=ProxyIdentity(
                IdentityType.CHARACTER,
                display_name="Guide",
                avatar_bytes=b"avatar",
            ),
        )

        with mock.patch.dict(sys.modules, {"discord": discord_module()}):
            result = await publisher.preview(draft=draft, channel=channel)

        self.assertIs(result, preview)
        webhook.edit.assert_awaited_once_with(avatar=b"avatar")
        kwargs = webhook.send.await_args.kwargs
        self.assertEqual(kwargs["username"], "Guide")
        self.assertIs(kwargs["thread"], channel)
        self.assertFalse(kwargs["allowed_mentions"].users)
        store.record_message.assert_not_awaited()

    async def test_bot_standalone_sends_with_user_only_mentions_and_tracks_message(
        self,
    ) -> None:
        published = SimpleNamespace(
            id=300,
            channel=SimpleNamespace(id=200),
            delete=mock.AsyncMock(),
        )
        channel = SimpleNamespace(
            id=200,
            guild=SimpleNamespace(id=100),
            send=mock.AsyncMock(return_value=published),
        )
        tracked = object()
        store = SimpleNamespace(record_message=mock.AsyncMock(return_value=tracked))
        publisher = bot_proxy.BotProxyPublisher(store)
        draft = BotProxyDraft(
            destination=ProxyDestination(guild_id=100, channel_id=200),
            content="Hello <@123> <@&456> @everyone",
            identity=ProxyIdentity(IdentityType.BOT),
        )

        with mock.patch.dict(sys.modules, {"discord": discord_module()}):
            result = await publisher.publish(
                draft=draft,
                moderator_id=400,
                channel=channel,
            )

        self.assertIs(result, published)
        channel.send.assert_awaited_once()
        send_kwargs = channel.send.await_args.kwargs
        self.assertEqual(send_kwargs["content"], draft.content)
        assert_user_only_mentions(self, send_kwargs["allowed_mentions"])
        tracked_kwargs = store.record_message.await_args.kwargs
        self.assertEqual(tracked_kwargs["guild_id"], 100)
        self.assertEqual(tracked_kwargs["channel_id"], 200)
        self.assertEqual(tracked_kwargs["message_id"], 300)
        self.assertEqual(tracked_kwargs["moderator_id"], 400)
        self.assertEqual(tracked_kwargs["sender"].value, "bot")
        self.assertIsNone(tracked_kwargs["webhook_id"])
        self.assertEqual(tracked_kwargs["content"], draft.content)
        self.assertIsNone(tracked_kwargs["reply_message_id"])

    async def test_bot_reply_fetches_source_and_never_mentions_its_author(self) -> None:
        published = SimpleNamespace(
            id=301,
            channel=SimpleNamespace(id=200),
            delete=mock.AsyncMock(),
        )
        source = SimpleNamespace(reply=mock.AsyncMock(return_value=published))
        channel = SimpleNamespace(
            id=200,
            guild=SimpleNamespace(id=100),
            fetch_message=mock.AsyncMock(return_value=source),
            send=mock.AsyncMock(),
        )
        store = SimpleNamespace(record_message=mock.AsyncMock(return_value=object()))
        publisher = bot_proxy.BotProxyPublisher(store)
        draft = BotProxyDraft(
            destination=ProxyDestination(
                guild_id=100,
                channel_id=200,
                message_id=250,
            ),
            content="A native reply",
        )

        with mock.patch.dict(sys.modules, {"discord": discord_module()}):
            result = await publisher.publish(
                draft=draft,
                moderator_id=400,
                channel=channel,
            )

        self.assertIs(result, published)
        channel.fetch_message.assert_awaited_once_with(250)
        channel.send.assert_not_awaited()
        source.reply.assert_awaited_once()
        reply_kwargs = source.reply.await_args.kwargs
        self.assertEqual(reply_kwargs["content"], "A native reply")
        self.assertFalse(reply_kwargs["mention_author"])
        assert_user_only_mentions(self, reply_kwargs["allowed_mentions"])
        tracked_kwargs = store.record_message.await_args.kwargs
        self.assertEqual(tracked_kwargs["message_id"], 301)
        self.assertEqual(tracked_kwargs["reply_message_id"], 250)

    async def test_tracking_failure_deletes_the_untracked_discord_message(self) -> None:
        published = SimpleNamespace(
            id=300,
            channel=SimpleNamespace(id=200),
            delete=mock.AsyncMock(),
        )
        channel = SimpleNamespace(
            id=200,
            guild=SimpleNamespace(id=100),
            send=mock.AsyncMock(return_value=published),
        )
        tracking_error = RuntimeError("tracking failed")
        store = SimpleNamespace(
            record_message=mock.AsyncMock(side_effect=tracking_error),
        )
        publisher = bot_proxy.BotProxyPublisher(store)
        draft = BotProxyDraft(
            destination=ProxyDestination(guild_id=100, channel_id=200),
            content="Published but not tracked",
        )

        with (
            mock.patch.dict(sys.modules, {"discord": discord_module()}),
            self.assertRaisesRegex(RuntimeError, "tracking failed") as raised,
        ):
            await publisher.publish(
                draft=draft,
                moderator_id=400,
                channel=channel,
            )

        self.assertIs(raised.exception, tracking_error)
        published.delete.assert_awaited_once_with()

    async def test_character_reuses_owned_webhook_and_clears_previous_avatar(self) -> None:
        published = SimpleNamespace(
            id=302,
            channel=SimpleNamespace(id=200),
            delete=mock.AsyncMock(),
        )
        webhook = SimpleNamespace(id=500)
        webhook.edit = mock.AsyncMock(return_value=webhook)
        webhook.send = mock.AsyncMock(return_value=published)
        channel = SimpleNamespace(
            id=200,
            guild=SimpleNamespace(id=100),
            webhooks=mock.AsyncMock(return_value=[webhook]),
            create_webhook=mock.AsyncMock(),
        )
        store = SimpleNamespace(
            get_webhook_id=mock.AsyncMock(return_value=500),
            forget_webhook=mock.AsyncMock(),
            remember_webhook=mock.AsyncMock(),
            record_message=mock.AsyncMock(return_value=object()),
        )
        publisher = bot_proxy.BotProxyPublisher(store)
        draft = BotProxyDraft(
            destination=ProxyDestination(guild_id=100, channel_id=200),
            content="Spoken as a character",
            identity=ProxyIdentity(
                IdentityType.CHARACTER,
                display_name="Narrator",
                preset_name="narrator",
            ),
        )

        with mock.patch.dict(sys.modules, {"discord": discord_module()}):
            result = await publisher.publish(
                draft=draft,
                moderator_id=400,
                channel=channel,
            )

        self.assertIs(result, published)
        store.get_webhook_id.assert_awaited_once_with(100, 200)
        channel.webhooks.assert_awaited_once_with()
        channel.create_webhook.assert_not_awaited()
        store.remember_webhook.assert_not_awaited()
        webhook.edit.assert_awaited_once_with(avatar=None)
        webhook.send.assert_awaited_once()
        send_kwargs = webhook.send.await_args.kwargs
        self.assertEqual(send_kwargs["content"], draft.content)
        self.assertEqual(send_kwargs["username"], "Narrator")
        self.assertTrue(send_kwargs["wait"])
        self.assertNotIn("thread", send_kwargs)
        assert_user_only_mentions(self, send_kwargs["allowed_mentions"])
        tracked_kwargs = store.record_message.await_args.kwargs
        self.assertEqual(tracked_kwargs["sender"].value, "character")
        self.assertEqual(tracked_kwargs["webhook_id"], 500)
        self.assertEqual(tracked_kwargs["character_preset_name"], "narrator")
        self.assertEqual(tracked_kwargs["character_display_name"], "Narrator")
        self.assertIsNone(tracked_kwargs["avatar_sha256"])

    async def test_character_creates_owned_parent_webhook_and_routes_to_thread(self) -> None:
        thread = SimpleNamespace(id=201, guild=SimpleNamespace(id=100))
        parent = SimpleNamespace(id=200, guild=thread.guild)
        thread.parent = parent
        published = SimpleNamespace(id=302, channel=thread, delete=mock.AsyncMock())
        webhook = SimpleNamespace(id=500)
        webhook.edit = mock.AsyncMock(return_value=webhook)
        webhook.send = mock.AsyncMock(return_value=published)
        parent.webhooks = mock.AsyncMock()
        parent.create_webhook = mock.AsyncMock(return_value=webhook)
        store = SimpleNamespace(
            get_webhook_id=mock.AsyncMock(return_value=None),
            forget_webhook=mock.AsyncMock(),
            remember_webhook=mock.AsyncMock(),
            record_message=mock.AsyncMock(return_value=object()),
        )
        publisher = bot_proxy.BotProxyPublisher(store)
        avatar = b"character-avatar"
        draft = BotProxyDraft(
            destination=ProxyDestination(guild_id=100, channel_id=201),
            content="Posted in a thread",
            identity=ProxyIdentity(
                IdentityType.CHARACTER,
                display_name="Guide",
                avatar_bytes=avatar,
                avatar_media_type="image/png",
            ),
        )

        with mock.patch.dict(sys.modules, {"discord": discord_module()}):
            await publisher.publish(
                draft=draft,
                moderator_id=400,
                channel=thread,
            )

        store.get_webhook_id.assert_awaited_once_with(100, 200)
        parent.webhooks.assert_not_awaited()
        parent.create_webhook.assert_awaited_once_with(name="Bot Proxy")
        store.remember_webhook.assert_awaited_once_with(100, 200, 500)
        webhook.edit.assert_awaited_once_with(avatar=avatar)
        self.assertIs(webhook.send.await_args.kwargs["thread"], thread)
        self.assertEqual(
            store.record_message.await_args.kwargs["avatar_sha256"],
            "8d3ff446b0d3e350144e3b958812bbae8a992bff4dc1b9ae996f4eb31f0f39f3",
        )

    async def test_character_publication_is_serialized_per_parent_channel(self) -> None:
        events: list[tuple[str, str, bytes | None]] = []

        class Webhook:
            id = 500
            avatar: bytes | None = None

            async def edit(self, *, avatar: bytes | None):
                self.avatar = avatar
                await asyncio.sleep(0)
                return self

            async def send(self, *, username: str, **_kwargs):
                events.append(("send", username, self.avatar))
                await asyncio.sleep(0)
                return SimpleNamespace(
                    id=300 + len(events),
                    channel=channel,
                    delete=mock.AsyncMock(),
                )

        webhook = Webhook()
        channel = SimpleNamespace(id=200, guild=SimpleNamespace(id=100))
        channel.webhooks = mock.AsyncMock(return_value=[webhook])
        channel.create_webhook = mock.AsyncMock()
        store = SimpleNamespace(
            get_webhook_id=mock.AsyncMock(return_value=500),
            forget_webhook=mock.AsyncMock(),
            remember_webhook=mock.AsyncMock(),
            record_message=mock.AsyncMock(return_value=object()),
        )
        publisher = bot_proxy.BotProxyPublisher(store)

        def draft(name: str, avatar: bytes) -> BotProxyDraft:
            return BotProxyDraft(
                destination=ProxyDestination(guild_id=100, channel_id=200),
                content=name,
                identity=ProxyIdentity(
                    IdentityType.CHARACTER,
                    display_name=name,
                    avatar_bytes=avatar,
                    avatar_media_type="image/png",
                ),
            )

        with mock.patch.dict(sys.modules, {"discord": discord_module()}):
            await asyncio.gather(
                publisher.publish(
                    draft=draft("First", b"first"),
                    moderator_id=400,
                    channel=channel,
                ),
                publisher.publish(
                    draft=draft("Second", b"second"),
                    moderator_id=401,
                    channel=channel,
                ),
            )

        self.assertEqual(
            events,
            [
                ("send", "First", b"first"),
                ("send", "Second", b"second"),
            ],
        )

if __name__ == "__main__":
    unittest.main()
