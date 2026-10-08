"""Guard work that depends on privileged Gateway data."""

import json
import os
from pathlib import Path
from typing import Any

GRANT_FLAGS = {
    "members": ("gateway_guild_members", "gateway_guild_members_limited"),
    "presences": ("gateway_presence", "gateway_presence_limited"),
    "message_content": ("gateway_message_content", "gateway_message_content_limited"),
}


class GatewayCapabilityUnavailable(RuntimeError):
    """Required data is unavailable and durable work must remain pending."""

    def __init__(self, capability: str):
        self.capability = capability
        super().__init__(f"Gateway {capability} data is unavailable. Keep the operation pending")


def available(bot: Any, capability: str) -> bool:
    if capability not in GRANT_FLAGS:
        raise ValueError(f"Unknown Gateway capability: {capability}")
    intents = getattr(bot, "intents", None)
    requested = getattr(intents, capability, None) if intents is not None else None
    if requested is None or not requested:
        return False
    grants = getattr(bot, "_nhcogs_gateway_grants", None)
    if isinstance(grants, dict) and capability in grants:
        return bool(grants[capability])
    flags = getattr(bot, "application_flags", None)
    values = [getattr(flags, name, None) for name in GRANT_FLAGS[capability]]
    known = [value for value in values if value is not None]
    return any(known) if known else False


def require(bot: Any, capability: str) -> None:
    if not available(bot, capability):
        raise GatewayCapabilityUnavailable(capability)


def update_grants(bot: Any, flags: Any) -> None:
    """Use a fresh application response without altering the requested mask."""
    previous = getattr(bot, "_nhcogs_gateway_grants", None)
    grants = dict(previous) if isinstance(previous, dict) else {}
    for capability, names in GRANT_FLAGS.items():
        values = [getattr(flags, name, None) for name in names]
        known = [value for value in values if value is not None]
        if known:
            grants[capability] = any(known)
    bot._nhcogs_gateway_grants = grants


def signal_full_intent_recovery(bot: Any, flags: Any) -> bool:
    """Ask the external launcher to reconnect only after full approval returns."""
    marker = os.environ.get("NHC0GS_GATEWAY_RECOVERY_PATH")
    requested = os.environ.get("NHC0GS_GATEWAY_RECOVERY_INTENTS", "")
    capabilities = tuple(key for key in requested.split(",") if key)
    if not marker or not capabilities or any(key not in GRANT_FLAGS for key in capabilities):
        return False
    if not all(getattr(flags, GRANT_FLAGS[key][0], False) for key in capabilities):
        return False
    intents = getattr(bot, "intents", None)
    if not any(getattr(intents, key, None) is False for key in capabilities):
        return False
    path = Path(marker)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"approved": capabilities}), encoding="utf-8")
    temporary.replace(path)
    return True
