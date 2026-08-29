from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TypeVar

log = logging.getLogger("red.Honeypot")
_T = TypeVar("_T")
_EnumT = TypeVar("_EnumT", bound=Enum)


class ImageScanDetectorActionOption(str, Enum):
    NONE = "none"
    REVIEW = "review"
    KICK = "kick"
    BAN = "ban"


class ReviewKickFailWarningMode(str, Enum):
    FALSE = "false"
    TRUE = "true"
    MANUAL = "manual"


class CoreActionOption(str, Enum):
    KICK = "kick"
    BAN = "ban"
    REVIEW = "review"
    NONE = "none"


class FallbackActionOption(str, Enum):
    REVIEW = "review"
    KICK = "kick"
    BAN = "ban"
    NONE = "none"


class WhitelistModeOption(str, Enum):
    BYPASS = "bypass"
    REVIEW = "review"
    FALLBACK = "fallback"
    NONE = "none"


class JoinwatchAutoRoleActionOption(str, Enum):
    NONE = "none"
    KICK = "kick"
    BAN = "ban"


class BaitActionOption(str, Enum):
    KICK = "kick"
    BAN = "ban"


IMAGE_SCAN_DETECTOR_ACTION_OPTIONS = tuple(
    member.value for member in ImageScanDetectorActionOption
)
REVIEW_KICK_FAIL_WARNING_MODES = tuple(member.value for member in ReviewKickFailWarningMode)
CORE_ACTION_OPTIONS = tuple(member.value for member in CoreActionOption)
FALLBACK_ACTION_OPTIONS = tuple(member.value for member in FallbackActionOption)
WHITELIST_MODE_OPTIONS = tuple(member.value for member in WhitelistModeOption)
JOINWATCH_AUTO_ROLE_ACTION_OPTIONS = tuple(
    member.value for member in JoinwatchAutoRoleActionOption
)
BAIT_ACTION_OPTIONS = tuple(member.value for member in BaitActionOption)
BOOL_OPTIONS = ("false", "true")

DEFAULT_STATS = {
    "detections": 0,
    "suspicious": 0,
    "reviewed": 0,
    "review_expired": 0,
    "ignored": 0,
    "kicked": 0,
    "banned": 0,
    "failed_actions": 0,
    "dry_run_actions": 0,
    "whitelisted": 0,
    "pending_mutes": 0,
    "pending_mute_failures": 0,
    "purged_messages": 0,
    "cached_purge_deletes": 0,
    "forward_purge_deletes": 0,
    "forward_purge_delete_failures": 0,
    "evidence_capture_failures": 0,
    "delete_forbidden": 0,
    "delete_transient_failures": 0,
    "firstpost_seen": 0,
    "firstpost_hits": 0,
    "firstpost_reviews": 0,
    "firstpost_kicks": 0,
    "firstpost_bans": 0,
    "early_catches": 0,
    "spam_hits": 0,
    "spam_reviews": 0,
    "spam_kicks": 0,
    "spam_bans": 0,
    "spam_catches": 0,
    "honeypot_hits": 0,
    "honeypot_reviews": 0,
    "honeypot_kicks": 0,
    "honeypot_bans": 0,
    "honeypot_catches": 0,
    "image_hits": 0,
    "image_reviews": 0,
    "image_kicks": 0,
    "image_bans": 0,
    "image_catches": 0,
    "joinwatch_total_joins": 0,
    "joinwatch_young_joins": 0,
    "joinwatch_auto_roles_scheduled": 0,
    "joinwatch_auto_roles": 0,
    "joinwatch_auto_role_failures": 0,
    "joinwatch_auto_roles_cleared": 0,
    "joinwatch_auto_role_punishments": 0,
}
PURGE_BACKWARD_DEFAULT_SECONDS = 60
PURGE_FORWARD_DEFAULT_SECONDS = 10
MAX_MANUAL_PUNISHMENT_ROLES_PER_CHANNEL = 25
SCAM_KEYWORDS = [
    "free nitro",
    "giveaway",
    "steam gift",
    "free discord",
    "discord.gift",
    "claim your",
    "you won",
    "free vbucks",
    "free robux",
    "free coins",
    "boost your server",
    "limited time",
    "exclusive offer",
    "free membership",
    "hack",
    "crack",
    "generator",
]
DEFAULT_ATTACHMENT_PATTERNS = [r"^image$", r"^image ?\(\d+\)$", r"^\d+$"]


DEFAULTS: Mapping[str, object] = MappingProxyType(
    {
        "enabled": False,
        "action": None,
        "fallback_action": "review",
        "dry_run": False,
        "daily_stats_channel": None,
        "manual_evidence_channel": None,
        "manual_punishment_roles": {},
        "honeypot_channels": [],
        "mute_role": None,
        "purge_backward_seconds": PURGE_BACKWARD_DEFAULT_SECONDS,
        "purge_forward_seconds": PURGE_FORWARD_DEFAULT_SECONDS,
        "whitelisted_roles": [],
        "firstpost_collect_enabled": False,
        "firstpost_enabled": False,
        "firstpost_action": "review",
        "spam_enabled": False,
        "spam_action": "review",
        "spam_window_seconds": 10,
        "spam_min_channels": 2,
        "imagescan_detector_enabled": False,
        "imagescan_detector_action": "review",
        "imagescan_detector_threshold": 20,
        "review_enabled": False,
        "review_channel": None,
        "review_kick_fail_warning": "false",
        "automated_kick_fail_warning": False,
        "whitelist_mode": "bypass",
        "stats": DEFAULT_STATS.copy(),
        "scam_keywords": SCAM_KEYWORDS.copy(),
        "attachment_patterns": DEFAULT_ATTACHMENT_PATTERNS.copy(),
        "gif_detector_enabled": False,
        "gif_detector_debug_enabled": False,
        "gif_detector_debug_channel": None,
        "gif_detector_animation_enabled": True,
        "gif_detector_channels": [],
        "gif_detector_secondary_message": "No gifs!",
        "gif_detector_retention_seconds": 5,
        "gif_detector_threshold": 3,
        "gif_detector_window_seconds": 60,
        "gif_detector_mute_duration_seconds": 3600,
        "joinwatch_enabled": False,
        "joinwatch_alert_enabled": True,
        "joinwatch_channel": None,
        "joinwatch_min_age_hours": 24,
        "joinwatch_auto_role_enabled": False,
        "joinwatch_auto_role_id": None,
        "joinwatch_auto_role_timer_minutes": 1440,
        "joinwatch_auto_role_action": "none",
        "joinwatch_auto_role_random_delay_enabled": False,
        "joinwatch_auto_role_random_delay_min_minutes": 1,
        "joinwatch_auto_role_random_delay_max_minutes": 10,
        "joinwatch_pending_role_assignments": {},
        "joinwatch_pending_roles": {},
        "baitrole_enabled": False,
        "baitrole_channel": None,
        "baitrole_id": None,
        "baitrole_action": "ban",
    }
)


def _bool(raw: Mapping[str, object], key: str) -> bool:
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, bool):
        return value
    log.warning("Invalid guild setting %s=%r; using default %r", key, value, DEFAULTS[key])
    return bool(DEFAULTS[key])


def _str(raw: Mapping[str, object], key: str) -> str:
    default = DEFAULTS[key]
    if not isinstance(default, str):
        raise TypeError(f"Internal default for {key} is not a string")
    value = raw.get(key, default)
    if isinstance(value, str):
        return value
    log.warning("Invalid guild setting %s=%r; using default %r", key, value, default)
    return default


def _int(raw: Mapping[str, object], key: str) -> int:
    default = DEFAULTS[key]
    if not isinstance(default, int) or isinstance(default, bool):
        raise TypeError(f"Internal default for {key} is not an integer")
    value = raw.get(key, default)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    log.warning("Invalid guild setting %s=%r; using default %r", key, value, default)
    return default


def _optional_int(raw: Mapping[str, object], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    log.warning("Invalid guild setting %s=%r; using default None", key, value)
    return None


def _list(raw: Mapping[str, object], key: str, item_type: type[_T]) -> list[_T]:
    default = DEFAULTS[key]
    if not isinstance(default, list):
        raise TypeError(f"Internal default for {key} is not a list")
    value = raw.get(key, default)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if all(
            isinstance(item, item_type)
            and not (item_type is int and isinstance(item, bool))
            for item in items
        ):
            return items
    log.warning("Invalid guild setting %s=%r; using default %r", key, value, default)
    return list(default)


def _int_dict(raw: Mapping[str, object], key: str) -> dict[str, int]:
    default = DEFAULTS[key]
    if not isinstance(default, dict):
        raise TypeError(f"Internal default for {key} is not a dict")
    value = raw.get(key, default)
    if isinstance(value, Mapping) and all(
        isinstance(item_key, str)
        and isinstance(item_value, int)
        and not isinstance(item_value, bool)
        for item_key, item_value in value.items()
    ):
        return dict(value)
    log.warning("Invalid guild setting %s=%r; using default %r", key, value, default)
    return dict(default)


def _nested_dict(raw: Mapping[str, object], key: str) -> dict[str, dict[str, object]]:
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, Mapping) and all(
        isinstance(item_key, str) and isinstance(item_value, Mapping)
        for item_key, item_value in value.items()
    ):
        return {item_key: dict(item_value) for item_key, item_value in value.items()}
    log.warning("Invalid guild setting %s=%r; using default %r", key, value, DEFAULTS[key])
    return {}


@dataclass(frozen=True)
class ManualPunishmentRoleSettings:
    role_id: int
    source_channel_ids: tuple[int, ...]
    notification_channel_id: int | None


def _manual_punishment_roles(
    raw: Mapping[str, object],
) -> dict[int, ManualPunishmentRoleSettings]:
    value = raw.get("manual_punishment_roles", DEFAULTS["manual_punishment_roles"])
    if not isinstance(value, Mapping):
        log.warning(
            "Invalid guild setting manual_punishment_roles=%r; using default {}",
            value,
        )
        return {}

    parsed: dict[int, ManualPunishmentRoleSettings] = {}
    for raw_role_id, raw_entry in value.items():
        try:
            role_id = int(raw_role_id)
        except (TypeError, ValueError):
            role_id = 0
        if role_id <= 0 or isinstance(raw_role_id, bool) or not isinstance(raw_entry, Mapping):
            log.warning("Ignoring invalid manual punishment role entry %r", raw_role_id)
            continue

        raw_channel_ids = raw_entry.get("source_channel_ids")
        if not isinstance(raw_channel_ids, (list, tuple, set, frozenset)):
            log.warning("Ignoring invalid manual punishment role entry %r", raw_role_id)
            continue
        channel_ids: list[int] = []
        valid_channels = True
        for channel_id in raw_channel_ids:
            if (
                not isinstance(channel_id, int)
                or isinstance(channel_id, bool)
                or channel_id <= 0
            ):
                valid_channels = False
                break
            if channel_id not in channel_ids:
                channel_ids.append(channel_id)
        if not valid_channels or not channel_ids:
            log.warning("Ignoring invalid manual punishment role entry %r", raw_role_id)
            continue

        notification_channel_id = raw_entry.get("notification_channel_id")
        if notification_channel_id is not None and (
            not isinstance(notification_channel_id, int)
            or isinstance(notification_channel_id, bool)
            or notification_channel_id <= 0
        ):
            log.warning("Ignoring invalid manual punishment role entry %r", raw_role_id)
            continue
        parsed[role_id] = ManualPunishmentRoleSettings(
            role_id=role_id,
            source_channel_ids=tuple(channel_ids),
            notification_channel_id=notification_channel_id,
        )
    return parsed


def _enum(
    raw: Mapping[str, object], key: str, enum_type: type[_EnumT], default: _EnumT
) -> _EnumT:
    value = raw.get(key, default.value)
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        log.warning("Invalid guild setting %s=%r; using default %r", key, value, DEFAULTS[key])
        return default


def _optional_enum(
    raw: Mapping[str, object], key: str, enum_type: type[_EnumT]
) -> _EnumT | None:
    value = raw.get(key)
    if value is None:
        return None
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        log.warning("Invalid guild setting %s=%r; using default None", key, value)
        return None


@dataclass(frozen=True)
class GuildSettings:
    enabled: bool
    action: CoreActionOption | None
    fallback_action: FallbackActionOption
    dry_run: bool
    daily_stats_channel: int | None
    manual_evidence_channel: int | None
    manual_punishment_roles: dict[int, ManualPunishmentRoleSettings]
    honeypot_channels: list[int]
    mute_role: int | None
    purge_backward_seconds: int
    purge_forward_seconds: int
    whitelisted_roles: list[int]
    firstpost_collect_enabled: bool
    firstpost_enabled: bool
    firstpost_action: CoreActionOption
    spam_enabled: bool
    spam_action: CoreActionOption
    spam_window_seconds: int
    spam_min_channels: int
    imagescan_detector_enabled: bool
    imagescan_detector_action: ImageScanDetectorActionOption
    imagescan_detector_threshold: int
    review_enabled: bool
    review_channel: int | None
    review_kick_fail_warning: ReviewKickFailWarningMode
    automated_kick_fail_warning: bool
    whitelist_mode: WhitelistModeOption
    stats: dict[str, int]
    scam_keywords: list[str]
    attachment_patterns: list[str]
    gif_detector_enabled: bool
    gif_detector_debug_enabled: bool
    gif_detector_debug_channel: int | None
    gif_detector_animation_enabled: bool
    gif_detector_channels: list[int]
    gif_detector_secondary_message: str
    gif_detector_retention_seconds: int
    gif_detector_threshold: int
    gif_detector_window_seconds: int
    gif_detector_mute_duration_seconds: int
    joinwatch_enabled: bool
    joinwatch_alert_enabled: bool
    joinwatch_channel: int | None
    joinwatch_min_age_hours: int
    joinwatch_auto_role_enabled: bool
    joinwatch_auto_role_id: int | None
    joinwatch_auto_role_timer_minutes: int
    joinwatch_auto_role_action: JoinwatchAutoRoleActionOption
    joinwatch_auto_role_random_delay_enabled: bool
    joinwatch_auto_role_random_delay_min_minutes: int
    joinwatch_auto_role_random_delay_max_minutes: int
    joinwatch_pending_role_assignments: dict[str, dict[str, object]]
    joinwatch_pending_roles: dict[str, dict[str, object]]
    baitrole_enabled: bool
    baitrole_channel: int | None
    baitrole_id: int | None
    baitrole_action: BaitActionOption

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> GuildSettings:
        if not isinstance(raw, Mapping):
            log.warning("Invalid guild settings mapping %r; using defaults", raw)
            raw = {}
        return cls(
            enabled=_bool(raw, "enabled"),
            action=_optional_enum(raw, "action", CoreActionOption),
            fallback_action=_enum(
                raw, "fallback_action", FallbackActionOption, FallbackActionOption.REVIEW
            ),
            dry_run=_bool(raw, "dry_run"),
            daily_stats_channel=_optional_int(raw, "daily_stats_channel"),
            manual_evidence_channel=_optional_int(raw, "manual_evidence_channel"),
            manual_punishment_roles=_manual_punishment_roles(raw),
            honeypot_channels=_list(raw, "honeypot_channels", int),
            mute_role=_optional_int(raw, "mute_role"),
            purge_backward_seconds=_int(raw, "purge_backward_seconds"),
            purge_forward_seconds=_int(raw, "purge_forward_seconds"),
            whitelisted_roles=_list(raw, "whitelisted_roles", int),
            firstpost_collect_enabled=_bool(raw, "firstpost_collect_enabled"),
            firstpost_enabled=_bool(raw, "firstpost_enabled"),
            firstpost_action=_enum(
                raw, "firstpost_action", CoreActionOption, CoreActionOption.REVIEW
            ),
            spam_enabled=_bool(raw, "spam_enabled"),
            spam_action=_enum(raw, "spam_action", CoreActionOption, CoreActionOption.REVIEW),
            spam_window_seconds=_int(raw, "spam_window_seconds"),
            spam_min_channels=_int(raw, "spam_min_channels"),
            imagescan_detector_enabled=_bool(raw, "imagescan_detector_enabled"),
            imagescan_detector_action=_enum(
                raw,
                "imagescan_detector_action",
                ImageScanDetectorActionOption,
                ImageScanDetectorActionOption.REVIEW,
            ),
            imagescan_detector_threshold=_int(raw, "imagescan_detector_threshold"),
            review_enabled=_bool(raw, "review_enabled"),
            review_channel=_optional_int(raw, "review_channel"),
            review_kick_fail_warning=_enum(
                raw,
                "review_kick_fail_warning",
                ReviewKickFailWarningMode,
                ReviewKickFailWarningMode.FALSE,
            ),
            automated_kick_fail_warning=_bool(raw, "automated_kick_fail_warning"),
            whitelist_mode=_enum(
                raw, "whitelist_mode", WhitelistModeOption, WhitelistModeOption.BYPASS
            ),
            stats=_int_dict(raw, "stats"),
            scam_keywords=_list(raw, "scam_keywords", str),
            attachment_patterns=_list(raw, "attachment_patterns", str),
            gif_detector_enabled=_bool(raw, "gif_detector_enabled"),
            gif_detector_debug_enabled=_bool(raw, "gif_detector_debug_enabled"),
            gif_detector_debug_channel=_optional_int(
                raw, "gif_detector_debug_channel"
            ),
            gif_detector_animation_enabled=_bool(
                raw, "gif_detector_animation_enabled"
            ),
            gif_detector_channels=_list(raw, "gif_detector_channels", int),
            gif_detector_secondary_message=_str(
                raw, "gif_detector_secondary_message"
            ),
            gif_detector_retention_seconds=_int(
                raw, "gif_detector_retention_seconds"
            ),
            gif_detector_threshold=_int(raw, "gif_detector_threshold"),
            gif_detector_window_seconds=_int(raw, "gif_detector_window_seconds"),
            gif_detector_mute_duration_seconds=_int(
                raw, "gif_detector_mute_duration_seconds"
            ),
            joinwatch_enabled=_bool(raw, "joinwatch_enabled"),
            joinwatch_alert_enabled=_bool(raw, "joinwatch_alert_enabled"),
            joinwatch_channel=_optional_int(raw, "joinwatch_channel"),
            joinwatch_min_age_hours=_int(raw, "joinwatch_min_age_hours"),
            joinwatch_auto_role_enabled=_bool(raw, "joinwatch_auto_role_enabled"),
            joinwatch_auto_role_id=_optional_int(raw, "joinwatch_auto_role_id"),
            joinwatch_auto_role_timer_minutes=_int(raw, "joinwatch_auto_role_timer_minutes"),
            joinwatch_auto_role_action=_enum(
                raw,
                "joinwatch_auto_role_action",
                JoinwatchAutoRoleActionOption,
                JoinwatchAutoRoleActionOption.NONE,
            ),
            joinwatch_auto_role_random_delay_enabled=_bool(
                raw, "joinwatch_auto_role_random_delay_enabled"
            ),
            joinwatch_auto_role_random_delay_min_minutes=_int(
                raw, "joinwatch_auto_role_random_delay_min_minutes"
            ),
            joinwatch_auto_role_random_delay_max_minutes=_int(
                raw, "joinwatch_auto_role_random_delay_max_minutes"
            ),
            joinwatch_pending_role_assignments=_nested_dict(
                raw, "joinwatch_pending_role_assignments"
            ),
            joinwatch_pending_roles=_nested_dict(raw, "joinwatch_pending_roles"),
            baitrole_enabled=_bool(raw, "baitrole_enabled"),
            baitrole_channel=_optional_int(raw, "baitrole_channel"),
            baitrole_id=_optional_int(raw, "baitrole_id"),
            baitrole_action=_enum(
                raw, "baitrole_action", BaitActionOption, BaitActionOption.BAN
            ),
        )
