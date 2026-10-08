import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.harness import (
    DetectionPipelineTestCase,
    _Bot,
    _isolated_honeypot_modules,
    _operational_support,
)

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class BacklogSpamWindowTests(DetectionPipelineTestCase):
    async def test_old_replayed_message_does_not_count_messages_from_days_later(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog = module.Honeypot(_Bot(), _operational_support())
            await cog._message_registry.initialize()
            old = self._message(module, attachment_count=1, message_id=300, channel_id=400)
            old.created_at = NOW - timedelta(days=2)
            fresh = self._message(module, attachment_count=1, message_id=301, channel_id=401)
            fresh.created_at = NOW
            await module.detection._observe_message(cog, fresh)
            await module.detection._observe_message(cog, old)
            settings = module.GuildSettings.from_mapping({
                "spam_window_seconds": 10, "spam_min_channels": 2,
            })
            self.assertEqual(await module.detection._spam_suspicion_reasons(cog, old, settings), [])

    async def test_real_window_includes_lower_boundary_and_excludes_later_messages(self):
        with TemporaryDirectory() as directory, _isolated_honeypot_modules(Path(directory)) as module:
            cog = module.Honeypot(_Bot(), _operational_support())
            await cog._message_registry.initialize()
            current = self._message(module, attachment_count=1, message_id=300, channel_id=400)
            current.created_at = NOW
            earlier = self._message(module, attachment_count=1, message_id=301, channel_id=401)
            earlier.created_at = NOW - timedelta(seconds=10)
            later = self._message(module, attachment_count=1, message_id=302, channel_id=402)
            later.created_at = NOW + timedelta(seconds=1)
            outside = self._message(module, attachment_count=1, message_id=303, channel_id=403)
            outside.created_at = NOW - timedelta(seconds=11)
            for message in (later, outside, earlier, current):
                await module.detection._observe_message(cog, message)
            settings = module.GuildSettings.from_mapping({
                "spam_window_seconds": 10, "spam_min_channels": 2,
            })
            reasons = await module.detection._spam_suspicion_reasons(cog, current, settings)
            self.assertEqual(reasons, ["Same message in 2 channels within 10s"])
            fingerprint = module.detection.message_spam_fingerprint(current)
            # Callers without an upper bound retain the existing query semantics.
            self.assertEqual(await cog._message_registry.matching_channel_count(
                current.guild.id, current.author.id, fingerprint,
                since_utc=NOW - timedelta(seconds=10),
            ), 3)


if __name__ == "__main__":
    unittest.main()
