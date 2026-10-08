import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def load_capabilities():
    path = Path(__file__).resolve().parents[1] / "NHCogs" / "gateway_capabilities.py"
    spec = importlib.util.spec_from_file_location("gateway_capabilities_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GatewayCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.module = load_capabilities()
        self.flags = SimpleNamespace(
            gateway_guild_members=True, gateway_guild_members_limited=False,
            gateway_presence=True, gateway_presence_limited=False,
            gateway_message_content=True, gateway_message_content_limited=False,
        )
        self.bot = SimpleNamespace(
            intents=SimpleNamespace(members=True, presences=True, message_content=True),
            application_flags=self.flags,
        )

    def test_unknown_or_disabled_data_does_not_allow_work(self):
        for capability in self.module.GRANT_FLAGS:
            with self.subTest(capability=capability):
                self.assertTrue(self.module.available(self.bot, capability))
                self.assertFalse(self.module.available(SimpleNamespace(), capability))
                self.assertFalse(self.module.available(
                    SimpleNamespace(intents=self.bot.intents), capability,
                ))
                setattr(self.bot.intents, capability, False)
                self.assertFalse(self.module.available(self.bot, capability))
                with self.assertRaises(self.module.GatewayCapabilityUnavailable):
                    self.module.require(self.bot, capability)
                setattr(self.bot.intents, capability, True)

    def test_fresh_denial_overrides_cache_and_restore_cannot_override_disabled_mask(self):
        denied = SimpleNamespace(
            gateway_guild_members=False, gateway_guild_members_limited=False,
            gateway_presence=False, gateway_presence_limited=False,
            gateway_message_content=False, gateway_message_content_limited=False,
        )
        self.module.update_grants(self.bot, denied)
        self.assertFalse(self.module.available(self.bot, "members"))
        self.module.update_grants(self.bot, SimpleNamespace())
        self.assertFalse(self.module.available(self.bot, "members"))
        self.module.update_grants(self.bot, self.flags)
        self.assertTrue(self.module.available(self.bot, "members"))
        self.bot.intents.members = False
        self.assertFalse(self.module.available(self.bot, "members"))

    def test_recovery_requires_full_grants_and_preserves_requested_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "recovery.json"
            environment = {
                "NHC0GS_GATEWAY_RECOVERY_PATH": str(marker),
                "NHC0GS_GATEWAY_RECOVERY_INTENTS": "members,message_content",
            }
            self.bot.intents.members = False
            self.bot.intents.message_content = False
            with mock.patch.dict(os.environ, environment):
                self.flags.gateway_guild_members = False
                self.flags.gateway_guild_members_limited = True
                self.assertFalse(self.module.signal_full_intent_recovery(self.bot, self.flags))
                self.assertFalse(marker.exists())
                self.flags.gateway_guild_members = True
                self.flags.gateway_presence = False
                self.assertTrue(self.module.signal_full_intent_recovery(self.bot, self.flags))
                self.assertEqual(json.loads(marker.read_text())['approved'], [
                    "members", "message_content",
                ])
                self.assertFalse(marker.with_name(marker.name + ".tmp").exists())

    def test_unknown_capability_is_rejected_without_enabling_it(self):
        with self.assertRaises(ValueError):
            self.module.available(self.bot, "unknown")


if __name__ == "__main__":
    unittest.main()
