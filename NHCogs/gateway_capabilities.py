"""Guard work that depends on privileged Gateway data."""

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
