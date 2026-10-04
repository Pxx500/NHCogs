"""Private CAPTCHA interactions through Discord view callbacks."""

import asyncio
import copy
import importlib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.harness import _isolated_honeypot_modules
from tests.test_joinwatch_verification import _runtime


def interaction(user_id=20, guild_id=10, *, cog=None):
    return SimpleNamespace(
        id=12345,
        guild_id=guild_id,
        created_at=datetime.now(timezone.utc),
        client=SimpleNamespace(get_cog=lambda name: cog if name == "Honeypot" else None),
        user=SimpleNamespace(id=user_id),
        guild=SimpleNamespace(id=guild_id),
        response=SimpleNamespace(defer=mock.AsyncMock()),
        edit_original_response=mock.AsyncMock(),
        message=SimpleNamespace(edit=mock.AsyncMock()),
    )


class CaptchaViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_verify_retry_and_answer_buttons_use_ready_cog_after_reload(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as honeypot:
            views = importlib.import_module("NHCogs.honeypot.captcha_views")
            old, current = _runtime(honeypot), _runtime(honeypot)
            old.cog._joinwatch_verification = old.owner
            current.cog._joinwatch_verification = current.owner
            try:
                await old.owner.start(old.member)
                await old.owner.preparation.wait()
                panel = views.VerifyPanelView(old.cog)
                retry = views.CaptchaRetryView(old.cog, 10, 20)
                click = interaction(cog=old.cog)
                click.user = old.member
                await panel.children[0].callback(click)
                old_question = click.edit_original_response.await_args.kwargs["view"]
                current.raw.clear()
                current.raw.update(copy.deepcopy(old.raw))
                await old.owner.close()
                await current.owner.start(current.member)
                await current.owner.preparation.wait()
                self.assertTrue((await current.owner.inspect(current.member))["ready"])
                for view in (panel, retry):
                    click = interaction(cog=current.cog)
                    click.user = current.member
                    await view.children[0].callback(click)
                    self.assertIn("1 of 2", click.edit_original_response.await_args.kwargs["content"])
                click = interaction(cog=current.cog)
                click.user = current.member
                await old_question.children[2].callback(click)
                self.assertIn("2 of 2", click.edit_original_response.await_args.kwargs["content"])
            finally:
                await old.owner.close()
                await current.owner.close()

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
                payload = click.edit_original_response.await_args.kwargs
                self.assertEqual(payload["embed"].image.url, "attachment://quick-check.webp")
                self.assertEqual(payload["attachments"][0].filename, "quick-check.webp")
                first = payload["view"]
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
                self.assertIsNone(result["embed"])
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
                cog = SimpleNamespace(_joinwatch_verification=owner)
                click.client.get_cog = lambda name: cog
                panel = views.VerifyPanelView(cog)
                with self.assertLogs("red.Honeypot.captcha", level="INFO") as captured:
                    await panel.children[0].callback(click)
                for event in ("received", "acknowledged", "reply sending", "reply sent"):
                    self.assertTrue(any(f"CAPTCHA {event}:" in line for line in captured.output))
                owner.start.assert_awaited_once_with(click.user)
                click.edit_original_response.assert_awaited_once()
                self.assertIn("preparing", click.edit_original_response.await_args.kwargs["content"].lower())

    async def test_failed_acknowledgement_is_logged_before_question_work(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)):
            views = importlib.import_module("NHCogs.honeypot.captcha_views")
            click = interaction()
            click.response.defer.side_effect = RuntimeError("Connection lost")
            owner = SimpleNamespace(start=mock.AsyncMock())
            panel = views.VerifyPanelView(SimpleNamespace(_joinwatch_verification=owner))
            with self.assertLogs("red.Honeypot.captcha", level="INFO") as captured:
                with self.assertRaisesRegex(RuntimeError, "Connection lost"):
                    await panel.children[0].callback(click)
            self.assertTrue(any("acknowledgement failed" in line for line in captured.output))
            owner.start.assert_not_awaited()

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
                wrong.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)

    async def test_parallel_answers_consume_only_one_stage_and_update_the_private_reply(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)):
                views = importlib.import_module("NHCogs.honeypot.captcha_views")
                started, finish = asyncio.Event(), asyncio.Event()
                result = SimpleNamespace(status="question", session_id="next-session", stage=1, incident_id="same-incident", question=SimpleNamespace(prompt="Count circles", image_webp=b"webp"))

                async def submit(*args):
                    started.set()
                    await finish.wait()
                    return result

                owner = SimpleNamespace(submit=mock.AsyncMock(side_effect=submit))
                cog = SimpleNamespace(_joinwatch_verification=owner)
                view = views.CaptchaQuestionView(cog, 10, 20, SimpleNamespace(session_id="first-session", stage=0))
                first, second = interaction(cog=cog), interaction(cog=cog)
                pending = asyncio.create_task(view.children[0].callback(first))
                await started.wait()
                duplicate = asyncio.create_task(view.children[1].callback(second))
                await asyncio.sleep(0)
                second.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
                finish.set()
                await asyncio.gather(pending, duplicate)
                owner.submit.assert_awaited_once_with(first.user, "first-session", 0, 0)
                first.edit_original_response.assert_awaited_once()
                self.assertIn("2 of 2", first.edit_original_response.await_args.kwargs["content"])
                self.assertEqual(first.edit_original_response.await_args.kwargs["attachments"][0].filename, "quick-check.webp")
                self.assertEqual(first.edit_original_response.await_args.kwargs["embed"].image.url, "attachment://quick-check.webp")
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
