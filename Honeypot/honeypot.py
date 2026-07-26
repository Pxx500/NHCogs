import asyncio
import hashlib
import logging
import re
from time import perf_counter
import typing
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import tasks

from AAA3A_utils import Cog
from redbot.core import Config, commands, modlog
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import box

from .detection_cases import (
    ActionIntent,
    AttachmentKey,
    CaseStatus,
    DeleteStatus,
    DetectionCaseStore,
    DetectionSignal,
    NewAttachment,  # noqa: F401 - public module re-export
    NewMessage,  # noqa: F401 - public module re-export
    OPERATION_RESULT_CHANNEL_UNAVAILABLE,
    OPERATION_RESULT_MEMBER_UNAVAILABLE,
    OPERATION_RESULT_SUPERSEDED_BY_MODERATION,
    OPERATION_RESULT_UNSUPPORTED_CHANNEL,
    OperationStatus,  # noqa: F401 - public module re-export
    OperationType,
    effective_action,
)
from .case_review import (
    CaseFeedbackItem,  # noqa: F401 - public module re-export
    CaseReviewService,
    case_feedback_items,
    render_case,
    render_timeline,  # noqa: F401 - public module re-export
)
from .console_dump import ReadOnlyLogBuffer
from .firstpost_store import FirstPostStore
from .imagescan_store import ImageScanStore
from .operations import OperationHandlerRegistry, executor_operation_policy
from .operations.context import (
    DETECTION_FAST_RETRY_LIMIT,
    DETECTION_FAST_RETRY_SECONDS,
    DETECTION_SLOW_RETRY_MINUTES,
    CompletionMode,
    FollowUpKind,
    OperationContext,
    OperationLease,
    OperationOutcome,
    apply_operation_policy,
)
from . import diagnostics
from . import detection_runtime
from . import imagescan
from . import joinwatch
from . import review_publication
from .image_detector import ImageSample
from . import settings
from .settings import (
    BAIT_ACTION_OPTIONS,
    BOOL_OPTIONS,
    CORE_ACTION_OPTIONS,
    DEFAULT_ATTACHMENT_PATTERNS,
    DEFAULT_STATS,
    FALLBACK_ACTION_OPTIONS,
    IMAGE_SCAN_DETECTOR_ACTION_OPTIONS,  # noqa: F401 - public module re-export
    JOINWATCH_AUTO_ROLE_ACTION_OPTIONS,  # noqa: F401 - public module re-export
    REVIEW_KICK_FAIL_WARNING_MODES,
    SCAM_KEYWORDS,
    WHITELIST_MODE_OPTIONS,
    BaitActionOption,  # noqa: F401 - public module re-export
    CoreActionOption,  # noqa: F401 - public module re-export
    FallbackActionOption,  # noqa: F401 - public module re-export
    GuildSettings,
    ImageScanDetectorActionOption,  # noqa: F401 - public module re-export
    JoinwatchAutoRoleActionOption,  # noqa: F401 - public module re-export
    ReviewKickFailWarningMode,  # noqa: F401 - public module re-export
    WhitelistModeOption,
)
from .views import (
    DetectionBulkConfirmationView,  # noqa: F401 - public module re-export
    DetectionCaseView,
    DetectionIndividualView,  # noqa: F401 - public module re-export
    DetectionModerationConfirmationView,  # noqa: F401 - public module re-export
)

_ = Translator("Honeypot", __file__)
log = logging.getLogger("red.Honeypot")
COG_AUTHOR = "Pxx500"
COG_REPO_NAME = "NHCogs"
COG_REPO_URL = "https://github.com/Pxx500/NHCogs"
JOINWATCH_MAX_ACCOUNT_AGE_HOURS = joinwatch.JOINWATCH_MAX_ACCOUNT_AGE_HOURS
CONSOLE_DUMP_USAGE = diagnostics.CONSOLE_DUMP_USAGE

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional metadata enrichment.
    Image = None

PURGE_PERMISSION_REQUIREMENTS = (
    ("View Channel", "view_channel"),
    ("Read Message History", "read_message_history"),
    ("Manage Messages", "manage_messages"),
)
JOINWATCH_RETRY_DELAY_MINUTES = joinwatch.JOINWATCH_RETRY_DELAY_MINUTES
JOINWATCH_MAX_RETRIES = joinwatch.JOINWATCH_MAX_RETRIES
POST_BAN_SWEEP_DELAY_SECONDS = 5
PURGE_MIN_RETENTION_SECONDS = 60
PURGE_BACKWARD_MAX_SECONDS = 3600
PURGE_FORWARD_MAX_SECONDS = 300
SPAM_WINDOW_MIN_SECONDS = 3
SPAM_WINDOW_MAX_SECONDS = 60
SPAM_CHANNEL_MIN = 2
SPAM_CHANNEL_MAX = 10
REVIEW_DUMP_START = diagnostics.REVIEW_DUMP_START
REVIEW_DUMP_MAX_ZIP_BYTES = diagnostics.REVIEW_DUMP_MAX_ZIP_BYTES
REVIEW_DUMP_ATTACHMENT_DELAY_SECONDS = diagnostics.REVIEW_DUMP_ATTACHMENT_DELAY_SECONDS
IMAGE_SCAN_EXTENSIONS = imagescan.IMAGE_SCAN_EXTENSIONS
IMAGE_SCAN_COUNTS = imagescan.IMAGE_SCAN_COUNTS
IMAGE_SCAN_MAX_ATTACHMENTS = imagescan.IMAGE_SCAN_MAX_ATTACHMENTS
IMAGE_SCAN_FEEDBACK_TIMEOUT_SECONDS = imagescan.IMAGE_SCAN_FEEDBACK_TIMEOUT_SECONDS
DETECTION_CAPTURE_DEADLINE_SECONDS = review_publication.DETECTION_CAPTURE_DEADLINE_SECONDS
DETECTION_ATTACHMENT_TIMEOUT_SECONDS = detection_runtime.DETECTION_ATTACHMENT_TIMEOUT_SECONDS
DETECTION_IMAGE_READ_MAX_BYTES = detection_runtime.DETECTION_IMAGE_READ_MAX_BYTES
DETECTION_CAPTURE_CONCURRENCY = review_publication.DETECTION_CAPTURE_CONCURRENCY
DETECTION_HEARTBEAT_INTERVAL_SECONDS = 60.0
IMAGE_SCAN_FEEDBACK_BULK_LABELS = imagescan.IMAGE_SCAN_FEEDBACK_BULK_LABELS


DoctorResult = diagnostics.DoctorResult
DoctorCheck = diagnostics.DoctorCheck


JoinwatchSelectedAction = joinwatch.JoinwatchSelectedAction
JoinwatchSelection = joinwatch.JoinwatchSelection
select_due_joinwatch_assignments = joinwatch.select_due_joinwatch_assignments


ImageScanDecision = imagescan.ImageScanDecision
IMAGE_SCAN_DECISIONS = imagescan.IMAGE_SCAN_DECISIONS
KICK_FAIL_WARNING_REASON = "Suspicious activity: target left before the kick could be applied."
def missing_purge_permissions(permissions: object) -> list[str]:
    if not bool(getattr(permissions, "view_channel", False)):
        return ["View Channel"]
    return [
        name
        for name, attribute in PURGE_PERMISSION_REQUIREMENTS
        if not bool(getattr(permissions, attribute, False))
    ]


def is_purgeable_message_channel(channel: object) -> bool:
    return callable(getattr(channel, "purge", None))


joinwatch_channel_id = joinwatch.joinwatch_channel_id


is_image_attachment = imagescan.is_image_attachment
plan_imagescan_event_cache_cleanup = imagescan.plan_imagescan_event_cache_cleanup
match_imagescan_sample_identifier = imagescan.match_imagescan_sample_identifier
is_imagescan_sample_path_safe = imagescan.is_imagescan_sample_path_safe
summarize_imagescan_sample_storage = imagescan.summarize_imagescan_sample_storage


case_evidence_root = review_publication.case_evidence_root


class MessageRef(typing.NamedTuple):
    channel_id: int
    message_id: int
    created_at: datetime
    fingerprint: str


GENERIC_ATTACHMENT_NAME_RE = re.compile(r"^(?:image(?: ?\(\d+\))?|\d+)$", re.IGNORECASE)
ATTACHMENT_ONLY_SCAM_KEYWORDS = {"bro"}
WORD_KEYWORD_RE = re.compile(r"^[\w ]+$")
IMAGE_ATTACHMENT_EXTENSIONS = imagescan.IMAGE_ATTACHMENT_EXTENSIONS


def keyword_matches_content(keyword: str, content: str) -> bool:
    keyword = keyword.strip().lower()
    if not keyword:
        return False
    if WORD_KEYWORD_RE.fullmatch(keyword):
        return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", content) is not None
    return keyword in content


def matched_scam_keywords(
    keywords: typing.Iterable[str],
    content: str,
    *,
    include_attachment_only: bool = False,
) -> list[str]:
    return [
        keyword
        for keyword in keywords
        if (
            include_attachment_only
            or keyword.strip().lower() not in ATTACHMENT_ONLY_SCAM_KEYWORDS
        )
        and keyword_matches_content(keyword, content)
    ]


def message_spam_fingerprint(message: discord.Message) -> str:
    content = re.sub(r"\s+", " ", message.content.strip().lower())
    attachments = tuple(
        (
            attachment.filename.lower(),
            attachment.size,
            (attachment.content_type or "").lower(),
        )
        for attachment in message.attachments
    )
    raw = repr((content, attachments))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@cog_i18n(_)
class Honeypot(Cog):
    """Detect and review suspicious activity with honeypot channels, image scanning, and join monitoring."""

    def format_help_for_context(self, ctx: commands.Context) -> str:
        help_text = commands.Cog.format_help_for_context(self, ctx)
        return (
            f"{help_text}\n\n"
            f"Author: {COG_AUTHOR}\n"
            f"Cog version: {self.__version__}\n"
            f"Repo name: {COG_REPO_NAME}\n"
            f"Repository: {COG_REPO_URL}"
        )

    def __init__(self, bot: Red) -> None:
        super().__init__(bot=bot)

        self.config: Config = Config.get_conf(
            self,
            identifier=205192943327321000143939875896557571750,
            force_registration=True,
        )
        self.config.register_guild(**settings.DEFAULTS)

        self._console_log_buffer = ReadOnlyLogBuffer()
        self._post_ban_sweep_tasks: set[asyncio.Task] = set()
        self._case_review_tasks: set[asyncio.Task] = set()
        self._recent_user_messages: dict[int, dict[int, deque[MessageRef]]] = defaultdict(
            lambda: defaultdict(deque)
        )
        self._hot_purge_users: dict[int, dict[int, datetime]] = defaultdict(dict)
        self._firstpost_db_path = cog_data_path(self) / "firstpost_seen.sqlite"
        self._firstpost_store = FirstPostStore(self._firstpost_db_path)
        self._firstpost_db_lock: asyncio.Lock = asyncio.Lock()
        self._firstpost_seen_authors: dict[int, set[int]] = defaultdict(set)
        self._firstpost_dirty_seen_authors: dict[int, set[int]] = defaultdict(set)
        self._firstpost_loaded_guilds: set[int] = set()
        self._review_dump_lock: asyncio.Lock = asyncio.Lock()
        self._imagescan_db_path = cog_data_path(self) / "imagescan.sqlite"
        self._imagescan_files_path = cog_data_path(self) / "imagescan_files"
        self._imagescan_store = ImageScanStore(
            self._imagescan_db_path,
            self._imagescan_files_path,
        )
        self._imagescan_db_lock: asyncio.Lock = asyncio.Lock()
        self._detection_case_db_path = cog_data_path(self) / "detection_cases.sqlite"
        self._detection_case_files_path = cog_data_path(self) / "detection_case_files"
        self._case_store = DetectionCaseStore(self._detection_case_db_path)
        self._case_review_service = CaseReviewService(self._case_store)
        self._case_views: dict[str, object] = {}
        self._case_restore_task: asyncio.Task | None = None
        self._initial_image_scan_tasks: set[asyncio.Task] = set()
        self._initial_image_scan_batches: dict[
            tuple[int, int], dict[int, asyncio.Task]
        ] = {}
        self._detection_case_evidence_lock: asyncio.Lock = asyncio.Lock()
        self._detection_case_capture_slots = asyncio.Semaphore(
            DETECTION_CAPTURE_CONCURRENCY
        )
        self._detection_admission_locks = tuple(asyncio.Lock() for _ in range(64))
        self._detection_publication_locks = tuple(asyncio.Lock() for _ in range(64))
        self._detection_heartbeat_interval_seconds = DETECTION_HEARTBEAT_INTERVAL_SECONDS
        self._detection_operation_handlers = OperationHandlerRegistry()

    async def red_delete_data_for_user(
        self, *, requester: typing.Any, user_id: int
    ) -> None:
        """Delete detection-case metadata and evidence associated with a Red user."""
        await review_publication._delete_detection_case_scope(
            self, self._case_store.plan_user_case_deletion, user_id
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Delete detection-case metadata and evidence when Red leaves a guild."""
        await review_publication._delete_detection_case_scope(
            self, self._case_store.plan_guild_case_deletion, guild.id
        )

    async def cog_after_invoke(self, ctx: commands.Context) -> commands.Context | None:
        """Finish command cleanup without AAA3A_utils' redundant success reaction."""
        if isinstance(ctx.command, commands.Group) and (
            ctx.invoked_subcommand is not None or not ctx.command.invoke_without_command
        ):
            return None
        if ctx.command_failed:
            return await super().cog_after_invoke(ctx)
        typing = getattr(ctx, "_typing", None)
        task = getattr(typing, "task", None)
        if callable(getattr(task, "cancel", None)):
            task.cancel()
        return ctx

    async def _increment_stat(self, guild: discord.Guild, key: str, amount: int = 1) -> None:
        guild_config = getattr(self.config, "guild", None)
        if not callable(guild_config):
            return
        async with guild_config(guild).stats() as stats:
            stats.setdefault(key, 0)
            stats[key] += amount

    async def _record_detection_stats(
        self, guild: discord.Guild, signals: tuple[DetectionSignal, ...]
    ) -> None:
        signals = tuple(
            signal
            for signal in signals
            if not signal.metadata.get("whitelist_bypass")
        )
        if not signals:
            return
        await self._increment_stat(guild, "detections")
        if any(signal.decisive for signal in signals):
            await self._increment_stat(guild, "suspicious")
        for detector, prefix, catch_key in (
            ("honeypot", "honeypot", "honeypot_catches"),
            ("firstpost", "firstpost", "early_catches"),
            ("spam", "spam", "spam_catches"),
            ("image", "image", "image_catches"),
        ):
            detector_signals = tuple(
                signal for signal in signals if signal.detector == detector
            )
            if not detector_signals:
                continue
            await self._increment_stat(guild, f"{prefix}_hits")
            if any(signal.decisive for signal in detector_signals):
                await self._increment_stat(guild, catch_key)
            intents = {signal.action for signal in detector_signals}
            for intent, suffix in (
                (ActionIntent.REVIEW, "reviews"),
                (ActionIntent.KICK, "kicks"),
                (ActionIntent.BAN, "bans"),
            ):
                if intent in intents:
                    await self._increment_stat(guild, f"{prefix}_{suffix}")

    async def _init_firstpost_seen_store(self) -> None:
        await asyncio.to_thread(self._firstpost_store.initialize)

    # Imagescan seam: the detection path and its tests reach these through
    # `self`, so the cog keeps a one-line delegation while the implementation
    # lives in `imagescan.py`. Reclaimed when `detection.py` lands and the
    # caller becomes a module.
    async def _init_imagescan_store(self) -> None:
        return await imagescan._init_imagescan_store(self)

    async def _imagescan_load_samples(self, guild_id: int) -> list[ImageSample]:
        return await imagescan._imagescan_load_samples(self, guild_id)

    async def _imagescan_model_state(self, guild_id: int, configured_threshold: int) -> dict[str, typing.Any]:
        return await imagescan._imagescan_model_state(self, guild_id, configured_threshold)

    async def _imagescan_profile(self, guild_id: int) -> dict[str, int]:
        return await imagescan._imagescan_profile(self, guild_id)

    async def _imagescan_increment_profile(self, guild_id: int, increments: dict[str, int]) -> None:
        return await imagescan._imagescan_increment_profile(self, guild_id, increments)

    async def _imagescan_add_file_sample(
        self,
        guild_id: int,
        source_path: Path,
        decision: str,
        moderator_id: int | None,
    ) -> tuple[str, dict[str, typing.Any] | None]:
        return await imagescan._imagescan_add_file_sample(
            self, guild_id, source_path, decision, moderator_id
        )

    async def _count_firstpost_seen_authors(self, guild_id: int) -> int:
        return await asyncio.to_thread(self._firstpost_store.count, guild_id)

    async def _ensure_firstpost_seen_loaded(self, guild_id: int) -> None:
        if guild_id in self._firstpost_loaded_guilds:
            return
        async with self._firstpost_db_lock:
            if guild_id in self._firstpost_loaded_guilds:
                return
            seen = await asyncio.to_thread(self._firstpost_store.load_guild, guild_id)
            self._firstpost_seen_authors[guild_id].update(seen)
            self._firstpost_loaded_guilds.add(guild_id)

    async def _flush_firstpost_seen_authors(self) -> None:
        async with self._firstpost_db_lock:
            dirty = {
                guild_id: set(user_ids)
                for guild_id, user_ids in self._firstpost_dirty_seen_authors.items()
                if user_ids
            }
        if not dirty:
            return
        for guild_id, user_ids in dirty.items():
            await asyncio.to_thread(self._firstpost_store.flush, guild_id, user_ids)
        async with self._firstpost_db_lock:
            for guild_id, user_ids in dirty.items():
                remaining = self._firstpost_dirty_seen_authors.get(guild_id)
                if remaining is None:
                    continue
                remaining.difference_update(user_ids)
                if not remaining:
                    self._firstpost_dirty_seen_authors.pop(guild_id, None)

    async def _is_joinwatch_active_role(
        self,
        guild: discord.Guild,
        member_id: int,
        role_id: int,
    ) -> bool:
        pending_roles = await self.config.guild(guild).joinwatch_pending_roles()
        pending_role = pending_roles.get(str(member_id))
        if pending_role is None:
            return False
        try:
            return int(pending_role["role_id"]) == role_id
        except (KeyError, TypeError, ValueError):
            return False

    async def _remove_review_mute_role(
        self,
        member: discord.Member,
        role: discord.Role,
        reason: str,
    ) -> bool:
        if await self._is_joinwatch_active_role(member.guild, member.id, role.id):
            return True
        try:
            await member.remove_roles(role, reason=reason)
        except discord.NotFound:
            return True
        except discord.HTTPException:
            return False
        return True

    async def _get_member_or_fetch(self, guild: discord.Guild, member_id: int) -> discord.Member | None:
        member = guild.get_member(member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return None

    async def _get_user_or_object(self, user_id: int) -> discord.User | discord.Object:
        try:
            return await self.bot.fetch_user(user_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return discord.Object(id=user_id)

    def _automated_kick_fail_warning_enabled(self, enabled: bool) -> bool:
        return enabled

    async def _create_kick_fail_warning(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        moderator: typing.Any = None,
    ) -> tuple[str | None, str | None]:
        user = await self._get_user_or_object(user_id)
        try:
            await modlog.create_case(
                self.bot,
                guild,
                datetime.now(timezone.utc),
                action_type="warning",
                user=user,
                moderator=moderator or guild.me,
                reason=KICK_FAIL_WARNING_REASON,
            )
        except Exception:
            log.exception("Failed to create kick-fail warning case for user %s in guild %s", user_id, guild.id)
            return (None, _("I couldn't create a warning case."))
        return (_("Warning applied: suspicious kick avoidance."), None)

    def _format_options(self, options: tuple[str, ...]) -> str:
        return ", ".join(f"`{option}`" for option in options)

    def _get_text_channel_or_thread(
        self, guild: discord.Guild, channel_id: int | None
    ) -> discord.TextChannel | discord.Thread | None:
        if channel_id is None:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None and hasattr(guild, "get_thread"):
            channel = guild.get_thread(channel_id)
        if channel is None:
            channel = self.bot.get_channel(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def _fetch_text_channel_or_thread(
        self, guild: discord.Guild, channel_id: int | None
    ) -> discord.TextChannel | discord.Thread | None:
        channel = self._get_text_channel_or_thread(guild, channel_id)
        if channel is not None or channel_id is None:
            return channel
        fetch_channel = getattr(guild, "fetch_channel", None)
        if not callable(fetch_channel):
            return None
        try:
            channel = await fetch_channel(channel_id)
        except discord.NotFound:
            return None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def _fetch_message_channel(
        self, guild: discord.Guild, channel_id: int | None
    ) -> typing.Any | None:
        if channel_id is None:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None and hasattr(guild, "get_thread"):
            channel = guild.get_thread(channel_id)
        if channel is None:
            channel = self.bot.get_channel(channel_id)
        if channel is None:
            fetch_channel = getattr(guild, "fetch_channel", None)
            if not callable(fetch_channel):
                raise RuntimeError("detection source channel cannot be fetched")
            try:
                channel = await fetch_channel(channel_id)
            except discord.NotFound:
                return None
        if not callable(getattr(channel, "fetch_message", None)):
            raise RuntimeError("detection source channel cannot resolve messages")
        return channel

    def _missing_channel_permissions(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel | discord.Thread,
        *,
        send_messages: bool = True,
        read_history: bool = False,
        manage_messages: bool = False,
        create_public_threads: bool = False,
        send_in_threads: bool = False,
        embed_links: bool = False,
        attach_files: bool = False,
        manage_threads: bool = False,
    ) -> str | None:
        me = guild.me
        if me is None:
            return _("I couldn't find my server member.")
        perms = channel.permissions_for(me)
        if not perms.view_channel:
            return _("I need `View Channel` in {channel}.").format(channel=channel.mention)
        if send_messages and not perms.send_messages:
            return _("I need `Send Messages` in {channel}.").format(channel=channel.mention)
        if read_history and not perms.read_message_history:
            return _("I need `Read Message History` in {channel}.").format(channel=channel.mention)
        if manage_messages and not perms.manage_messages:
            return _("I need `Manage Messages` in {channel}.").format(channel=channel.mention)
        if create_public_threads and not perms.create_public_threads:
            return _("I need `Create Public Threads` in {channel}.").format(
                channel=channel.mention
            )
        if send_in_threads and not perms.send_messages_in_threads:
            return _("I need `Send Messages in Threads` in {channel}.").format(
                channel=channel.mention
            )
        if embed_links and not perms.embed_links:
            return _("I need `Embed Links` in {channel}.").format(
                channel=channel.mention
            )
        if attach_files and not perms.attach_files:
            return _("I need `Attach Files` in {channel}.").format(
                channel=channel.mention
            )
        if manage_threads and not perms.manage_threads:
            return _("I need `Manage Threads` in {channel}.").format(
                channel=channel.mention
            )
        return None

    def _format_channel_setting(self, guild: discord.Guild, channel_id: int | None) -> str:
        channel = self._get_text_channel_or_thread(guild, channel_id)
        if channel is not None:
            return f"{channel.mention} ({channel.id})"
        return _("not set") if channel_id is None else _("missing ({id})").format(id=channel_id)

    def _honeypot_channel_ids(
        self,
        honeypot_channels: typing.Iterable[object],
        legacy_channel_id: object,
    ) -> list[int]:
        channel_ids: list[int] = []
        for channel_id in honeypot_channels:
            if isinstance(channel_id, int) and channel_id not in channel_ids:
                channel_ids.append(channel_id)
        if isinstance(legacy_channel_id, int) and legacy_channel_id not in channel_ids:
            channel_ids.append(legacy_channel_id)
        return channel_ids

    def _format_honeypot_channel_list(self, guild: discord.Guild, channel_ids: list[int]) -> str:
        if not channel_ids:
            return _("not set")
        return "\n".join(
            f"{index}. {self._format_channel_setting(guild, channel_id)}"
            for index, channel_id in enumerate(channel_ids, 1)
        )

    def _format_role_setting(self, guild: discord.Guild, role_id: int | None) -> str:
        role = guild.get_role(role_id) if role_id else None
        if role is not None:
            return f"{role.mention} ({role.id})"
        return _("not set") if role_id is None else _("missing ({id})").format(id=role_id)

    def _format_bool_setting(self, value: bool) -> str:
        return _("enabled") if value else _("disabled")

    async def _send_config_dump(
        self,
        ctx: commands.Context,
        title: str,
        entries: list[tuple[str, typing.Any]],
    ) -> None:
        lines = [f"{label}: {value}" for label, value in entries]
        await ctx.send(_("{title}:\n").format(title=title) + box("\n".join(lines)))

    def _dry_run_label(self, action: str) -> str:
        if action == "ban":
            return _("Dry run: I would ban this member.")
        if action == "kick":
            return _("Dry run: I would kick this member.")
        return _("Dry run: I would not take action.")

    @staticmethod
    def _ban_delete_message_seconds() -> int:
        return 0

    def _missing_action_permission(self, guild: discord.Guild, action: str) -> str | None:
        me = guild.me
        if me is None:
            return _("**Failed:** I couldn't find my server member.")
        permissions = me.guild_permissions
        if action == "kick" and not permissions.kick_members:
            return _("**Failed:** I do not have the `Kick Members` permission.")
        if action == "ban" and not permissions.ban_members:
            return _("**Failed:** I do not have the `Ban Members` permission.")
        return None

    def _missing_role_assignment_permission(self, guild: discord.Guild, role: discord.Role) -> str | None:
        me = guild.me
        if me is None:
            return _("I couldn't find my server member.")
        if not me.guild_permissions.manage_roles:
            return _("I need `Manage Roles` permission to apply the joinwatch auto-role.")
        if me.top_role <= role:
            return _("My top role must be above the joinwatch auto-role.")
        return None

    async def _is_protected_member(
        self,
        member: discord.Member | discord.User,
        guild: discord.Guild | None = None,
    ) -> bool:
        if member.id in getattr(self.bot, "owner_ids", ()):
            return True
        member_guild = getattr(member, "guild", None)
        guild = member_guild or guild
        if guild is None:
            return True
        if member_guild is None or not hasattr(member, "guild_permissions"):
            resolved_member = guild.get_member(member.id)
            if resolved_member is None:
                fetch_member = getattr(guild, "fetch_member", None)
                if not callable(fetch_member):
                    await self._record_operational_failure(
                        guild.id,
                        "member_resolution",
                        f"Could not resolve guild member {member.id}: lookup unavailable",
                    )
                    return True
                try:
                    resolved_member = await fetch_member(member.id)
                except discord.NotFound:
                    return False
                except discord.HTTPException as error:
                    await self._record_operational_failure(
                        guild.id,
                        "member_resolution",
                        f"Could not resolve guild member {member.id}: {error}",
                    )
                    return True
            member = resolved_member
        me = guild.me
        if me is None:
            return True
        return (
            await self.bot.is_mod(member)
            or await self.bot.is_admin(member)
            or member.guild_permissions.manage_guild
            or member.top_role >= me.top_role
        )

    def _format_bytes(self, size: int) -> str:
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        if size >= 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size} B"

    def _install_console_log_buffer(self) -> None:
        root_logger = logging.getLogger()
        if self._console_log_buffer not in root_logger.handlers:
            root_logger.addHandler(self._console_log_buffer)

    def _remove_console_log_buffer(self) -> None:
        root_logger = logging.getLogger()
        if self._console_log_buffer in root_logger.handlers:
            root_logger.removeHandler(self._console_log_buffer)

    async def cog_load(self) -> None:
        await super().cog_load()
        await self._init_firstpost_seen_store()
        await self._init_imagescan_store()
        self._detection_case_files_path.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._case_store.initialize)
        await self._run_detection_reconciliation()
        self.joinwatch_auto_role_loop.start()
        self.purge_cache_cleanup_loop.start()
        self.firstpost_seen_flush_loop.start()
        self.detection_case_loop.start()
        self.detection_reconciliation_loop.start()
        self._case_restore_task = asyncio.create_task(self._restore_detection_case_views())
        self._case_restore_task.add_done_callback(
            lambda task: self._observe_background_task(task, "detection case view restoration")
        )
        self._install_console_log_buffer()

    @staticmethod
    def _observe_background_task(task: asyncio.Task, label: str) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            log.error(
                "%s failed",
                label,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _cancel_owned_task(self, task: asyncio.Task | None) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # The done callback observes and logs task failures.
            pass

    async def cog_unload(self) -> None:
        self._remove_console_log_buffer()
        self.joinwatch_auto_role_loop.cancel()
        self.purge_cache_cleanup_loop.cancel()
        self.firstpost_seen_flush_loop.cancel()
        self.detection_case_loop.cancel()
        self.detection_reconciliation_loop.cancel()
        try:
            await self._cancel_owned_task(self._case_restore_task)
        finally:
            self._case_restore_task = None
        pending_sweeps = tuple(self._post_ban_sweep_tasks)
        for task in pending_sweeps:
            task.cancel()
        if pending_sweeps:
            await asyncio.gather(*pending_sweeps, return_exceptions=True)
        self._post_ban_sweep_tasks.clear()
        pending_case_reviews = tuple(self._case_review_tasks)
        for task in pending_case_reviews:
            task.cancel()
        if pending_case_reviews:
            await asyncio.gather(*pending_case_reviews, return_exceptions=True)
        self._case_review_tasks.clear()
        pending_scans = tuple(self._initial_image_scan_tasks)
        for task in pending_scans:
            task.cancel()
        if pending_scans:
            await asyncio.gather(*pending_scans, return_exceptions=True)
        self._initial_image_scan_tasks.clear()
        self._initial_image_scan_batches.clear()
        await self._flush_firstpost_seen_authors()
        await super().cog_unload()

    @tasks.loop(seconds=60)
    async def firstpost_seen_flush_loop(self) -> None:
        await self._flush_firstpost_seen_authors()

    @tasks.loop(minutes=1)
    async def purge_cache_cleanup_loop(self) -> None:
        raw_configs = await self.config.all_guilds()
        guild_settings_by_id = {
            int(guild_id): GuildSettings.from_mapping(raw_config)
            for guild_id, raw_config in raw_configs.items()
        }
        self._prune_purge_cache(guild_settings_by_id)

    @tasks.loop(minutes=1)
    async def detection_case_loop(self) -> None:
        await self._run_detection_case_expiry()

    @detection_case_loop.before_loop
    async def before_detection_case_loop(self) -> None:
        await self.bot.wait_until_red_ready()

    @tasks.loop(seconds=DETECTION_FAST_RETRY_SECONDS)
    async def detection_reconciliation_loop(self) -> None:
        await self._run_detection_reconciliation()

    @detection_reconciliation_loop.before_loop
    async def before_detection_reconciliation_loop(self) -> None:
        await self.bot.wait_until_red_ready()

    async def _run_detection_case_expiry(self) -> None:
        now = datetime.now(timezone.utc)
        due_cases = await asyncio.to_thread(self._case_store.list_due_cases, now)
        for case in due_cases:
            await self.resolve_detection_case(case.case_id, "expired", now=now)

    async def _send_operational_alert(self, guild_id: int, content: str) -> None:
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            raw_config = await self.config.guild_from_id(guild_id).all()
            guild_settings = GuildSettings.from_mapping(raw_config)
            channel = self._get_text_channel_or_thread(
                guild, guild_settings.logs_channel
            )
            if channel is None:
                return
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            log.warning("Could not publish Honeypot operational alert", exc_info=True)

    async def _record_operational_failure(
        self,
        guild_id: int,
        source: OperationType | str,
        summary: str,
        *,
        case_id: str | None = None,
        operation_id: str | None = None,
        attempts: int = 1,
        terminal: bool = False,
    ) -> None:
        source_value = source.value if isinstance(source, OperationType) else source
        try:
            failure = await asyncio.to_thread(
                self._case_store.record_operational_failure,
                guild_id=guild_id,
                source=source,
                summary=summary,
                occurred_at=datetime.now(timezone.utc),
                case_id=case_id,
                operation_id=operation_id,
            )
        except Exception:
            log.exception("Could not persist Honeypot operational failure")
            return
        slow_retry_started = (
            not terminal and attempts == DETECTION_FAST_RETRY_LIMIT + 1
        )
        if failure.occurrences == 1 or slow_retry_started:
            if terminal:
                state = "terminal"
            elif slow_retry_started:
                state = "fast retries exhausted; slow retry scheduled"
            else:
                state = "will retry"
            await self._send_operational_alert(
                guild_id,
                f"⚠️ Honeypot operation failed ({source_value}, attempt {attempts}, {state}): "
                f"{summary[:500]}",
            )

    async def _run_detection_reconciliation(
        self, *, now: datetime | None = None
    ) -> None:
        try:
            await review_publication._retry_detection_orphan_publications(self)
        except Exception:
            log.warning("Detection orphan publication retry failed", exc_info=True)
        try:
            await review_publication._retry_detection_case_deletions(self)
        except Exception:
            log.warning("Detection case deletion retry failed", exc_info=True)
        current_time = now or datetime.now(timezone.utc)
        stale_before = current_time - timedelta(minutes=5)
        await asyncio.to_thread(
            self._case_store.reconcile_moderator_actions, current_time
        )
        operations = await asyncio.to_thread(
            self._case_store.claim_due_operations,
            current_time,
            50,
            stale_before,
        )
        for operation in operations:
            await self._execute_detection_case_operation(operation, current_time)
        cases = await asyncio.to_thread(
            self._case_store.list_reconcilable_cases, current_time, stale_before
        )
        for case in cases:
            await self.resolve_detection_case(
                case.case_id, "expired", now=current_time
            )

    async def resolve_detection_case(
        self,
        case_id: str,
        resolution: str,
        moderator_id: int | None = None,
        *,
        now: datetime | None = None,
        defer_final_operations: bool = False,
    ) -> bool:
        resolved_at = now or datetime.now(timezone.utc)
        lease = await asyncio.to_thread(
            self._case_store.claim_resolution,
            case_id,
            resolved_at,
            resolved_at - timedelta(minutes=5),
            require_terminal_captures=resolution == "ignore",
        )
        if lease is None:
            return False
        try:
            status = CaseStatus.EXPIRED if resolution == "expired" else CaseStatus.RESOLVED
            decision = {
                "tp": "true_positive",
                "fp": "false_positive",
                "ignore": "ignored",
            }.get(resolution.removeprefix("images:"))
            snapshot = await asyncio.to_thread(self._case_store.get_case, case_id)
            decisions = (
                {item.key: decision for item in case_feedback_items(snapshot)}
                if snapshot is not None and decision is not None
                else None
            )
            owned_role_ids = await asyncio.to_thread(
                self._case_store.owned_role_ids, case_id
            )
            final_operations = [
                (OperationType.REVIEW_UPDATE, f"review-update:{case_id}"),
                (OperationType.EVIDENCE_CLEANUP, f"evidence-cleanup:{case_id}"),
            ]
            for role_id in owned_role_ids:
                final_operations.append(
                    (
                        OperationType.ROLE_RELEASE,
                        f"role-release:{case_id}:{int(role_id)}",
                    )
                )
            finished = await asyncio.to_thread(
                self._case_store.finish_resolution,
                lease,
                status,
                resolution,
                moderator_id,
                resolved_at,
                decisions,
                tuple(final_operations),
            )
        except BaseException:
            await asyncio.to_thread(self._case_store.release_resolution, lease)
            raise
        if not finished:
            return False
        snapshot = await asyncio.to_thread(self._case_store.get_case, case_id)
        guild = self.bot.get_guild(snapshot.case.guild_id)
        if guild is not None and resolution == "expired":
            await self._increment_stat(guild, "review_expired")
        elif guild is not None and resolution == "ignore":
            await self._increment_stat(guild, "ignored")
        if defer_final_operations:
            return True
        await self._execute_case_final_operations(case_id, resolved_at)
        return True

    async def _execute_case_final_operations(
        self,
        case_id: str,
        now: datetime,
    ) -> None:
        snapshot = await asyncio.to_thread(self._case_store.get_case, case_id)
        if snapshot is None:
            return
        final_operation_priority: dict[OperationType | str, int] = {
            OperationType.REVIEW_UPDATE: 0,
            OperationType.ROLE_RELEASE: 1,
            OperationType.EVIDENCE_CLEANUP: 2,
        }
        for operation in sorted(
            snapshot.operations,
            key=lambda item: (
                final_operation_priority.get(item.operation_type, 99),
                item.operation_id,
            ),
        ):
            if operation.operation_type not in {
                OperationType.REVIEW_UPDATE,
                OperationType.ROLE_RELEASE,
                OperationType.EVIDENCE_CLEANUP,
            }:
                continue
            claimed = await asyncio.to_thread(
                self._case_store.claim_operation, operation.operation_id, now
            )
            if claimed is not None:
                await self._execute_detection_case_operation(claimed, now)

    async def _execute_detection_message_child(
        self,
        snapshot,
        operation_type: OperationType,
        sequence: int,
        now: datetime,
        *,
        publication_channel=None,
    ) -> bool:
        operation = next(
            (
                item
                for item in snapshot.operations
                if item.operation_type == operation_type
                and item.message_sequence == sequence
            ),
            None,
        )
        if operation is None:
            return False
        claim_time = now
        if operation.status.value == "failed" and operation.retry_at is not None:
            claim_time = max(claim_time, operation.retry_at)
        claimed = await asyncio.to_thread(
            self._case_store.claim_operation, operation.operation_id, claim_time
        )
        if claimed is not None:
            await self._execute_detection_case_operation(
                claimed,
                claim_time,
                publication_channel=publication_channel,
            )
            return True
        return False

    async def _release_detection_case_roles(
        self, case_id: str, now: datetime
    ) -> None:
        role_ids = await asyncio.to_thread(self._case_store.owned_role_ids, case_id)
        for role_id in role_ids:
            operation = await asyncio.to_thread(
                self._case_store.ensure_operation,
                case_id,
                OperationType.ROLE_RELEASE,
                f"role-release:{case_id}:{int(role_id)}",
            )
            claimed = await asyncio.to_thread(
                self._case_store.claim_operation, operation.operation_id, now
            )
            if claimed is not None:
                await self._execute_detection_case_operation(claimed, now)

    @staticmethod
    def _persisted_capture_results(snapshot, sequence: int):
        terminal_statuses = {
            status.value for status in detection_runtime.CaptureStatus
        }
        return tuple(
            detection_runtime.CaptureResult(
                attachment.position,
                detection_runtime.CaptureStatus(attachment.capture_status),
                Path(attachment.evidence_path)
                if attachment.evidence_path is not None
                else None,
                attachment.error,
            )
            for attachment in snapshot.attachments
            if attachment.message_sequence == sequence
            and attachment.capture_status in terminal_statuses
        )

    @asynccontextmanager
    async def _operation_lease(
        self, operation
    ) -> typing.AsyncIterator[OperationLease]:
        heartbeat = asyncio.create_task(self._renew_detection_operation(operation))
        try:
            yield OperationLease(
                operation_id=operation.operation_id,
                claim_token=operation.claim_token,
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _settle_detection_operation_failure(
        self,
        operation,
        lease: OperationLease,
        now: datetime,
        snapshot,
        outcome: OperationOutcome,
        error: Exception,
        operation_type_value: str,
    ) -> None:
        retry_at = now + (
            timedelta(seconds=DETECTION_FAST_RETRY_SECONDS)
            if operation.attempts <= DETECTION_FAST_RETRY_LIMIT
            else timedelta(minutes=DETECTION_SLOW_RETRY_MINUTES)
        )
        if operation.operation_type == OperationType.SOURCE_DELETE:
            retry_at = now + timedelta(seconds=DETECTION_FAST_RETRY_SECONDS)
        cached_purge_exhausted = (
            operation.operation_type == OperationType.CACHED_PURGE
            and outcome.result == DeleteStatus.TRANSIENT_FAILURE.value
            and operation.attempts >= 3
        )
        if operation.operation_type == OperationType.CACHED_PURGE and (
            outcome.result
            in (
                DeleteStatus.FORBIDDEN.value,
                OPERATION_RESULT_CHANNEL_UNAVAILABLE,
                OPERATION_RESULT_UNSUPPORTED_CHANNEL,
            )
            or cached_purge_exhausted
        ):
            retry_at = None
            await asyncio.to_thread(
                self._case_store.mark_case_needs_attention, operation.case_id
            )
        failure = await asyncio.to_thread(
            self._case_store.fail_operation,
            lease.operation_id,
            lease.claim_token,
            f"{type(error).__name__}: {error}",
            now,
            retry_at,
            outcome.result,
        )
        if failure and snapshot is not None:
            await self._record_operational_failure(
                snapshot.case.guild_id,
                operation.operation_type,
                f"{type(error).__name__}: {error}",
                case_id=operation.case_id,
                operation_id=operation.operation_id,
                attempts=operation.attempts,
                terminal=retry_at is None,
            )
        if operation.operation_type == OperationType.ROLE_APPLY:
            await review_publication._case_review_rerender_safely(self, operation.case_id)
        if operation.operation_type == OperationType.ROLE_APPLY and snapshot is not None:
            failed_guild = self.bot.get_guild(snapshot.case.guild_id)
            if failed_guild is not None:
                await self._increment_stat(failed_guild, "pending_mute_failures")
        log.warning(
            "Detection case operation failed case=%s operation=%s kind=%s error=%s",
            operation.case_id,
            operation.operation_id,
            operation_type_value,
            error,
        )

    async def _settle_detection_operation_success(
        self,
        context: OperationContext,
        outcome: OperationOutcome,
    ) -> OperationOutcome:
        operation = context.operation
        if outcome.completion_mode is CompletionMode.MODERATOR_ACTION:
            completed = await asyncio.to_thread(
                self._case_store.complete_moderator_action,
                context.lease.operation_id,
                context.lease.claim_token,
                context.now,
                outcome.result,
            )
        else:
            completed = await asyncio.to_thread(
                self._case_store.complete_operation,
                context.lease.operation_id,
                context.lease.claim_token,
                context.now,
                outcome.result,
            )
        if not completed:
            current_case = await asyncio.to_thread(
                self._case_store.get_case, operation.case_id
            )
            if current_case is not None:
                raise RuntimeError(
                    "detection case operation lease was lost before completion"
                )
        elif context.snapshot is not None and (
            operation.attempts > 1 or outcome.resolve_failure_on_first_attempt
        ):
            recovered = await asyncio.to_thread(
                self._case_store.resolve_operational_failure,
                operation.operation_id,
                context.now,
            )
            if recovered and operation.attempts > 1:
                await self._send_operational_alert(
                    context.snapshot.case.guild_id,
                    f"✅ Recovered: {operation.operation_type.value} succeeded after "
                    f"{operation.attempts} attempts.",
                )
        elif outcome.role_was_added and context.snapshot is not None:
            guild = self.bot.get_guild(context.snapshot.case.guild_id)
            if guild is not None:
                await self._increment_stat(guild, "pending_mutes")
        return replace(outcome, completed=completed)

    async def _run_detection_operation_follow_ups(
        self,
        context: OperationContext,
        outcome: OperationOutcome,
    ) -> None:
        operation = context.operation
        for follow_up in outcome.follow_ups:
            if follow_up.requires_completion and not outcome.completed:
                continue
            if follow_up.kind is FollowUpKind.ROLE_APPLY_RERENDER:
                if operation.attempts > 1 or outcome.result in {
                    OPERATION_RESULT_SUPERSEDED_BY_MODERATION,
                    OPERATION_RESULT_MEMBER_UNAVAILABLE,
                }:
                    await review_publication._case_review_rerender_safely(self, operation.case_id)
            elif follow_up.kind is FollowUpKind.COMPACT_TERMINAL_CASE:
                await asyncio.to_thread(
                    self._case_store.compact_terminal_case, operation.case_id
                )
            elif follow_up.kind is FollowUpKind.FINISH_MODERATION:
                await self._finish_case_review_if_ready(
                    operation.case_id,
                    operation.actor_id,
                )
                await review_publication._case_review_rerender_safely(self, operation.case_id)
            elif follow_up.kind is FollowUpKind.FINISH_MESSAGE_PROCESS:
                await self._finish_case_review_if_ready(operation.case_id, None)

    async def _execute_detection_case_operation(
        self,
        operation,
        now: datetime,
        *,
        publication_channel=None,
        live_message=None,
        timings: dict[str, float] | None = None,
    ) -> None:
        lease_context = self._operation_lease(operation)
        lease = await lease_context.__aenter__()
        operation_type_value = (
            operation.operation_type.value
            if isinstance(operation.operation_type, OperationType)
            else operation.operation_type
        )
        snapshot = None
        context = None
        operation_outcome = OperationOutcome()
        operation_error = None
        cancellation = None
        try:
            snapshot = await asyncio.to_thread(self._case_store.get_case, operation.case_id)
            if snapshot is None:
                return
            context = OperationContext(
                operation=operation,
                snapshot=snapshot,
                lease=lease,
                now=now,
                publication_channel=publication_channel,
                live_message=live_message,
                timings=timings,
            )
            operation_policy = executor_operation_policy(operation.operation_type)
            handler = (
                self._detection_operation_handlers.resolve(operation.operation_type)
                if operation_policy is not None
                else None
            )
            if handler is None:
                raise RuntimeError(
                    "unsupported detection case operation: "
                    f"{operation_type_value}"
                )
            operation_outcome = apply_operation_policy(
                await handler(self, context), operation_policy
            )
            if operation_outcome.error is not None:
                raise operation_outcome.error
        except asyncio.CancelledError as error:
            cancellation = error
        except Exception as error:
            operation_error = error
        finally:
            await lease_context.__aexit__(None, None, None)
        if cancellation is not None:
            raise cancellation
        if operation_error is not None:
            await self._settle_detection_operation_failure(
                operation,
                lease,
                now,
                snapshot,
                replace(operation_outcome, error=operation_error),
                operation_error,
                operation_type_value,
            )
            return
        operation_outcome = await self._settle_detection_operation_success(
            context,
            operation_outcome,
        )
        await self._run_detection_operation_follow_ups(context, operation_outcome)

    async def _renew_detection_operation(self, operation) -> None:
        while True:
            await asyncio.sleep(self._detection_heartbeat_interval_seconds)
            renewed = await asyncio.to_thread(
                self._case_store.renew_operation_claim,
                operation.operation_id,
                operation.claim_token,
                datetime.now(timezone.utc),
            )
            if not renewed:
                return

    async def _restore_detection_case_views(self) -> None:
        await self.bot.wait_until_red_ready()
        snapshots = await asyncio.to_thread(self._case_store.list_open_cases)
        for snapshot in snapshots:
            message_id = snapshot.case.review_message_id
            if message_id is None:
                continue
            projection = render_case(snapshot)
            pending_feedback = review_publication._pending_feedback_items(
                projection.feedback_items
            )
            view = DetectionCaseView(
                self,
                snapshot.case.case_id,
                has_image_feedback=bool(pending_feedback),
                feedback_items=pending_feedback,
                moderation_actions=projection.moderation_actions,
            )
            self._case_views[snapshot.case.case_id] = view
            self.bot.add_view(view, message_id=message_id)
            await self._case_review_rerender(snapshot.case.case_id)

    # ─── Detection ────────────────────────────────────────────────────────

    def _forward_purge_signal(self, message: discord.Message) -> DetectionSignal | None:
        if not self._is_forward_purge_active(message.guild.id, message.author.id):
            return None
        return DetectionSignal(
            detector="forward_purge",
            reason="Active forward-purge containment window",
            action=ActionIntent.REVIEW,
            decisive=True,
            metadata={"containment_required": True},
        )

    @staticmethod
    def _signal_action(value: object, valid_actions: tuple[str, ...]) -> ActionIntent:
        action = value if value in valid_actions else "review"
        return ActionIntent(typing.cast(str, action))

    def _spam_signal(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> DetectionSignal | None:
        if not guild_settings.spam_enabled:
            return None
        reasons = self._spam_suspicion_reasons(message, guild_settings)
        if not reasons:
            return None
        return DetectionSignal(
            detector="spam",
            reason="\n".join(reasons),
            action=self._signal_action(
                guild_settings.spam_action.value, CORE_ACTION_OPTIONS
            ),
            decisive=True,
            metadata={"reasons": tuple(reasons)},
        )

    async def _firstpost_signal(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> DetectionSignal | None:
        firstpost_enabled = guild_settings.firstpost_enabled
        collect_enabled = guild_settings.firstpost_collect_enabled
        if not firstpost_enabled and not collect_enabled:
            return None
        await self._ensure_firstpost_seen_loaded(message.guild.id)
        if message.author.id in self._firstpost_seen_authors[message.guild.id]:
            return None
        if not firstpost_enabled:
            return None
        reasons = self._firstpost_suspicion_reasons(message, guild_settings)
        if not reasons:
            return None
        return DetectionSignal(
            detector="firstpost",
            reason="\n".join(reasons),
            action=self._signal_action(
                guild_settings.firstpost_action.value, CORE_ACTION_OPTIONS
            ),
            decisive=True,
            metadata={"reasons": tuple(reasons)},
        )

    def _firstpost_candidate(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> DetectionSignal | None:
        if not guild_settings.firstpost_enabled:
            return None
        reasons = self._firstpost_suspicion_reasons(message, guild_settings)
        if not reasons:
            return None
        return DetectionSignal(
            detector="firstpost",
            reason="\n".join(reasons),
            action=self._signal_action(
                guild_settings.firstpost_action.value, CORE_ACTION_OPTIONS
            ),
            decisive=True,
            metadata={"reasons": tuple(reasons)},
        )

    async def _honeypot_signals(
        self,
        message: discord.Message,
        guild_settings: GuildSettings,
        *,
        image_evidence: DetectionSignal | None = None,
    ) -> tuple[DetectionSignal, ...]:
        if message.channel.id not in self._honeypot_channel_ids(
            guild_settings.honeypot_channels,
            guild_settings.honeypot_channel,
        ):
            return ()
        whitelisted_role_ids = set(guild_settings.whitelisted_roles)
        has_whitelist_role = any(
            role.id in whitelisted_role_ids for role in message.author.roles
        )
        whitelist_mode = guild_settings.whitelist_mode if has_whitelist_role else None
        if whitelist_mode is WhitelistModeOption.BYPASS:
            return (
                DetectionSignal(
                    detector="honeypot",
                    reason="Message posted in a configured honeypot channel",
                    action=ActionIntent.NONE,
                    decisive=True,
                    metadata={"whitelist_bypass": True},
                ),
            )
        reasons = await self._suspicion_reasons(message, guild_settings)
        if image_evidence is not None:
            reasons.append(_("Known suspicious image match"))
        second_strike_role_ids = {
            role_id
            for role_id in (
                guild_settings.mute_role,
                guild_settings.joinwatch_auto_role_id,
            )
            if role_id
        }
        second_strike = bool(second_strike_role_ids) and any(
            role.id in second_strike_role_ids for role in message.author.roles
        )
        if second_strike:
            reasons.append(_("Repeat honeypot activity"))
        force_review = whitelist_mode is WhitelistModeOption.REVIEW
        force_fallback = whitelist_mode is WhitelistModeOption.FALLBACK
        if second_strike and not force_review and not force_fallback:
            action = ActionIntent.BAN
        elif force_review:
            action = ActionIntent.REVIEW
        elif force_fallback or not reasons:
            action = self._signal_action(
                guild_settings.fallback_action.value, FALLBACK_ACTION_OPTIONS
            )
        else:
            action = self._signal_action(
                (
                    guild_settings.action.value
                    if guild_settings.action is not None
                    else None
                ),
                CORE_ACTION_OPTIONS,
            )
        return (
            DetectionSignal(
                detector="honeypot",
                reason="\n".join(reasons) if reasons else "Message posted in a configured honeypot channel",
                action=action,
                decisive=True,
                metadata={
                    "reasons": tuple(reasons),
                    "second_strike": second_strike,
                    "force_review": force_review,
                    "force_fallback": force_fallback,
                },
            ),
        )

    # Imagescan seam - see `_init_imagescan_store`.
    async def _initial_image_signal(
        self,
        message: discord.Message,
        guild_settings: GuildSettings,
        *,
        action_override: ActionIntent | None = None,
    ) -> DetectionSignal | None:
        return await imagescan._initial_image_signal(
            self, message, guild_settings, action_override=action_override
        )

    async def _collect_detection_signals(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> tuple[DetectionSignal, ...]:
        forward = self._forward_purge_signal(message)
        signals: list[DetectionSignal] = []
        if forward is not None:
            signals.append(forward)
        in_honeypot = (
            message.channel.id
            in self._honeypot_channel_ids(
                guild_settings.honeypot_channels,
                guild_settings.honeypot_channel,
            )
        )
        if in_honeypot:
            image = None
            if not any(signal.decisive for signal in signals):
                image = await self._initial_image_signal(
                    message,
                    guild_settings,
                    action_override=ActionIntent.NONE,
                )
            signals.extend(
                await self._honeypot_signals(
                    message,
                    guild_settings,
                    image_evidence=image,
                )
            )
            if image is not None:
                signals.append(image)
        else:
            spam = self._spam_signal(message, guild_settings)
            if spam is not None:
                signals.append(spam)
            firstpost = await self._firstpost_signal(message, guild_settings)
            if firstpost is not None:
                signals.append(firstpost)
            if not any(signal.decisive for signal in signals):
                image = await self._initial_image_signal(message, guild_settings)
                if image is not None:
                    signals.append(image)
        return tuple(signals)

    @staticmethod
    def _public_moderation_reason(
        signals: tuple[DetectionSignal, ...], action: ActionIntent
    ) -> str:
        owning_signal = next(
            (signal for signal in signals if signal.action is action),
            next((signal for signal in signals if signal.decisive), None),
        )
        if owning_signal is None:
            return "Honeypot"
        if owning_signal.detector == "spam":
            return "Same message in multiple channels"
        if owning_signal.detector == "firstpost":
            return "Suspicious first observed message."
        if owning_signal.detector == "image":
            return "Honeypot"
        if owning_signal.detector == "honeypot":
            if owning_signal.metadata.get("review_fallback"):
                return "Message in the honeypot channel without a matching scam pattern."
            if owning_signal.metadata.get("second_strike"):
                return "Suspicious Activity"
            if owning_signal.metadata.get("reasons") and not owning_signal.metadata.get(
                "force_fallback"
            ):
                return "Suspicious message in the honeypot channel."
            return "Message in the honeypot channel without a matching scam pattern."
        return "Honeypot"

    @classmethod
    def _resolve_unavailable_review_signals(
        cls,
        guild_settings: GuildSettings,
        signals: tuple[DetectionSignal, ...],
    ) -> tuple[DetectionSignal, ...]:
        review_available = bool(
            guild_settings.review_enabled
            and (
                guild_settings.review_channel is not None
                or guild_settings.logs_channel is not None
            )
        )
        if review_available:
            return signals
        fallback = cls._signal_action(
            guild_settings.fallback_action.value, FALLBACK_ACTION_OPTIONS
        )
        if fallback is ActionIntent.REVIEW:
            fallback = ActionIntent.NONE
        return tuple(
            DetectionSignal(
                signal.detector,
                signal.reason,
                (
                    fallback
                    if signal.detector == "honeypot"
                    and signal.action is ActionIntent.REVIEW
                    and not signal.metadata.get("containment_required")
                    else signal.action
                ),
                signal.decisive,
                (
                    {**signal.metadata, "review_fallback": True}
                    if signal.detector == "honeypot"
                    and signal.action is ActionIntent.REVIEW
                    else signal.metadata
                ),
            )
            for signal in signals
        )

    # Imagescan seam - see `_init_imagescan_store`.
    async def _scan_image_attachments(
        self,
        message: discord.Message,
        samples,
        threshold: int,
        *,
        capture_results: tuple[detection_runtime.CaptureResult, ...] = (),
        limit: int | None = None,
        stop_after_match: bool = False,
        batch_key: tuple[int, int] | None = None,
        skip_positions: frozenset[int] = frozenset(),
    ) -> tuple[dict[str, object], ...]:
        return await imagescan._scan_image_attachments(
            self,
            message,
            samples,
            threshold,
            capture_results=capture_results,
            limit=limit,
            stop_after_match=stop_after_match,
            batch_key=batch_key,
            skip_positions=skip_positions,
        )

    # Imagescan seam - see `_init_imagescan_store`.
    async def _scan_all_case_message_images(
        self,
        message: discord.Message,
        guild_settings: GuildSettings,
        case_id: str,
        sequence: int,
        capture_results: tuple[detection_runtime.CaptureResult, ...],
    ) -> None:
        return await imagescan._scan_all_case_message_images(
            self,
            message,
            guild_settings,
            case_id,
            sequence,
            capture_results=capture_results,
        )

    # Imagescan seam - see `_init_imagescan_store`.
    async def _scan_case_message_images(
        self,
        guild_id: int,
        attachments: tuple,
        guild_settings: GuildSettings,
        case_id: str,
        sequence: int,
        capture_results: tuple[detection_runtime.CaptureResult, ...],
        initial_scan_key: tuple[int, int] | None = None,
    ) -> None:
        return await imagescan._scan_case_message_images(
            self,
            guild_id,
            attachments,
            guild_settings,
            case_id=case_id,
            sequence=sequence,
            capture_results=capture_results,
            initial_scan_key=initial_scan_key,
        )

    # Review publication seam: `views.py`, the detection operation handlers and
    # the detection tests reach these through `self`, so the cog keeps a one-line
    # delegation while the implementation lives in `review_publication.py`.
    # Reclaimed when `detection.py` lands and the callers become modules.
    _case_timeline_attachment_line = staticmethod(
        review_publication._case_timeline_attachment_line
    )
    _case_timeline_message_content = staticmethod(
        review_publication._case_timeline_message_content
    )

    async def _capture_case_attachments(
        self,
        message: discord.Message,
        case_id: str,
        sequence: int,
        *,
        started_event: asyncio.Event | None = None,
    ) -> tuple[detection_runtime.CaptureResult, ...]:
        return await review_publication._capture_case_attachments(
            self, message, case_id, sequence, started_event=started_event
        )

    async def _publish_detection_case(
        self,
        case_id: str,
        review_channel_id: int | None,
        logs_channel: discord.TextChannel | discord.Thread | None,
        *,
        message_sequence: int | None = None,
        skip_if_done: asyncio.Task | None = None,
    ) -> bool:
        return await review_publication._publish_detection_case(
            self,
            case_id,
            review_channel_id,
            logs_channel,
            message_sequence=message_sequence,
            skip_if_done=skip_if_done,
        )

    async def _publish_detection_case_serial(
        self,
        case_id: str,
        review_channel_id: int | None,
        logs_channel: discord.TextChannel | discord.Thread | None,
        *,
        message_sequence: int | None = None,
    ) -> None:
        return await review_publication._publish_detection_case_serial(
            self, case_id, review_channel_id, logs_channel, message_sequence=message_sequence
        )

    async def _dismiss_case_review_prompt(
        self, interaction: discord.Interaction
    ) -> None:
        return await review_publication._dismiss_case_review_prompt(self, interaction)

    async def _case_review_rerender(self, case_id: str) -> None:
        return await review_publication._case_review_rerender(self, case_id)

    async def _case_review_rerender_if_open(self, case_id: str) -> None:
        return await review_publication._case_review_rerender_if_open(self, case_id)

    def _schedule_case_review_followup(self, case_id: str) -> None:
        return review_publication._schedule_case_review_followup(self, case_id)

    async def _finish_case_review_if_ready(
        self,
        case_id: str,
        moderator_id: int | None,
        *,
        defer_final_operations: bool = False,
    ) -> bool:
        return await review_publication._finish_case_review_if_ready(
            self, case_id, moderator_id, defer_final_operations=defer_final_operations
        )

    async def _case_review_bulk_interaction(
        self,
        interaction: discord.Interaction,
        case_id: str,
        action: str,
        *,
        confirmed: bool = False,
        expected_keys: tuple[AttachmentKey, ...] = (),
    ) -> bool:
        return await review_publication._case_review_bulk_interaction(
            self, interaction, case_id, action, confirmed=confirmed, expected_keys=expected_keys
        )

    async def _case_review_message_bulk_interaction(
        self,
        interaction: discord.Interaction,
        case_id: str,
        message_sequence: int,
        action: str,
        *,
        confirmed: bool = False,
        expected_keys: tuple[AttachmentKey, ...] = (),
    ) -> bool:
        return await review_publication._case_review_message_bulk_interaction(
            self,
            interaction,
            case_id,
            message_sequence,
            action,
            confirmed=confirmed,
            expected_keys=expected_keys,
        )

    async def _case_review_moderation_interaction(
        self,
        interaction: discord.Interaction,
        case_id: str,
        action: str,
        *,
        confirmed: bool = False,
    ) -> bool:
        return await review_publication._case_review_moderation_interaction(
            self, interaction, case_id, action, confirmed=confirmed
        )

    async def _case_review_attachment_interaction(
        self, interaction: discord.Interaction, key: AttachmentKey, action: str
    ) -> None:
        return await review_publication._case_review_attachment_interaction(
            self, interaction, key, action
        )

    async def _case_review_individual_prompt(
        self,
        interaction: discord.Interaction,
        case_id: str,
        *,
        message_sequence: int | None = None,
    ) -> None:
        return await review_publication._case_review_individual_prompt(
            self, interaction, case_id, message_sequence=message_sequence
        )

    async def _process_detected_message(
        self,
        message: discord.Message,
        guild_settings: GuildSettings,
        logs_channel: discord.TextChannel | discord.Thread | None,
        signals: tuple[DetectionSignal, ...],
        *,
        timings: dict[str, float] | None = None,
        admission_lock: asyncio.Lock | None = None,
    ) -> None:
        timings = timings if timings is not None else {}
        signals = self._resolve_unavailable_review_signals(guild_settings, signals)
        role_id = guild_settings.mute_role

        def initial_operations(owned_signals):
            action = effective_action(owned_signals)
            whitelist_bypass = bool(owned_signals) and all(
                signal.metadata.get("whitelist_bypass") for signal in owned_signals
            )
            publish_review = guild_settings.review_enabled and not whitelist_bypass
            containment = any(
                signal.action != ActionIntent.NONE
                or (
                    signal.detector == "honeypot"
                    and not signal.metadata.get("whitelist_bypass")
                )
                or signal.metadata.get("containment_required")
                for signal in owned_signals
            )
            operations = []
            if publish_review:
                operations.append(
                    (
                        OperationType.REVIEW_PUBLISH,
                        "review_publish:{case_id}:{sequence}",
                    )
                )
            if containment or message.attachments:
                operations.append(
                    (
                        OperationType.MESSAGE_PROCESS,
                        "message-process:{case_id}:{sequence}",
                    )
                )
            if action in {ActionIntent.KICK, ActionIntent.BAN}:
                operations.append(
                    (
                        OperationType.MODERATION_ACTION,
                        f"moderation_action:{{case_id}}:{{sequence}}:{action.value}",
                    )
                )
            if (
                role_id is not None
                and action is ActionIntent.REVIEW
                and not guild_settings.dry_run
            ):
                operations.append(
                    (
                        OperationType.ROLE_APPLY,
                        f"role-apply:{{case_id}}:{int(role_id)}",
                    )
                )
            return tuple(operations)

        tracking_firstpost = (
            guild_settings.firstpost_enabled
            or guild_settings.firstpost_collect_enabled
        )
        admission_started = perf_counter()
        try:
            append = await asyncio.to_thread(
                self._case_store.append_message,
                review_publication._new_case_message(message),
                signals,
                initial_operations,
                claim_firstpost=tracking_firstpost,
            )
        finally:
            if admission_lock is not None:
                admission_lock.release()
        timings["admission_ms"] = (perf_counter() - admission_started) * 1000
        if append is None:
            self._firstpost_seen_authors[message.guild.id].add(message.author.id)
            return
        if tracking_firstpost:
            self._firstpost_seen_authors[message.guild.id].add(message.author.id)
            if append.firstpost_claimed:
                self._firstpost_dirty_seen_authors[message.guild.id].add(
                    message.author.id
                )
                await self._increment_stat(message.guild, "firstpost_seen")
        admitted_snapshot = await asyncio.to_thread(
            self._case_store.get_case, append.case.case_id
        )
        persisted_signals = tuple(
            item.signal
            for item in admitted_snapshot.signals
            if item.message_sequence == append.message.sequence
        )
        if append.message_created:
            await self._record_detection_stats(message.guild, persisted_signals)
        if append.message_created and any(
            signal.metadata.get("whitelist_bypass") for signal in persisted_signals
        ):
            await self._increment_stat(message.guild, "whitelisted")
        durable_operations = initial_operations(persisted_signals)
        if not append.message_created:
            for operation_type, idempotency_key in durable_operations:
                await asyncio.to_thread(
                    self._case_store.ensure_operation,
                    append.case.case_id,
                    operation_type,
                    idempotency_key.format(
                        case_id=append.case.case_id,
                        sequence=append.message.sequence,
                    ),
                    append.message.sequence,
                )
            admitted_snapshot = await asyncio.to_thread(
                self._case_store.get_case, append.case.case_id
            )
        pipeline_operation = next(
            (
                operation
                for operation in admitted_snapshot.operations
                if operation.operation_type == OperationType.MESSAGE_PROCESS
                and operation.message_sequence == append.message.sequence
            ),
            None,
        )
        pipeline_claim = (
            await asyncio.to_thread(
                self._case_store.claim_operation,
                pipeline_operation.operation_id,
                datetime.now(timezone.utc),
            )
            if pipeline_operation is not None
            else None
        )
        if pipeline_operation is not None:
            if pipeline_claim is None:
                if (
                    not append.message_created
                    and pipeline_operation.status.value == "succeeded"
                ):
                    for child_type in (
                        OperationType.MODERATION_ACTION,
                        OperationType.ROLE_APPLY,
                        OperationType.REVIEW_PUBLISH,
                    ):
                        await self._execute_detection_message_child(
                            admitted_snapshot,
                            child_type,
                            append.message.sequence,
                            datetime.now(timezone.utc),
                            publication_channel=logs_channel,
                        )
                    if any(
                        operation.operation_type == OperationType.REVIEW_PUBLISH
                        and operation.message_sequence == append.message.sequence
                        for operation in admitted_snapshot.operations
                    ):
                        await self._publish_detection_case(
                            append.case.case_id,
                            guild_settings.review_channel,
                            logs_channel,
                        )
                return
            await self._execute_detection_case_operation(
                pipeline_claim,
                datetime.now(timezone.utc),
                publication_channel=logs_channel,
                live_message=message,
                timings=timings,
            )
            return

        review_operation = next(
            (
                operation
                for operation in admitted_snapshot.operations
                if operation.operation_type == OperationType.REVIEW_PUBLISH
                and operation.message_sequence == append.message.sequence
            ),
            None,
        )
        if review_operation is not None:
            review_claim = await asyncio.to_thread(
                self._case_store.claim_operation,
                review_operation.operation_id,
                datetime.now(timezone.utc),
            )
            if review_claim is not None:
                await self._execute_detection_case_operation(
                    review_claim,
                    datetime.now(timezone.utc),
                    publication_channel=logs_channel,
                )
            elif (
                not append.message_created
                and review_operation.status.value == "succeeded"
            ):
                await self._publish_detection_case(
                    append.case.case_id,
                    guild_settings.review_channel,
                    logs_channel,
                )
        return
    async def _suspicion_reasons(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> list[str]:
        reasons: list[str] = []
        content = message.content.lower()
        if message.author.created_at > datetime.now(timezone.utc) - timedelta(days=7):
            reasons.append(_("Account is under 7 days old"))
        scam_keywords = guild_settings.scam_keywords or SCAM_KEYWORDS
        matched_keywords = matched_scam_keywords(scam_keywords, content)
        if matched_keywords:
            reasons.append(_("Matched keywords: {keywords}").format(keywords=", ".join(matched_keywords[:5])))
        if message.attachments and message.author.created_at > datetime.now(timezone.utc) - timedelta(days=14):
            reasons.append(_("Attachment from an account under 14 days old"))
        image_attachment_count = sum(1 for attachment in message.attachments if is_image_attachment(attachment))
        if image_attachment_count >= 4:
            reasons.append(_("Multiple image attachments: {count}").format(count=image_attachment_count))
        attachment_patterns = (
            guild_settings.attachment_patterns or DEFAULT_ATTACHMENT_PATTERNS
        )
        filename_bases = [attachment.filename.rsplit(".", 1)[0].lower() for attachment in message.attachments]
        generic_attachment_count = sum(1 for filename_base in filename_bases if GENERIC_ATTACHMENT_NAME_RE.fullmatch(filename_base))
        if generic_attachment_count >= 2:
            reasons.append(_("Multiple generic attachment names: {count}").format(count=generic_attachment_count))
        matched_patterns: list[str] = []
        matched_attachment_indexes: set[int] = set()
        for pattern in attachment_patterns:
            try:
                matches = [
                    index
                    for index, filename_base in enumerate(filename_bases)
                    if re.fullmatch(pattern, filename_base, flags=re.IGNORECASE)
                ]
            except re.error:
                continue
            if matches:
                matched_attachment_indexes.update(matches)
                matched_patterns.append(pattern)
        if len(matched_attachment_indexes) >= 2 and matched_patterns:
            reasons.append(_("Matched attachment rules: {patterns}").format(patterns=", ".join(matched_patterns[:3])))
        return reasons

    def _record_recent_user_message(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> None:
        if message.guild is None:
            return
        refs = self._recent_user_messages[message.guild.id][message.author.id]
        refs.append(
            MessageRef(
                message.channel.id,
                message.id,
                message.created_at,
                message_spam_fingerprint(message),
            )
        )
        self._prune_recent_user_messages(
            message.guild.id,
            message.author.id,
            retention_seconds=self._purge_retention_seconds(
                guild_settings.purge_backward_seconds
            ),
        )

    @staticmethod
    def _purge_backward_seconds(value: int) -> int:
        return max(PURGE_MIN_RETENTION_SECONDS, min(value, PURGE_BACKWARD_MAX_SECONDS))

    @staticmethod
    def _purge_forward_seconds(value: int) -> int:
        return max(0, min(value, PURGE_FORWARD_MAX_SECONDS))

    @staticmethod
    def _purge_retention_seconds(purge_backward_seconds: int | None = None) -> int:
        if purge_backward_seconds is None:
            return PURGE_MIN_RETENTION_SECONDS
        return max(
            PURGE_MIN_RETENTION_SECONDS,
            Honeypot._purge_backward_seconds(purge_backward_seconds),
        )

    def _prune_recent_user_messages(
        self, guild_id: int, user_id: int, *, retention_seconds: int = PURGE_MIN_RETENTION_SECONDS
    ) -> None:
        refs = self._recent_user_messages.get(guild_id, {}).get(user_id)
        if not refs:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=retention_seconds)
        while refs and refs[0].created_at < cutoff:
            refs.popleft()
        if not refs:
            self._recent_user_messages[guild_id].pop(user_id, None)

    def _prune_purge_cache(
        self,
        settings_by_guild_id: dict[int, GuildSettings] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        for guild_id, users in list(self._recent_user_messages.items()):
            guild_settings = (settings_by_guild_id or {}).get(guild_id)
            retention_seconds = self._purge_retention_seconds(
                guild_settings.purge_backward_seconds
                if guild_settings is not None
                else None
            )
            for user_id in list(users):
                self._prune_recent_user_messages(
                    guild_id, user_id, retention_seconds=retention_seconds
                )
            if not users:
                self._recent_user_messages.pop(guild_id, None)
        for guild_id, users in list(self._hot_purge_users.items()):
            for user_id, expires_at in list(users.items()):
                if expires_at <= now:
                    users.pop(user_id, None)
            if not users:
                self._hot_purge_users.pop(guild_id, None)

    def _activate_forward_purge(
        self, guild_id: int, user_id: int, purge_forward_seconds: int
    ) -> None:
        forward_seconds = self._purge_forward_seconds(purge_forward_seconds)
        if forward_seconds <= 0:
            self._deactivate_forward_purge(guild_id, user_id)
            return
        self._hot_purge_users[guild_id][user_id] = datetime.now(timezone.utc) + timedelta(
            seconds=forward_seconds
        )

    def _deactivate_forward_purge(self, guild_id: int, user_id: int) -> None:
        users = self._hot_purge_users.get(guild_id)
        if users is not None:
            users.pop(user_id, None)

    def _is_forward_purge_active(self, guild_id: int, user_id: int) -> bool:
        expires_at = self._hot_purge_users.get(guild_id, {}).get(user_id)
        if expires_at is None:
            return False
        if expires_at <= datetime.now(timezone.utc):
            self._hot_purge_users[guild_id].pop(user_id, None)
            return False
        return True

    def _get_cached_message_channel(
        self, guild: discord.Guild, channel_id: int
    ) -> typing.Any | None:
        return guild.get_channel(channel_id) or guild.get_thread(channel_id)

    async def _delete_cached_message_ref(
        self, guild: discord.Guild, user_id: int, ref: MessageRef
    ) -> bool:
        channel = self._get_cached_message_channel(guild, ref.channel_id)
        if channel is None:
            return False
        get_partial_message = getattr(channel, "get_partial_message", None)
        if not callable(get_partial_message):
            return False
        try:
            await get_partial_message(ref.message_id).delete()
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException) as exc:
            await self._record_operational_failure(
                guild.id,
                "cached_message_deletion",
                f"{type(exc).__name__}: {exc}",
            )
            log.debug(
                "Failed to delete cached message %s for user %s in channel %s: %r",
                ref.message_id,
                user_id,
                ref.channel_id,
                exc,
            )
            return False

    async def _delete_recent_cached_user_messages(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        exclude_message_id: int | None = None,
        retention_seconds: int = PURGE_MIN_RETENTION_SECONDS,
    ) -> int:
        self._prune_recent_user_messages(
            guild.id, user_id, retention_seconds=retention_seconds
        )
        refs = list(self._recent_user_messages.get(guild.id, {}).get(user_id, ()))
        deleted = 0
        for ref in refs:
            if exclude_message_id is not None and ref.message_id == exclude_message_id:
                continue
            if await self._delete_cached_message_ref(guild, user_id, ref):
                deleted += 1
        return deleted

    async def _cached_purge_user_messages(
        self,
        guild: discord.Guild,
        user_id: int,
        guild_settings: GuildSettings,
        *,
        exclude_message_id: int | None = None,
    ) -> int:
        deleted = await self._delete_recent_cached_user_messages(
            guild,
            user_id,
            exclude_message_id=exclude_message_id,
            retention_seconds=self._purge_retention_seconds(
                guild_settings.purge_backward_seconds
            ),
        )
        self._activate_forward_purge(
            guild.id,
            user_id,
            guild_settings.purge_forward_seconds,
        )
        return deleted

    def _schedule_post_ban_sweep(self, guild: discord.Guild, user_id: int) -> None:
        """After a ban, delete recent cached messages that Discord may have missed."""
        task = self.bot.loop.create_task(
            self._post_ban_message_sweep(guild.id, user_id),
            name=f"honeypot-post-ban-sweep-{guild.id}-{user_id}",
        )
        self._post_ban_sweep_tasks.add(task)
        task.add_done_callback(self._post_ban_sweep_tasks.discard)

    async def _post_ban_message_sweep(self, guild_id: int, user_id: int) -> None:
        try:
            await asyncio.sleep(POST_BAN_SWEEP_DELAY_SECONDS)
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            raw_config = await self.config.guild(guild).all()
            guild_settings = GuildSettings.from_mapping(raw_config)
            deleted = await self._cached_purge_user_messages(
                guild, user_id, guild_settings
            )
            if deleted:
                await self._increment_stat(guild, "purged_messages", deleted)
                await self._increment_stat(guild, "cached_purge_deletes", deleted)
        except Exception as error:
            await self._record_operational_failure(
                guild_id,
                "post_ban_cached_purge",
                f"{type(error).__name__}: {error}",
            )
            log.exception(
                "Post-ban cached message purge failed for user %s in guild %s",
                user_id,
                guild_id,
            )

    async def _purge_detection_case_cached_messages(
        self,
        guild: discord.Guild,
        user_id: int,
        guild_settings: GuildSettings,
        case_id: str,
        message_sequence: int,
        *,
        exclude_message_id: int | None = None,
    ) -> int:
        retention_seconds = self._purge_retention_seconds(
            guild_settings.purge_backward_seconds
        )
        self._prune_recent_user_messages(
            guild.id, user_id, retention_seconds=retention_seconds
        )
        refs = tuple(self._recent_user_messages.get(guild.id, {}).get(user_id, ()))
        deleted = 0
        for ref in refs:
            if exclude_message_id is not None and ref.message_id == exclude_message_id:
                continue
            operation = await asyncio.to_thread(
                self._case_store.ensure_operation,
                case_id,
                OperationType.CACHED_PURGE,
                f"cached_purge:{case_id}:{ref.channel_id}:{ref.message_id}",
                message_sequence,
            )
            was_deleted = (
                operation.status.value == "succeeded"
                and operation.result == DeleteStatus.DELETED.value
            )
            now = datetime.now(timezone.utc)
            if operation.status.value == "failed" and operation.retry_at is not None:
                now = max(now, operation.retry_at)
            claimed = await asyncio.to_thread(
                self._case_store.claim_operation, operation.operation_id, now
            )
            if claimed is not None:
                if guild_settings.dry_run:
                    await asyncio.to_thread(
                        self._case_store.complete_operation,
                        claimed.operation_id,
                        claimed.claim_token,
                        now,
                        "planned",
                    )
                else:
                    await self._execute_detection_case_operation(claimed, now)
            snapshot = await asyncio.to_thread(self._case_store.get_case, case_id)
            persisted = next(
                item
                for item in snapshot.operations
                if item.operation_id == operation.operation_id
            )
            if persisted.result == DeleteStatus.DELETED.value and not was_deleted:
                deleted += 1
        if not guild_settings.dry_run:
            self._activate_forward_purge(
                guild.id,
                user_id,
                guild_settings.purge_forward_seconds,
            )
        return deleted

    def _firstpost_suspicion_reasons(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> list[str]:
        attachment_count = len(message.attachments)
        reasons: list[str] = []
        content = message.content.strip().lower()
        if attachment_count == 4:
            reasons.append(_("First post with four attachments"))
        elif attachment_count == 2:
            scam_keywords = guild_settings.scam_keywords or SCAM_KEYWORDS
            matched_keywords = matched_scam_keywords(
                scam_keywords,
                content,
                include_attachment_only=True,
            )
            if matched_keywords:
                reasons.append(
                    _("First post with two attachments and keywords: {keywords}").format(
                        keywords=", ".join(matched_keywords[:5])
                    )
                )
        return reasons

    def _spam_suspicion_reasons(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> list[str]:
        window_seconds = guild_settings.spam_window_seconds or 10
        window_seconds = max(SPAM_WINDOW_MIN_SECONDS, min(window_seconds, SPAM_WINDOW_MAX_SECONDS))
        min_channels = guild_settings.spam_min_channels or 2
        min_channels = max(SPAM_CHANNEL_MIN, min(min_channels, SPAM_CHANNEL_MAX))
        content = message.content.strip().lower()
        scam_keywords = guild_settings.scam_keywords or SCAM_KEYWORDS
        has_signal = bool(message.attachments) or bool(matched_scam_keywords(scam_keywords, content))
        if not has_signal:
            return []
        current_fingerprint = message_spam_fingerprint(message)
        cutoff = message.created_at - timedelta(seconds=window_seconds)
        channel_ids = {
            ref.channel_id
            for ref in self._recent_user_messages.get(message.guild.id, {}).get(message.author.id, ())
            if ref.fingerprint == current_fingerprint and ref.created_at >= cutoff
        }
        if len(channel_ids) < min_channels:
            return []
        return [
            _("Same message in {count} channels within {seconds}s").format(
                count=len(channel_ids),
                seconds=window_seconds,
            )
        ]

    async def _execute_action(
        self,
        guild: discord.Guild,
        member: discord.Member | discord.User | discord.Object,
        created_at: datetime,
        settings: GuildSettings,
        reason: str,
        action: str | None = None,
        moderator: discord.Member | discord.User | discord.Object | None = None,
    ) -> tuple[str | None, str | None]:
        """Execute the configured action (kick/ban) against a guild member.
        Returns (action_label, failed_message) where failed_message is None on success.
        """
        action = action or (
            settings.action.value if settings.action is not None else None
        )
        if action not in ("kick", "ban"):
            return (_("No action configured."), None)
        if settings.dry_run:
            await self._increment_stat(guild, "dry_run_actions")
            return (self._dry_run_label(action), None)
        missing_permission = self._missing_action_permission(guild, action)
        if missing_permission is not None:
            await self._increment_stat(guild, "failed_actions")
            return (None, missing_permission)
        try:
            if action == "kick":
                self._activate_forward_purge(
                    guild.id,
                    member.id,
                    settings.purge_forward_seconds,
                )
                try:
                    await member.kick(reason=reason)
                except discord.NotFound:
                    if self._automated_kick_fail_warning_enabled(
                        settings.automated_kick_fail_warning
                    ):
                        self._deactivate_forward_purge(guild.id, member.id)
                        return await self._create_kick_fail_warning(guild, member.id)
                    raise
                await self._increment_stat(guild, "kicked")
            elif action == "ban":
                self._activate_forward_purge(
                    guild.id,
                    member.id,
                    settings.purge_forward_seconds,
                )
                delete_message_seconds = self._ban_delete_message_seconds()
                member_ban = getattr(member, "ban", None)
                if callable(member_ban):
                    await member_ban(
                        reason=reason,
                        delete_message_seconds=delete_message_seconds,
                    )
                else:
                    await guild.ban(
                        member,
                        reason=reason,
                        delete_message_seconds=delete_message_seconds,
                    )
                self._schedule_post_ban_sweep(guild, member.id)
                await self._increment_stat(guild, "banned")
        except discord.HTTPException as e:
            self._deactivate_forward_purge(guild.id, member.id)
            await self._increment_stat(guild, "failed_actions")
            return (None, _("**Action failed:**\n") + box(str(e), lang="py"))
        try:
            await modlog.create_case(
                self.bot,
                guild,
                created_at,
                action_type=action,
                user=member,
                moderator=moderator or guild.me,
                reason=reason,
            )
        except Exception:
            log.exception("Failed to create modlog case in _execute_action")
        label = _("The member has been kicked.") if action == "kick" else _("The member has been banned.")
        return (label, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if message.author.bot:
            return
        if message.webhook_id is not None:
            return
        lock_index = (
            message.guild.id * 31 + message.author.id
        ) % len(self._detection_admission_locks)
        batch_key = (message.guild.id, message.id)
        pipeline_started = perf_counter()
        admission_lock = self._detection_admission_locks[lock_index]
        admission_lock_owned = False
        try:
            await admission_lock.acquire()
            admission_lock_owned = True
            try:
                queue_wait_ms = (perf_counter() - pipeline_started) * 1000
                if await self.bot.cog_disabled_in_guild(self, message.guild):
                    return
                raw_config = await self.config.guild(message.guild).all()
                guild_settings = GuildSettings.from_mapping(raw_config)
                if not guild_settings.enabled:
                    return
                logs_channel = self._get_text_channel_or_thread(
                    message.guild, guild_settings.logs_channel
                )
                if await self._is_protected_member(message.author, message.guild):
                    return
                self._record_recent_user_message(message, guild_settings)
                signals_started = perf_counter()
                signals = await self._collect_detection_signals(
                    message, guild_settings
                )
                timings = {
                    "queue_wait_ms": queue_wait_ms,
                    "signals_ms": (perf_counter() - signals_started) * 1000,
                }
                if not signals:
                    return
                admission_lock_owned = False
                await self._process_detected_message(
                    message,
                    guild_settings,
                    logs_channel,
                    signals,
                    timings=timings,
                    admission_lock=admission_lock,
                )
            finally:
                if admission_lock_owned:
                    admission_lock.release()
        finally:
            self._initial_image_scan_batches.pop(batch_key, None)
        return

    @tasks.loop(minutes=1)
    async def joinwatch_auto_role_loop(self) -> None:
        return await joinwatch.joinwatch_auto_role_loop(self)

    @joinwatch_auto_role_loop.before_loop
    async def before_joinwatch_auto_role(self) -> None:
        await self.bot.wait_until_red_ready()

    # ─── New account join alert ────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        return await joinwatch.on_member_join(self, member)

    # ─── Baited role trap ─────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        return await joinwatch.on_member_update(self, before, after)

    # ─── Commands ─────────────────────────────────────────────────────────

    @commands.command(name="consoledump")
    @commands.guild_only()
    async def console_dump(
        self,
        ctx: commands.Context,
        scope: str | None = None,
        hours: str | None = None,
        level: str | None = None,
    ) -> None:
        """Export recent sanitized Python logs to a private text channel."""
        return await diagnostics.console_dump(self, ctx, scope, hours, level)

    @commands.guild_only()
    @commands.permissions_check(lambda ctx: ctx.author.id == ctx.guild.owner_id or ctx.author.id in ctx.bot.owner_ids)
    @commands.group()
    async def honeypot(self, ctx: commands.Context) -> None:
        """Configure server safety and honeypot protections."""

    @honeypot.group(name="debug")
    async def debug(self, ctx: commands.Context) -> None:
        """Maintenance, debug, and export tools."""

    @debug.group(name="imagescan")
    async def debug_imagescan(self, ctx: commands.Context) -> None:
        """Maintenance tools for image scan training data."""

    @debug_imagescan.command(name="cleanup_events")
    async def imagescan_cleanup_events(self, ctx: commands.Context, confirm: str = None) -> None:
        """Clean old image scan event files after dumping them."""
        should_delete = (confirm or "").lower() == "confirm"
        if confirm is not None and not should_delete:
            await ctx.send(_("Use `confirm` to delete event files, or omit it for a dry run."))
            return
        plan = await asyncio.to_thread(
            plan_imagescan_event_cache_cleanup,
            self._imagescan_files_path,
            ctx.guild.id,
            delete=should_delete,
        )
        size_mb = plan["bytes"] / 1024 / 1024
        if should_delete:
            await ctx.send(
                _(
                    "Image scan event cleanup finished: deleted {deleted}/{event_dirs} event folder(s), "
                    "{files} file(s), {size:.2f} MB. Samples were not touched."
                ).format(
                    deleted=plan["deleted_event_dirs"],
                    event_dirs=plan["event_dirs"],
                    files=plan["files"],
                    size=size_mb,
                )
            )
        else:
            await ctx.send(
                _(
                    "Image scan event cleanup dry run: {event_dirs} event folder(s), "
                    "{files} file(s), {size:.2f} MB. Run with `confirm` to delete. "
                    "Samples will not be touched."
                ).format(
                    event_dirs=plan["event_dirs"],
                    files=plan["files"],
                    size=size_mb,
                )
            )

    @debug.command(name="reviewdump")
    async def review_dump(self, ctx: commands.Context) -> None:
        """Export banned review cases from the current channel."""
        return await diagnostics.review_dump(self, ctx)

    # ─── honeypot sub-group ───────────────────────────────────────────

    @honeypot.group(name="honeypot")
    async def honeypot_settings(self, ctx: commands.Context) -> None:
        """Configure the main honeypot detection layer."""

    @honeypot_settings.command(name="toggle")
    async def honeypot_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable the main honeypot layer."""
        if value is None:
            v = await self.config.guild(ctx.guild).enabled()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=str(v).lower(),
                    options=self._format_options(BOOL_OPTIONS),
                )
            )
        else:
            await self.config.guild(ctx.guild).enabled.set(value)
            await ctx.send(_("✅ Enabled set to {value}").format(value=value))

    @honeypot_settings.command()
    async def action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the default action for honeypot detections."""
        if value is None:
            v = await self.config.guild(ctx.guild).action()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=v or _("not set"),
                    options=self._format_options(CORE_ACTION_OPTIONS),
                )
            )
        elif value not in CORE_ACTION_OPTIONS:
            await ctx.send(_("Choose one of: {options}").format(options=self._format_options(CORE_ACTION_OPTIONS)))
        else:
            await self.config.guild(ctx.guild).action.set(value)
            await ctx.send(_("✅ Action set to {value}").format(value=value))

    @honeypot_settings.command(name="fallback_action")
    async def fallback_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the action used when a detector falls back to honeypot handling."""
        if value is None:
            v = await self.config.guild(ctx.guild).fallback_action()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=v,
                    options=self._format_options(FALLBACK_ACTION_OPTIONS),
                )
            )
        elif value not in FALLBACK_ACTION_OPTIONS:
            await ctx.send(_("Choose one of: {options}").format(options=self._format_options(FALLBACK_ACTION_OPTIONS)))
        else:
            await self.config.guild(ctx.guild).fallback_action.set(value)
            await ctx.send(_("✅ Fallback action set to {value}").format(value=value))

    @honeypot_settings.command(name="dry_run")
    async def dry_run(self, ctx: commands.Context, value: bool = None) -> None:
        """Log what would happen without applying punishments."""
        if value is None:
            v = await self.config.guild(ctx.guild).dry_run()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=str(v).lower(),
                    options=self._format_options(BOOL_OPTIONS),
                )
            )
        else:
            await self.config.guild(ctx.guild).dry_run.set(value)
            await ctx.send(_("✅ Dry run set to {value}").format(value=value))

    @honeypot_settings.command(name="whitelist_mode")
    async def whitelist_mode(self, ctx: commands.Context, value: str = None) -> None:
        """Set how whitelisted roles are handled by honeypot detections."""
        if value is None:
            v = await self.config.guild(ctx.guild).whitelist_mode()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=v,
                    options=self._format_options(WHITELIST_MODE_OPTIONS),
                )
            )
        elif value not in WHITELIST_MODE_OPTIONS:
            await ctx.send(_("Choose one of: {options}").format(options=self._format_options(WHITELIST_MODE_OPTIONS)))
        else:
            await self.config.guild(ctx.guild).whitelist_mode.set(value)
            await ctx.send(_("✅ Whitelist mode set to {value}").format(value=value))

    @honeypot_settings.command(name="automated_kick_fail_warn")
    async def automated_kick_fail_warn(self, ctx: commands.Context, value: bool = None) -> None:
        """Warn when an automated kick cannot run because the user already left."""
        if value is None:
            v = await self.config.guild(ctx.guild).automated_kick_fail_warning()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=str(v).lower(),
                    options=self._format_options(BOOL_OPTIONS),
                )
            )
        else:
            await self.config.guild(ctx.guild).automated_kick_fail_warning.set(value)
            await ctx.send(_("✅ Warn on automated kick fail set to {value}").format(value=value))

    # ─── channel sub-group ────────────────────────────────────────────

    @honeypot.group(name="channel")
    async def channels(self, ctx: commands.Context) -> None:
        """Configure honeypot and log channels."""

    @commands.bot_has_guild_permissions(manage_channels=True)
    @channels.command()
    async def create(self, ctx: commands.Context) -> None:
        """Create and register a new honeypot channel."""
        me = ctx.guild.me
        if me is None:
            raise commands.UserFeedbackCheckFailure(_("I couldn't find my server member."))
        honeypot_channel = await ctx.guild.create_text_channel(
            name="honeypot",
            position=0,
            overwrites={
                me: discord.PermissionOverwrite(
                    view_channel=True, read_messages=True, send_messages=True,
                    manage_messages=True, manage_channels=True,
                ),
                ctx.guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, read_messages=True, send_messages=True,
                ),
            },
            reason=_("Honeypot channel requested by {author}.").format(author=ctx.author),
        )
        async with self.config.guild(ctx.guild).honeypot_channels() as channel_ids:
            if honeypot_channel.id not in channel_ids:
                channel_ids.append(honeypot_channel.id)
        await ctx.send(_("✅ Honeypot channel added: {channel.mention}").format(channel=honeypot_channel))

    @channels.command(name="add")
    async def channel_add(self, ctx: commands.Context, target: discord.TextChannel | discord.Thread) -> None:
        """Register an existing channel as a honeypot channel."""
        missing = self._missing_channel_permissions(
            ctx.guild,
            target,
            send_messages=False,
            read_history=True,
            manage_messages=True,
        )
        if missing is not None:
            raise commands.UserFeedbackCheckFailure(missing)
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        if target.id in self._honeypot_channel_ids(
            guild_settings.honeypot_channels,
            guild_settings.honeypot_channel,
        ):
            raise commands.UserFeedbackCheckFailure(_("That channel is already a honeypot channel."))
        async with self.config.guild(ctx.guild).honeypot_channels() as channel_ids:
            channel_ids.append(target.id)
        await ctx.send(_("✅ Honeypot channel added: {channel.mention}").format(channel=target))

    @channels.command(name="remove")
    async def channel_remove(self, ctx: commands.Context, target: discord.TextChannel | discord.Thread) -> None:
        """Unregister a honeypot channel."""
        removed = False
        async with self.config.guild(ctx.guild).honeypot_channels() as channel_ids:
            while target.id in channel_ids:
                channel_ids.remove(target.id)
                removed = True
        if await self.config.guild(ctx.guild).honeypot_channel() == target.id:
            await self.config.guild(ctx.guild).honeypot_channel.set(None)
            removed = True
        if not removed:
            raise commands.UserFeedbackCheckFailure(_("That channel is not a honeypot channel."))
        await ctx.send(_("✅ Honeypot channel removed: {channel.mention}").format(channel=target))

    @channels.command(name="list")
    async def channel_list(self, ctx: commands.Context) -> None:
        """List registered honeypot channels."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        channel_ids = self._honeypot_channel_ids(
            guild_settings.honeypot_channels,
            guild_settings.honeypot_channel,
        )
        await ctx.send(
            _("Honeypot channels:\n{channels}").format(
                channels=self._format_honeypot_channel_list(ctx.guild, channel_ids),
            )
        )

    @channels.command()
    async def logs(self, ctx: commands.Context, target: discord.TextChannel = None) -> None:
        """Set the channel used for honeypot logs."""
        if target is None:
            v = await self.config.guild(ctx.guild).logs_channel()
            await ctx.send(_("Logs channel: {channel}").format(channel=ctx.guild.get_channel(v) if v else _("not set")))
        else:
            if not isinstance(target, discord.TextChannel):
                raise commands.UserFeedbackCheckFailure(
                    _("The logs channel must be a normal text channel.")
                )
            missing = self._missing_channel_permissions(ctx.guild, target)
            if missing is not None:
                raise commands.UserFeedbackCheckFailure(missing)
            await self.config.guild(ctx.guild).logs_channel.set(target.id)
            await ctx.send(_("✅ Logs channel set to {channel.mention}").format(channel=target))

    # ─── punishment sub-group ─────────────────────────────────────────

    @honeypot.group()
    async def punishment(self, ctx: commands.Context) -> None:
        """Configure roles used while a case is awaiting review."""

    @punishment.command(name="mute_role")
    async def punishment_mute_role(self, ctx: commands.Context, role: discord.Role = None) -> None:
        """Set the temporary mute role for pending reviews."""
        if role is None:
            v = await self.config.guild(ctx.guild).mute_role()
            r = ctx.guild.get_role(v) if v else None
            await ctx.send(_("Mute role: {role}").format(role=r.mention if r else _("not set")))
        else:
            await self.config.guild(ctx.guild).mute_role.set(role.id)
            await ctx.send(_("✅ Mute role set to {role.mention}").format(role=role))

    # ─── purge sub-group ───────────────────────────────────────────────

    @honeypot.group(name="purge")
    async def purge(self, ctx: commands.Context) -> None:
        """Configure event-registry message purge windows."""

    @purge.command(name="backward")
    async def purge_backward(self, ctx: commands.Context, seconds: int = None) -> None:
        """Set how far back cached message purge can delete."""
        if seconds is None:
            raw_config = await self.config.guild(ctx.guild).all()
            guild_settings = GuildSettings.from_mapping(raw_config)
            await ctx.send(
                _("Backward purge window: {seconds}s").format(
                    seconds=self._purge_backward_seconds(
                        guild_settings.purge_backward_seconds
                    ),
                )
            )
        elif seconds < PURGE_MIN_RETENTION_SECONDS or seconds > PURGE_BACKWARD_MAX_SECONDS:
            await ctx.send(
                _("Backward purge must be between {minimum} and {maximum} seconds.").format(
                    minimum=PURGE_MIN_RETENTION_SECONDS,
                    maximum=PURGE_BACKWARD_MAX_SECONDS,
                )
            )
        else:
            await self.config.guild(ctx.guild).purge_backward_seconds.set(seconds)
            await ctx.send(_("✅ Backward purge window set to {seconds}s").format(seconds=seconds))

    @purge.command(name="forward")
    async def purge_forward(self, ctx: commands.Context, seconds: int = None) -> None:
        """Set how long future messages are purged after a trigger."""
        if seconds is None:
            raw_config = await self.config.guild(ctx.guild).all()
            guild_settings = GuildSettings.from_mapping(raw_config)
            await ctx.send(
                _("Forward purge window: {seconds}s").format(
                    seconds=self._purge_forward_seconds(
                        guild_settings.purge_forward_seconds
                    ),
                )
            )
        elif seconds < 0 or seconds > PURGE_FORWARD_MAX_SECONDS:
            await ctx.send(
                _("Forward purge must be between 0 and {maximum} seconds.").format(
                    maximum=PURGE_FORWARD_MAX_SECONDS,
                )
            )
        else:
            await self.config.guild(ctx.guild).purge_forward_seconds.set(seconds)
            await ctx.send(_("✅ Forward purge window set to {seconds}s").format(seconds=seconds))

    # ─── spam sub-group ────────────────────────────────────────────────

    @honeypot.group()
    async def spam(self, ctx: commands.Context) -> None:
        """Configure duplicate-message spam detection."""

    @spam.command(name="toggle")
    async def spam_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable duplicate-message spam detection."""
        if value is None:
            v = await self.config.guild(ctx.guild).spam_enabled()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=str(v).lower(),
                    options=self._format_options(BOOL_OPTIONS),
                )
            )
        else:
            await self.config.guild(ctx.guild).spam_enabled.set(value)
            await ctx.send(_("✅ Spam detection set to {value}").format(value=value))

    @spam.command(name="action")
    async def spam_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the action for duplicate-message spam detections."""
        if value is None:
            v = await self.config.guild(ctx.guild).spam_action()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=v,
                    options=self._format_options(CORE_ACTION_OPTIONS),
                )
            )
        elif value not in CORE_ACTION_OPTIONS:
            await ctx.send(_("Choose one of: {options}").format(options=self._format_options(CORE_ACTION_OPTIONS)))
        else:
            await self.config.guild(ctx.guild).spam_action.set(value)
            await ctx.send(_("✅ Spam action set to {value}").format(value=value))

    @spam.command(name="window")
    async def spam_window(self, ctx: commands.Context, seconds: int = None) -> None:
        """Set the time window for duplicate-message detection."""
        if seconds is None:
            v = await self.config.guild(ctx.guild).spam_window_seconds()
            await ctx.send(_("Spam window: {seconds}s").format(seconds=v))
        elif seconds < SPAM_WINDOW_MIN_SECONDS or seconds > SPAM_WINDOW_MAX_SECONDS:
            await ctx.send(
                _("Seconds must be between {minimum} and {maximum}.").format(
                    minimum=SPAM_WINDOW_MIN_SECONDS,
                    maximum=SPAM_WINDOW_MAX_SECONDS,
                )
            )
        else:
            await self.config.guild(ctx.guild).spam_window_seconds.set(seconds)
            await ctx.send(_("✅ Spam window set to {seconds}s").format(seconds=seconds))

    @spam.command(name="channels")
    async def spam_channels(self, ctx: commands.Context, count: int = None) -> None:
        """Set how many channels must contain the same message."""
        if count is None:
            v = await self.config.guild(ctx.guild).spam_min_channels()
            await ctx.send(_("Spam channel threshold: {count}").format(count=v))
        elif count < SPAM_CHANNEL_MIN or count > SPAM_CHANNEL_MAX:
            await ctx.send(
                _("Channel count must be between {minimum} and {maximum}.").format(
                    minimum=SPAM_CHANNEL_MIN,
                    maximum=SPAM_CHANNEL_MAX,
                )
            )
        else:
            await self.config.guild(ctx.guild).spam_min_channels.set(count)
            await ctx.send(_("✅ Spam channel threshold set to {count}").format(count=count))

    # ─── imagescan sub-group ───────────────────────────────────────────

    @honeypot.group(name="imagescan")
    async def imagescan(self, ctx: commands.Context) -> None:
        """Configure adaptive scam-image detection."""

    @imagescan.command(name="add")
    async def imagescan_add(self, ctx: commands.Context) -> None:
        """Add scam images from the message this command replies to."""
        return await imagescan.imagescan_add(self, ctx)

    @imagescan.command(name="dropfile")
    async def imagescan_dropfile(self, ctx: commands.Context, identifier: str) -> None:
        """Remove a stored image file while keeping its hashes active."""
        return await imagescan.imagescan_dropfile(self, ctx, identifier)

    @imagescan.command(name="remove")
    async def imagescan_remove(self, ctx: commands.Context, identifier: str) -> None:
        """Remove an image sample and its stored file from the active dataset."""
        return await imagescan.imagescan_remove(self, ctx, identifier)

    @debug_imagescan.command(name="legacy_toggle")
    async def imagescan_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable image shadow reviews."""
        return await imagescan.imagescan_toggle(self, ctx, value)

    @debug_imagescan.command(name="legacy_channel")
    async def imagescan_channel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | discord.Thread = None,
    ) -> None:
        """Set the channel for image shadow reviews."""
        return await imagescan.imagescan_channel(self, ctx, channel)

    @imagescan.group(name="detector")
    async def imagescan_detector(self, ctx: commands.Context) -> None:
        """Configure production image detector behavior."""

    @imagescan_detector.command(name="toggle")
    async def imagescan_detector_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable production image detection."""
        return await imagescan.imagescan_detector_toggle(self, ctx, value)

    @imagescan_detector.command(name="action")
    async def imagescan_detector_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set image detector action."""
        return await imagescan.imagescan_detector_action(self, ctx, value)

    @imagescan_detector.command(name="threshold")
    async def imagescan_detector_threshold(self, ctx: commands.Context, value: int = None) -> None:
        """Set maximum image hash distance."""
        return await imagescan.imagescan_detector_threshold(self, ctx, value)

    @imagescan.command(name="rebuild")
    async def imagescan_model_rebuild(self, ctx: commands.Context) -> None:
        """Recompute image detector threshold state."""
        return await imagescan.imagescan_model_rebuild(self, ctx)

    @imagescan.command(name="status")
    async def imagescan_status(self, ctx: commands.Context) -> None:
        """Show image detector settings, samples, and timing."""
        return await imagescan.imagescan_status(self, ctx)

    @debug_imagescan.command(name="dump")
    async def imagescan_dump(self, ctx: commands.Context) -> None:
        """Export image shadow-review events and copied files."""
        return await imagescan.imagescan_dump(self, ctx)

    # ─── firstpost sub-group ────────────────────────────────────────────

    @debug_imagescan.command(name="importtpzip")
    async def imagescan_import_tp_zip(self, ctx: commands.Context) -> None:
        """Import true-positive scam images from attached zip files."""
        return await imagescan.imagescan_import_tp_zip(self, ctx)

    @honeypot.group()
    async def firstpost(self, ctx: commands.Context) -> None:
        """Configure first-message detection."""

    @firstpost.command(name="toggle")
    async def firstpost_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable first-message enforcement."""
        if value is None:
            v = await self.config.guild(ctx.guild).firstpost_enabled()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=str(v).lower(),
                    options=self._format_options(BOOL_OPTIONS),
                )
            )
        else:
            await self.config.guild(ctx.guild).firstpost_enabled.set(value)
            if value:
                await self.config.guild(ctx.guild).firstpost_collect_enabled.set(False)
            await ctx.send(_("✅ Firstpost enabled set to {value}").format(value=value))

    @firstpost.command(name="warmup")
    async def firstpost_collect(self, ctx: commands.Context, value: bool = None) -> None:
        """Record first-message senders without taking action."""
        if value is None:
            v = await self.config.guild(ctx.guild).firstpost_collect_enabled()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=str(v).lower(),
                    options=self._format_options(BOOL_OPTIONS),
                )
            )
        else:
            await self.config.guild(ctx.guild).firstpost_collect_enabled.set(value)
            if value:
                await self.config.guild(ctx.guild).firstpost_enabled.set(False)
            await ctx.send(_("✅ Firstpost warmup set to {value}").format(value=value))

    @firstpost.command(name="action")
    async def firstpost_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the action for suspicious first messages."""
        if value is None:
            v = await self.config.guild(ctx.guild).firstpost_action()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=v,
                    options=self._format_options(CORE_ACTION_OPTIONS),
                )
            )
        elif value not in CORE_ACTION_OPTIONS:
            await ctx.send(_("Choose one of: {options}").format(options=self._format_options(CORE_ACTION_OPTIONS)))
        else:
            await self.config.guild(ctx.guild).firstpost_action.set(value)
            await ctx.send(_("✅ Firstpost action set to {value}").format(value=value))

    # ─── review sub-group ─────────────────────────────────────────────

    @honeypot.group()
    async def review(self, ctx: commands.Context) -> None:
        """Configure moderator review for suspicious cases."""

    @review.command(name="toggle")
    async def review_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable moderator review routing."""
        if value is None:
            v = await self.config.guild(ctx.guild).review_enabled()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=str(v).lower(),
                    options=self._format_options(BOOL_OPTIONS),
                )
            )
        else:
            await self.config.guild(ctx.guild).review_enabled.set(value)
            await ctx.send(_("✅ Review enabled set to {value}").format(value=value))

    @review.command(name="channel")
    async def review_channel(
        self, ctx: commands.Context, target: discord.TextChannel = None
    ) -> None:
        """Set the channel for moderator review requests."""
        if target is None:
            v = await self.config.guild(ctx.guild).review_channel()
            await ctx.send(_("Review channel: {channel}").format(channel=ctx.guild.get_channel(v) if v else _("not set")))
        else:
            if not isinstance(target, discord.TextChannel):
                raise commands.UserFeedbackCheckFailure(
                    _("The review destination must be a normal text channel.")
                )
            missing = self._missing_channel_permissions(
                ctx.guild,
                target,
                read_history=True,
                create_public_threads=True,
                send_in_threads=True,
                embed_links=True,
                attach_files=True,
                manage_threads=True,
            )
            if missing is not None:
                raise commands.UserFeedbackCheckFailure(missing)
            await self.config.guild(ctx.guild).review_channel.set(target.id)
            await ctx.send(_("✅ Review channel set to {channel.mention}").format(channel=target))

    @review.command(name="kick_fail_warn")
    async def review_kick_fail_warn(self, ctx: commands.Context, value: str = None) -> None:
        """Set how review kicks report users who already left."""
        if value is None:
            v = await self.config.guild(ctx.guild).review_kick_fail_warning()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=v,
                    options=self._format_options(REVIEW_KICK_FAIL_WARNING_MODES),
                )
            )
            return
        value = value.lower()
        if value not in REVIEW_KICK_FAIL_WARNING_MODES:
            await ctx.send(_("Choose one of: {options}").format(options=self._format_options(REVIEW_KICK_FAIL_WARNING_MODES)))
            return
        await self.config.guild(ctx.guild).review_kick_fail_warning.set(value)
        await ctx.send(_("✅ Kick-fail warning set to {value}").format(value=value))

    # ─── roles sub-group (was whitelistedroles) ───────────────────────

    @honeypot_settings.group()
    async def roles(self, ctx: commands.Context) -> None:
        """Manage roles trusted by the main honeypot layer."""

    @roles.command(name="add")
    async def roles_add(self, ctx: commands.Context, role: discord.Role) -> None:
        """Add a role to the honeypot whitelist."""
        async with self.config.guild(ctx.guild).whitelisted_roles() as roles:
            if role.id in roles:
                raise commands.UserFeedbackCheckFailure(_("That role is already whitelisted."))
            roles.append(role.id)
        await ctx.send(_("✅ {role} added to the whitelist.").format(role=role.mention))

    @roles.command(name="remove")
    async def roles_remove(self, ctx: commands.Context, role: discord.Role) -> None:
        """Remove a role from the honeypot whitelist."""
        async with self.config.guild(ctx.guild).whitelisted_roles() as roles:
            if role.id not in roles:
                raise commands.UserFeedbackCheckFailure(_("That role is not in the whitelist."))
            roles.remove(role.id)
        await ctx.send(_("✅ {role} removed from the whitelist.").format(role=role.mention))

    @roles.command(name="list")
    async def roles_list(self, ctx: commands.Context) -> None:
        """List roles on the honeypot whitelist."""
        role_ids = await self.config.guild(ctx.guild).whitelisted_roles()
        if not role_ids:
            await ctx.send(_("No whitelisted roles."))
            return
        roles = [ctx.guild.get_role(rid) for rid in role_ids if ctx.guild.get_role(rid) is not None]
        if not roles:
            await ctx.send(_("No valid roles found (deleted?)."))
            return
        await ctx.send(_("**Whitelisted roles:**\n{lines}").format(lines="\n".join(f"- {r.mention}" for r in roles)))

    # ─── keywords sub-group (was scamkeywords) ────────────────────────

    @honeypot_settings.group()
    async def keywords(self, ctx: commands.Context) -> None:
        """Manage text and attachment patterns used by honeypot detection."""

    @keywords.command(name="add")
    async def keywords_add(self, ctx: commands.Context, *, keyword: str) -> None:
        """Add a honeypot keyword."""
        keyword = keyword.strip().lower()
        if not keyword:
            raise commands.UserFeedbackCheckFailure(_("Keyword cannot be empty."))
        async with self.config.guild(ctx.guild).scam_keywords() as keywords:
            if keyword in [kw.lower() for kw in keywords]:
                raise commands.UserFeedbackCheckFailure(_("Keyword already exists."))
            keywords.append(keyword)
        await ctx.send(_("✅ Keyword added: `{keyword}`").format(keyword=keyword))

    @keywords.command(name="remove")
    async def keywords_remove(self, ctx: commands.Context, *, keyword: str) -> None:
        """Remove a honeypot keyword."""
        keyword = keyword.strip().lower()
        async with self.config.guild(ctx.guild).scam_keywords() as keywords:
            for existing in list(keywords):
                if existing.lower() == keyword:
                    keywords.remove(existing)
                    await ctx.send(_("✅ Keyword removed: `{keyword}`").format(keyword=existing))
                    return
        raise commands.UserFeedbackCheckFailure(_("Keyword not found."))

    @keywords.command(name="list")
    async def keywords_list(self, ctx: commands.Context) -> None:
        """List configured honeypot keywords."""
        keywords = await self.config.guild(ctx.guild).scam_keywords()
        if not keywords:
            await ctx.send(_("No keywords configured."))
            return
        await ctx.send(_("**Scam keywords:**\n{lines}").format(lines="\n".join(f"`{i}.` {kw}" for i, kw in enumerate(keywords, 1))))

    @keywords.command(name="reset")
    async def keywords_reset(self, ctx: commands.Context) -> None:
        """Reset honeypot keywords to defaults."""
        await self.config.guild(ctx.guild).scam_keywords.set(SCAM_KEYWORDS.copy())
        await ctx.send(_("✅ Keywords reset to defaults."))

    @keywords.group(name="attachments")
    async def keyword_attachments(self, ctx: commands.Context) -> None:
        """Manage attachment filename patterns used by honeypot detection."""

    @keyword_attachments.command(name="add")
    async def keyword_attachments_add(self, ctx: commands.Context, *, pattern: str) -> None:
        """Add an attachment filename pattern."""
        try:
            re.compile(pattern)
        except re.error as exc:
            raise commands.UserFeedbackCheckFailure(_("Invalid regex: {error}").format(error=exc))
        async with self.config.guild(ctx.guild).attachment_patterns() as patterns:
            if pattern in patterns:
                raise commands.UserFeedbackCheckFailure(_("Pattern already exists."))
            patterns.append(pattern)
        await ctx.send(_("✅ Attachment pattern added: `{pattern}`").format(pattern=pattern))

    @keyword_attachments.command(name="remove")
    async def keyword_attachments_remove(self, ctx: commands.Context, *, pattern: str) -> None:
        """Remove an attachment filename pattern."""
        async with self.config.guild(ctx.guild).attachment_patterns() as patterns:
            if pattern not in patterns:
                raise commands.UserFeedbackCheckFailure(_("Pattern not found."))
            patterns.remove(pattern)
        await ctx.send(_("✅ Attachment pattern removed: `{pattern}`").format(pattern=pattern))

    @keyword_attachments.command(name="list")
    async def keyword_attachments_list(self, ctx: commands.Context) -> None:
        """List configured attachment filename patterns."""
        patterns = await self.config.guild(ctx.guild).attachment_patterns()
        if not patterns:
            await ctx.send(_("No attachment patterns configured."))
            return
        await ctx.send(_("**Attachment patterns:**\n{lines}").format(lines="\n".join(f"`{i}.` {pattern}" for i, pattern in enumerate(patterns, 1))))

    @keyword_attachments.command(name="reset")
    async def keyword_attachments_reset(self, ctx: commands.Context) -> None:
        """Reset attachment filename patterns to defaults."""
        await self.config.guild(ctx.guild).attachment_patterns.set(DEFAULT_ATTACHMENT_PATTERNS.copy())
        await ctx.send(_("✅ Attachment patterns reset to defaults."))

    # ─── joinwatch sub-group ──────────────────────────────────────────

    @honeypot.group()
    async def joinwatch(self, ctx: commands.Context) -> None:
        """Configure young-account join monitoring."""

    @joinwatch.command(name="toggle")
    async def joinwatch_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable young-account join monitoring."""
        return await joinwatch.joinwatch_toggle(self, ctx, value)

    @joinwatch.command()
    async def channel(self, ctx: commands.Context, target: discord.TextChannel | discord.Thread = None) -> None:
        """Set the channel for young-account join alerts."""
        return await joinwatch.channel(self, ctx, target)

    @joinwatch.group(name="alert")
    async def joinwatch_alert(self, ctx: commands.Context) -> None:
        """Configure joinwatch alert delivery."""

    @joinwatch_alert.command(name="toggle")
    async def joinwatch_alert_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable joinwatch alert messages."""
        return await joinwatch.joinwatch_alert_toggle(self, ctx, value)

    @joinwatch.command(name="max_age")
    async def max_age(self, ctx: commands.Context, hours: int = None) -> None:
        """Set the maximum account age for joinwatch alerts."""
        return await joinwatch.max_age(self, ctx, hours)

    @joinwatch.group(name="autorole")
    async def joinwatch_autorole(self, ctx: commands.Context) -> None:
        """Configure temporary roles for young accounts."""

    @joinwatch_autorole.command(name="toggle")
    async def joinwatch_autorole_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable joinwatch auto-role handling."""
        return await joinwatch.joinwatch_autorole_toggle(self, ctx, value)

    @joinwatch_autorole.command(name="role")
    async def joinwatch_autorole_role(self, ctx: commands.Context, role: discord.Role = None) -> None:
        """Set the temporary role for young accounts."""
        return await joinwatch.joinwatch_autorole_role(self, ctx, role)

    @joinwatch_autorole.command(name="timer")
    async def joinwatch_autorole_timer(self, ctx: commands.Context, minutes: int = None) -> None:
        """Set how long the temporary role may remain."""
        return await joinwatch.joinwatch_autorole_timer(self, ctx, minutes)

    @joinwatch_autorole.command(name="action")
    async def joinwatch_autorole_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the action when the temporary role is not removed in time."""
        return await joinwatch.joinwatch_autorole_action(self, ctx, value)

    @joinwatch_autorole.command(name="bantimers")
    async def joinwatch_autorole_bantimers(self, ctx: commands.Context) -> None:
        """List active joinwatch auto-role timers."""
        return await joinwatch.joinwatch_autorole_bantimers(self, ctx)

    @joinwatch_autorole.group(name="randomize")
    async def joinwatch_autorole_randomize(self, ctx: commands.Context) -> None:
        """Configure randomized auto-role delay."""

    @joinwatch_autorole_randomize.command(name="toggle")
    async def joinwatch_autorole_randomize_toggle(
        self, ctx: commands.Context, value: bool = None
    ) -> None:
        """Enable or disable randomized auto-role delay."""
        return await joinwatch.joinwatch_autorole_randomize_toggle(self, ctx, value)

    @joinwatch_autorole_randomize.command(name="min_time")
    async def joinwatch_autorole_randomize_min_time(
        self, ctx: commands.Context, minutes: int = None
    ) -> None:
        """Set the minimum randomized auto-role delay."""
        return await joinwatch.joinwatch_autorole_randomize_min_time(self, ctx, minutes)

    @joinwatch_autorole_randomize.command(name="max_time")
    async def joinwatch_autorole_randomize_max_time(
        self, ctx: commands.Context, minutes: int = None
    ) -> None:
        """Set the maximum randomized auto-role delay."""
        return await joinwatch.joinwatch_autorole_randomize_max_time(self, ctx, minutes)

    # ─── bait role sub-group ──────────────────────────────────────────

    @honeypot.group(name="bait_role")
    async def bait_role(self, ctx: commands.Context) -> None:
        """Configure the bait role trap."""

    @bait_role.command(name="toggle")
    async def bait_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable bait role enforcement."""
        if value is None:
            v = await self.config.guild(ctx.guild).baitrole_enabled()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=str(v).lower(),
                    options=self._format_options(BOOL_OPTIONS),
                )
            )
        else:
            await self.config.guild(ctx.guild).baitrole_enabled.set(value)
            await ctx.send(_("✅ Bait role trap set to {value}").format(value=value))

    @bait_role.command()
    async def role(self, ctx: commands.Context, role: discord.Role = None) -> None:
        """Set the role that triggers bait role enforcement."""
        if role is None:
            v = await self.config.guild(ctx.guild).baitrole_id()
            r = ctx.guild.get_role(v) if v else None
            await ctx.send(_("Bait role: {role}").format(role=r.mention if r else _("not set")))
        else:
            await self.config.guild(ctx.guild).baitrole_id.set(role.id)
            await ctx.send(_("✅ Bait role set to {role.mention}").format(role=role))

    @bait_role.command(name="action")
    async def bait_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the action for bait role enforcement."""
        if value is None:
            v = await self.config.guild(ctx.guild).baitrole_action()
            await ctx.send(
                _("Current: {value}. Choices: {options}").format(
                    value=v,
                    options=self._format_options(BAIT_ACTION_OPTIONS),
                )
            )
        elif value not in BAIT_ACTION_OPTIONS:
            await ctx.send(_("Choose one of: {options}").format(options=self._format_options(BAIT_ACTION_OPTIONS)))
        else:
            await self.config.guild(ctx.guild).baitrole_action.set(value)
            await ctx.send(_("✅ Bait action set to {value}").format(value=value))

    # ─── config dump ───────────────────────────────────────────────────

    @honeypot.group(name="config")
    async def config_dump(self, ctx: commands.Context) -> None:
        """Show current honeypot configuration by section."""
        return await diagnostics.config_dump(self, ctx)

    @config_dump.command(name="honeypot")
    async def config_honeypot(self, ctx: commands.Context) -> None:
        """Show main honeypot detection settings."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await self._send_config_dump(
            ctx,
            _("Honeypot config"),
            [
                (_("Enabled"), self._format_bool_setting(guild_settings.enabled)),
                (
                    _("Action"),
                    guild_settings.action.value
                    if guild_settings.action is not None
                    else _("not set"),
                ),
                (_("Fallback action"), guild_settings.fallback_action.value),
                (_("Dry run"), self._format_bool_setting(guild_settings.dry_run)),
                (_("Whitelist mode"), guild_settings.whitelist_mode.value),
                (
                    _("Warn on automated kick fail"),
                    self._format_bool_setting(
                        guild_settings.automated_kick_fail_warning
                    ),
                ),
            ],
        )

    @config_dump.command(name="channel")
    async def config_channel(self, ctx: commands.Context) -> None:
        """Show honeypot and log channel settings."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await self._send_config_dump(
            ctx,
            _("Channel config"),
            [
                (
                    _("Honeypot channels"),
                    self._format_honeypot_channel_list(
                        ctx.guild,
                        self._honeypot_channel_ids(
                            guild_settings.honeypot_channels,
                            guild_settings.honeypot_channel,
                        ),
                    ),
                ),
                (
                    _("Logs channel"),
                    self._format_channel_setting(
                        ctx.guild, guild_settings.logs_channel
                    ),
                ),
            ],
        )

    @config_dump.command(name="punishment")
    async def config_punishment(self, ctx: commands.Context) -> None:
        """Show review punishment settings."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await self._send_config_dump(
            ctx,
            _("Punishment config"),
            [
                (
                    _("Mute role"),
                    self._format_role_setting(ctx.guild, guild_settings.mute_role),
                ),
            ],
        )

    @config_dump.command(name="purge")
    async def config_purge(self, ctx: commands.Context) -> None:
        """Show message purge behavior."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await self._send_config_dump(
            ctx,
            _("Purge config"),
            [
                (_("Mode"), _("Event registry purge")),
                (
                    _("Backward window"),
                    _("{seconds}s").format(
                        seconds=self._purge_backward_seconds(
                            guild_settings.purge_backward_seconds
                        )
                    ),
                ),
                (
                    _("Forward window"),
                    _("{seconds}s").format(
                        seconds=self._purge_forward_seconds(
                            guild_settings.purge_forward_seconds
                        )
                    ),
                ),
                (_("Minimum retention"), _("{seconds}s").format(seconds=PURGE_MIN_RETENTION_SECONDS)),
            ],
        )

    @config_dump.command(name="firstpost")
    async def config_firstpost(self, ctx: commands.Context) -> None:
        """Show first-message detection settings."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        seen_count = await self._count_firstpost_seen_authors(ctx.guild.id)
        await self._send_config_dump(
            ctx,
            _("Firstpost config"),
            [
                (
                    _("Enabled"),
                    self._format_bool_setting(guild_settings.firstpost_enabled),
                ),
                (
                    _("Warmup"),
                    self._format_bool_setting(
                        guild_settings.firstpost_collect_enabled
                    ),
                ),
                (_("Action"), guild_settings.firstpost_action.value),
                (_("Seen authors"), seen_count),
            ],
        )

    @config_dump.command(name="imagescan")
    async def config_imagescan(self, ctx: commands.Context) -> None:
        """Show image detector settings."""
        return await imagescan.config_imagescan(self, ctx)

    @config_dump.command(name="spam")
    async def config_spam(self, ctx: commands.Context) -> None:
        """Show duplicate-message spam settings."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await self._send_config_dump(
            ctx,
            _("Spam config"),
            [
                (_("Enabled"), self._format_bool_setting(guild_settings.spam_enabled)),
                (_("Action"), guild_settings.spam_action.value),
                (
                    _("Window"),
                    _("{seconds}s").format(
                        seconds=guild_settings.spam_window_seconds
                    ),
                ),
                (_("Channels"), guild_settings.spam_min_channels),
            ],
        )

    @config_dump.command(name="review")
    async def config_review(self, ctx: commands.Context) -> None:
        """Show moderator review settings."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await self._send_config_dump(
            ctx,
            _("Review config"),
            [
                (_("Enabled"), self._format_bool_setting(guild_settings.review_enabled)),
                (
                    _("Channel"),
                    self._format_channel_setting(
                        ctx.guild, guild_settings.review_channel
                    ),
                ),
                (_("Case lifetime"), _("24 hours (fixed)")),
                (
                    _("Kick fail warning"),
                    guild_settings.review_kick_fail_warning.value,
                ),
            ],
        )

    @config_dump.command(name="roles")
    async def config_roles(self, ctx: commands.Context) -> None:
        """Show honeypot whitelist role settings."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        roles = [
            self._format_role_setting(ctx.guild, role_id)
            for role_id in guild_settings.whitelisted_roles
        ]
        await self._send_config_dump(
            ctx,
            _("Roles config"),
            [
                (_("Whitelist mode"), guild_settings.whitelist_mode.value),
                (_("Whitelisted roles"), ", ".join(roles) if roles else _("none")),
            ],
        )

    @config_dump.command(name="keywords")
    async def config_keywords(self, ctx: commands.Context) -> None:
        """Show honeypot keyword and attachment pattern counts."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await self._send_config_dump(
            ctx,
            _("Keywords config"),
            [
                (_("Scam keywords"), len(guild_settings.scam_keywords)),
                (
                    _("Attachment patterns"),
                    len(guild_settings.attachment_patterns),
                ),
            ],
        )

    @config_dump.command(name="joinwatch")
    async def config_joinwatch(self, ctx: commands.Context) -> None:
        """Show joinwatch settings."""
        return await joinwatch.config_joinwatch(self, ctx)

    @config_dump.command(name="bait_role")
    async def config_bait(self, ctx: commands.Context) -> None:
        """Show bait role trap settings."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await self._send_config_dump(
            ctx,
            _("Bait config"),
            [
                (
                    _("Enabled"),
                    self._format_bool_setting(guild_settings.baitrole_enabled),
                ),
                (
                    _("Role"),
                    self._format_role_setting(ctx.guild, guild_settings.baitrole_id),
                ),
                (_("Action"), guild_settings.baitrole_action.value),
            ],
        )

    @config_dump.command(name="stats")
    async def config_stats(self, ctx: commands.Context) -> None:
        """Show stored stat and pending timer counts."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        stats = DEFAULT_STATS.copy()
        stats.update(guild_settings.stats)
        now = datetime.now(timezone.utc)
        case_counts = await asyncio.to_thread(
            self._case_store.operational_counts,
            ctx.guild.id,
            now,
            now - timedelta(minutes=5),
        )
        await self._send_config_dump(
            ctx,
            _("Stats config"),
            [
                (_("Stored stats"), len(stats)),
                (
                    _("Pending joinwatch role applications"),
                    len(guild_settings.joinwatch_pending_role_assignments),
                ),
                (
                    _("Active joinwatch auto-role timers"),
                    len(guild_settings.joinwatch_pending_roles),
                ),
                (_("Active detection cases"), case_counts["active_cases"]),
                (_("Due detection cases"), case_counts["due_cases"]),
                (_("Stale resolving cases"), case_counts["stale_resolving_cases"]),
                (_("Failed containment cases"), case_counts["failed_containment"]),
                (_("Forbidden message deletes"), case_counts["forbidden_deletes"]),
                (_("Outstanding durable operations"), case_counts["outstanding_operations"]),
                (_("Queued privacy deletions"), case_counts["privacy_deletion_jobs"]),
            ],
        )

    @config_dump.command(name="all")
    async def config_all(self, ctx: commands.Context) -> None:
        """Show a compact summary of all honeypot settings."""
        raw_config = await self.config.guild(ctx.guild).all()
        guild_settings = GuildSettings.from_mapping(raw_config)
        await self._send_config_dump(
            ctx,
            _("Honeypot config summary"),
            [
                (_("Honeypot"), self._format_bool_setting(guild_settings.enabled)),
                (
                    _("Honeypot channels"),
                    self._format_honeypot_channel_list(
                        ctx.guild,
                        self._honeypot_channel_ids(
                            guild_settings.honeypot_channels,
                            guild_settings.honeypot_channel,
                        ),
                    ),
                ),
                (
                    _("Logs channel"),
                    self._format_channel_setting(
                        ctx.guild, guild_settings.logs_channel
                    ),
                ),
                (_("Review"), self._format_bool_setting(guild_settings.review_enabled)),
                (_("Spam"), self._format_bool_setting(guild_settings.spam_enabled)),
                (
                    _("Image scan"),
                    self._format_bool_setting(
                        guild_settings.imagescan_detector_enabled
                    ),
                ),
                (
                    _("Joinwatch"),
                    self._format_bool_setting(guild_settings.joinwatch_enabled),
                ),
                (
                    _("Joinwatch auto-role"),
                    self._format_bool_setting(
                        guild_settings.joinwatch_auto_role_enabled
                    ),
                ),
                (
                    _("Bait role"),
                    self._format_bool_setting(guild_settings.baitrole_enabled),
                ),
                (
                    _("Pending joinwatch role applications"),
                    len(guild_settings.joinwatch_pending_role_assignments),
                ),
                (
                    _("Active joinwatch auto-role timers"),
                    len(guild_settings.joinwatch_pending_roles),
                ),
            ],
        )

    @honeypot.group(name="errors", invoke_without_command=True)
    async def honeypot_errors(self, ctx: commands.Context) -> None:
        """Show unacknowledged Honeypot operational failures."""
        return await diagnostics.honeypot_errors(self, ctx)

    @honeypot_errors.command(name="clear")
    async def honeypot_errors_clear(self, ctx: commands.Context) -> None:
        """Acknowledge all currently visible Honeypot operational failures."""
        return await diagnostics.honeypot_errors_clear(self, ctx)

    # ─── stats ────────────────────────────────────────────────────────

    @honeypot.command(name="modstats")
    async def honeypot_mod_stats(self, ctx: commands.Context) -> None:
        """Show detailed moderation statistics."""
        return await diagnostics.honeypot_mod_stats(self, ctx)

    @honeypot.command(name="stats")
    async def honeypot_stats(self, ctx: commands.Context) -> None:
        """Show public server safety statistics."""
        return await diagnostics.honeypot_stats(self, ctx)

    @debug.command(name="resetstats")
    @commands.permissions_check(lambda ctx: ctx.author.id == ctx.guild.owner_id or ctx.author.id in ctx.bot.owner_ids)
    async def honeypot_reset_stats(self, ctx: commands.Context) -> None:
        """Reset stored honeypot statistics."""
        return await diagnostics.honeypot_reset_stats(self, ctx)

    async def _doctor_channel_permission_checks(
        self,
        guild,
        me,
        honeypot_channels: typing.Sequence,
        logs_channel,
        review_channel,
    ) -> tuple[DoctorResult, ...]:
        # Binds the two purge predicates, which stay in this module: importing
        # them into diagnostics would close an import cycle.
        return await diagnostics._doctor_channel_permission_checks(
            self,
            guild,
            me,
            honeypot_channels,
            logs_channel,
            review_channel,
            missing_purge_permissions=missing_purge_permissions,
            is_purgeable_message_channel=is_purgeable_message_channel,
        )

    @honeypot.command(name="doctor")
    async def honeypot_doctor(self, ctx: commands.Context) -> None:
        """Check honeypot configuration and required permissions."""
        return await diagnostics.honeypot_doctor(self, ctx)
