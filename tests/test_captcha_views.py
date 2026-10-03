"""Private CAPTCHA interactions through Discord view callbacks."""

import asyncio
import importlib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules


def interaction(user_id=20, guild_id=10):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=SimpleNamespace(id=guild_id),
        response=SimpleNamespace(defer=mock.AsyncMock()),
        edit_original_response=mock.AsyncMock(),
        message=SimpleNamespace(edit=mock.AsyncMock()),
    )


class CaptchaViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_practice_rejects_replayed_answers_and_stops_after_two_failed_attempts(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)):
            views = importlib.import_module("NHCogs.honeypot.captcha_views")
            captcha = importlib.import_module("NHCogs.honeypot.captcha")
            rng = SimpleNamespace(choice=lambda choices: choices[0], randint=lambda low, high: low, randrange=lambda stop: 2, shuffle=lambda items: None)
            # No enrollment/release owner is supplied: practice can't require one.
            invitation = views.CaptchaPracticeView(object(), 10, 20)
            with mock.patch.object(captcha.random, "SystemRandom", return_value=rng):
                click = interaction()
                await invitation.children[0].callback(click)
                first = click.edit_original_response.await_args.kwargs["view"]
                click = interaction()
                await first.children[2].callback(click)
                second = click.edit_original_response.await_args.kwargs["view"]
                replay = interaction()
                await first.children[0].callback(replay)
                replay.edit_original_response.assert_not_awaited()
                click = interaction()
                await second.children[0].callback(click)
                retry = click.edit_original_response.await_args.kwargs["view"]
                self.assertEqual(len(retry.children), 1)
                click = interaction()
                await retry.children[0].callback(click)
                payload = click.edit_original_response.await_args.kwargs
                self.assertIn("1 of 2", payload["content"])
                click = interaction()
                await payload["view"].children[0].callback(click)
                result = click.edit_original_response.await_args.kwargs
                self.assertIsNone(result["view"])
                self.assertEqual(result["attachments"], [])
                self.assertIn("attempts", result["content"])
                self.assertIn("No restrictions", result["content"])

    async def test_verify_defers_before_loading_a_question(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                views = importlib.import_module("NHCogs.honeypot.captcha_views")
                click = interaction()

                async def start(member):
                    click.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                    return SimpleNamespace(status="preparing", deadline=None)

                owner = SimpleNamespace(start=mock.AsyncMock(side_effect=start))
                panel = views.VerifyPanelView(SimpleNamespace(_joinwatch_verification=owner))
                await panel.children[0].callback(click)
                owner.start.assert_awaited_once_with(click.user)
                click.edit_original_response.assert_awaited_once()
                self.assertIn("preparing", click.edit_original_response.await_args.kwargs["content"].lower())

    async def test_question_is_private_numbered_and_rejects_other_participants(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                views = importlib.import_module("NHCogs.honeypot.captcha_views")
                owner = SimpleNamespace(submit=mock.AsyncMock())
                cog = SimpleNamespace(_joinwatch_verification=owner)
                result = SimpleNamespace(session_id="opaque-session", stage=0, incident_id="incident")
                view = views.CaptchaQuestionView(cog, 10, 20, result)
                self.assertEqual([button.label for button in view.children], list("123456"))
                self.assertEqual([button.row for button in view.children], [0, 0, 0, 1, 1, 1])
                self.assertEqual(len({button.custom_id for button in view.children}), 6)
                wrong = interaction(user_id=21)
                await view.children[0].callback(wrong)
                owner.submit.assert_not_awaited()
                wrong.response.defer.assert_awaited_once_with(ephemeral=True)

    async def test_parallel_answers_consume_only_one_stage_and_update_the_private_reply(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                views = importlib.import_module("NHCogs.honeypot.captcha_views")
                started, finish = asyncio.Event(), asyncio.Event()
                result = SimpleNamespace(status="question", session_id="next-session", stage=1, incident_id="same-incident", question=SimpleNamespace(prompt="Count circles", image_png=b"png"))

                async def submit(*args):
                    started.set()
                    await finish.wait()
                    return result

                owner = SimpleNamespace(submit=mock.AsyncMock(side_effect=submit))
                view = views.CaptchaQuestionView(SimpleNamespace(_joinwatch_verification=owner), 10, 20, SimpleNamespace(session_id="first-session", stage=0))
                first, second = interaction(), interaction()
                pending = asyncio.create_task(view.children[0].callback(first))
                await started.wait()
                duplicate = asyncio.create_task(view.children[1].callback(second))
                await asyncio.sleep(0)
                second.response.defer.assert_awaited_once_with(ephemeral=True)
                finish.set()
                await asyncio.gather(pending, duplicate)
                owner.submit.assert_awaited_once_with(first.user, "first-session", 0, 0)
                first.edit_original_response.assert_awaited_once()
                self.assertIn("2 of 2", first.edit_original_response.await_args.kwargs["content"])
                self.assertEqual(first.edit_original_response.await_args.kwargs["attachments"][0].filename, "quick-check.png")
                second.edit_original_response.assert_not_awaited()

    async def test_exhausted_attempts_show_dm_and_real_deadline_without_controls(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                views = importlib.import_module("NHCogs.honeypot.captcha_views")
                click = interaction()
                deadline = datetime(2026, 10, 3, 16, tzinfo=timezone.utc)
                await views.show_result(object(), click, SimpleNamespace(status="locked", deadline=deadline))
                payload = click.edit_original_response.await_args.kwargs
                self.assertIn("DM a moderator", payload["content"])
                self.assertIn(str(int(deadline.timestamp())), payload["content"])
                self.assertIsNone(payload["view"])
                self.assertNotIn("JoinWatch", payload["content"])
