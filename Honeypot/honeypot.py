import asyncio
import logging
import typing
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import discord
from AAA3A_utils import Cog
from discord.ext import tasks
from redbot.core import Config, commands, modlog
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from redbot.core.i18n import Translator, cog_i18n

from . import (
    cleanup,
    detection,
    detection_runtime,
    diagnostics,
    gif_detector,
    imagescan,
    joinwatch,
    manual_evidence,
    review_publication,
    settings,
)
from .case_review import (
    CaseFeedbackItem,  # noqa: F401 - public module re-export
    CaseReviewService,
    case_feedback_items,  # noqa: F401 - public module re-export
    render_case,
    render_timeline,  # noqa: F401 - public module re-export
)
from .cleanup import CleanupResult
from .console_dump import ReadOnlyLogBuffer
from .detection_cases import (
    ActionIntent,
    AttachmentKey,
    CaseStatus,  # noqa: F401 - public module re-export
    DeleteStatus,  # noqa: F401 - public module re-export
    DetectionCaseStore,
    DetectionSignal,
    NewAttachment,  # noqa: F401 - public module re-export
    NewMessage,  # noqa: F401 - public module re-export
    OperationStatus,  # noqa: F401 - public module re-export
    OperationType,
)
from .effects import ModerationEffectResult, punitive_effect_allowed
from .firstpost_store import FirstPostStore
from .image_detector import ImageSample
from .imagescan_store import ImageScanStore
from .message_registry import (
    MessageRecord,  # noqa: F401 - public module re-export
    MessageRegistry,
)
from .operations import OperationHandlerRegistry
from .operations.context import (
    DETECTION_FAST_RETRY_LIMIT,
    DETECTION_FAST_RETRY_SECONDS,
    OperationContext,  # noqa: F401 - public module re-export
    OperationLease,  # noqa: F401 - public module re-export
    OperationOutcome,  # noqa: F401 - public module re-export
)
from .remote_media import RemoteMediaInspector
from .settings import (
    BAIT_ACTION_OPTIONS,  # noqa: F401 - public module re-export
    CORE_ACTION_OPTIONS,  # noqa: F401 - public module re-export
    FALLBACK_ACTION_OPTIONS,  # noqa: F401 - public module re-export
    IMAGE_SCAN_DETECTOR_ACTION_OPTIONS,  # noqa: F401 - public module re-export
    JOINWATCH_AUTO_ROLE_ACTION_OPTIONS,  # noqa: F401 - public module re-export
    REVIEW_KICK_FAIL_WARNING_MODES,  # noqa: F401 - public module re-export
    WHITELIST_MODE_OPTIONS,  # noqa: F401 - public module re-export
    BaitActionOption,  # noqa: F401 - public module re-export
    CoreActionOption,  # noqa: F401 - public module re-export
    FallbackActionOption,  # noqa: F401 - public module re-export
    GuildSettings,
    ImageScanDetectorActionOption,  # noqa: F401 - public module re-export
    JoinwatchAutoRoleActionOption,  # noqa: F401 - public module re-export
    ReviewKickFailWarningMode,  # noqa: F401 - public module re-export
    WhitelistModeOption,  # noqa: F401 - public module re-export
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


JOINWATCH_RETRY_DELAY_MINUTES = joinwatch.JOINWATCH_RETRY_DELAY_MINUTES
JOINWATCH_MAX_RETRIES = joinwatch.JOINWATCH_MAX_RETRIES
REVIEW_DUMP_START = diagnostics.REVIEW_DUMP_START
REVIEW_DUMP_MAX_ZIP_BYTES = diagnostics.REVIEW_DUMP_MAX_ZIP_BYTES
REVIEW_DUMP_ATTACHMENT_DELAY_SECONDS = diagnostics.REVIEW_DUMP_ATTACHMENT_DELAY_SECONDS
IMAGE_SCAN_EXTENSIONS = imagescan.IMAGE_SCAN_EXTENSIONS
IMAGE_SCAN_MAX_ATTACHMENTS = imagescan.IMAGE_SCAN_MAX_ATTACHMENTS
DETECTION_ATTACHMENT_TIMEOUT_SECONDS = detection_runtime.DETECTION_ATTACHMENT_TIMEOUT_SECONDS
DETECTION_HEARTBEAT_INTERVAL_SECONDS = 60.0


DoctorResult = diagnostics.DoctorResult
DoctorCheck = diagnostics.DoctorCheck


JoinwatchSelectedAction = joinwatch.JoinwatchSelectedAction
JoinwatchSelection = joinwatch.JoinwatchSelection
select_due_joinwatch_assignments = joinwatch.select_due_joinwatch_assignments


ImageScanDecision = imagescan.ImageScanDecision
IMAGE_SCAN_DECISIONS = imagescan.IMAGE_SCAN_DECISIONS
KICK_FAIL_WARNING_REASON = "Suspicious activity: target left before the kick could be applied."




joinwatch_channel_id = joinwatch.joinwatch_channel_id


plan_imagescan_event_cache_cleanup = imagescan.plan_imagescan_event_cache_cleanup
match_imagescan_sample_identifier = imagescan.match_imagescan_sample_identifier
is_imagescan_sample_path_safe = imagescan.is_imagescan_sample_path_safe
summarize_imagescan_sample_storage = imagescan.summarize_imagescan_sample_storage


case_evidence_root = review_publication.case_evidence_root


IMAGE_ATTACHMENT_EXTENSIONS = imagescan.IMAGE_ATTACHMENT_EXTENSIONS




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
        self._manual_evidence = manual_evidence.ManualEvidenceController(self)

        self._console_log_buffer = ReadOnlyLogBuffer()
        self._post_ban_sweep_tasks: set[asyncio.Task] = set()
        self._case_review_tasks: set[asyncio.Task] = set()
        self._gif_detector_tasks: set[asyncio.Task] = set()
        self._gif_detector_seen_messages: dict[tuple[int, int], None] = {}
        self._gif_detector_animated_guilds: set[int] = set()
        self._gif_detector_hits: dict[tuple[int, int], deque[float]] = {}
        self._gif_detector_active_mutes: dict[tuple[int, int], float] = {}
        self._gif_detector_mutes_in_flight: set[tuple[int, int]] = set()
        self._gif_detector_remote_media_in_flight: set[tuple[int, int]] = set()
        self._gif_detector_rate_lock = asyncio.Lock()
        self._gif_detector_remote_inspector = RemoteMediaInspector()
        self._hot_purge_users: dict[int, dict[int, datetime]] = defaultdict(dict)
        self._message_registry = MessageRegistry(
            cog_data_path(self) / "message_registry.sqlite"
        )
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
        # Read through the module so this semaphore and the deletion barrier in
        # review_publication.py can never be sized from two separate bindings.
        self._detection_case_capture_slots = asyncio.Semaphore(
            review_publication.DETECTION_CAPTURE_CONCURRENCY
        )
        self._detection_admission_locks = tuple(asyncio.Lock() for _ in range(64))
        self._detection_publication_locks = tuple(asyncio.Lock() for _ in range(64))
        self._detection_heartbeat_interval_seconds = DETECTION_HEARTBEAT_INTERVAL_SECONDS
        self._detection_operation_handlers = OperationHandlerRegistry()

    async def red_delete_data_for_user(
        self, *, requester: typing.Any, user_id: int
    ) -> None:
        """Delete retained message and detection-case data for a Red user."""
        await self._delete_retained_data_scope(
            self._message_registry.forget_user,
            self._case_store.plan_user_case_deletion,
            user_id,
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Delete retained message and detection-case data when Red leaves a guild."""
        await self._delete_retained_data_scope(
            self._message_registry.forget_guild,
            self._case_store.plan_guild_case_deletion,
            guild.id,
        )

    async def _delete_retained_data_scope(
        self,
        forget_scope: typing.Callable[[int], typing.Awaitable[typing.Any]],
        plan_scope: typing.Callable[[int], typing.Any],
        scope_id: int,
    ) -> None:
        failures: list[Exception] = []
        try:
            await forget_scope(scope_id)
        except Exception as error:
            failures.append(error)
        try:
            await review_publication._delete_detection_case_scope(
                self,
                plan_scope,
                scope_id,
            )
        except Exception as error:
            failures.append(error)
        if not failures:
            return
        if len(failures) > 1:
            log.error(
                "Multiple retained-data deletion operations failed",
                exc_info=(
                    type(failures[1]),
                    failures[1],
                    failures[1].__traceback__,
                ),
            )
        raise failures[0]

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: typing.Any) -> None:
        await self._message_registry.forget(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: typing.Any) -> None:
        await self._message_registry.forget_many(payload.message_ids)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: typing.Any) -> None:
        if "pinned" in payload.data:
            await self._message_registry.set_pinned(
                payload.message_id,
                bool(payload.data["pinned"]),
            )
        await gif_detector.on_raw_message_edit(self, payload)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: typing.Any) -> None:
        await self._message_registry.forget_channel(channel.guild.id, channel.id)
        await manual_evidence.clear_deleted_channel(self, channel)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: typing.Any) -> None:
        await self._message_registry.forget_channel(thread.guild.id, thread.id)

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

    async def cleanup_channel(
        self, ctx: commands.Context, count: int
    ) -> CleanupResult:
        return await cleanup.cleanup_channel(self, ctx, count)

    async def cleanup_user(
        self, ctx: commands.Context, user_id: int, count: int
    ) -> CleanupResult:
        return await cleanup.cleanup_user(self, ctx, user_id, count)

    async def _increment_stat(self, guild: discord.Guild, key: str, amount: int = 1) -> None:
        guild_config = getattr(self.config, "guild", None)
        if not callable(guild_config):
            return
        async with guild_config(guild).stats() as stats:
            stats.setdefault(key, 0)
            stats[key] += amount

    # Detection seam: another cog module or a test reaches these through
    # `self`, so the cog keeps a one-line delegation while the implementation
    # lives in `detection.py`. The four static re-exports below were
    # `@staticmethod`s that never touched `self`, so their twins take no cog.
    _ban_delete_message_seconds = staticmethod(detection._ban_delete_message_seconds)
    _persisted_capture_results = staticmethod(detection._persisted_capture_results)
    _signal_action = staticmethod(detection._signal_action)
    _public_moderation_reason = staticmethod(detection._public_moderation_reason)

    async def _init_firstpost_seen_store(self) -> None:
        return await detection._init_firstpost_seen_store(self)

    async def _flush_firstpost_seen_authors(self) -> None:
        return await detection._flush_firstpost_seen_authors(self)

    async def _remove_review_mute_role(
        self,
        member: discord.Member,
        role: discord.Role,
        reason: str,
    ) -> bool:
        return await detection._remove_review_mute_role(self, member, role, reason)

    def _honeypot_channel_ids(
        self,
        honeypot_channels: typing.Iterable[object],
        legacy_channel_id: object,
    ) -> list[int]:
        return detection._honeypot_channel_ids(self, honeypot_channels, legacy_channel_id)

    async def _send_config_dump(
        self,
        ctx: commands.Context,
        title: str,
        entries: list[tuple[str, typing.Any]],
    ) -> None:
        return await detection._send_config_dump(self, ctx, title, entries)

    def _dry_run_label(self, action: str) -> str:
        return detection._dry_run_label(self, action)

    def _missing_action_permission(self, guild: discord.Guild, action: str) -> str | None:
        return detection._missing_action_permission(self, guild, action)

    def _missing_role_assignment_permission(self, guild: discord.Guild, role: discord.Role) -> str | None:
        return detection._missing_role_assignment_permission(self, guild, role)

    async def _run_detection_reconciliation(
        self, *, now: datetime | None = None
    ) -> None:
        return await detection._run_detection_reconciliation(self, now=now)

    async def resolve_detection_case(
        self,
        case_id: str,
        resolution: str,
        moderator_id: int | None = None,
        *,
        now: datetime | None = None,
        defer_final_operations: bool = False,
    ) -> bool:
        return await detection.resolve_detection_case(
            self,
            case_id,
            resolution,
            moderator_id,
            now=now,
            defer_final_operations=defer_final_operations,
        )

    async def _execute_case_final_operations(
        self,
        case_id: str,
        now: datetime,
    ) -> None:
        return await detection._execute_case_final_operations(self, case_id, now)

    async def _execute_detection_message_child(
        self,
        snapshot,
        operation_type: OperationType,
        sequence: int,
        now: datetime,
        *,
        publication_channel=None,
    ) -> bool:
        return await detection._execute_detection_message_child(
            self, snapshot, operation_type, sequence, now, publication_channel=publication_channel
        )

    async def _release_detection_case_roles(
        self, case_id: str, now: datetime
    ) -> None:
        return await detection._release_detection_case_roles(self, case_id, now)

    async def _execute_detection_case_operation(
        self,
        operation,
        now: datetime,
        *,
        publication_channel=None,
        live_message=None,
        timings: dict[str, float] | None = None,
    ) -> None:
        return await detection._execute_detection_case_operation(
            self,
            operation,
            now,
            publication_channel=publication_channel,
            live_message=live_message,
            timings=timings,
        )

    async def _renew_detection_operation(self, operation) -> None:
        return await detection._renew_detection_operation(self, operation)

    async def _collect_detection_signals(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> tuple[DetectionSignal, ...]:
        return await detection._collect_detection_signals(self, message, guild_settings)

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
        return await detection._process_detected_message(
            self,
            message,
            guild_settings,
            logs_channel,
            signals,
            timings=timings,
            admission_lock=admission_lock,
        )

    async def _suspicion_reasons(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> list[str]:
        return await detection._suspicion_reasons(self, message, guild_settings)

    async def _observe_message(self, message: discord.Message) -> None:
        return await detection._observe_message(self, message)

    def _deactivate_forward_purge(self, guild_id: int, user_id: int) -> None:
        return detection._deactivate_forward_purge(self, guild_id, user_id)

    def _is_forward_purge_active(self, guild_id: int, user_id: int) -> bool:
        return detection._is_forward_purge_active(self, guild_id, user_id)

    def _get_cached_message_channel(
        self, guild: discord.Guild, channel_id: int
    ) -> typing.Any | None:
        return detection._get_cached_message_channel(self, guild, channel_id)

    async def _cached_purge_user_messages(
        self,
        guild: discord.Guild,
        user_id: int,
        guild_settings: GuildSettings,
        *,
        exclude_message_id: int | None = None,
    ) -> int:
        return await detection._cached_purge_user_messages(
            self, guild, user_id, guild_settings, exclude_message_id=exclude_message_id
        )

    def _schedule_post_ban_sweep(self, guild: discord.Guild, user_id: int) -> None:
        return detection._schedule_post_ban_sweep(self, guild, user_id)

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
        return await detection._purge_detection_case_cached_messages(
            self,
            guild,
            user_id,
            guild_settings,
            case_id=case_id,
            message_sequence=message_sequence,
            exclude_message_id=exclude_message_id,
        )

    async def _spam_suspicion_reasons(
        self, message: discord.Message, guild_settings: GuildSettings
    ) -> list[str]:
        return await detection._spam_suspicion_reasons(self, message, guild_settings)

    async def _punitive_effect_allowed(self, guild: discord.Guild) -> bool:
        return await punitive_effect_allowed(self, guild)

    async def _execute_action(
        self,
        guild: discord.Guild,
        member: discord.Member | discord.User | discord.Object,
        created_at: datetime,
        settings: GuildSettings,
        *,
        reason: str,
        action: str | None = None,
        moderator: discord.Member | discord.User | discord.Object | None = None,
    ) -> ModerationEffectResult:
        return await detection._execute_action(
            self,
            guild,
            member,
            created_at,
            settings,
            reason=reason,
            action=action,
            moderator=moderator,
        )

    # Imagescan seam: `detection.py` and its tests reach these through `self`,
    # so the cog keeps a one-line delegation while the implementation lives in
    # `imagescan.py`. `detection.py` has landed and still calls them on the cog,
    # because the detection tests stub them as cog attributes.
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

    def _format_role_setting(self, guild: discord.Guild, role_id: int | None) -> str:
        role = guild.get_role(role_id) if role_id else None
        if role is not None:
            return f"{role.mention} ({role.id})"
        return _("not set") if role_id is None else _("missing ({id})").format(id=role_id)

    def _format_bool_setting(self, value: bool) -> str:
        return _("enabled") if value else _("disabled")

    @staticmethod
    def _group_overview_is_private(ctx: commands.Context) -> bool:
        guild = getattr(ctx, "guild", None)
        channel = getattr(ctx, "channel", None)
        return Honeypot._channel_is_private(guild, channel)

    @staticmethod
    def _channel_is_private(guild: typing.Any, channel: typing.Any) -> bool:
        default_role = getattr(guild, "default_role", None)
        permissions_for = getattr(channel, "permissions_for", None)
        if default_role is None or not callable(permissions_for):
            return False
        try:
            permissions = permissions_for(default_role)
        except (AttributeError, TypeError):
            return False
        return not bool(getattr(permissions, "view_channel", True))

    async def _send_group_overview(
        self,
        ctx: commands.Context,
        config_sender: typing.Callable[..., typing.Awaitable[None]] | None = None,
        *,
        include_descendants: bool = False,
    ) -> None:
        private = self._group_overview_is_private(ctx)
        if private and config_sender is not None:
            await config_sender(self, ctx)

        command = ctx.command
        embed = discord.Embed(
            title=command.name.replace("_", " ").title(),
            description=command.short_doc,
        )
        if not private and config_sender is not None:
            embed.add_field(
                name=_("Current configuration"),
                value=_(
                    "Current values are hidden in channels visible to regular members. "
                    "Run this command in a private moderator channel to view them."
                ),
                inline=False,
            )

        def visible_commands(parent: commands.Group) -> typing.Iterator[typing.Any]:
            for child in getattr(parent, "commands", ()):
                descendants = getattr(child, "commands", ())
                if include_descendants and descendants:
                    yield from visible_commands(child)
                else:
                    yield child

        command_lines = []
        for child in visible_commands(command):
            usage = f"{ctx.clean_prefix}{child.qualified_name}"
            if child.signature:
                usage = f"{usage} {child.signature}"
            command_lines.append(f"`{usage}` — {child.short_doc}")

        chunks: list[str] = []
        current: list[str] = []
        for line in command_lines:
            candidate = "\n".join((*current, line))
            if current and len(candidate) > 1024:
                chunks.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            chunks.append("\n".join(current))

        for index, chunk in enumerate(chunks):
            embed.add_field(
                name=_("Commands") if index == 0 else _("Commands (continued)"),
                value=chunk,
                inline=False,
            )
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
        await self._message_registry.initialize()
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
        self._manual_evidence.register()

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
        await self._manual_evidence.shutdown()
        self.joinwatch_auto_role_loop.cancel()
        self.purge_cache_cleanup_loop.cancel()
        self.firstpost_seen_flush_loop.cancel()
        self.detection_case_loop.cancel()
        self.detection_reconciliation_loop.cancel()
        try:
            await self._cancel_owned_task(self._case_restore_task)
        finally:
            self._case_restore_task = None
        pending_gif_tasks = tuple(self._gif_detector_tasks)
        for task in pending_gif_tasks:
            task.cancel()
        if pending_gif_tasks:
            await asyncio.gather(*pending_gif_tasks, return_exceptions=True)
        self._gif_detector_tasks.clear()
        self._gif_detector_animated_guilds.clear()
        self._gif_detector_hits.clear()
        self._gif_detector_active_mutes.clear()
        self._gif_detector_mutes_in_flight.clear()
        self._gif_detector_remote_media_in_flight.clear()
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
        return await detection.purge_cache_cleanup_loop(self)

    @tasks.loop(minutes=1)
    async def detection_case_loop(self) -> None:
        return await detection.detection_case_loop(self)

    @detection_case_loop.before_loop
    async def before_detection_case_loop(self) -> None:
        await self.bot.wait_until_red_ready()

    @tasks.loop(seconds=DETECTION_FAST_RETRY_SECONDS)
    async def detection_reconciliation_loop(self) -> None:
        await self._run_detection_reconciliation()

    @detection_reconciliation_loop.before_loop
    async def before_detection_reconciliation_loop(self) -> None:
        await self.bot.wait_until_red_ready()

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
    # `detection.py` has landed; it imports `review_publication` directly and
    # only these `self`-reached entry points still need the seam.
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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        gif_detected = await gif_detector.on_message(self, message)
        result = await detection.on_message(self, message)
        if not gif_detected:
            await gif_detector.schedule_remote_media_fallback(self, message)
        return result

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
    @commands.group(invoke_without_command=True)
    async def honeypot(self, ctx: commands.Context) -> None:
        """Configure server safety and honeypot protections."""
        return await self._send_group_overview(ctx, detection.config_all)

    @honeypot.group(name="evidence", invoke_without_command=True)
    async def manual_evidence_settings(self, ctx: commands.Context) -> None:
        """Configure manual evidence collection and punishments."""
        return await manual_evidence.show_status(self, ctx)

    @manual_evidence_settings.command(name="memes_channel")
    async def manual_evidence_memes_channel(
        self,
        ctx: commands.Context,
        target: discord.TextChannel,
    ) -> None:
        """Set the channel where the memen't action is available."""
        return await manual_evidence.set_memes_channel(self, ctx, target)

    @manual_evidence_settings.command(name="mement_notification_channel")
    async def manual_evidence_mement_notification_channel(
        self,
        ctx: commands.Context,
        target: discord.TextChannel,
    ) -> None:
        """Set the channel used for memen't notifications."""
        return await manual_evidence.set_mement_notification_channel(
            self,
            ctx,
            target,
        )

    @manual_evidence_settings.command(name="status")
    async def manual_evidence_status(self, ctx: commands.Context) -> None:
        """Show manual evidence configuration."""
        return await manual_evidence.show_status(self, ctx)

    @honeypot.group(name="debug", invoke_without_command=True)
    async def debug(self, ctx: commands.Context) -> None:
        """Maintenance, debug, and export tools."""
        return await self._send_group_overview(ctx)

    @debug.group(name="imagescan", invoke_without_command=True)
    async def debug_imagescan(self, ctx: commands.Context) -> None:
        """Maintenance tools for image scan training data."""
        return await self._send_group_overview(ctx)

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

    @honeypot.group(name="honeypot", invoke_without_command=True)
    async def honeypot_settings(self, ctx: commands.Context) -> None:
        """Configure the main honeypot detection layer."""
        return await self._send_group_overview(ctx, detection.config_honeypot)

    @honeypot_settings.command(name="toggle")
    async def honeypot_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable the main honeypot layer."""
        return await detection.honeypot_toggle(self, ctx, value)

    @honeypot_settings.command()
    async def action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the default action for honeypot detections."""
        return await detection.action(self, ctx, value)

    @honeypot_settings.command(name="fallback_action")
    async def fallback_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the action used when a detector falls back to honeypot handling."""
        return await detection.fallback_action(self, ctx, value)

    @honeypot_settings.command(name="dry_run")
    async def dry_run(self, ctx: commands.Context, value: bool = None) -> None:
        """Log what would happen without applying punishments."""
        return await detection.dry_run(self, ctx, value)

    @honeypot_settings.command(name="whitelist_mode")
    async def whitelist_mode(self, ctx: commands.Context, value: str = None) -> None:
        """Set how whitelisted roles are handled by honeypot detections."""
        return await detection.whitelist_mode(self, ctx, value)

    @honeypot_settings.command(name="automated_kick_fail_warn")
    async def automated_kick_fail_warn(self, ctx: commands.Context, value: bool = None) -> None:
        """Warn when an automated kick cannot run because the user already left."""
        return await detection.automated_kick_fail_warn(self, ctx, value)

    # ─── GIF detector sub-group ────────────────────────────────────────

    @honeypot.group(name="gifdetector", invoke_without_command=True)
    @commands.mod_or_permissions(manage_messages=True)
    async def gif_detector(self, ctx: commands.Context) -> None:
        """Configure channel-scoped GIF interception."""
        return await self._send_group_overview(
            ctx,
            gif_detector.config_gif_detector,
            include_descendants=True,
        )

    @gif_detector.command(name="toggle", usage="<true|false>")
    async def gif_detector_toggle(self, ctx: commands.Context, value: bool) -> None:
        """Enable or disable GIF interception."""
        return await gif_detector.gif_detector_toggle(self, ctx, value)

    @gif_detector.command(name="animation", usage="<true|false>")
    async def gif_detector_animation(self, ctx: commands.Context, value: bool) -> None:
        """Enable or disable the animated ICBM warning."""
        return await gif_detector.gif_detector_animation(self, ctx, value)

    @gif_detector.command(name="retention", usage="[0-60]")
    async def gif_detector_retention(
        self, ctx: commands.Context, seconds: int = None
    ) -> None:
        """Show or set how long detected GIFs remain visible."""
        return await gif_detector.gif_detector_retention(self, ctx, seconds)

    @gif_detector.command(name="threshold", usage="[2-20]")
    async def gif_detector_threshold(
        self, ctx: commands.Context, value: int = None
    ) -> None:
        """Show or set the GIF count required for a mute."""
        return await gif_detector.gif_detector_threshold(self, ctx, value)

    @gif_detector.command(name="window", usage="[5-3600]")
    async def gif_detector_window(
        self, ctx: commands.Context, seconds: int = None
    ) -> None:
        """Show or set the rolling GIF window in seconds."""
        return await gif_detector.gif_detector_window(self, ctx, seconds)

    @gif_detector.command(name="muteduration", usage="[60-604800]")
    async def gif_detector_mute_duration(
        self, ctx: commands.Context, seconds: int = None
    ) -> None:
        """Show or set the role mute duration in seconds."""
        return await gif_detector.gif_detector_mute_duration(self, ctx, seconds)

    @gif_detector.group(name="channel", invoke_without_command=True)
    async def gif_detector_channel(self, ctx: commands.Context) -> None:
        """Configure channels where GIF interception is active."""
        return await self._send_group_overview(ctx, gif_detector.config_gif_detector)

    @gif_detector_channel.command(name="add")
    async def gif_detector_channel_add(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | discord.Thread = None,
    ) -> None:
        """Add a channel or its parent thread scope."""
        return await gif_detector.gif_detector_channel_add(self, ctx, channel)

    @gif_detector_channel.command(name="remove")
    async def gif_detector_channel_remove(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | discord.Thread = None,
    ) -> None:
        """Remove a channel or its parent thread scope."""
        return await gif_detector.gif_detector_channel_remove(self, ctx, channel)

    @gif_detector.group(name="message", invoke_without_command=True)
    async def gif_detector_message(self, ctx: commands.Context) -> None:
        """Configure the static warning shown for additional GIFs."""
        return await self._send_group_overview(ctx, gif_detector.config_gif_detector)

    @gif_detector_message.command(name="set")
    async def gif_detector_message_set(
        self, ctx: commands.Context, *, text: str
    ) -> None:
        """Set the static GIF warning text."""
        return await gif_detector.gif_detector_message_set(self, ctx, text=text)

    @gif_detector_message.command(name="reset")
    async def gif_detector_message_reset(self, ctx: commands.Context) -> None:
        """Reset the static GIF warning text to its default."""
        return await gif_detector.gif_detector_message_reset(self, ctx)

    # ─── channel sub-group ────────────────────────────────────────────

    @honeypot.group(name="channel", invoke_without_command=True)
    async def channels(self, ctx: commands.Context) -> None:
        """Configure honeypot and log channels."""
        return await self._send_group_overview(ctx, detection.config_channel)

    @commands.bot_has_guild_permissions(manage_channels=True)
    @channels.command()
    async def create(self, ctx: commands.Context) -> None:
        """Create and register a new honeypot channel."""
        return await detection.create(self, ctx)

    @channels.command(name="add")
    async def channel_add(self, ctx: commands.Context, target: discord.TextChannel | discord.Thread) -> None:
        """Register an existing channel as a honeypot channel."""
        return await detection.channel_add(self, ctx, target)

    @channels.command(name="remove")
    async def channel_remove(self, ctx: commands.Context, target: discord.TextChannel | discord.Thread) -> None:
        """Unregister a honeypot channel."""
        return await detection.channel_remove(self, ctx, target)

    @channels.command(name="list")
    async def channel_list(self, ctx: commands.Context) -> None:
        """List registered honeypot channels."""
        return await detection.channel_list(self, ctx)

    @channels.command()
    async def logs(self, ctx: commands.Context, target: discord.TextChannel = None) -> None:
        """Set the channel used for honeypot logs."""
        return await detection.logs(self, ctx, target)

    # ─── punishment sub-group ─────────────────────────────────────────

    @honeypot.group(invoke_without_command=True)
    async def punishment(self, ctx: commands.Context) -> None:
        """Configure roles used while a case is awaiting review."""
        return await self._send_group_overview(ctx, detection.config_punishment)

    @punishment.command(name="mute_role")
    async def punishment_mute_role(self, ctx: commands.Context, role: discord.Role = None) -> None:
        """Set the temporary mute role for pending reviews."""
        return await detection.punishment_mute_role(self, ctx, role)

    # ─── purge sub-group ───────────────────────────────────────────────

    @honeypot.group(name="purge", invoke_without_command=True)
    async def purge(self, ctx: commands.Context) -> None:
        """Configure event-registry message purge windows."""
        return await self._send_group_overview(ctx, detection.config_purge)

    @purge.command(name="backward")
    async def purge_backward(self, ctx: commands.Context, seconds: int = None) -> None:
        """Set how far back cached message purge can delete."""
        return await detection.purge_backward(self, ctx, seconds)

    @purge.command(name="forward")
    async def purge_forward(self, ctx: commands.Context, seconds: int = None) -> None:
        """Set how long future messages are purged after a trigger."""
        return await detection.purge_forward(self, ctx, seconds)

    # ─── spam sub-group ────────────────────────────────────────────────

    @honeypot.group(invoke_without_command=True)
    async def spam(self, ctx: commands.Context) -> None:
        """Configure duplicate-message spam detection."""
        return await self._send_group_overview(ctx, detection.config_spam)

    @spam.command(name="toggle")
    async def spam_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable duplicate-message spam detection."""
        return await detection.spam_toggle(self, ctx, value)

    @spam.command(name="action")
    async def spam_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the action for duplicate-message spam detections."""
        return await detection.spam_action(self, ctx, value)

    @spam.command(name="window")
    async def spam_window(self, ctx: commands.Context, seconds: int = None) -> None:
        """Set the time window for duplicate-message detection."""
        return await detection.spam_window(self, ctx, seconds)

    @spam.command(name="channels")
    async def spam_channels(self, ctx: commands.Context, count: int = None) -> None:
        """Set how many channels must contain the same message."""
        return await detection.spam_channels(self, ctx, count)

    # ─── imagescan sub-group ───────────────────────────────────────────

    @honeypot.group(name="imagescan", invoke_without_command=True)
    async def imagescan(self, ctx: commands.Context) -> None:
        """Configure adaptive scam-image detection."""
        return await self._send_group_overview(ctx, imagescan.config_imagescan)

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

    @imagescan.group(name="detector", invoke_without_command=True)
    async def imagescan_detector(self, ctx: commands.Context) -> None:
        """Configure production image detector behavior."""
        return await self._send_group_overview(ctx, imagescan.config_imagescan)

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

    @honeypot.group(invoke_without_command=True)
    async def firstpost(self, ctx: commands.Context) -> None:
        """Configure first-message detection."""
        return await self._send_group_overview(ctx, detection.config_firstpost)

    @firstpost.command(name="toggle")
    async def firstpost_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable first-message enforcement."""
        return await detection.firstpost_toggle(self, ctx, value)

    @firstpost.command(name="warmup")
    async def firstpost_collect(self, ctx: commands.Context, value: bool = None) -> None:
        """Record first-message senders without taking action."""
        return await detection.firstpost_collect(self, ctx, value)

    @firstpost.command(name="action")
    async def firstpost_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the action for suspicious first messages."""
        return await detection.firstpost_action(self, ctx, value)

    # ─── review sub-group ─────────────────────────────────────────────

    @honeypot.group(invoke_without_command=True)
    async def review(self, ctx: commands.Context) -> None:
        """Configure moderator review for suspicious cases."""
        return await self._send_group_overview(ctx, detection.config_review)

    @review.command(name="toggle")
    async def review_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable moderator review routing."""
        return await detection.review_toggle(self, ctx, value)

    @review.command(name="channel")
    async def review_channel(
        self, ctx: commands.Context, target: discord.TextChannel = None
    ) -> None:
        """Set the channel for moderator review requests."""
        return await detection.review_channel(self, ctx, target)

    @review.command(name="kick_fail_warn")
    async def review_kick_fail_warn(self, ctx: commands.Context, value: str = None) -> None:
        """Set how review kicks report users who already left."""
        return await detection.review_kick_fail_warn(self, ctx, value)

    # ─── roles sub-group (was whitelistedroles) ───────────────────────

    @honeypot_settings.group(invoke_without_command=True)
    async def roles(self, ctx: commands.Context) -> None:
        """Manage roles trusted by the main honeypot layer."""
        return await self._send_group_overview(ctx, detection.config_roles)

    @roles.command(name="add")
    async def roles_add(self, ctx: commands.Context, role: discord.Role) -> None:
        """Add a role to the honeypot whitelist."""
        return await detection.roles_add(self, ctx, role)

    @roles.command(name="remove")
    async def roles_remove(self, ctx: commands.Context, role: discord.Role) -> None:
        """Remove a role from the honeypot whitelist."""
        return await detection.roles_remove(self, ctx, role)

    @roles.command(name="list")
    async def roles_list(self, ctx: commands.Context) -> None:
        """List roles on the honeypot whitelist."""
        return await detection.roles_list(self, ctx)

    # ─── keywords sub-group (was scamkeywords) ────────────────────────

    @honeypot_settings.group(invoke_without_command=True)
    async def keywords(self, ctx: commands.Context) -> None:
        """Manage text and attachment patterns used by honeypot detection."""
        return await self._send_group_overview(ctx, detection.config_keywords)

    @keywords.command(name="add")
    async def keywords_add(self, ctx: commands.Context, *, keyword: str) -> None:
        """Add a honeypot keyword."""
        return await detection.keywords_add(self, ctx, keyword=keyword)

    @keywords.command(name="remove")
    async def keywords_remove(self, ctx: commands.Context, *, keyword: str) -> None:
        """Remove a honeypot keyword."""
        return await detection.keywords_remove(self, ctx, keyword=keyword)

    @keywords.command(name="list")
    async def keywords_list(self, ctx: commands.Context) -> None:
        """List configured honeypot keywords."""
        return await detection.keywords_list(self, ctx)

    @keywords.command(name="reset")
    async def keywords_reset(self, ctx: commands.Context) -> None:
        """Reset honeypot keywords to defaults."""
        return await detection.keywords_reset(self, ctx)

    @keywords.group(name="attachments", invoke_without_command=True)
    async def keyword_attachments(self, ctx: commands.Context) -> None:
        """Manage attachment filename patterns used by honeypot detection."""
        return await self._send_group_overview(ctx, detection.config_keywords)

    @keyword_attachments.command(name="add")
    async def keyword_attachments_add(self, ctx: commands.Context, *, pattern: str) -> None:
        """Add an attachment filename pattern."""
        return await detection.keyword_attachments_add(self, ctx, pattern=pattern)

    @keyword_attachments.command(name="remove")
    async def keyword_attachments_remove(self, ctx: commands.Context, *, pattern: str) -> None:
        """Remove an attachment filename pattern."""
        return await detection.keyword_attachments_remove(self, ctx, pattern=pattern)

    @keyword_attachments.command(name="list")
    async def keyword_attachments_list(self, ctx: commands.Context) -> None:
        """List configured attachment filename patterns."""
        return await detection.keyword_attachments_list(self, ctx)

    @keyword_attachments.command(name="reset")
    async def keyword_attachments_reset(self, ctx: commands.Context) -> None:
        """Reset attachment filename patterns to defaults."""
        return await detection.keyword_attachments_reset(self, ctx)

    # ─── joinwatch sub-group ──────────────────────────────────────────

    @honeypot.group(invoke_without_command=True)
    async def joinwatch(self, ctx: commands.Context) -> None:
        """Configure young-account join monitoring."""
        return await self._send_group_overview(ctx, joinwatch.config_joinwatch)

    @joinwatch.command(name="toggle")
    async def joinwatch_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable young-account join monitoring."""
        return await joinwatch.joinwatch_toggle(self, ctx, value)

    @joinwatch.command()
    async def channel(self, ctx: commands.Context, target: discord.TextChannel | discord.Thread = None) -> None:
        """Set the channel for young-account join alerts."""
        return await joinwatch.channel(self, ctx, target)

    @joinwatch.group(name="alert", invoke_without_command=True)
    async def joinwatch_alert(self, ctx: commands.Context) -> None:
        """Configure joinwatch alert delivery."""
        return await self._send_group_overview(ctx, joinwatch.config_joinwatch)

    @joinwatch_alert.command(name="toggle")
    async def joinwatch_alert_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable joinwatch alert messages."""
        return await joinwatch.joinwatch_alert_toggle(self, ctx, value)

    @joinwatch.command(name="max_age")
    async def max_age(self, ctx: commands.Context, hours: int = None) -> None:
        """Set the maximum account age for joinwatch alerts."""
        return await joinwatch.max_age(self, ctx, hours)

    @joinwatch.group(name="autorole", invoke_without_command=True)
    async def joinwatch_autorole(self, ctx: commands.Context) -> None:
        """Configure temporary roles for young accounts."""
        return await self._send_group_overview(ctx, joinwatch.config_joinwatch)

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

    @joinwatch_autorole.group(name="randomize", invoke_without_command=True)
    async def joinwatch_autorole_randomize(self, ctx: commands.Context) -> None:
        """Configure randomized auto-role delay."""
        return await self._send_group_overview(ctx, joinwatch.config_joinwatch)

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

    @honeypot.group(name="bait_role", invoke_without_command=True)
    async def bait_role(self, ctx: commands.Context) -> None:
        """Configure the bait role trap."""
        return await self._send_group_overview(ctx, detection.config_bait)

    @bait_role.command(name="toggle")
    async def bait_toggle(self, ctx: commands.Context, value: bool = None) -> None:
        """Enable or disable bait role enforcement."""
        return await detection.bait_toggle(self, ctx, value)

    @bait_role.command()
    async def role(self, ctx: commands.Context, role: discord.Role = None) -> None:
        """Set the role that triggers bait role enforcement."""
        return await detection.role(self, ctx, role)

    @bait_role.command(name="action")
    async def bait_action(self, ctx: commands.Context, value: str = None) -> None:
        """Set the action for bait role enforcement."""
        return await detection.bait_action(self, ctx, value)

    # ─── config dump ───────────────────────────────────────────────────

    @honeypot.group(name="config", invoke_without_command=True)
    async def config_dump(self, ctx: commands.Context) -> None:
        """Show current honeypot configuration by section."""
        return await diagnostics.config_dump(self, ctx, detection.config_all)

    @config_dump.command(name="honeypot")
    async def config_honeypot(self, ctx: commands.Context) -> None:
        """Show main honeypot detection settings."""
        return await detection.config_honeypot(self, ctx)

    @config_dump.command(name="channel")
    async def config_channel(self, ctx: commands.Context) -> None:
        """Show honeypot and log channel settings."""
        return await detection.config_channel(self, ctx)

    @config_dump.command(name="punishment")
    async def config_punishment(self, ctx: commands.Context) -> None:
        """Show review punishment settings."""
        return await detection.config_punishment(self, ctx)

    @config_dump.command(name="purge")
    async def config_purge(self, ctx: commands.Context) -> None:
        """Show message purge behavior."""
        return await detection.config_purge(self, ctx)

    @config_dump.command(name="firstpost")
    async def config_firstpost(self, ctx: commands.Context) -> None:
        """Show first-message detection settings."""
        return await detection.config_firstpost(self, ctx)

    @config_dump.command(name="imagescan")
    async def config_imagescan(self, ctx: commands.Context) -> None:
        """Show image detector settings."""
        return await imagescan.config_imagescan(self, ctx)

    @config_dump.command(name="spam")
    async def config_spam(self, ctx: commands.Context) -> None:
        """Show duplicate-message spam settings."""
        return await detection.config_spam(self, ctx)

    @config_dump.command(name="review")
    async def config_review(self, ctx: commands.Context) -> None:
        """Show moderator review settings."""
        return await detection.config_review(self, ctx)

    @config_dump.command(name="roles")
    async def config_roles(self, ctx: commands.Context) -> None:
        """Show honeypot whitelist role settings."""
        return await detection.config_roles(self, ctx)

    @config_dump.command(name="keywords")
    async def config_keywords(self, ctx: commands.Context) -> None:
        """Show honeypot keyword and attachment pattern counts."""
        return await detection.config_keywords(self, ctx)

    @config_dump.command(name="joinwatch")
    async def config_joinwatch(self, ctx: commands.Context) -> None:
        """Show joinwatch settings."""
        return await joinwatch.config_joinwatch(self, ctx)

    @config_dump.command(name="bait_role")
    async def config_bait(self, ctx: commands.Context) -> None:
        """Show bait role trap settings."""
        return await detection.config_bait(self, ctx)

    @config_dump.command(name="stats")
    async def config_stats(self, ctx: commands.Context) -> None:
        """Show stored stat and pending timer counts."""
        return await detection.config_stats(self, ctx)

    @config_dump.command(name="all")
    async def config_all(self, ctx: commands.Context) -> None:
        """Show a compact summary of all honeypot settings."""
        return await detection.config_all(self, ctx)

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
        # Binds the two purge predicates through their owning module: importing
        # them into diagnostics would close an import cycle, and reading them
        # from the re-export below would give the pair two live bindings.
        return await diagnostics._doctor_channel_permission_checks(
            self,
            guild,
            me,
            honeypot_channels,
            logs_channel,
            review_channel=review_channel,
            missing_purge_permissions=detection.missing_purge_permissions,
            is_purgeable_message_channel=detection.is_purgeable_message_channel,
        )

    @honeypot.command(name="doctor")
    async def honeypot_doctor(self, ctx: commands.Context) -> None:
        """Check honeypot configuration and required permissions."""
        return await diagnostics.honeypot_doctor(self, ctx)
