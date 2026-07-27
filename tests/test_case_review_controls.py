"""Case review controls: the case and message views, image classification,
and the bulk and individual confirmation prompts.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from tests.detection_case_fixtures import capture_attachment
from tests.harness import (
    CaseExpiryTestCase,
    _Bot,
    _isolated_honeypot_modules,
    drain_background_work,
)


class CaseReviewControlTests(CaseExpiryTestCase):
    async def test_bulk_tp_interaction_ignores_captured_pdf_evidence(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=datetime.now(timezone.utc),
                        jump_url="https://discord.test/messages/40",
                        attachments=(
                            honeypot.NewAttachment(
                                0, "proof.png", 10, "image/png", None, None, "png-url"
                            ),
                            honeypot.NewAttachment(
                                1, "invoice.pdf", 10, "application/pdf", None, None, "pdf-url"
                            ),
                        ),
                    ),
                    (),
                )
                for position, filename in enumerate(("proof.png", "invoice.pdf")):
                    self.assertTrue(
                        capture_attachment(
                            cog._case_store,
                            appended.case.case_id,
                            appended.message.sequence,
                            position,
                            Path(directory) / filename,
                        )
                    )
                cog._execute_detection_case_operation = mock.AsyncMock()
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        send_message=mock.AsyncMock(),
                        is_done=lambda: False,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_bulk_interaction(
                    interaction, appended.case.case_id, "tp"
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                self.assertTrue(
                    all(
                        attachment.learning_decision is None
                        for attachment in snapshot.attachments
                    )
                )
                interaction.response.send_message.assert_awaited_once()

                completed = await cog._case_review_bulk_interaction(
                    interaction,
                    appended.case.case_id,
                    "tp",
                    confirmed=True,
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                decisions = {
                    attachment.filename: attachment.learning_decision
                    for attachment in snapshot.attachments
                }
                self.assertTrue(completed)
                self.assertEqual(decisions["proof.png"], "true_positive")
                self.assertIsNone(decisions["invoice.pdf"])
                await drain_background_work(cog)

    async def test_moderator_ban_requires_confirmation_for_unreviewed_attachment(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                member = SimpleNamespace(id=20, roles=[], ban=mock.AsyncMock())
                guild = SimpleNamespace(
                    id=10,
                    me=SimpleNamespace(id=1),
                    get_member=lambda user_id: member,
                )
                bot = _Bot()
                bot.get_guild = lambda guild_id: guild
                cog = honeypot.Honeypot(bot)
                cog._case_store.initialize()
                cog.config = self._config({"dry_run": True})
                honeypot.DetectionModerationConfirmationView.add_item = (
                    lambda view, item: setattr(
                        view, "children", getattr(view, "children", []) + [item]
                    )
                )
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                now = datetime.now(timezone.utc)
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=now,
                        jump_url="https://discord.test/messages/40",
                        attachments=(
                            honeypot.NewAttachment(
                                0,
                                "proof.png",
                                5,
                                "image/png",
                                10,
                                10,
                                "https://cdn.test/proof.png",
                            ),
                        ),
                    ),
                    (),
                )
                response = SimpleNamespace(
                    defer=mock.AsyncMock(),
                    send_message=mock.AsyncMock(),
                    is_done=lambda: False,
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(ban_members=True),
                    ),
                    response=response,
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ban"
                )

                response.defer.assert_not_awaited()
                response.send_message.assert_awaited_once()
                self.assertTrue(response.send_message.await_args.kwargs["ephemeral"])
                confirmation = response.send_message.await_args.kwargs["view"]
                self.assertEqual(
                    [item.label for item in confirmation.children],
                    ["Confirm Ban"],
                )
                snapshot = cog._case_store.get_case(appended.case.case_id)
                self.assertEqual(snapshot.case.status.value, "pending")
                self.assertEqual(snapshot.operations, ())

                confirmation_response_done = False

                async def defer_confirmation():
                    nonlocal confirmation_response_done
                    confirmation_response_done = True

                confirmation_interaction = SimpleNamespace(
                    user=interaction.user,
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer_confirmation),
                        is_done=lambda: confirmation_response_done,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(),
                )
                await confirmation.children[0].callback(confirmation_interaction)

                snapshot = cog._case_store.get_case(appended.case.case_id)
                operation = next(
                    item
                    for item in snapshot.operations
                    if item.operation_type == "moderator_ban"
                )
                confirmation_interaction.response.defer.assert_awaited_once()
                confirmation_interaction.delete_original_response.assert_awaited_once_with()
                self.assertEqual(operation.status.value, "succeeded")
                self.assertEqual(operation.result, "planned_ban")

    async def test_bulk_confirmation_dismisses_prompt_before_action_finishes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                honeypot.DetectionBulkConfirmationView.add_item = (
                    lambda view, item: setattr(
                        view, "children", getattr(view, "children", []) + [item]
                    )
                )
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                cog = honeypot.Honeypot(_Bot())
                action_started = asyncio.Event()
                release_action = asyncio.Event()
                order = []

                async def run_action(*args, **kwargs):
                    order.append("action")
                    action_started.set()
                    await release_action.wait()
                    return False

                cog._case_review_bulk_interaction = mock.AsyncMock(
                    side_effect=run_action
                )
                view = honeypot.DetectionBulkConfirmationView(
                    cog,
                    "case-1",
                    "tp",
                )
                response_done = False

                async def defer():
                    nonlocal response_done
                    order.append("defer")
                    response_done = True

                async def delete_original_response():
                    order.append("delete")

                interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(
                        side_effect=delete_original_response
                    ),
                )

                callback = asyncio.create_task(
                    view.children[0].callback(interaction)
                )
                await action_started.wait()

                interaction.delete_original_response.assert_awaited_once_with()
                self.assertEqual(order, ["defer", "delete", "action"])
                release_action.set()
                await callback

    async def test_classification_returns_before_final_operations_finish(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime.now(timezone.utc)
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        10,
                        20,
                        30,
                        40,
                        "evidence",
                        now,
                        None,
                        (
                            honeypot.NewAttachment(
                                0,
                                "proof.png",
                                4,
                                "image/png",
                                None,
                                None,
                                "https://cdn.test/proof.png",
                            ),
                        ),
                    ),
                    (),
                )
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        Path(directory) / "proof.png",
                    )
                )
                moderation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "moderator_ban",
                    f"moderator-ban:{appended.case.case_id}",
                    actor_id=99,
                )
                claimed_moderation = cog._case_store.claim_operation(
                    moderation.operation_id,
                    now,
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        claimed_moderation.operation_id,
                        claimed_moderation.claim_token,
                        now,
                        "ban",
                    )
                )
                final_operation_started = asyncio.Event()
                release_final_operation = asyncio.Event()

                async def block_final_operation(*_args, **_kwargs):
                    final_operation_started.set()
                    await release_final_operation.wait()

                cog._execute_detection_case_operation = mock.AsyncMock(
                    side_effect=block_final_operation
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        is_done=lambda: False,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                interaction_task = asyncio.create_task(
                    cog._case_review_bulk_interaction(
                        interaction,
                        appended.case.case_id,
                        "tp",
                        confirmed=True,
                        expected_keys=(
                            honeypot.AttachmentKey(
                                appended.case.case_id,
                                appended.message.sequence,
                                0,
                            ),
                        ),
                    )
                )
                await final_operation_started.wait()
                try:
                    try:
                        completed = await asyncio.wait_for(
                            asyncio.shield(interaction_task),
                            timeout=0.05,
                        )
                    except TimeoutError:
                        self.fail(
                            "classification interaction waited for final operations"
                        )
                    self.assertTrue(completed)
                    snapshot = cog._case_store.get_case(appended.case.case_id)
                    self.assertEqual(snapshot.case.status.value, "resolved")
                    self.assertEqual(
                        snapshot.attachments[0].learning_decision,
                        "true_positive",
                    )
                    self.assertTrue(
                        any(
                            operation.operation_type == "review_update"
                            for operation in snapshot.operations
                        )
                    )
                finally:
                    release_final_operation.set()
                    await interaction_task
                    await drain_background_work(cog)

    async def test_dismissed_confirmation_reports_failure_in_new_ephemeral_message(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                honeypot.DetectionBulkConfirmationView.add_item = (
                    lambda view, item: setattr(
                        view, "children", getattr(view, "children", []) + [item]
                    )
                )
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                cog = honeypot.Honeypot(_Bot())
                view = honeypot.DetectionBulkConfirmationView(
                    cog,
                    "case-1",
                    "tp",
                )
                response_done = False

                async def defer():
                    nonlocal response_done
                    response_done = True

                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(
                            manage_messages=False,
                            moderate_members=False,
                            ban_members=False,
                            kick_members=False,
                        ),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(),
                )

                await view.children[0].callback(interaction)

                interaction.delete_original_response.assert_awaited_once_with()
                interaction.followup.send.assert_awaited_once_with(
                    "You do not have permission to review this case.",
                    ephemeral=True,
                )

    async def test_moderation_confirmation_dismisses_prompt_before_action_finishes(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                honeypot.DetectionModerationConfirmationView.add_item = (
                    lambda view, item: setattr(
                        view, "children", getattr(view, "children", []) + [item]
                    )
                )
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                cog = honeypot.Honeypot(_Bot())
                action_started = asyncio.Event()
                release_action = asyncio.Event()
                order = []

                async def run_action(*args, **kwargs):
                    order.append("action")
                    action_started.set()
                    await release_action.wait()
                    return True

                cog._case_review_moderation_interaction = mock.AsyncMock(
                    side_effect=run_action
                )
                view = honeypot.DetectionModerationConfirmationView(
                    cog,
                    "case-1",
                    "ban",
                )
                response_done = False

                async def defer():
                    nonlocal response_done
                    order.append("defer")
                    response_done = True

                async def delete_original_response():
                    order.append("delete")

                interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(
                        side_effect=delete_original_response
                    ),
                )

                callback = asyncio.create_task(
                    view.children[0].callback(interaction)
                )
                await action_started.wait()

                interaction.delete_original_response.assert_awaited_once_with()
                self.assertEqual(order, ["defer", "delete", "action"])
                release_action.set()
                await callback

    async def test_case_view_keeps_moderation_and_image_controls_separate(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                matched = honeypot.CaseFeedbackItem(
                    honeypot.AttachmentKey("case-1", 1, 0),
                    "proof.png",
                    None,
                    True,
                )

                view = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=True,
                    feedback_items=(matched,),
                )

                self.assertEqual(
                    [item.label for item in view.children],
                    [
                        "Ban",
                        "Kick",
                        "Ignore",
                        "All TP",
                        "All FP",
                        "Ignore",
                        "Individual",
                    ],
                )

                after_ban = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=True,
                    feedback_items=(matched,),
                    moderation_actions=(),
                )
                self.assertEqual(
                    [item.label for item in after_ban.children],
                    ["All TP", "All FP", "Ignore", "Individual"],
                )

                after_classification = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=False,
                )
                self.assertEqual(
                    [item.label for item in after_classification.children],
                    ["Ban", "Kick", "Ignore"],
                )
                self.assertEqual(
                    [item.custom_id for item in view.children[:3]],
                    [
                        "honeypot:case:case-1:moderate:ban",
                        "honeypot:case:case-1:moderate:kick",
                        "honeypot:case:case-1:moderate:ignore",
                    ],
                )
                self.assertEqual(
                    [item.emoji for item in view.children[:3]],
                    ["🔨", "👢", "✅"],
                )
                self.assertEqual(
                    [item.style for item in view.children[:3]],
                    [
                        honeypot.discord.ButtonStyle.danger,
                        honeypot.discord.ButtonStyle.secondary,
                        honeypot.discord.ButtonStyle.success,
                    ],
                )

    async def test_message_view_only_offers_message_feedback(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)

                view = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=True,
                    message_sequence=2,
                    feedback_items=(
                        SimpleNamespace(detector_matched=True, decision=None),
                    ),
                )

                self.assertEqual(
                    [item.label for item in view.children],
                    ["All TP", "All FP", "Ignore", "Individual"],
                )

    async def test_unmatched_and_mixed_views_offer_add_without_fp(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                cog = honeypot.Honeypot(_Bot())
                cog._case_review_bulk_interaction = mock.AsyncMock()
                matched = SimpleNamespace(detector_matched=True, decision=None)
                unmatched = SimpleNamespace(detector_matched=False, decision=None)

                unmatched_view = honeypot.DetectionCaseView(
                    cog,
                    "case-1",
                    has_image_feedback=True,
                    feedback_items=(unmatched,),
                )
                mixed_view = honeypot.DetectionCaseView(
                    cog,
                    "case-2",
                    has_image_feedback=True,
                    feedback_items=(matched, unmatched),
                )

                self.assertEqual(
                    [item.label for item in unmatched_view.children[3:]],
                    ["Add all", "Ignore", "Individual"],
                )
                self.assertEqual(
                    [item.label for item in mixed_view.children[3:]],
                    ["Add all", "Ignore", "Individual"],
                )
                self.assertNotIn(
                    "All FP", [item.label for item in unmatched_view.children]
                )
                await unmatched_view.children[3].callback(SimpleNamespace())
                cog._case_review_bulk_interaction.assert_awaited_once_with(
                    mock.ANY, "case-1", "tp"
                )

    async def test_case_summary_represents_each_source_message_channel(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime(2026, 7, 24, 8, tzinfo=timezone.utc)
                first = cog._case_store.append_message(
                    honeypot.NewMessage(
                        10, 20, 30, 40, "first", now, None, ()
                    ),
                    (
                        honeypot.DetectionSignal(
                            "honeypot",
                            "Multiple image attachments: 4\nKnown suspicious image match",
                            honeypot.ActionIntent.REVIEW,
                            True,
                            {},
                        ),
                        honeypot.DetectionSignal(
                            "image",
                            "Initial image scan matched known suspicious content",
                            honeypot.ActionIntent.REVIEW,
                            True,
                            {},
                        ),
                        honeypot.DetectionSignal(
                            "spam",
                            "Repeated suspicious content",
                            honeypot.ActionIntent.REVIEW,
                            True,
                            {},
                        ),
                    ),
                )
                cog._case_store.append_message(
                    honeypot.NewMessage(
                        10,
                        20,
                        31,
                        41,
                        "second",
                        now + timedelta(seconds=3),
                        None,
                        (),
                    ),
                    (
                        honeypot.DetectionSignal(
                            "spam",
                            "Same message in 2 channels within 3s",
                            honeypot.ActionIntent.REVIEW,
                            True,
                            {},
                        ),
                    ),
                )

                snapshot = cog._case_store.get_case(first.case.case_id)
                projection = honeypot.render_case(snapshot)
                timeline = honeypot.render_timeline(snapshot)

                self.assertIn("Message 1 · <#30>", projection.description)
                self.assertIn("Message 2 · <#31>", projection.description)
                self.assertIn(
                    "Same message in 2 channels within 3s",
                    projection.description,
                )
                rendered_signals = "\n".join(projection.signal_lines)
                self.assertNotIn(
                    "Known suspicious image match",
                    rendered_signals,
                )
                self.assertEqual(
                    rendered_signals.count(
                        "Initial image scan matched known suspicious content"
                    ),
                    1,
                )
                self.assertEqual(
                    timeline.messages[0].signal_reasons,
                    (
                        "Multiple image attachments: 4",
                        "Initial image scan matched known suspicious content",
                        "Repeated suspicious content",
                    ),
                )
                self.assertIn(
                    "Signals:\n"
                    "Message 1 · <#30>:\n"
                    "Multiple image attachments: 4\n"
                    "Initial image scan matched known suspicious content (+1 more)\n"
                    "Message 2 · <#31>:\n"
                    "Same message in 2 channels within 3s",
                    projection.description,
                )

    async def test_identical_reasons_survive_across_source_messages(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime(2026, 7, 24, 8, tzinfo=timezone.utc)
                signal = lambda: honeypot.DetectionSignal(
                    "spam",
                    "Repeated suspicious content",
                    honeypot.ActionIntent.REVIEW,
                    True,
                    {},
                )
                first = cog._case_store.append_message(
                    honeypot.NewMessage(10, 20, 30, 40, "first", now, None, ()),
                    (signal(),),
                )
                cog._case_store.append_message(
                    honeypot.NewMessage(
                        10,
                        20,
                        31,
                        41,
                        "second",
                        now + timedelta(seconds=3),
                        None,
                        (),
                    ),
                    (signal(),),
                )

                projection = honeypot.render_case(
                    cog._case_store.get_case(first.case.case_id)
                )

                self.assertIn("Message 1 · <#30>", projection.description)
                self.assertIn("Message 2 · <#31>", projection.description)
                self.assertEqual(
                    sum(
                        "Repeated suspicious content" in line
                        for line in projection.signal_lines
                    ),
                    2,
                )

    async def test_completed_moderation_with_pending_image_is_awaiting_classification(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime(2026, 7, 24, 8, tzinfo=timezone.utc)
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        10,
                        20,
                        30,
                        40,
                        "evidence",
                        now,
                        None,
                        (
                            honeypot.NewAttachment(
                                0,
                                "proof.png",
                                4,
                                "image/png",
                                None,
                                None,
                                "https://cdn.test/proof.png",
                            ),
                        ),
                    ),
                    (),
                )
                self.assertTrue(
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        appended.message.sequence,
                        0,
                        Path(directory) / "proof.png",
                    )
                )
                operation = cog._case_store.ensure_operation(
                    appended.case.case_id,
                    "moderator_ban",
                    f"moderator-ban:{appended.case.case_id}",
                    actor_id=99,
                )
                claimed = cog._case_store.claim_operation(
                    operation.operation_id,
                    now,
                )
                self.assertTrue(
                    cog._case_store.complete_operation(
                        claimed.operation_id,
                        claimed.claim_token,
                        now,
                        "ban",
                    )
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                projection = honeypot.render_case(snapshot)

                self.assertEqual(snapshot.case.status.value, "pending")
                self.assertIn("Status: Awaiting classification", projection.description)

    async def test_case_view_hides_individual_when_case_has_too_many_images(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)

                view = honeypot.DetectionCaseView(
                    honeypot.Honeypot(_Bot()),
                    "case-1",
                    has_image_feedback=True,
                    allow_individual=False,
                )

                self.assertNotIn("Individual", [item.label for item in view.children])

    async def test_case_summary_warns_and_hides_individual_above_25_images(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                bot = _Bot()
                cog = honeypot.Honeypot(bot)
                await asyncio.to_thread(cog._case_store.initialize)
                now = datetime.now(timezone.utc)
                attachments = tuple(
                    honeypot.NewAttachment(
                        position,
                        f"proof-{position}.png",
                        5,
                        "image/png",
                        10,
                        10,
                        f"https://cdn.test/proof-{position}.png",
                    )
                    for position in range(26)
                )
                appended = await asyncio.to_thread(
                    cog._case_store.append_message,
                    honeypot.NewMessage(
                        10, 20, 30, 40, "evidence", now, None, attachments
                    ),
                    (),
                )
                for position in range(26):
                    evidence = data_path / f"proof-{position}.png"
                    evidence.write_bytes(b"image")
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        1,
                        position,
                        evidence,
                    )
                snapshot = await asyncio.to_thread(
                    cog._case_store.get_case, appended.case.case_id
                )
                projection = honeypot.render_case(snapshot)
                visible = "\n".join(field.value for field in projection.fields)

                self.assertIn(
                    "Too many images for one menu\nReview them in the thread", visible
                )

                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                honeypot.DetectionCaseView.add_item = add_item
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                view = honeypot.DetectionCaseView(
                    cog,
                    appended.case.case_id,
                    has_image_feedback=True,
                    allow_individual=len(projection.feedback_items) <= 25,
                )
                self.assertNotIn("Individual", [item.label for item in view.children])

    def test_timeline_attachment_humanizes_decision_and_escapes_filename(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                attachment = SimpleNamespace(
                    capture_status="captured",
                    match_metadata={},
                    learning_decision="false_positive",
                    publication_error=None,
                    key=SimpleNamespace(position=0),
                    filename="[proof](https://evil.test).png",
                )

                line = honeypot.Honeypot._case_timeline_attachment_line(attachment)

                self.assertEqual(
                    line,
                    "- 1. `[proof](https://evil.test).png`\n  captured; False positive",
                )
                self.assertNotIn("decision:", line)

    async def test_individual_image_action_opens_dropdown_and_routes_selected_image(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory)
            with _isolated_honeypot_modules(data_path) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                cog._case_store.initialize()
                now = datetime.now(timezone.utc)
                appended = cog._case_store.append_message(
                    honeypot.NewMessage(
                        guild_id=10,
                        user_id=20,
                        channel_id=30,
                        message_id=40,
                        content="evidence",
                        created_at=now,
                        jump_url="https://discord.test/messages/40",
                        attachments=tuple(
                            honeypot.NewAttachment(
                                position,
                                filename,
                                5,
                                "image/png",
                                10,
                                10,
                                f"https://cdn.test/{filename}",
                            )
                            for position, filename in enumerate(
                                ("proof-one.png", "proof-two.png")
                            )
                        ),
                    ),
                    (),
                )
                for position, filename in enumerate(("proof-one.png", "proof-two.png")):
                    evidence = data_path / filename
                    evidence.write_bytes(b"image")
                    capture_attachment(
                        cog._case_store,
                        appended.case.case_id,
                        1,
                        position,
                        evidence,
                    )
                self.assertTrue(
                    cog._case_store.update_attachment_scan(
                        appended.case.case_id,
                        1,
                        0,
                        "sha",
                        "phash",
                        match_metadata={"matched": True},
                        error=None,
                    )
                )

                def add_item(view, item):
                    view.children = getattr(view, "children", []) + [item]

                def remove_item(view, item):
                    view.children.remove(item)

                honeypot.DetectionIndividualView.add_item = add_item
                honeypot.DetectionIndividualView.remove_item = remove_item
                honeypot.discord.ui.Select = lambda **kwargs: SimpleNamespace(**kwargs)
                honeypot.discord.ui.Button = lambda **kwargs: SimpleNamespace(**kwargs)
                honeypot.discord.SelectOption = lambda **kwargs: SimpleNamespace(**kwargs)
                cog._case_review_attachment_interaction = mock.AsyncMock()
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(manage_messages=True),
                    ),
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(),
                        send_message=mock.AsyncMock(),
                    ),
                )

                await cog._case_review_individual_prompt(
                    interaction, appended.case.case_id
                )

                interaction.response.defer.assert_not_awaited()
                interaction.response.send_message.assert_awaited_once()
                self.assertTrue(
                    interaction.response.send_message.await_args.kwargs["ephemeral"]
                )
                view = interaction.response.send_message.await_args.kwargs["view"]
                self.assertEqual(len(view.children), 1)
                selector = view.children[0]
                self.assertEqual(
                    [option.label for option in selector.options],
                    ["1.1 proof-one.png", "1.2 proof-two.png"],
                )

                selector.values = [selector.options[0].value]
                selection = SimpleNamespace(
                    response=SimpleNamespace(edit_message=mock.AsyncMock()),
                    delete_original_response=mock.AsyncMock(),
                )
                await selector.callback(selection)
                selection.delete_original_response.assert_not_awaited()
                self.assertEqual(
                    [item.label for item in view.children[1:]],
                    ["TP", "FP", "Ignore"],
                )
                response_done = False

                async def defer():
                    nonlocal response_done
                    response_done = True

                action_interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    delete_original_response=mock.AsyncMock(),
                )
                await view.children[1].callback(action_interaction)
                action_interaction.delete_original_response.assert_awaited_once_with()
                cog._case_review_attachment_interaction.assert_awaited_with(
                    action_interaction,
                    honeypot.AttachmentKey(appended.case.case_id, 1, 0),
                    "tp",
                )

                selector.values = [selector.options[1].value]
                await selector.callback(selection)
                self.assertEqual(
                    [item.label for item in view.children[1:]],
                    ["Add", "Ignore"],
                )
                response_done = False
                action_interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        defer=mock.AsyncMock(side_effect=defer),
                        is_done=lambda: response_done,
                    ),
                    delete_original_response=mock.AsyncMock(),
                )
                await view.children[1].callback(action_interaction)
                action_interaction.delete_original_response.assert_awaited_once_with()
                cog._case_review_attachment_interaction.assert_awaited_with(
                    action_interaction,
                    honeypot.AttachmentKey(appended.case.case_id, 1, 1),
                    "tp",
                )
                self.assertEqual(
                    [getattr(option, "default", False) for option in selector.options],
                    [False, True],
                )

    async def test_manage_messages_can_use_case_ban_control(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                cog = honeypot.Honeypot(_Bot())
                appended = self._append_case(
                    honeypot, cog, datetime.now(timezone.utc)
                )
                response = SimpleNamespace(
                    defer=mock.AsyncMock(),
                    send_message=mock.AsyncMock(),
                    is_done=lambda: False,
                )
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        id=99,
                        guild_permissions=SimpleNamespace(
                            manage_messages=True,
                            ban_members=False,
                            kick_members=False,
                        ),
                    ),
                    response=response,
                    followup=SimpleNamespace(send=mock.AsyncMock()),
                )

                cog._execute_detection_case_operation = mock.AsyncMock()
                await cog._case_review_moderation_interaction(
                    interaction, appended.case.case_id, "ban", confirmed=True
                )

                snapshot = cog._case_store.get_case(appended.case.case_id)
                response.defer.assert_awaited_once()
                self.assertEqual(snapshot.case.status.value, "resolving")
                self.assertEqual(snapshot.operations[0].operation_type, "moderator_ban")

    async def test_moderate_members_can_ignore_and_classify_case_evidence(self):
        with TemporaryDirectory() as directory:
            with _isolated_honeypot_modules(Path(directory)) as honeypot:
                honeypot.Honeypot(_Bot())
                interaction = SimpleNamespace(
                    user=SimpleNamespace(
                        guild_permissions=SimpleNamespace(
                            moderate_members=True,
                            manage_messages=False,
                            ban_members=False,
                            kick_members=False,
                        )
                    )
                )

                self.assertTrue(honeypot.review_publication._case_review_has_permission(interaction))
                self.assertTrue(
                    honeypot.review_publication._case_review_has_action_permission(interaction, "ignore")
                )
