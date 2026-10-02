import importlib.util
import json
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zipfile import ZipFile

MODULE_PATH = Path(__file__).parents[1] / "NHCogs" / "honeypot" / "ban_research.py"
SPEC = importlib.util.spec_from_file_location("honeypot_ban_research_test", MODULE_PATH)
ban_research = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ban_research
SPEC.loader.exec_module(ban_research)

BOT = 100000000000000001
SUBJECT = 100000000000000002
MODERATOR = 100000000000000003
ROLE = 100000000000000004
START = datetime(2026, 9, 1, tzinfo=timezone.utc)


class LogChannel:
    def __init__(self, channel_id, messages):
        self.id = channel_id
        self.messages = messages
        self.history_calls = []
        self.guild = SimpleNamespace(roles=[SimpleNamespace(id=ROLE, name="French")])

    def history(self, **kwargs):
        self.history_calls.append(kwargs)

        async def iterator():
            for message in sorted(self.messages, key=lambda m: m.created_at):
                if message.created_at < kwargs["before"]:
                    yield message

        return iterator()


def message(number, minutes, embed, *, author_id=BOT):
    return SimpleNamespace(
        id=number,
        author=SimpleNamespace(id=author_id),
        created_at=START + timedelta(minutes=minutes),
        jump_url=f"https://discord.com/channels/10/20/{number}",
        content="",
        embeds=[SimpleNamespace(to_dict=lambda: embed)],
    )


def ban_embed(user_id=SUBJECT, *, case=1, kind="Ban"):
    return {
        "title": f"Case #{case} | {kind}",
        "author": {
            "name": f"Example ({MODERATOR}) User ({user_id})",
            "icon_url": "https://cdn.test/avatar",
        },
        "fields": [
            {"name": "Moderator", "value": f"Mod ({MODERATOR})"},
            {"name": "Reason", "value": "Scam"},
        ],
    }


def member_embed(description="", *, user_id=SUBJECT, joined=False):
    return {
        "author": {
            "name": f"Example User ({user_id}) "
            + ("has joined the guild" if joined else "updated"),
            "icon_url": "https://cdn.test/avatar",
        },
        "description": description,
        "fields": [
            {"name": "Member ID", "value": f"```{user_id}```"},
            {"name": "Updated by", "value": f"<@{MODERATOR}>"},
        ],
    }


def read_export(result):
    contents = {}
    for path in result.archives:
        with ZipFile(path) as archive:
            for name in archive.namelist():
                contents[name] = contents.get(name, b"") + archive.read(name)
    return contents


class BanResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_normal_bans_enter_the_export_cohort(self):
        kinds = ("Ban", "Hackban", "Tempban", "Softban")
        moderation = LogChannel(
            20,
            [
                message(
                    index + 1,
                    5 + index,
                    ban_embed(
                        user_id=SUBJECT + index,
                        case=index + 1,
                        kind=kind,
                    ),
                )
                for index, kind in enumerate(kinds)
            ],
        )
        members = LogChannel(
            30,
            [
                message(index + 10, 1, member_embed(user_id=SUBJECT + index, joined=True))
                for index in range(len(kinds))
            ],
        )
        with TemporaryDirectory() as directory:
            result = await ban_research.export_ban_research(
                moderation,
                members,
                Path(directory),
                bot_id=BOT,
                upload_limit=100_000,
                cutoff=START + timedelta(days=1),
            )
            self.assertEqual(result.account_count, 1)
            self.assertEqual(result.ban_count, 1)
            self.assertEqual(result.member_event_count, 1)
            data = read_export(result)
            account = json.loads(data["accounts.jsonl"])
            self.assertEqual(account["user_id"], str(SUBJECT))
            self.assertEqual([ban["kind"] for ban in account["bans"]], ["Ban"])
            self.assertEqual(len(data["moderation-events.jsonl"].splitlines()), 1)
            self.assertEqual(len(data["member-events.jsonl"].splitlines()), 1)

    async def test_export_matches_banned_subject_and_reconstructs_observed_roles(self):
        moderation = LogChannel(
            20,
            [
                message(1, 5, ban_embed()),
                message(2, 6, ban_embed(kind="Kick", case=2)),
                message(3, 7, ban_embed(kind="Unban", case=3)),
                message(4, 8, ban_embed(user_id=MODERATOR, case=4), author_id=SUBJECT),
            ],
        )
        members = LogChannel(
            30,
            [
                message(10, 1, member_embed(joined=True)),
                message(11, 2, member_embed(f"<@{SUBJECT}> had the <@&{ROLE}> role applied.")),
                message(12, 9, member_embed(f"<@{SUBJECT}> had the <@&{ROLE}> role removed.")),
                message(13, 3, member_embed(user_id=MODERATOR)),
            ],
        )
        with TemporaryDirectory() as directory:
            result = await ban_research.export_ban_research(
                moderation,
                members,
                Path(directory),
                bot_id=BOT,
                upload_limit=100_000,
                cutoff=START + timedelta(days=1),
            )

            self.assertEqual(result.account_count, 1)
            self.assertEqual(result.ban_count, 1)
            self.assertEqual(result.member_event_count, 3)
            data = read_export(result)
            account = json.loads(data["accounts.jsonl"])
            self.assertEqual(account["user_id"], str(SUBJECT))
            self.assertEqual(account["bans"][0]["known_role_ids_at_ban"], [str(ROLE)])
            self.assertFalse(account["bans"][0]["role_snapshot_complete"])
            self.assertEqual(
                account["bans"][0]["joined_at"], (START + timedelta(minutes=1)).isoformat()
            )
            raw_ban = json.loads(data["moderation-events.jsonl"])
            self.assertEqual(raw_ban["embeds"][0], ban_embed())
            self.assertEqual(len(data["member-events.jsonl"].splitlines()), 3)
            self.assertEqual(
                moderation.history_calls,
                [{"limit": None, "oldest_first": True, "before": START + timedelta(days=1)}],
            )
            self.assertEqual(len(members.history_calls), 1)
            labels = json.loads(data["metadata.json"])["current_role_labels"]
            self.assertEqual(labels[str(ROLE)], "French")

    async def test_rejoin_resets_roles_and_duplicate_cases_keep_raw_provenance(self):
        moderation = LogChannel(
            20,
            [
                message(1, 5, ban_embed()),
                message(2, 6, ban_embed()),
                message(3, 10, ban_embed(case=2)),
            ],
        )
        members = LogChannel(
            30,
            [
                message(10, 1, member_embed(joined=True)),
                message(11, 2, member_embed(f"<@{SUBJECT}> had the <@&{ROLE}> role applied.")),
                message(12, 8, member_embed(joined=True)),
                message(13, 9, member_embed(f"<@{SUBJECT}> had the <@&{ROLE + 1}> role applied.")),
            ],
        )
        with TemporaryDirectory() as directory:
            result = await ban_research.export_ban_research(
                moderation,
                members,
                Path(directory),
                bot_id=BOT,
                upload_limit=100_000,
                cutoff=START + timedelta(days=1),
            )
            data = read_export(result)
            account = json.loads(data["accounts.jsonl"])
            self.assertEqual(result.ban_count, 2)
            self.assertEqual(
                [ban["known_role_ids_at_ban"] for ban in account["bans"]],
                [[str(ROLE)], [str(ROLE + 1)]],
            )
            self.assertEqual(len(data["moderation-events.jsonl"].splitlines()), 3)
            self.assertEqual(
                account["bans"][1]["joined_at"], (START + timedelta(minutes=8)).isoformat()
            )

    async def test_timestamp_provenance_and_missing_history_are_explicit(self):
        embed = ban_embed()
        embed["timestamp"] = (START + timedelta(minutes=5)).isoformat()
        moderation = LogChannel(
            20, [message(1, 10, embed), message(2, 20, ban_embed(user_id=ROLE, case=2))]
        )
        members = LogChannel(
            30,
            [
                message(10, 8, member_embed(f"<@{SUBJECT}> had the <@&{ROLE}> role applied.")),
            ],
        )
        with TemporaryDirectory() as directory:
            result = await ban_research.export_ban_research(
                moderation,
                members,
                Path(directory),
                bot_id=BOT,
                upload_limit=100_000,
                cutoff=START + timedelta(days=1),
            )
            accounts = [
                json.loads(line) for line in read_export(result)["accounts.jsonl"].splitlines()
            ]
            for account in accounts:
                ban = account["bans"][0]
                self.assertEqual(ban["known_role_ids_at_ban"], [])
                self.assertIsNone(ban["last_member_observation"])
                self.assertFalse(ban["role_snapshot_complete"])
            self.assertEqual(accounts[0]["bans"][0]["time_basis"], "embed_timestamp")
            self.assertEqual(accounts[1]["bans"][0]["time_basis"], "message_created_at")

    async def test_large_exports_split_without_losing_source_records(self):
        rng = random.Random(42)
        messages = []
        for index in range(50):
            embed = ban_embed(user_id=SUBJECT + index, case=index + 1)
            embed["fields"][1]["value"] = rng.randbytes(200).hex()
            messages.append(message(index + 1, index, embed))
        with TemporaryDirectory() as directory:
            result = await ban_research.export_ban_research(
                LogChannel(20, messages),
                LogChannel(30, []),
                Path(directory),
                bot_id=BOT,
                upload_limit=6000,
                cutoff=START + timedelta(days=1),
            )
            self.assertGreater(len(result.archives), 1)
            self.assertTrue(all(path.stat().st_size <= 6000 for path in result.archives))
            data = read_export(result)
            accounts = [json.loads(line) for line in data["accounts.jsonl"].splitlines()]
            originals = [json.loads(line) for line in data["moderation-events.jsonl"].splitlines()]
            self.assertEqual(
                {account["user_id"] for account in accounts},
                {str(SUBJECT + index) for index in range(50)},
            )
            self.assertEqual(
                [item["embeds"][0] for item in originals],
                [item.embeds[0].to_dict() for item in messages],
            )
            self.assertEqual(data["member-events.jsonl"], b"")
            self.assertEqual(json.loads(data["metadata.json"])["ban_count"], 50)
