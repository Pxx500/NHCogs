from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as datetime_time
from typing import TypeVar
from uuid import uuid4

import discord
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .achievement_definitions import (
    SOLO_GATER_DEFINITION,
    SOLO_GATER_KEY,
)
from .achievement_store import AchievementProfile, AchievementStore
from .achievement_sync import (
    DiscordRoleSnapshot,
    build_discord_priority_plan,
    build_discord_role_backup,
    build_discord_role_snapshot,
)
from .activity_storage import (
    ActivityConsistencyReport,
    ActivityDatabaseStats,
    ActivityLocation,
    ActivityStore,
    ChannelTimelineDay,
    ChannelUserCount,
    DailyDominantLocation,
    DailySummary,
    TimelineDay,
    TopChannel,
    UserChannelDistribution,
    UserStats,
)
from .forum_autopin import ForumAutopinService
from .gate_increment_store import (
    GateIncrementMemberPlan,
    GateIncrementSnapshot,
    GateIncrementStore,
    GateProgressConflict,
    MemberState,
    OperationState,
    RecoveryAction,
    SourceMessageKey,
    classify_member_recovery,
)
from .gate_roles import (
    GATE_TIER_ROLE_IDS,
    SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
    build_role_ids_for_target,
    plan_gate_transition,
)
from .role_analytics_service import (
    FullMemberRequestCooldownError,
    MemberIntentRequiredError,
    RoleAnalyticsService,
    SyncAlreadyRunningError,
)
from .role_analytics_store import (
    AnalyticsUnavailableError,
    RoleAnalyticsStore,
    SyncStatus,
)
from .role_export import ExportMember, ExportTooLarge, build_role_export
from .role_expression import (
    RoleExpressionSyntaxError,
    compile_role_expression,
    parse_role_expression,
    render_role_expression,
    role_ids,
)
from .sticky_roles import StickyRoleStore
from .voice_activity import VoiceChannelVisitTracker

log = logging.getLogger("red.NHMisc")

_InteractionResult = TypeVar("_InteractionResult")

DEFAULT_VCJUMPING_VISIT_COUNT = 3
DEFAULT_VCJUMPING_WINDOW_SECONDS = 30
ACHIEVEMENT_INTERACTION_DB_TIMEOUT_SECONDS = 15
DEFAULT_ACTIVITY_DETAIL_RETENTION_DAYS = 31
DEFAULT_ACTIVITY_HISTORY_RETENTION_DAYS = -1
CLEANUP_RESPONSE_TTL_SECONDS = 10
RETENTION_CONFIRMATION = "I understand"


def _parse_sticky_db_decision(content: str) -> tuple[str, int | None, str]:
    command, separator, remainder = content.strip().partition(" ")
    if not separator:
        return command.lower(), None, ""

    role_id_text, _, argument = remainder.strip().partition(" ")
    try:
        role_id = int(role_id_text)
    except ValueError:
        role_id = None
    return command.lower(), role_id, argument.strip()

# Categorical chart hues in fixed rank order, arranged so neighboring bars use
# clearly different colors. The neutral tone is reserved for undisplayed users.
CHATCHART_SERIES_COLORS = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
    "#00a6d6",
    "#7a5c00",
    "#a1c935",
    "#9f55d4",
    "#c44e9b",
    "#006d77",
    "#f48c06",
    "#264653",
    "#9b5de5",
    "#ef476f",
    "#118ab2",
    "#6a994e",
)
CHATCHART_OTHER_COLOR = "#898781"
DEFAULT_CHATCHART_USER_COUNT = 10
MAX_CHATCHART_USER_COUNT = 20
DISCORD_SNOWFLAKE_MIN_DIGITS = 15
STARGATE_EMOJI_NAME = "stargate"
STARGATE_EMOJI_ID = 769315278953381928
MAX_GATE_INCREMENT_CANDIDATES = 25
MAX_ACHIEVEMENT_RECIPIENTS = 25
TIER_DISTRIBUTION_ROLES = (
    ("Stone", "stoneTier", 757571320945967205, 757645112267243541),
    ("Steam", "steamTier", 757571510880829540, 757643319265460224),
    ("LV", "lvTier", 757571726790885378, 630848584539045926),
    ("MV", "mvTier", 757571761159012383, 631180331839389738),
    ("HV", "hvTier", 757571801961201714, 631180321727184896),
    ("EV", "evTier", 757571842209873991, 631180312906563594),
    ("IV", "ivTier", 757571883268046908, 631180295252738099),
    ("LuV", "luvTier", 757571961114066994, 631180266982866986),
    ("ZPM", "zpmTier", 757571992500305962, 631180246837624852),
    ("UV", "uvTier", 757572023269720078, 631180223928336414),
    ("UHV", "uhvTier", 757572062058643467, 631180193960296478),
    ("UEV", "uevTier", 888133083931476009, 631180158262575174),
    ("UIV", "uivTier", 888133292547772467, 631180143385247754),
    ("UMV", "umvTier", 888133377620852776, 631180120782012426),
    ("UXV", "uxvTier", 888133463461494864, 631180089782042625),
)


def _require_guild_role(
    guild,
    role_id: int,
    *,
    report_name: str,
    role_label: str,
):
    role = guild.get_role(role_id)
    if role is None:
        raise commands.UserFeedbackCheckFailure(
            f"{report_name} is misconfigured: {role_label} role "
            f"({role_id}) was not found in this server."
        )
    return role


@dataclass(frozen=True, slots=True)
class GateIncrementCandidate:
    user_id: int
    display_name: str
    current_gate_role_ids: tuple[int, ...]
    current_tier: int | None
    target_role_id: int | None
    has_solo_gater: bool = False


def _gate_increment_candidate_ids(source_message) -> tuple[int, ...]:
    ordered_user_ids = []
    if source_message.webhook_id is None:
        ordered_user_ids.append(source_message.author.id)
    ordered_user_ids.extend(source_message.raw_mentions)
    return tuple(dict.fromkeys(ordered_user_ids))


def _build_gate_increment_candidates(
    source_message,
) -> tuple[GateIncrementCandidate, ...]:
    guild = source_message.guild

    candidates = []
    for user_id in _gate_increment_candidate_ids(source_message):
        member = guild.get_member(user_id)
        if member is None or member.bot:
            continue
        candidates.append(_build_gate_increment_candidate(member))
    return tuple(candidates)


def _build_achievement_candidates(source_message) -> tuple:
    guild = source_message.guild
    candidates = []
    for user_id in _gate_increment_candidate_ids(source_message):
        member = guild.get_member(user_id)
        if member is None or member.bot:
            continue
        candidates.append(member)
    return tuple(candidates)


def _build_gate_increment_candidate(member) -> GateIncrementCandidate:
    member_role_ids = tuple(role.id for role in member.roles)
    transition = plan_gate_transition(member_role_ids)
    target_role_id = (
        GATE_TIER_ROLE_IDS[transition.target_tier - 1]
        if transition.target_tier is not None
        else None
    )
    return GateIncrementCandidate(
        user_id=member.id,
        display_name=member.display_name,
        current_gate_role_ids=transition.current_role_ids,
        current_tier=transition.current_tier,
        target_role_id=target_role_id,
        has_solo_gater=SINGLEPLAYER_GATE_COMPLETED_ROLE_ID in member_role_ids,
    )


def _validate_gate_increment_configuration(guild) -> tuple:
    bot_member = guild.me
    permissions = getattr(bot_member, "guild_permissions", None)
    if bot_member is None or not permissions or not permissions.manage_roles:
        raise commands.UserFeedbackCheckFailure("I need Manage Roles permission")

    roles = []
    for role_id in GATE_TIER_ROLE_IDS:
        role = guild.get_role(role_id)
        if (
            role is None
            or role.managed
            or role.position >= bot_member.top_role.position
        ):
            raise commands.UserFeedbackCheckFailure(
                "Gate roles are configured incorrectly"
            )
        roles.append(role)
    return tuple(roles)


class NHMisc(commands.Cog):
    """Miscellaneous small utilities for Red-DiscordBot."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=8597423150612235807,
            force_registration=True,
        )
        self.config.register_guild(
            voice_log_channel=None,
            alert_channel=None,
            vcjumping_visit_count=DEFAULT_VCJUMPING_VISIT_COUNT,
            vcjumping_window_seconds=DEFAULT_VCJUMPING_WINDOW_SECONDS,
            activity_channel=None,
            activity_detail_retention_days=DEFAULT_ACTIVITY_DETAIL_RETENTION_DAYS,
            activity_history_retention_days=DEFAULT_ACTIVITY_HISTORY_RETENTION_DAYS,
            sticky_debug_logging_enabled=False,
            forum_autopin_channel_ids=[],
        )
        self._voice_visits = VoiceChannelVisitTracker()
        self._audit_log_tasks: set[asyncio.Task] = set()
        self._forum_autopin = ForumAutopinService(
            self.config, alert_sender=self._send_guild_alert, logger=log
        )
        self._activity_store = ActivityStore(cog_data_path(self) / "activity.sqlite")
        self._sticky_roles = StickyRoleStore(cog_data_path(self) / "sticky_roles.sqlite")
        self._role_analytics_store = RoleAnalyticsStore(
            cog_data_path(self) / "role_analytics.sqlite"
        )
        self._role_analytics = RoleAnalyticsService(
            self.bot, self._role_analytics_store, logger=log
        )
        achievements_path = cog_data_path(self) / "achievements.sqlite"
        self._achievement_store = AchievementStore(achievements_path)
        self._achievement_syncing_guilds: set[int] = set()
        self._activity_task: asyncio.Task | None = None
        self._role_analytics_startup_task: asyncio.Task | None = None
        self._role_analytics_daily_task: asyncio.Task | None = None
        self._gate_increment_store = GateIncrementStore(
            achievements_path
        )
        self._gate_increment_recovery_task: asyncio.Task | None = None
        self._gate_increment_context_menu = discord.app_commands.ContextMenu(
            name="Increment Gate roles",
            callback=self._gate_increment_context_action,
        )
        self._gate_increment_context_menu.default_permissions = discord.Permissions(
            manage_messages=True
        )
        self._gate_increment_context_menu.guild_only = True
        self._gate_increment_context_registered = False
        self._achievements_slash_command = discord.app_commands.Command(
            name="achievements",
            description="Show a member's achievements",
            callback=self._achievements_slash,
        )
        self._achievements_slash_command.guild_only = True
        self._achievements_user_context_menu = discord.app_commands.ContextMenu(
            name="View achievements",
            callback=self._achievements_user_context_action,
        )
        self._achievements_user_context_menu.guild_only = True
        self._grant_achievements_context_menu = discord.app_commands.ContextMenu(
            name="Grant achievements",
            callback=self._grant_achievements_context_action,
        )
        self._grant_achievements_context_menu.default_permissions = (
            discord.Permissions(manage_messages=True)
        )
        self._grant_achievements_context_menu.guild_only = True
        self._achievement_commands_registered = False

    async def cog_load(self) -> None:
        await self._activity_store.initialize()
        await self._sticky_roles.initialize()
        await self._role_analytics_store.initialize()
        await self._achievement_store.initialize()
        await self._gate_increment_store.initialize()
        self._register_gate_increment_context_menu()
        self._register_achievement_commands()
        self._activity_task = asyncio.create_task(self._activity_midnight_loop())
        self._role_analytics_startup_task = asyncio.create_task(
            self._role_analytics_startup_reconcile()
        )
        self._role_analytics_daily_task = asyncio.create_task(
            self._role_analytics_daily_loop()
        )
        self._gate_increment_recovery_task = asyncio.create_task(
            self._recover_interrupted_gate_increments()
        )

    def cog_unload(self) -> None:
        for task in self._audit_log_tasks:
            task.cancel()
        if self._activity_task is not None:
            self._activity_task.cancel()
        if self._role_analytics_startup_task is not None:
            self._role_analytics_startup_task.cancel()
        if self._role_analytics_daily_task is not None:
            self._role_analytics_daily_task.cancel()
        if self._gate_increment_recovery_task is not None:
            self._gate_increment_recovery_task.cancel()
        self._unregister_gate_increment_context_menu()
        self._unregister_achievement_commands()
        self._role_analytics.cancel()

    def _register_gate_increment_context_menu(self) -> None:
        command_type = discord.AppCommandType.message
        existing = self.bot.tree.get_command(
            self._gate_increment_context_menu.name,
            type=command_type,
        )
        if existing is self._gate_increment_context_menu:
            self._gate_increment_context_registered = True
            return
        self.bot.tree.add_command(
            self._gate_increment_context_menu,
            override=True,
        )
        self._gate_increment_context_registered = True

    def _unregister_gate_increment_context_menu(self) -> None:
        if not self._gate_increment_context_registered:
            return
        command_type = discord.AppCommandType.message
        existing = self.bot.tree.get_command(
            self._gate_increment_context_menu.name,
            type=command_type,
        )
        if existing is self._gate_increment_context_menu:
            self.bot.tree.remove_command(
                self._gate_increment_context_menu.name,
                type=command_type,
            )
        self._gate_increment_context_registered = False

    def _register_achievement_commands(self) -> None:
        for command in (
            self._achievements_slash_command,
            self._achievements_user_context_menu,
            self._grant_achievements_context_menu,
        ):
            self.bot.tree.add_command(command, override=True)
        self._achievement_commands_registered = True

    def _unregister_achievement_commands(self) -> None:
        if not self._achievement_commands_registered:
            return
        self.bot.tree.remove_command(
            self._achievements_slash_command.name,
            type=discord.AppCommandType.chat_input,
        )
        self.bot.tree.remove_command(
            self._achievements_user_context_menu.name,
            type=discord.AppCommandType.user,
        )
        self.bot.tree.remove_command(
            self._grant_achievements_context_menu.name,
            type=discord.AppCommandType.message,
        )
        self._achievement_commands_registered = False

    async def configured_sticky_role_ids(self, guild_id: int) -> frozenset[int]:
        return frozenset(await self._sticky_roles.get_sticky_roles(guild_id))

    async def _achievement_discord_snapshot(
        self, guild: discord.Guild
    ) -> DiscordRoleSnapshot | None:
        cached_member_count = len(guild.members)
        reported_member_count = guild.member_count
        if (
            not guild.chunked
            or reported_member_count is None
            or cached_member_count != reported_member_count
        ):
            raise commands.UserFeedbackCheckFailure(
                "Discord member cache is incomplete. Run `!rolesync` first"
            )

        guild_id = guild.id
        state = await self._role_analytics_store.get_state(guild_id)
        if state.status is not SyncStatus.READY or state.last_completed_at is None:
            return None
        cached_non_bot_members = tuple(member for member in guild.members if not member.bot)
        try:
            analytics_member_count = await self._role_analytics_store.count_matching(
                guild_id,
                "1 = 1",
                (),
            )
        except AnalyticsUnavailableError:
            return None
        if analytics_member_count != len(cached_non_bot_members):
            raise commands.UserFeedbackCheckFailure(
                "Role analytics member count does not match Discord. Run `!rolesync` first"
            )
        if await self._achievement_store.is_bootstrapped(guild_id):
            definitions = await self._achievement_store.list_definitions(guild_id)
        else:
            definitions = (SOLO_GATER_DEFINITION,)
        bound_definitions = tuple(
            definition for definition in definitions if definition.role_id is not None
        )
        role_ids = (
            *GATE_TIER_ROLE_IDS,
            *(definition.role_id for definition in bound_definitions),
        )
        if any(guild.get_role(role_id) is None for role_id in role_ids):
            raise commands.UserFeedbackCheckFailure("Achievement roles are configured incorrectly")
        users_by_role = await self._role_analytics_users_with_roles(
            guild_id,
            role_ids,
        )
        if users_by_role is None:
            return None
        cached_role_ids_by_user = {
            member.id: {role.id for role in member.roles} for member in cached_non_bot_members
        }
        cached_users_by_role = tuple(
            tuple(
                sorted(
                    user_id
                    for user_id, member_role_ids in cached_role_ids_by_user.items()
                    if role_id in member_role_ids
                )
            )
            for role_id in role_ids
        )
        if users_by_role != cached_users_by_role:
            raise commands.UserFeedbackCheckFailure(
                "Role analytics role holders do not match Discord. Run `!rolesync` first"
            )
        gate_role_count = len(GATE_TIER_ROLE_IDS)
        return build_discord_role_snapshot(
            snapshot_at=state.last_completed_at,
            users_by_gate_role=users_by_role[:gate_role_count],
            boolean_users={
                definition.key: holders
                for definition, holders in zip(
                    bound_definitions,
                    users_by_role[gate_role_count:],
                    strict=True,
                )
            },
            role_holders=dict(zip(role_ids, cached_users_by_role, strict=True)),
            cached_member_count=cached_member_count,
            reported_member_count=reported_member_count,
        )

    async def _role_analytics_users_with_roles(
        self,
        guild_id: int,
        role_ids_to_query: tuple[int, ...],
    ) -> tuple[tuple[int, ...], ...] | None:
        predicate_sql = """
            EXISTS (
                SELECT 1
                FROM role_analytics_memberships AS membership
                WHERE membership.guild_id = member.guild_id
                    AND membership.generation = member.generation
                    AND membership.user_id = member.user_id
                    AND membership.role_id = ?
            )
        """
        try:
            users_by_role = []
            for role_id in role_ids_to_query:
                users_by_role.append(
                    await self._role_analytics_store.matching_user_ids(
                        guild_id,
                        predicate_sql,
                        (role_id,),
                    )
                )
        except AnalyticsUnavailableError:
            return None
        return tuple(users_by_role)

    async def _reconcile_achievement_roles_for_guild(
        self, guild: discord.Guild
    ) -> None:
        definitions = tuple(
            definition
            for definition in await self._achievement_store.list_definitions(guild.id)
            if definition.role_id is not None
        )
        users_by_role = await self._role_analytics_users_with_roles(
            guild.id,
            (
                *GATE_TIER_ROLE_IDS,
                *(definition.role_id for definition in definitions),
            ),
        )
        if users_by_role is None:
            return
        gate_role_count = len(GATE_TIER_ROLE_IDS)
        actual_gate_roles: dict[int, set[int]] = {}
        for role_id, user_ids in zip(
            GATE_TIER_ROLE_IDS,
            users_by_role[:gate_role_count],
            strict=True,
        ):
            for user_id in user_ids:
                actual_gate_roles.setdefault(user_id, set()).add(role_id)
        projections = await self._achievement_store.list_gate_projections(guild.id)
        corrected = 0
        failed = 0
        for user_id in set(actual_gate_roles) | set(projections):
            completed_count = projections.get(user_id, 0)
            expected = (
                {GATE_TIER_ROLE_IDS[completed_count - 1]}
                if 0 < completed_count <= len(GATE_TIER_ROLE_IDS)
                else set()
            )
            if actual_gate_roles.get(user_id, set()) == expected:
                continue
            try:
                member = await guild.fetch_member(user_id)
                if await self._restore_gate_projection(
                    guild,
                    member,
                    completed_count,
                    reason="Achievement database reconciliation",
                ):
                    corrected += 1
            except (
                commands.UserFeedbackCheckFailure,
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                log.exception(
                    "Failed to reconcile Gate projection for guild %s member %s",
                    guild.id,
                    user_id,
                )
                failed += 1

        for definition, actual_user_ids in zip(
            definitions,
            users_by_role[gate_role_count:],
            strict=True,
        ):
            actual_users = set(actual_user_ids)
            stored_users = set(
                await self._achievement_store.projected_users_for_boolean(
                    guild.id,
                    definition.key,
                )
            )
            for user_id in actual_users ^ stored_users:
                should_have_role = user_id in stored_users
                try:
                    member = await guild.fetch_member(user_id)
                    await self._edit_achievement_roles(
                        guild,
                        member,
                        add_role_ids=(definition.role_id,) if should_have_role else (),
                        remove_role_ids=() if should_have_role else (definition.role_id,),
                        reason="Achievement database reconciliation",
                    )
                    corrected += 1
                except (
                    commands.UserFeedbackCheckFailure,
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    failed += 1
                    log.exception(
                        "Failed to reconcile achievement %s for guild %s member %s",
                        definition.key,
                        guild.id,
                        user_id,
                    )
        if corrected or failed:
            await self._send_guild_alert(
                guild,
                "Achievement role reconciliation complete\n"
                f"Members corrected: {corrected}\n"
                f"Members skipped: {failed}",
            )

    @staticmethod
    async def _restore_gate_projection(
        guild: discord.Guild,
        member: discord.Member,
        completed_count: int,
        *,
        reason: str,
    ) -> bool:
        if not 0 <= completed_count <= len(GATE_TIER_ROLE_IDS):
            log.error(
                "Gate projection exceeds configured tiers for guild %s member %s",
                guild.id,
                member.id,
            )
            return False
        _validate_gate_increment_configuration(guild)
        if member.top_role.position >= guild.me.top_role.position:
            log.warning(
                "Cannot restore Gate projection for guild %s member %s due to hierarchy",
                guild.id,
                member.id,
            )
            return False
        current_role_ids = tuple(role.id for role in member.roles)
        non_gate_role_ids = tuple(
            role_id
            for role_id in current_role_ids
            if role_id not in GATE_TIER_ROLE_IDS
            and role_id != guild.default_role.id
        )
        desired_role_ids = non_gate_role_ids
        if completed_count:
            desired_role_ids = (
                *desired_role_ids,
                GATE_TIER_ROLE_IDS[completed_count - 1],
            )
        current_assignable = {
            role_id
            for role_id in current_role_ids
            if role_id != guild.default_role.id
        }
        if set(desired_role_ids) == current_assignable:
            return False
        desired_roles = [guild.get_role(role_id) for role_id in desired_role_ids]
        if any(role is None for role in desired_roles):
            raise commands.UserFeedbackCheckFailure(
                "Gate roles are configured incorrectly"
            )
        await member.edit(roles=desired_roles, reason=reason)
        return True

    async def _role_analytics_startup_reconcile(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self._role_analytics.reconcile_enabled_guilds(tuple(self.bot.guilds))
            for guild in self.bot.guilds:
                if not await self._achievement_store.is_bootstrapped(guild.id):
                    await self._send_guild_alert(
                        guild,
                        "Achievement initialization is required\n\n"
                        "The achievement database has not been initialized from the "
                        "current Discord roles.\n"
                        "Run `!rolesync discord`.",
                    )
                    continue
                await self._reconcile_achievement_roles_for_guild(guild)
        except Exception:
            log.exception("Failed to reconcile role analytics on startup")

    async def _role_analytics_daily_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(24 * 60 * 60)
            try:
                await self._role_analytics.run_daily_reconciliation(
                    tuple(self.bot.guilds)
                )
                for guild in self.bot.guilds:
                    if await self._achievement_store.is_bootstrapped(guild.id):
                        await self._reconcile_achievement_roles_for_guild(guild)
            except Exception:
                log.exception("Failed to run daily role analytics reconciliation")

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        try:
            await self._role_analytics.reconcile_enabled_guilds(tuple(self.bot.guilds))
            for guild in self.bot.guilds:
                if await self._achievement_store.is_bootstrapped(guild.id):
                    await self._reconcile_achievement_roles_for_guild(guild)
        except Exception:
            log.exception("Failed to reconcile achievement roles after resume")

    async def red_delete_data_for_user(self, *, requester, user_id: int) -> None:
        await self._activity_store.delete_user_everywhere(user_id)
        await self._sticky_roles.delete_user_everywhere(user_id)
        await self._role_analytics_store.delete_user_everywhere(user_id)
        await self._achievement_store.delete_user_everywhere(user_id)
        await self._gate_increment_store.redact_user_data(user_id)

    @staticmethod
    def _log_achievement_interaction_start(
        action: str,
        interaction: discord.Interaction,
    ) -> None:
        log.info(
            "Achievement interaction started: action=%s guild=%s user=%s",
            action,
            getattr(interaction.guild, "id", None),
            getattr(interaction.user, "id", None),
        )

    @staticmethod
    async def _await_achievement_interaction_data(
        awaitable: Awaitable[_InteractionResult],
    ) -> _InteractionResult:
        return await asyncio.wait_for(
            awaitable,
            timeout=ACHIEVEMENT_INTERACTION_DB_TIMEOUT_SECONDS,
        )

    @staticmethod
    async def _send_achievement_interaction_error(
        interaction: discord.Interaction,
        message: str,
        *,
        public_defer: bool,
    ) -> None:
        if public_defer:
            await interaction.delete_original_response()
            await interaction.followup.send(message, ephemeral=True)
            return
        await interaction.edit_original_response(content=message)

    async def _handle_achievement_interaction_failure(
        self,
        interaction: discord.Interaction,
        action: str,
        error: Exception,
        *,
        public_defer: bool,
    ) -> None:
        if isinstance(error, asyncio.TimeoutError):
            log.error(
                "Achievement interaction timed out: action=%s guild=%s user=%s",
                action,
                getattr(interaction.guild, "id", None),
                getattr(interaction.user, "id", None),
            )
            message = "Achievement data is busy. Try again in a moment"
        else:
            log.error(
                "Achievement interaction failed: action=%s guild=%s user=%s",
                action,
                getattr(interaction.guild, "id", None),
                getattr(interaction.user, "id", None),
                exc_info=error,
            )
            message = "Achievement data could not be loaded. Try again later"
        await self._send_achievement_interaction_error(
            interaction,
            message,
            public_defer=public_defer,
        )

    async def _achievements_slash(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        action = "achievements slash"
        self._log_achievement_interaction_start(action, interaction)
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            is_bootstrapped = await self._await_achievement_interaction_data(
                self._achievement_store.is_bootstrapped(interaction.guild.id)
            )
            if not is_bootstrapped:
                await self._send_achievement_interaction_error(
                    interaction,
                    "Achievement data is still initializing",
                    public_defer=True,
                )
                return
            target = user or interaction.user
            profile = await self._await_achievement_interaction_data(
                self._achievement_store.get_profile(
                    interaction.guild.id,
                    target.id,
                )
            )
            definitions = await self._await_achievement_interaction_data(
                self._achievement_store.list_definitions(interaction.guild.id)
            )
            await interaction.edit_original_response(
                embed=self._build_achievements_embed(
                    interaction.guild.id,
                    target,
                    profile,
                    definitions,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            await self._handle_achievement_interaction_failure(
                interaction,
                action,
                error,
                public_defer=True,
            )

    async def _achievements_user_context_action(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        action = "view achievements context action"
        self._log_achievement_interaction_start(action, interaction)
        if interaction.guild is None:
            await interaction.response.send_message(
                "This action can only be used in a server",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            is_bootstrapped = await self._await_achievement_interaction_data(
                self._achievement_store.is_bootstrapped(interaction.guild.id)
            )
            if not is_bootstrapped:
                await interaction.edit_original_response(
                    content="Achievement data is still initializing"
                )
                return
            profile = await self._await_achievement_interaction_data(
                self._achievement_store.get_profile(
                    interaction.guild.id,
                    user.id,
                )
            )
            definitions = await self._await_achievement_interaction_data(
                self._achievement_store.list_definitions(interaction.guild.id)
            )
            await interaction.edit_original_response(
                embed=self._build_achievements_embed(
                    interaction.guild.id,
                    user,
                    profile,
                    definitions,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            await self._handle_achievement_interaction_failure(
                interaction,
                action,
                error,
                public_defer=False,
            )

    @staticmethod
    def _build_achievements_embed(
        guild_id: int,
        user: discord.User | discord.Member,
        profile: AchievementProfile,
        definitions,
    ) -> discord.Embed:
        embed = discord.Embed(title=f"Achievements — {user.display_name}")
        embed.add_field(
            name="Stargates completed",
            value=str(profile.stargate_count),
            inline=False,
        )
        if profile.stargate_proofs:
            proof_lines = [
                (
                    f"Stargate {proof.ordinal} — "
                    f"[View message](https://discord.com/channels/"
                    f"{guild_id}/"
                    f"{proof.source_channel_id}/{proof.source_message_id})"
                )
                for proof in profile.stargate_proofs
            ]
            embed.add_field(
                name="Proofs",
                value="\n".join(proof_lines),
                inline=False,
            )
        active_boolean_keys = set(profile.boolean_keys)
        boolean_lines = [
            definition.display_name
            for definition in definitions
            if definition.key in active_boolean_keys
            and definition.key != "stargate_completed"
        ]
        if boolean_lines:
            embed.add_field(
                name="Achievements",
                value="\n".join(boolean_lines),
                inline=False,
            )
        if profile.stargate_count == 0 and not boolean_lines:
            embed.description = "No achievements recorded"
        return embed

    async def _grant_achievements_context_action(
        self,
        interaction: discord.Interaction,
        source_message: discord.Message,
    ) -> None:
        action = "grant achievements context action"
        self._log_achievement_interaction_start(action, interaction)
        permissions = interaction.permissions
        if (
            interaction.guild is None
            or permissions is None
            or not permissions.manage_messages
        ):
            await interaction.response.send_message(
                "You need Manage Messages permission",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            is_bootstrapped = await self._await_achievement_interaction_data(
                self._achievement_store.is_bootstrapped(interaction.guild.id)
            )
            if not is_bootstrapped:
                await interaction.edit_original_response(
                    content=(
                        "Achievement data is still initializing. Run rolesync first"
                    )
                )
                return
            candidates = _build_achievement_candidates(source_message)
            if not candidates:
                await interaction.edit_original_response(
                    content="This message has no eligible recipients"
                )
                return
            if len(candidates) > MAX_ACHIEVEMENT_RECIPIENTS:
                await interaction.edit_original_response(
                    content=(
                        "This action supports at most "
                        f"{MAX_ACHIEVEMENT_RECIPIENTS} users"
                    )
                )
                return
            all_definitions = await self._await_achievement_interaction_data(
                self._achievement_store.list_definitions(interaction.guild.id)
            )
            definitions = tuple(
                definition
                for definition in all_definitions
                if definition.grantable
            )
            if not definitions:
                await interaction.edit_original_response(
                    content="No achievements are available for manual grants"
                )
                return
            from .achievement_views import AchievementGrantView

            view = AchievementGrantView(
                self,
                source_message,
                interaction.user.id,
                candidates,
                definitions,
            )
            await interaction.edit_original_response(
                embed=view.render_embed(),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.message = await interaction.original_response()
        except Exception as error:
            await self._handle_achievement_interaction_failure(
                interaction,
                action,
                error,
                public_defer=False,
            )

    async def _confirm_achievement_grant(self, interaction, view) -> None:
        await interaction.response.defer()
        if not view.selected_user_ids or not view.selected_keys:
            await interaction.edit_original_response(
                embed=view.render_embed(
                    notice="Select at least one recipient and achievement"
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            source_message = await self._fetch_gate_increment_source(
                self._gate_increment_key(view.source_message)
            )
        except commands.UserFeedbackCheckFailure as error:
            await interaction.edit_original_response(
                embed=view.render_embed(notice=str(error)),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        candidates = _build_achievement_candidates(source_message)
        if tuple(member.id for member in candidates) != view.candidate_ids:
            view.source_message = source_message
            view.replace_candidates(candidates)
            await interaction.edit_original_response(
                embed=view.render_embed(
                    notice="The source message changed. Review the recipients again"
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        selected_members = tuple(
            member for member in candidates if member.id in view.selected_user_ids
        )
        selected_definitions = tuple(
            definition
            for definition in view.definitions
            if definition.key in view.selected_keys
        )
        current_definitions = {
            definition.key: definition
            for definition in await self._achievement_store.list_definitions(
                source_message.guild.id
            )
        }
        if any(
            current_definitions.get(definition.key) != definition
            for definition in selected_definitions
        ):
            await interaction.edit_original_response(
                embed=view.render_embed(
                    notice="Achievement configuration changed. Start again"
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            projected_roles = self._validate_achievement_role_projection(
                source_message.guild,
                selected_members,
                selected_definitions,
            )
        except commands.UserFeedbackCheckFailure as error:
            await interaction.edit_original_response(
                embed=view.render_embed(notice=str(error)),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        created_count = 0
        already_count = 0
        failed_members = 0
        for member in selected_members:
            created, already, failed = await self._grant_achievements_to_member(
                source_message,
                member,
                selected_definitions,
                projected_roles,
            )
            created_count += created
            already_count += already
            failed_members += int(failed)
        view.stop()
        status = f"Granted {created_count} achievements"
        if already_count:
            status += f"; {already_count} were already assigned"
        if failed_members:
            status += f"; {failed_members} users could not be updated"
        await interaction.edit_original_response(
            content=status,
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(name="achievement", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def achievement(self, ctx: commands.Context) -> None:
        """Manage member achievements."""
        embed = discord.Embed(
            title="Achievements",
            description=(
                "View profiles with `/achievements` or Apps → View achievements. "
                "Grant achievements from Apps → Grant achievements."
            ),
        )
        embed.add_field(
            name="Commands",
            value=self._format_direct_commands(ctx),
            inline=False,
        )
        await ctx.send(embed=embed)

    @achievement.command(name="create")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def achievement_create(
        self, ctx: commands.Context, *, display_name: str
    ) -> None:
        """Create a boolean achievement without a Discord role."""
        if not await self._achievement_store.is_bootstrapped(ctx.guild.id):
            raise commands.UserFeedbackCheckFailure(
                "Achievement data is still initializing. Run `!rolesync discord` first"
            )
        try:
            definition = await self._achievement_store.create_boolean_definition(
                ctx.guild.id,
                display_name,
            )
        except ValueError as error:
            raise commands.UserFeedbackCheckFailure(str(error)) from error
        await ctx.send(f"Created achievement: {definition.display_name}")

    @achievement.group(name="role", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def achievement_role(self, ctx: commands.Context) -> None:
        """Manage optional Discord role bindings for achievements."""
        await self.achievement_role_list.callback(self, ctx)

    @achievement_role.command(name="bind")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def achievement_role_bind(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        """Choose an achievement and bind it to an existing Discord role."""
        if not await self._achievement_store.is_bootstrapped(ctx.guild.id):
            raise commands.UserFeedbackCheckFailure(
                "Achievement data is still initializing. Run `!rolesync discord` first"
            )
        if role.id in GATE_TIER_ROLE_IDS:
            raise commands.UserFeedbackCheckFailure(
                "Gate progression roles cannot be bound as boolean achievements"
            )
        definitions = await self._achievement_store.list_definitions(ctx.guild.id)
        if any(definition.role_id == role.id for definition in definitions):
            raise commands.UserFeedbackCheckFailure(
                "This role is already bound to an achievement"
            )
        unbound = tuple(
            definition for definition in definitions if definition.role_id is None
        )
        if not unbound:
            raise commands.UserFeedbackCheckFailure(
                "There are no unbound achievements"
            )
        if len(unbound) > 25:
            raise commands.UserFeedbackCheckFailure(
                "There are too many unbound achievements for one selection menu"
            )
        users_by_role = await self._role_analytics_users_with_roles(
            ctx.guild.id,
            (role.id,),
        )
        if users_by_role is None:
            raise commands.UserFeedbackCheckFailure(
                "Role analytics is not ready. Run `!rolesync` first"
            )
        from .achievement_views import AchievementRoleBindView

        view = AchievementRoleBindView(
            self,
            ctx.author.id,
            role,
            users_by_role[0],
            unbound,
        )
        view.message = await ctx.send(
            embed=view.render_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _grant_achievements_to_member(
        self,
        source_message: discord.Message,
        member: discord.Member,
        definitions: tuple,
        projected_roles: tuple,
    ) -> tuple[int, int, bool]:
        created_keys = []
        already_count = 0
        for definition in definitions:
            result = await self._achievement_store.grant_boolean(
                source_message.guild.id,
                member.id,
                definition.key,
                source_channel_id=source_message.channel.id,
                source_message_id=source_message.id,
            )
            if result.created:
                created_keys.append(definition.key)
            else:
                already_count += 1
        if not projected_roles:
            return len(created_keys), already_count, False
        try:
            await self._edit_achievement_roles(
                source_message.guild,
                member,
                add_role_ids=tuple(role.id for role in projected_roles),
                reason=f"Achievement grant from message {source_message.id}",
            )
        except (discord.Forbidden, discord.HTTPException):
            await self._achievement_store.revoke_booleans(
                source_message.guild.id,
                (member.id,),
                tuple(created_keys),
            )
            return 0, already_count, True
        return len(created_keys), already_count, False

    async def _confirm_achievement_role_bind(self, interaction, view) -> None:
        await interaction.response.defer()
        definitions = await self._achievement_store.list_definitions(
            interaction.guild.id
        )
        selected = next(
            (
                definition
                for definition in definitions
                if definition.key == view.selected_key
                and definition.role_id is None
            ),
            None,
        )
        role_is_bound = any(
            definition.role_id == view.role.id for definition in definitions
        )
        if selected is None or role_is_bound:
            await interaction.edit_original_response(
                embed=view.render_embed(
                    notice="Achievement configuration changed. Start again"
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        users_by_role = await self._role_analytics_users_with_roles(
            interaction.guild.id,
            (view.role.id,),
        )
        if users_by_role is None:
            await interaction.edit_original_response(
                embed=view.render_embed(notice="Role analytics is not ready"),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if users_by_role[0] != view.holder_ids:
            view.holder_ids = users_by_role[0]
            await interaction.edit_original_response(
                embed=view.render_embed(
                    notice="Role holders changed. Review the plan again"
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        result = await self._achievement_store.bind_role(
            interaction.guild.id,
            selected.key,
            role_id=view.role.id,
            user_ids=view.holder_ids,
        )
        view.stop()
        await interaction.edit_original_response(
            content=(
                f"Bound {view.role.mention} to {result.definition.display_name}; "
                f"imported {result.imported_count} achievements"
            ),
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._reconcile_achievement_roles_for_guild(interaction.guild)

    @achievement_role.command(name="unbind")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def achievement_role_unbind(
        self, ctx: commands.Context, role: discord.Role
    ) -> None:
        """Stop tracking a Discord role without deleting achievement history."""
        if not await self._achievement_store.is_bootstrapped(ctx.guild.id):
            raise commands.UserFeedbackCheckFailure(
                "Achievement data is still initializing. Run `!rolesync discord` first"
            )
        try:
            definition = await self._achievement_store.unbind_role(
                ctx.guild.id,
                role.id,
            )
        except LookupError as error:
            raise commands.UserFeedbackCheckFailure(
                "This role is not bound to an achievement"
            ) from error
        await ctx.send(
            f"Stopped tracking {role.mention} for {definition.display_name}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @achievement_role.command(name="replace")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def achievement_role_replace(
        self,
        ctx: commands.Context,
        old_role: discord.Role,
        new_role: discord.Role,
    ) -> None:
        """Move an achievement binding to another Discord role."""
        if not await self._achievement_store.is_bootstrapped(ctx.guild.id):
            raise commands.UserFeedbackCheckFailure(
                "Achievement data is still initializing. Run `!rolesync discord` first"
            )
        if old_role.id == new_role.id:
            raise commands.UserFeedbackCheckFailure("Choose two different roles")
        if new_role.id in GATE_TIER_ROLE_IDS:
            raise commands.UserFeedbackCheckFailure(
                "Gate progression roles cannot be bound as boolean achievements"
            )
        definitions = await self._achievement_store.list_definitions(ctx.guild.id)
        definition = next(
            (
                item for item in definitions if item.role_id == old_role.id
            ),
            None,
        )
        if definition is None:
            raise commands.UserFeedbackCheckFailure(
                "The old role is not bound to an achievement"
            )
        if any(item.role_id == new_role.id for item in definitions):
            raise commands.UserFeedbackCheckFailure(
                "The new role is already bound to an achievement"
            )
        users_by_role = await self._role_analytics_users_with_roles(
            ctx.guild.id,
            (new_role.id,),
        )
        if users_by_role is None:
            raise commands.UserFeedbackCheckFailure(
                "Role analytics is not ready. Run `!rolesync` first"
            )
        await ctx.send(
            f"Replace {old_role.mention} with {new_role.mention} for "
            f"{definition.display_name}\n"
            f"Current new-role holders: {len(users_by_role[0])}\n"
            "Type `confirm` to continue",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        def confirmation_check(message: discord.Message) -> bool:
            return (
                message.guild is not None
                and message.guild.id == ctx.guild.id
                and message.channel.id == ctx.channel.id
                and message.author.id == ctx.author.id
                and message.content.strip().lower() == "confirm"
            )

        try:
            await self.bot.wait_for(
                "message",
                check=confirmation_check,
                timeout=300,
            )
        except TimeoutError:
            await ctx.send("Achievement role replacement expired")
            return
        fresh_users_by_role = await self._role_analytics_users_with_roles(
            ctx.guild.id,
            (new_role.id,),
        )
        if fresh_users_by_role is None or fresh_users_by_role != users_by_role:
            await ctx.send("Role holders changed. Start the command again")
            return
        result = await self._achievement_store.replace_role(
            ctx.guild.id,
            old_role_id=old_role.id,
            new_role_id=new_role.id,
            user_ids=users_by_role[0],
        )
        await ctx.send(
            f"Bound {new_role.mention} to {result.definition.display_name}; "
            f"imported {result.imported_count} achievements",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._reconcile_achievement_roles_for_guild(ctx.guild)

    @achievement_role.command(name="list")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def achievement_role_list(self, ctx: commands.Context) -> None:
        """List all active achievement role bindings."""
        if not await self._achievement_store.is_bootstrapped(ctx.guild.id):
            raise commands.UserFeedbackCheckFailure(
                "Achievement data is still initializing. Run `!rolesync discord` first"
            )
        definitions = await self._achievement_store.list_definitions(ctx.guild.id)
        lines = [
            f"<@&{definition.role_id}> {definition.display_name}"
            for definition in definitions
            if definition.role_id is not None
        ]
        await ctx.send(
            "\n".join(lines) if lines else "No achievement roles are configured",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @achievement.command(name="revoke")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def achievement_revoke(
        self,
        ctx: commands.Context,
        *members: discord.Member,
    ) -> None:
        """Review and revoke achievements shared by all selected members."""
        unique_members = tuple({member.id: member for member in members}.values())
        if not unique_members:
            raise commands.UserFeedbackCheckFailure("Mention at least one user")
        if len(unique_members) > MAX_ACHIEVEMENT_RECIPIENTS:
            raise commands.UserFeedbackCheckFailure(
                f"Select at most {MAX_ACHIEVEMENT_RECIPIENTS} users"
            )
        if not await self._achievement_store.is_bootstrapped(ctx.guild.id):
            raise commands.UserFeedbackCheckFailure(
                "Achievement data is still initializing. Run rolesync first"
            )
        shared_keys = set(
            await self._achievement_store.shared_boolean_keys(
                ctx.guild.id,
                tuple(member.id for member in unique_members),
            )
        )
        catalog = await self._achievement_store.list_definitions(ctx.guild.id)
        definitions = tuple(
            definition
            for definition in catalog
            if definition.revocable and definition.key in shared_keys
        )
        if not definitions:
            raise commands.UserFeedbackCheckFailure(
                "The selected users have no shared revocable achievements"
            )
        from .achievement_views import AchievementRevokeView

        view = AchievementRevokeView(
            self,
            ctx.author.id,
            unique_members,
            definitions,
        )
        view.message = await ctx.send(
            embed=view.render_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _confirm_achievement_revoke(self, interaction, view) -> None:
        await interaction.response.defer()
        shared_keys = set(
            await self._achievement_store.shared_boolean_keys(
                interaction.guild.id,
                tuple(member.id for member in view.members),
            )
        )
        if not view.selected_keys or not view.selected_keys <= shared_keys:
            await interaction.edit_original_response(
                embed=view.render_embed(
                    notice="Achievement ownership changed. Start the command again"
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        selected_definitions = tuple(
            definition
            for definition in view.definitions
            if definition.key in view.selected_keys
        )
        current_definitions = {
            definition.key: definition
            for definition in await self._achievement_store.list_definitions(
                interaction.guild.id
            )
        }
        if any(
            current_definitions.get(definition.key) != definition
            for definition in selected_definitions
        ):
            await interaction.edit_original_response(
                embed=view.render_embed(
                    notice="Achievement configuration changed. Start again"
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            projected_roles = self._validate_achievement_role_projection(
                interaction.guild,
                view.members,
                selected_definitions,
            )
        except commands.UserFeedbackCheckFailure as error:
            await interaction.edit_original_response(
                embed=view.render_embed(notice=str(error)),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        role_ids = tuple(role.id for role in projected_roles)
        achievement_keys = tuple(
            definition.key for definition in selected_definitions
        )
        revoked_count = 0
        failed_members = 0
        for member in view.members:
            revoked_count += await self._achievement_store.revoke_booleans(
                interaction.guild.id,
                (member.id,),
                achievement_keys,
            )
            try:
                if role_ids:
                    await self._edit_achievement_roles(
                        interaction.guild,
                        member,
                        remove_role_ids=role_ids,
                        reason=f"Achievement revocation by {interaction.user.id}",
                    )
            except (discord.Forbidden, discord.HTTPException):
                failed_members += 1
                continue
        view.stop()
        await interaction.edit_original_response(
            content=(
                f"Revoked {revoked_count} achievements"
                + (
                    f"; {failed_members} users could not be updated"
                    if failed_members
                    else ""
                )
            ),
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @staticmethod
    def _validate_achievement_role_projection(
        guild: discord.Guild,
        members: tuple,
        definitions: tuple,
    ) -> tuple:
        role_ids = tuple(
            definition.role_id
            for definition in definitions
            if definition.role_id is not None
        )
        if not role_ids:
            return ()
        bot_member = guild.me
        permissions = getattr(bot_member, "guild_permissions", None)
        if bot_member is None or not permissions or not permissions.manage_roles:
            raise commands.UserFeedbackCheckFailure("I need Manage Roles permission")
        roles = tuple(guild.get_role(role_id) for role_id in role_ids)
        if any(
            role is None
            or role.managed
            or role.position >= bot_member.top_role.position
            for role in roles
        ):
            raise commands.UserFeedbackCheckFailure(
                "Achievement roles are configured incorrectly"
            )
        if any(member.top_role.position >= bot_member.top_role.position for member in members):
            raise commands.UserFeedbackCheckFailure(
                "One or more selected users are above my role"
            )
        return roles

    @staticmethod
    async def _edit_achievement_roles(
        guild: discord.Guild,
        member: discord.Member,
        *,
        add_role_ids: tuple[int, ...] = (),
        remove_role_ids: tuple[int, ...] = (),
        reason: str,
    ) -> None:
        add_ids = set(add_role_ids)
        remove_ids = set(remove_role_ids)
        current_ids = {
            role.id
            for role in member.roles
            if role.id != guild.default_role.id
        }
        desired_ids = (current_ids | add_ids) - remove_ids
        desired_roles = [
            guild.get_role(role_id)
            for role_id in desired_ids
        ]
        if any(role is None for role in desired_roles):
            raise commands.UserFeedbackCheckFailure(
                "Achievement roles are configured incorrectly"
            )
        if desired_ids == current_ids:
            return
        await member.edit(roles=desired_roles, reason=reason)

    @commands.group(name="rolesync", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def rolesync(self, ctx: commands.Context) -> None:
        """Initialize or reconcile the role analytics database."""
        if self._role_analytics.is_syncing(ctx.guild.id):
            raise commands.UserFeedbackCheckFailure(
                "Role synchronization is already running"
            )
        await ctx.send("Role synchronization started")
        try:
            result = await self._role_analytics.sync_guild(ctx.guild, manual=True)
        except SyncAlreadyRunningError as error:
            raise commands.UserFeedbackCheckFailure(
                "Role synchronization is already running"
            ) from error
        except (MemberIntentRequiredError, FullMemberRequestCooldownError) as error:
            log.warning("Role synchronization unavailable for guild %s: %s", ctx.guild.id, error)
            raise commands.UserFeedbackCheckFailure(
                "Role synchronization is unavailable right now"
            ) from error
        except Exception as error:
            log.exception("Role synchronization failed for guild %s", ctx.guild.id)
            raise commands.UserFeedbackCheckFailure(
                "Role synchronization failed"
            ) from error

        if await self._achievement_store.is_bootstrapped(ctx.guild.id):
            await self._reconcile_achievement_roles_for_guild(ctx.guild)

        await ctx.send(
            f"Role synchronization complete: {result.member_count} members, "
            f"{result.membership_count} role memberships in {result.elapsed_seconds:.1f}s"
        )

    async def _upload_achievement_sync_backup(
        self,
        guild: discord.Guild,
        alert_channel: discord.TextChannel,
        snapshot: DiscordRoleSnapshot,
    ) -> None:
        database_bytes = await self._achievement_store.backup_database()
        holder_ids = {
            user_id for user_ids in snapshot.role_holders.values() for user_id in user_ids
        }
        members_by_id = {member.id: member for member in guild.members}
        if not holder_ids.issubset(members_by_id):
            raise commands.UserFeedbackCheckFailure(
                "Discord member cache changed. Run `!rolesync discord` again"
            )
        discord_roles_bytes = build_discord_role_backup(
            guild_id=guild.id,
            snapshot_at=snapshot.snapshot_at,
            cached_member_count=snapshot.cached_member_count,
            reported_member_count=snapshot.reported_member_count,
            role_holders=snapshot.role_holders,
            user_names={
                user_id: (
                    members_by_id[user_id].name,
                    members_by_id[user_id].display_name,
                )
                for user_id in holder_ids
            },
        )
        artifacts = (
            ("database.sqlite3", database_bytes),
            ("discord-roles.jsonl.gz", discord_roles_bytes),
        )
        if any(len(data) > guild.filesize_limit for _name, data in artifacts):
            raise commands.UserFeedbackCheckFailure(
                "Achievement synchronization backup is too large to upload"
            )

        backup_id = uuid4().hex
        database_hash = hashlib.sha256(database_bytes).hexdigest()
        discord_roles_hash = hashlib.sha256(discord_roles_bytes).hexdigest()
        files = [
            discord.File(
                io.BytesIO(data),
                filename=f"achievement-sync-{backup_id}-{name}",
            )
            for name, data in artifacts
        ]
        try:
            await alert_channel.send(
                "Achievement synchronization backup\n"
                f"Database SHA-256: {database_hash}\n"
                f"Discord roles SHA-256: {discord_roles_hash}",
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            log.exception(
                "Failed to upload achievement synchronization backup for guild %s",
                guild.id,
            )
            raise commands.UserFeedbackCheckFailure(
                "Could not upload the achievement synchronization backup"
            ) from error

    async def _achievement_discord_sync_summary(
        self,
        guild_id: int,
        snapshot: DiscordRoleSnapshot,
        *,
        bootstrapped: bool,
    ) -> str:
        if not bootstrapped:
            return snapshot.render_initialization_summary()
        stored_gate_tiers = await self._achievement_store.list_gate_projections(
            guild_id
        )
        stored_boolean_users = {
            key: await self._achievement_store.active_users_for_boolean(
                guild_id, key
            )
            for key in snapshot.boolean_users
        }
        plan = build_discord_priority_plan(
            snapshot,
            stored_gate_tiers=stored_gate_tiers,
            stored_boolean_users=stored_boolean_users,
        )
        return plan.render_summary()

    async def _wait_for_achievement_sync_confirmation(
        self,
        *,
        guild_id: int,
        channel: discord.TextChannel,
        moderator_id: int,
    ) -> bool:
        def confirmation_check(message: discord.Message) -> bool:
            return (
                message.guild is not None
                and message.guild.id == guild_id
                and message.channel.id == channel.id
                and message.author.id == moderator_id
                and message.content.strip().lower() == "confirm"
            )

        try:
            await self.bot.wait_for(
                "message",
                check=confirmation_check,
                timeout=300,
            )
        except TimeoutError:
            await self._send_voice_log(
                channel,
                "Achievement synchronization confirmation expired",
            )
            return False
        return True

    async def _apply_achievement_discord_snapshot(
        self,
        guild_id: int,
        snapshot: DiscordRoleSnapshot,
        *,
        bootstrapped: bool,
    ) -> str | None:
        if bootstrapped:
            result = await self._achievement_store.apply_discord_snapshot(
                guild_id,
                gate_tiers=snapshot.gate_tiers,
                boolean_users=snapshot.boolean_users,
            )
            return (
                "Achievement synchronization complete\n"
                f"Users changed: {result.changed_users}\n"
                f"Gate users changed: {result.gate_users_changed}\n"
                f"Achievements created: {result.boolean_grants}\n"
                f"Achievements revoked: {result.boolean_revocations}"
            )

        created = await self._achievement_store.bootstrap_guild(
            guild_id,
            gate_tiers=snapshot.gate_tiers,
            boolean_definitions=(SOLO_GATER_DEFINITION,),
            boolean_users=snapshot.boolean_users,
        )
        if not created:
            return None
        return (
            "Achievement initialization complete\n"
            f"Gate holders: {len(snapshot.gate_tiers)}\n"
            "Solo Gater holders: "
            f"{len(snapshot.boolean_users.get(SOLO_GATER_KEY, ()))}"
        )

    @rolesync.command(name="discord")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    async def rolesync_discord(self, ctx: commands.Context) -> None:
        """Replace achievement state with the current Discord role snapshot."""
        guild_id = ctx.guild.id
        if guild_id in self._achievement_syncing_guilds:
            raise commands.UserFeedbackCheckFailure(
                "Achievement synchronization is already awaiting confirmation"
            )
        self._achievement_syncing_guilds.add(guild_id)
        try:
            snapshot = await self._achievement_discord_snapshot(ctx.guild)
            if snapshot is None:
                await ctx.send(
                    "Role analytics is not ready. Run `!rolesync` first, then run "
                    "`!rolesync discord` again."
                )
                return
            alert_channel = self._get_log_channel(
                ctx.guild,
                await self.config.guild(ctx.guild).alert_channel(),
            )
            if alert_channel is None:
                raise commands.UserFeedbackCheckFailure(
                    "Configure the NHMisc alert channel first"
                )
            if self._channel_allows_everyone(alert_channel, ctx.guild):
                raise commands.UserFeedbackCheckFailure(
                    "Configure a private NHMisc alert channel first"
                )

            bootstrapped = await self._achievement_store.is_bootstrapped(guild_id)
            summary = await self._achievement_discord_sync_summary(
                guild_id,
                snapshot,
                bootstrapped=bootstrapped,
            )
            await self._upload_achievement_sync_backup(
                ctx.guild,
                alert_channel,
                snapshot,
            )

            plan_message = await self._send_voice_log(alert_channel, summary)
            if plan_message is None:
                raise commands.UserFeedbackCheckFailure(
                    "Could not publish the synchronization plan"
                )
            if ctx.channel.id != alert_channel.id:
                await ctx.send(
                    f"Synchronization plan sent to {alert_channel.mention}"
                )

            confirmed = await self._wait_for_achievement_sync_confirmation(
                guild_id=guild_id,
                channel=alert_channel,
                moderator_id=ctx.author.id,
            )
            if not confirmed:
                return

            fresh_snapshot = await self._achievement_discord_snapshot(ctx.guild)
            if fresh_snapshot != snapshot:
                await self._send_voice_log(
                    alert_channel,
                    "Role analytics changed. Run `!rolesync discord` again.",
                )
                return

            fresh_bootstrapped = await self._achievement_store.is_bootstrapped(guild_id)
            fresh_summary = await self._achievement_discord_sync_summary(
                guild_id,
                fresh_snapshot,
                bootstrapped=fresh_bootstrapped,
            )
            if fresh_bootstrapped != bootstrapped or fresh_summary != summary:
                await self._send_voice_log(
                    alert_channel,
                    "Achievement data changed. Run `!rolesync discord` again.",
                )
                return

            completion = await self._apply_achievement_discord_snapshot(
                guild_id,
                snapshot,
                bootstrapped=bootstrapped,
            )
            if completion is None:
                await self._send_voice_log(
                    alert_channel,
                    "Achievement initialization was already completed",
                )
                return
            await self._send_voice_log(alert_channel, completion)
        finally:
            self._achievement_syncing_guilds.discard(guild_id)

    @commands.command(name="rolestats")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def rolestats(self, ctx: commands.Context, *, expression: str) -> None:
        """Count members matching a boolean role expression."""
        parsed, predicate_sql, parameters = self._prepare_role_expression(
            ctx.guild, expression
        )
        try:
            count = await self._role_analytics_store.count_matching(
                ctx.guild.id, predicate_sql, parameters
            )
        except AnalyticsUnavailableError as error:
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            ) from error

        await ctx.send(
            f"{count} users match: {render_role_expression(parsed)}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="roleusers")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.cooldown(1, 10, commands.BucketType.guild)
    async def roleusers(self, ctx: commands.Context, *, expression: str) -> None:
        """Export members matching a boolean role expression."""
        self._require_private_role_export_channel(ctx)
        parsed, predicate_sql, parameters = self._prepare_role_expression(
            ctx.guild, expression
        )
        if not bool(ctx.guild.chunked):
            await self._repair_role_analytics_cache(ctx.guild)
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            )

        try:
            user_ids = await self._role_analytics_store.matching_user_ids(
                ctx.guild.id, predicate_sql, parameters
            )
        except AnalyticsUnavailableError as error:
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            ) from error
        if not user_ids:
            await ctx.send("No users match this expression")
            return

        members = [ctx.guild.get_member(user_id) for user_id in user_ids]
        if any(member is None for member in members):
            await self._repair_role_analytics_cache(ctx.guild)
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            )

        export_members = tuple(
            ExportMember(member.id, member.name, member.display_name)
            for member in members
        )
        try:
            payload = build_role_export(export_members, ctx.guild.filesize_limit)
        except ExportTooLarge:
            await ctx.send("Export is too large to upload")
            return

        await ctx.send(
            f"{len(user_ids)} users match: {render_role_expression(parsed)}",
            file=discord.File(io.BytesIO(payload.data), filename=payload.filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _configuration_embed(
        self,
        *,
        ctx: commands.Context,
        title: str,
        current: tuple[str, ...],
        action_heading: str = "Commands",
    ) -> discord.Embed:
        embed = discord.Embed(title=title)
        current_value = (
            "Run this command in a channel hidden from @everyone "
            "to view the current configuration."
            if self._channel_is_public(ctx)
            else "\n".join(current)
        )
        embed.add_field(
            name="Current configuration",
            value=current_value,
            inline=False,
        )
        embed.add_field(
            name=action_heading,
            value=self._format_direct_commands(ctx),
            inline=False,
        )
        return embed

    @staticmethod
    def _channel_allows_everyone(
        channel: discord.abc.GuildChannel,
        guild: discord.Guild,
    ) -> bool:
        permissions = channel.permissions_for(guild.default_role)
        return bool(permissions.view_channel)

    @staticmethod
    def _channel_is_public(ctx: commands.Context) -> bool:
        return NHMisc._channel_allows_everyone(ctx.channel, ctx.guild)

    @staticmethod
    def _format_direct_commands(ctx: commands.Context) -> str:
        lines = []
        for command in ctx.command.commands:
            if command.hidden:
                continue
            signature = command.signature.strip()
            usage = f"{ctx.clean_prefix}{command.qualified_name}"
            if signature:
                usage = f"{usage} {signature}"
            lines.append(f"`{usage}`")
        return "\n".join(lines) or "No subcommands available."

    @staticmethod
    def _configured_channel_label(
        guild: discord.Guild, channel_id: int | None
    ) -> str:
        if channel_id is None:
            return "Not configured"
        channel = guild.get_channel(channel_id)
        if channel is None:
            return "Configured channel is missing"
        return channel.mention

    @commands.group(name="nhmisc", invoke_without_command=True)
    @commands.guild_only()
    async def nhmisc(self, ctx: commands.Context) -> None:
        """Configure NHMisc."""
        embed = discord.Embed(
            title="NHMisc",
            description="Configuration, activity, and moderation tools.",
        )
        embed.add_field(
            name="Commands",
            value=self._format_direct_commands(ctx),
            inline=False,
        )
        await ctx.send(embed=embed)

    def _loaded_honeypot(self):
        honeypot = self.bot.get_cog("Honeypot")
        if honeypot is None:
            return None
        if not callable(getattr(honeypot, "cleanup_channel", None)):
            return None
        if not callable(getattr(honeypot, "cleanup_user", None)):
            return None
        return honeypot

    @nhmisc.group(name="cleanup", invoke_without_command=True)
    @commands.mod_or_permissions(manage_messages=True)
    async def nhmisc_cleanup(self, ctx: commands.Context, count: int) -> None:
        """Delete recently observed messages from the current channel."""
        if not 1 <= count <= 100:
            raise commands.UserFeedbackCheckFailure(
                "Count must be between 1 and 100."
            )
        honeypot = self._loaded_honeypot()
        if honeypot is None:
            await ctx.send("Honeypot is not loaded, so cleanup is unavailable.")
            return
        try:
            result = await honeypot.cleanup_channel(ctx, count)
        except Exception:
            log.exception("Honeypot channel cleanup failed")
            await ctx.send("Cleanup failed. Check the bot logs and try again.")
            return
        await ctx.send(
            result.public_message,
            delete_after=CLEANUP_RESPONSE_TTL_SECONDS,
        )

    @nhmisc_cleanup.command(name="user")
    @commands.mod_or_permissions(manage_messages=True)
    async def nhmisc_cleanup_user(
        self,
        ctx: commands.Context,
        target: str,
        count: int,
    ) -> None:
        """Delete recently observed messages from a user across this server."""
        if not 1 <= count <= 100:
            raise commands.UserFeedbackCheckFailure(
                "Count must be between 1 and 100."
            )
        user_id = self._parse_user_id(target)
        honeypot = self._loaded_honeypot()
        if honeypot is None:
            await ctx.send("Honeypot is not loaded, so cleanup is unavailable.")
            return
        try:
            result = await honeypot.cleanup_user(ctx, user_id, count)
        except Exception:
            log.exception("Honeypot user cleanup failed")
            await ctx.send("Cleanup failed. Check the bot logs and try again.")
            return
        await ctx.send(
            result.public_message,
            delete_after=CLEANUP_RESPONSE_TTL_SECONDS,
        )

    @nhmisc.group(name="roleanalytics", invoke_without_command=True)
    @commands.admin_or_permissions(administrator=True)
    async def nhmisc_roleanalytics(self, ctx: commands.Context) -> None:
        """Configure role analytics."""
        state = await self._role_analytics_store.get_state(ctx.guild.id)
        member_count = (
            f"{state.source_member_count:,}"
            if state.source_member_count is not None
            else "Not available"
        )
        embed = self._configuration_embed(
            ctx=ctx,
            title="Role analytics",
            current=(
                f"Enabled: {'Yes' if state.enabled else 'No'}",
                f"Status: {state.status.value.replace('_', ' ').title()}",
                f"Members in snapshot: {member_count}",
            ),
        )
        await ctx.send(embed=embed)

    @nhmisc_roleanalytics.command(name="disable")
    async def nhmisc_roleanalytics_disable(self, ctx: commands.Context) -> None:
        """Disable role analytics and delete this guild's analytics database."""
        await self._role_analytics.disable_guild(ctx.guild.id)
        await ctx.send("Role analytics disabled")

    @nhmisc.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the text channel used for voice event logs."""
        missing_permissions = self._missing_log_permissions(ctx.guild, channel)
        if missing_permissions is not None:
            raise commands.UserFeedbackCheckFailure(missing_permissions)

        await self.config.guild(ctx.guild).voice_log_channel.set(channel.id)
        await ctx.send(f"Voice log channel set to {channel.mention}.")

    @nhmisc.group(name="alert", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_alert(self, ctx: commands.Context) -> None:
        """Configure alert logging."""
        channel_id = await self.config.guild(ctx.guild).alert_channel()
        embed = self._configuration_embed(
            ctx=ctx,
            title="Alert logging",
            current=(
                f"Channel: {self._configured_channel_label(ctx.guild, channel_id)}",
            ),
            action_heading="Change it",
        )
        await ctx.send(embed=embed)

    @nhmisc_alert.command(name="channel")
    async def nhmisc_alert_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the text channel used for alert logs."""
        missing_permissions = self._missing_log_permissions(ctx.guild, channel)
        if missing_permissions is not None:
            raise commands.UserFeedbackCheckFailure(missing_permissions)

        await self.config.guild(ctx.guild).alert_channel.set(channel.id)
        await ctx.send(f"Alert channel set to {channel.mention}.")

    @nhmisc.group(name="vcjumping", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_vcjumping(self, ctx: commands.Context) -> None:
        """Configure voice channel jumping detection."""
        config = await self.config.guild(ctx.guild).all()
        embed = self._configuration_embed(
            ctx=ctx,
            title="VC jumping detection",
            current=(
                f"Channel entries: {config['vcjumping_visit_count']}",
                f"Time window: {config['vcjumping_window_seconds']} seconds",
            ),
            action_heading="Change it",
        )
        await ctx.send(embed=embed)

    @nhmisc_vcjumping.command(name="visits")
    async def nhmisc_vcjumping_visits(self, ctx: commands.Context, count: int) -> None:
        """Set how many voice channel entries trigger VC jumping alerts."""
        if count < 2:
            raise commands.UserFeedbackCheckFailure("VC jumping visit count must be at least 2.")

        await self.config.guild(ctx.guild).vcjumping_visit_count.set(count)
        await ctx.send(f"VC jumping alerts will trigger after {count} channel entries.")

    @nhmisc_vcjumping.command(name="seconds")
    async def nhmisc_vcjumping_seconds(self, ctx: commands.Context, seconds: int) -> None:
        """Set the VC jumping detection time window in seconds."""
        if seconds < 1:
            raise commands.UserFeedbackCheckFailure("VC jumping window must be at least 1 second.")

        await self.config.guild(ctx.guild).vcjumping_window_seconds.set(seconds)
        await ctx.send(f"VC jumping window set to {seconds} seconds.")

    @nhmisc.command(name="status")
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_status(self, ctx: commands.Context) -> None:
        """Show the current voice log configuration."""
        config = await self.config.guild(ctx.guild).all()
        channel = self._get_log_channel(ctx.guild, config["voice_log_channel"])
        alert_channel = self._get_log_channel(ctx.guild, config["alert_channel"])
        channel_label = channel.mention if channel is not None else "not set"
        alert_channel_label = alert_channel.mention if alert_channel is not None else "not set"
        await ctx.send(
            "Voice log channel: {channel}\n"
            "Alert channel: {alert_channel}\n"
            "VC jumping: {count} channel entries in {seconds} seconds.".format(
                channel=channel_label,
                alert_channel=alert_channel_label,
                count=config["vcjumping_visit_count"],
                seconds=config["vcjumping_window_seconds"],
            )
        )

    @nhmisc.group(name="forumautopin", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def nhmisc_forumautopin(self, ctx: commands.Context) -> None:
        """Configure automatic pinning for new forum post starter messages."""
        configured = await self._forum_autopin.get_forum_ids(ctx.guild)
        forum_lines = [f"Configured forums: {len(configured)}"]
        for channel_id in configured[:10]:
            channel = ctx.guild.get_channel(channel_id)
            forum_lines.append(
                channel.mention if channel is not None else "Configured forum is missing"
            )
        if len(configured) > 10:
            forum_lines.append(f"...and {len(configured) - 10} more")

        embed = self._configuration_embed(
            ctx=ctx,
            title="Forum autopin",
            current=tuple(forum_lines),
        )
        await ctx.send(embed=embed)

    @nhmisc_forumautopin.command(name="add")
    async def nhmisc_forumautopin_add(
        self,
        ctx: commands.Context,
        channel: discord.ForumChannel,
    ) -> None:
        """Enable starter-message autopinning in a forum."""
        missing_permission = self._forum_autopin.missing_permissions(ctx.guild, channel)
        if missing_permission is not None:
            raise commands.UserFeedbackCheckFailure(missing_permission)

        enabled = await self._forum_autopin.enable(ctx.guild, channel.id)
        state = "is now enabled" if enabled else "is already enabled"
        await ctx.send(
            f"Forum autopin {state} for {channel.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmisc_forumautopin.command(name="remove")
    async def nhmisc_forumautopin_remove(
        self,
        ctx: commands.Context,
        channel: discord.ForumChannel,
    ) -> None:
        """Disable starter-message autopinning in a forum."""
        disabled = await self._forum_autopin.disable(ctx.guild, channel.id)
        state = "is disabled" if disabled else "is not enabled"
        await ctx.send(
            f"Forum autopin {state} for {channel.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmisc_forumautopin.command(name="list")
    async def nhmisc_forumautopin_list(self, ctx: commands.Context) -> None:
        """List forums configured for starter-message autopinning."""
        configured = await self._forum_autopin.get_forum_ids(ctx.guild)
        if not configured:
            await ctx.send(
                "No forums are configured for automatic starter-message pinning."
            )
            return

        lines = ["Forums with starter-message autopinning:"]
        for channel_id in configured:
            channel = ctx.guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
            if isinstance(channel, discord.ForumChannel):
                lines.append(f"- {channel.mention} (`{channel_id}`)")
            else:
                lines.append(f"- Missing forum (`{channel_id}`)")
        await self._send_paginated_text(ctx, "\n".join(lines))

    @commands.command(name="gatecount")
    @commands.guild_only()
    async def gatecount(self, ctx: commands.Context) -> None:
        """Show member counts for the current Gate roles."""
        _require_guild_role(
            ctx.guild,
            SINGLEPLAYER_GATE_COMPLETED_ROLE_ID,
            report_name="Gatecount",
            role_label="Singleplayer completed",
        )
        for tier, role_id in enumerate(GATE_TIER_ROLE_IDS, start=1):
            _require_guild_role(
                ctx.guild,
                role_id,
                report_name="Gatecount",
                role_label=f"Tier {tier}",
            )
        singleplayer_count = await self._count_role_expression(
            ctx.guild, str(SINGLEPLAYER_GATE_COMPLETED_ROLE_ID)
        )
        tier_counts = await self._count_highest_role_buckets(
            ctx.guild, GATE_TIER_ROLE_IDS
        )
        total_gates = sum(
            tier * count for tier, count in enumerate(tier_counts, start=1)
        )

        def format_role_count(role_id: int, count: int) -> str:
            player_label = "player" if count == 1 else "players"
            return f"<@&{role_id}> — **{count} {player_label}**"

        lines = [
            format_role_count(
                SINGLEPLAYER_GATE_COMPLETED_ROLE_ID, singleplayer_count
            )
        ]
        lines.extend(
            format_role_count(role_id, count)
            for role_id, count in zip(GATE_TIER_ROLE_IDS, tier_counts, strict=True)
        )
        lines.extend(("", f"**Total Gates: {total_gates}**"))
        embed = discord.Embed(
            title="Current Gatecount:",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _gate_increment_context_action(
        self,
        interaction: discord.Interaction,
        source_message: discord.Message,
    ) -> None:
        action = "increment gate roles context action"
        self._log_achievement_interaction_start(action, interaction)
        permissions = interaction.permissions
        if (
            interaction.guild is None
            or permissions is None
            or not permissions.manage_messages
        ):
            await interaction.response.send_message(
                "You need Manage Messages permission",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            is_bootstrapped = await self._await_achievement_interaction_data(
                self._achievement_store.is_bootstrapped(interaction.guild.id)
            )
            if not is_bootstrapped:
                await interaction.edit_original_response(
                    content=(
                        "Achievement data is still initializing. Run rolesync first"
                    )
                )
                return
            await self._gate_increment_context_action_after_defer(
                interaction,
                source_message,
            )
        except Exception as error:
            await self._handle_achievement_interaction_failure(
                interaction,
                action,
                error,
                public_defer=False,
            )

    async def _gate_increment_context_action_after_defer(
        self,
        interaction: discord.Interaction,
        source_message: discord.Message,
    ) -> None:
        existing = await self._await_achievement_interaction_data(
            self._gate_increment_store.get_operation(
                self._gate_increment_key(source_message)
            )
        )
        if existing is not None:
            existing = await self._retry_gate_increment_publication(
                source_message, existing
            )
            if existing.operation.state is OperationState.COMPLETED:
                await interaction.edit_original_response(
                    content=self._format_gate_increment_operation(existing),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                view = self._create_existing_gate_increment_view(
                    source_message,
                    interaction.user.id,
                    existing,
                    ephemeral=True,
                )
                await interaction.edit_original_response(
                    embed=view.render_embed(),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                view.message = await interaction.original_response()
            return

        try:
            view = self._create_gate_increment_review(
                source_message,
                interaction.user.id,
                ephemeral=True,
            )
        except commands.UserFeedbackCheckFailure as error:
            await interaction.edit_original_response(content=str(error))
            return
        await interaction.edit_original_response(
            embed=view.render_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = await interaction.original_response()

    def _create_gate_increment_review(
        self,
        source_message: discord.Message,
        opener_id: int,
        *,
        ephemeral: bool,
    ):
        _validate_gate_increment_configuration(source_message.guild)
        candidates = _build_gate_increment_candidates(source_message)
        if not candidates:
            raise commands.UserFeedbackCheckFailure(
                "This message has no eligible users to increment"
            )
        if len(candidates) > MAX_GATE_INCREMENT_CANDIDATES:
            raise commands.UserFeedbackCheckFailure(
                f"This message contains {len(candidates)} users. Gate increment "
                f"supports at most {MAX_GATE_INCREMENT_CANDIDATES} at a time"
            )
        from .gate_increment_views import GateIncrementReviewView

        return GateIncrementReviewView(
            self,
            source_message,
            opener_id,
            candidates,
            ephemeral=ephemeral,
        )

    def _create_existing_gate_increment_view(
        self,
        source_message: discord.Message,
        opener_id: int,
        snapshot: GateIncrementSnapshot,
        *,
        ephemeral: bool,
    ):
        from .gate_increment_views import GateIncrementExistingView

        return GateIncrementExistingView(
            self,
            source_message,
            opener_id,
            snapshot,
            ephemeral=ephemeral,
        )

    @staticmethod
    def _gate_increment_key(source_message: discord.Message) -> SourceMessageKey:
        return SourceMessageKey(
            source_message.guild.id,
            source_message.channel.id,
            source_message.id,
        )

    @staticmethod
    def _format_gate_increment_operation(snapshot: GateIncrementSnapshot) -> str:
        operation = snapshot.operation
        if operation.state is OperationState.APPLYING:
            return (
                "⏳ **GATE INCREMENT IN PROGRESS**\n"
                "This message is already being processed"
            )
        if operation.state is OperationState.COMPLETED:
            return (
                "⚠️ **GATE INCREMENT BLOCKED**\n"
                "This message was already processed\n"
                f"{operation.completed_count} users were updated successfully"
            )
        return (
            "⚠️ **GATE INCREMENT PARTIALLY COMPLETED**\n"
            f"{operation.completed_count} users were updated and "
            f"{operation.failed_count + operation.conflict_count} changes require "
            "attention\nThis message cannot start another increment"
        )

    async def _retry_gate_increment_publication(
        self,
        source_message: discord.Message,
        snapshot: GateIncrementSnapshot,
    ) -> GateIncrementSnapshot:
        operation = snapshot.operation
        if operation.completed_count and operation.result_message_id is None:
            await self._publish_gate_increment_result(source_message, snapshot)
            refreshed = await self._gate_increment_store.get_operation(operation.key)
            if refreshed is not None:
                return refreshed
        return snapshot

    async def _resume_gate_increment_review(self, interaction, view) -> None:
        await interaction.response.defer()
        try:
            source_message = await self._fetch_gate_increment_source(
                view.snapshot.operation.key
            )
            snapshot = await self._execute_gate_increment_operation(
                source_message,
                interaction.user.id,
            )
            if snapshot.operation.state is OperationState.APPLYING:
                await self._finish_gate_increment_review(
                    interaction,
                    view,
                    self._format_gate_increment_operation(snapshot),
                )
                return
            published = await self._publish_gate_increment_result(
                source_message,
                snapshot,
            )
        except commands.UserFeedbackCheckFailure as error:
            await interaction.edit_original_response(
                content=str(error),
                embed=view.render_embed(),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self._finish_gate_increment_review(
            interaction,
            view,
            self._format_gate_increment_completion(snapshot, published),
        )

    async def _refresh_gate_increment_review(self, interaction, view) -> None:
        await interaction.response.defer()
        try:
            source_message = await self._fetch_gate_increment_source(
                self._gate_increment_key(view.source_message)
            )
            _validate_gate_increment_configuration(source_message.guild)
            candidates = await self._fetch_gate_increment_candidates(source_message)
            self._validate_gate_increment_candidate_count(candidates)
        except commands.UserFeedbackCheckFailure as error:
            await interaction.edit_original_response(
                content=None,
                embed=view.render_embed(notice=str(error)),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view.source_message = source_message
        view.replace_candidates(candidates)
        await interaction.edit_original_response(
            content=None,
            embed=view.render_embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _confirm_gate_increment_review(self, interaction, view) -> None:
        await interaction.response.defer()
        if not await self._achievement_store.is_bootstrapped(
            view.source_message.guild.id
        ):
            await interaction.edit_original_response(
                content=None,
                embed=view.render_embed(
                    notice="Achievement data is still initializing. Run rolesync first"
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not view.selected_user_ids:
            await interaction.edit_original_response(
                embed=view.render_embed(notice="Select at least one user"),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            source_message = await self._fetch_gate_increment_source(
                self._gate_increment_key(view.source_message)
            )
            _validate_gate_increment_configuration(source_message.guild)
            live_candidates = await self._fetch_gate_increment_candidates(
                source_message
            )
            self._validate_gate_increment_candidate_count(live_candidates)
        except commands.UserFeedbackCheckFailure as error:
            await interaction.edit_original_response(
                content=None,
                embed=view.render_embed(notice=str(error)),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if self._gate_increment_review_is_stale(view, live_candidates):
            view.source_message = source_message
            view.replace_candidates(live_candidates)
            await interaction.edit_original_response(
                content=None,
                embed=view.render_embed(
                    notice=(
                        "The source message or member roles changed. Review the "
                        "updated increment plan before confirming"
                    )
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        selected_candidates = tuple(
            candidate
            for candidate in live_candidates
            if candidate.user_id in view.selected_user_ids
            and candidate.target_role_id is not None
        )
        plans = tuple(
            GateIncrementMemberPlan(
                candidate.user_id,
                candidate.current_gate_role_ids,
                candidate.target_role_id,
                target_ordinal=(candidate.current_tier or 0) + 1,
                grant_solo=(
                    view.solo_gater_enabled and len(selected_candidates) == 1
                ),
            )
            for candidate in selected_candidates
        )
        key = self._gate_increment_key(source_message)
        try:
            claim = await self._gate_increment_store.claim(
                key,
                interaction.user.id,
                plans,
            )
        except GateProgressConflict:
            await interaction.edit_original_response(
                content=None,
                embed=view.render_embed(
                    notice=(
                        "A selected user's Gate progress changed. Refresh and "
                        "review the plan again"
                    )
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not claim.created:
            existing = await self._gate_increment_store.get_operation(key)
            await self._finish_gate_increment_review(
                interaction,
                view,
                self._format_gate_increment_operation(existing),
            )
            return

        snapshot = await self._execute_gate_increment_operation(
            source_message,
            interaction.user.id,
        )
        published = await self._publish_gate_increment_result(
            source_message,
            snapshot,
        )
        status = self._format_gate_increment_completion(snapshot, published)
        await self._finish_gate_increment_review(interaction, view, status)

    async def _fetch_gate_increment_candidates(
        self, source_message: discord.Message
    ) -> tuple[GateIncrementCandidate, ...]:
        candidates = []
        for user_id in _gate_increment_candidate_ids(source_message):
            try:
                member = await source_message.guild.fetch_member(user_id)
            except discord.NotFound:
                continue
            except discord.HTTPException as error:
                raise commands.UserFeedbackCheckFailure(
                    "Could not refresh the users in this message"
                ) from error
            if member.bot:
                continue
            candidates.append(_build_gate_increment_candidate(member))
            if len(candidates) > MAX_GATE_INCREMENT_CANDIDATES:
                break
        return tuple(candidates)

    @staticmethod
    def _validate_gate_increment_candidate_count(
        candidates: tuple[GateIncrementCandidate, ...],
    ) -> None:
        if not candidates:
            raise commands.UserFeedbackCheckFailure(
                "This message has no eligible users to increment"
            )
        if len(candidates) > MAX_GATE_INCREMENT_CANDIDATES:
            raise commands.UserFeedbackCheckFailure(
                f"This message contains more than {MAX_GATE_INCREMENT_CANDIDATES} "
                "users. Gate increment supports at most "
                f"{MAX_GATE_INCREMENT_CANDIDATES} at a time"
            )

    @staticmethod
    def _gate_increment_review_is_stale(view, live_candidates) -> bool:
        if tuple(candidate.user_id for candidate in live_candidates) != view.candidate_ids:
            return True
        preview_by_user_id = {
            candidate.user_id: candidate for candidate in view.candidates
        }
        return any(
            (
                candidate.current_gate_role_ids
                != preview_by_user_id[candidate.user_id].current_gate_role_ids
                or candidate.target_role_id
                != preview_by_user_id[candidate.user_id].target_role_id
            )
            for candidate in live_candidates
            if candidate.user_id in view.selected_user_ids
        )

    async def _execute_gate_increment_operation(
        self,
        source_message: discord.Message,
        moderator_id: int,
    ) -> GateIncrementSnapshot:
        key = self._gate_increment_key(source_message)
        token = uuid4().hex
        if not await self._gate_increment_store.acquire_execution_lease(key, token):
            snapshot = await self._gate_increment_store.get_operation(key)
            if snapshot is None:
                raise RuntimeError("Gate increment operation disappeared")
            return snapshot

        try:
            _validate_gate_increment_configuration(source_message.guild)
            snapshot = await self._gate_increment_store.get_operation(key)
            if snapshot is None:
                raise RuntimeError("Gate increment operation disappeared")
            for member_plan in snapshot.members:
                if member_plan.state is MemberState.COMPLETED:
                    continue
                await self._recover_gate_increment_member(
                    source_message.guild,
                    key,
                    member_plan,
                    moderator_id,
                )
            return await self._gate_increment_store.finalize_operation(key)
        except Exception:
            await self._gate_increment_store.release_execution_lease(key, token)
            raise

    async def _recover_gate_increment_member(
        self,
        guild: discord.Guild,
        key: SourceMessageKey,
        member_plan,
        moderator_id: int,
    ) -> None:
        await self._gate_increment_store.mark_member_in_progress(
            key, member_plan.position
        )
        if member_plan.user_id is None or member_plan.target_role_id is None:
            await self._gate_increment_store.mark_member_conflict(
                key, member_plan.position, "redacted"
            )
            return
        completed = False
        conflict_code = None
        failure_code = None
        try:
            member = await guild.fetch_member(member_plan.user_id)
            transition = plan_gate_transition(role.id for role in member.roles)
            recovery = classify_member_recovery(
                member_plan,
                transition.current_role_ids,
            )
            if recovery is RecoveryAction.COMPLETE:
                completed = True
            elif recovery is RecoveryAction.CONFLICT:
                conflict_code = "roles_changed"
            else:
                failure_code = await self._apply_fixed_gate_target(
                    guild,
                    member,
                    member_plan.target_role_id,
                    key,
                    moderator_id,
                    grant_solo=member_plan.grant_solo,
                )
                completed = failure_code is None
        except discord.NotFound:
            failure_code = "member_missing"
        except discord.Forbidden:
            failure_code = "forbidden"
        except discord.HTTPException:
            failure_code = "discord_error"

        if completed:
            await self._gate_increment_store.mark_member_completed(
                key, member_plan.position
            )
        elif conflict_code is not None:
            await self._gate_increment_store.mark_member_conflict(
                key, member_plan.position, conflict_code
            )
        else:
            await self._gate_increment_store.mark_member_failed(
                key, member_plan.position, failure_code or "unknown"
            )

    @staticmethod
    async def _apply_fixed_gate_target(
        guild: discord.Guild,
        member: discord.Member,
        target_role_id: int,
        key: SourceMessageKey,
        moderator_id: int,
        *,
        grant_solo: bool = False,
    ) -> str | None:
        if member.top_role.position >= guild.me.top_role.position:
            return "hierarchy"
        desired_role_ids = build_role_ids_for_target(
            (role.id for role in member.roles),
            target_role_id,
        )
        if grant_solo and SINGLEPLAYER_GATE_COMPLETED_ROLE_ID not in desired_role_ids:
            desired_role_ids = (*desired_role_ids, SINGLEPLAYER_GATE_COMPLETED_ROLE_ID)
        desired_roles = [
            guild.get_role(role_id)
            for role_id in desired_role_ids
            if role_id != guild.default_role.id
        ]
        if any(role is None for role in desired_roles):
            return "role_missing"
        await member.edit(
            roles=desired_roles,
            reason=(
                f"Gate increment for message {key.message_id}; "
                f"moderator {moderator_id}"
            ),
        )
        return None

    async def _publish_gate_increment_result(
        self,
        source_message: discord.Message,
        snapshot: GateIncrementSnapshot,
    ) -> bool:
        if snapshot.operation.result_message_id is not None:
            return True
        publication_token = uuid4().hex
        if not await self._gate_increment_store.acquire_publication_lease(
            snapshot.operation.key,
            publication_token,
        ):
            return True
        lines = [
            f"<@{member.user_id}> <@&{member.target_role_id}>"
            for member in snapshot.members
            if member.state is MemberState.COMPLETED
            and member.user_id is not None
            and member.target_role_id is not None
        ]
        if not lines:
            await self._gate_increment_store.release_publication_lease(
                snapshot.operation.key,
                publication_token,
            )
            return True
        try:
            result_message = await source_message.reply(
                "🎉 **Congratulations!**\n" + "\n".join(lines),
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
        except discord.HTTPException:
            await self._gate_increment_store.release_publication_lease(
                snapshot.operation.key,
                publication_token,
            )
            log.exception(
                "Failed to publish Gate increment result for message %s",
                source_message.id,
            )
            return False
        await self._gate_increment_store.record_result_message(
            snapshot.operation.key,
            publication_token,
            result_message.channel.id,
            result_message.id,
        )
        return True

    @staticmethod
    def _format_gate_increment_completion(
        snapshot: GateIncrementSnapshot,
        published: bool,
    ) -> str:
        operation = snapshot.operation
        if operation.state is OperationState.COMPLETED:
            status = f"Updated {operation.completed_count} users"
        else:
            status = (
                f"Updated {operation.completed_count} users; "
                f"{operation.failed_count + operation.conflict_count} changes "
                "require attention"
            )
        if operation.completed_count and not published:
            status += "\nRoles changed, but congratulations could not be sent"
        return status

    async def _finish_gate_increment_review(
        self,
        interaction,
        view,
        status: str,
    ) -> None:
        view.stop()
        if view.ephemeral:
            await interaction.edit_original_response(
                content=status,
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if view.message is not None:
            try:
                await view.message.delete()
            except discord.HTTPException:
                pass
        await interaction.followup.send(
            status,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _fetch_gate_increment_source(
        self, key: SourceMessageKey
    ) -> discord.Message:
        guild = self.bot.get_guild(key.guild_id)
        if guild is None:
            raise commands.UserFeedbackCheckFailure(
                "The source server is unavailable"
            )
        channel = guild.get_channel(key.channel_id) or self.bot.get_channel(
            key.channel_id
        )
        if channel is None:
            raise commands.UserFeedbackCheckFailure(
                "The source channel is unavailable"
            )
        try:
            return await channel.fetch_message(key.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            raise commands.UserFeedbackCheckFailure(
                "The source message is unavailable"
            ) from error

    async def _recover_interrupted_gate_increments(self) -> None:
        interrupted = await self._gate_increment_store.list_interrupted_operations()
        for snapshot in interrupted:
            try:
                source_message = await self._fetch_gate_increment_source(
                    snapshot.operation.key
                )
                recovered = await self._execute_gate_increment_operation(
                    source_message,
                    snapshot.operation.moderator_id or self.bot.user.id,
                )
                await self._publish_gate_increment_result(
                    source_message,
                    recovered,
                )
            except Exception:
                log.exception(
                    "Failed to recover Gate increment for message %s",
                    snapshot.operation.key.message_id,
                )

    @commands.command(name="tierdistribution")
    @commands.guild_only()
    async def tierdistribution(self, ctx: commands.Context) -> None:
        """Show the current distribution of progression and Gate player roles."""
        resolved_tiers = []
        for label, emoji_name, emoji_id, role_id in TIER_DISTRIBUTION_ROLES:
            role = _require_guild_role(
                ctx.guild,
                role_id,
                report_name="Tier distribution",
                role_label=label,
            )
            resolved_tiers.append((emoji_name, emoji_id, role))

        resolved_gate_roles = [
            _require_guild_role(
                ctx.guild,
                role_id,
                report_name="Tier distribution",
                role_label=f"Gate Tier {tier}",
            )
            for tier, role_id in enumerate(GATE_TIER_ROLE_IDS, start=1)
        ]

        tier_bucket_counts = await self._count_highest_role_buckets(
            ctx.guild,
            tuple(role.id for _, _, role in resolved_tiers),
        )
        tier_counts = [
            (emoji_name, emoji_id, count)
            for (emoji_name, emoji_id, _), count in zip(
                resolved_tiers, tier_bucket_counts, strict=True
            )
        ]
        gate_count = await self._count_role_expression(
            ctx.guild,
            " OR ".join(str(role.id) for role in resolved_gate_roles),
        )
        total_count = sum(count for _, _, count in tier_counts) + gate_count

        def format_count(count: int) -> str:
            player_label = "Player" if count == 1 else "Players"
            percentage = count / total_count * 100 if total_count else 0.0
            return f"**{count} {player_label}** ({percentage:.1f}%)"

        lines = [
            f"<:{emoji_name}:{emoji_id}> — {format_count(count)}"
            for emoji_name, emoji_id, count in tier_counts
        ]
        lines.append(
            f"<:{STARGATE_EMOJI_NAME}:{STARGATE_EMOJI_ID}> — "
            f"{format_count(gate_count)}"
        )

        embed = discord.Embed(
            title="Current Tier Distribution:",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await ctx.send(embed=embed)

    @nhmisc.group(name="stickyroles", invoke_without_command=True)
    async def nhmisc_stickyroles(self, ctx: commands.Context) -> None:
        """Configure sticky role persistence."""
        await self._require_manage_guild(ctx)
        role_ids = sorted(await self._sticky_roles.get_sticky_roles(ctx.guild.id))
        role_lines = [f"Configured roles: {len(role_ids)}"]
        for role_id in role_ids[:10]:
            role = ctx.guild.get_role(role_id)
            role_lines.append(
                role.mention if role is not None else "Configured role is missing"
            )
        if len(role_ids) > 10:
            role_lines.append(f"...and {len(role_ids) - 10} more")

        embed = self._configuration_embed(
            ctx=ctx,
            title="Sticky roles",
            current=tuple(role_lines),
        )
        await ctx.send(embed=embed)

    @nhmisc_stickyroles.command(name="add")
    async def nhmisc_stickyroles_add(self, ctx: commands.Context, role: str) -> None:
        """Mark a role as sticky by role mention or raw role ID."""
        await self._require_manage_guild(ctx)
        role_id = self._parse_role_id(role)
        discord_role = ctx.guild.get_role(role_id)
        if discord_role is None:
            raise commands.UserFeedbackCheckFailure("That role does not exist on this server.")
        if not self._can_restore_role(ctx.guild, discord_role):
            raise commands.UserFeedbackCheckFailure(
                "I cannot restore that role. Check Manage Roles and role hierarchy."
            )

        added = await self._sticky_roles.add_sticky_role(ctx.guild.id, role_id)
        if added:
            await ctx.send(
                f"{discord_role.mention} is now sticky.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(
                f"{discord_role.mention} is already sticky.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @nhmisc_stickyroles.command(name="remove")
    async def nhmisc_stickyroles_remove(self, ctx: commands.Context, role: str) -> None:
        """Remove a sticky role by role mention or raw role ID."""
        await self._require_manage_guild(ctx)
        role_id = self._parse_role_id(role)
        config_exists, saved_rows = await self._sticky_roles.get_role_state(
            ctx.guild.id, role_id
        )
        if not config_exists and saved_rows == 0:
            await ctx.send(
                f"{self._format_role_reference(ctx.guild, role_id)} is not present in the sticky role DB.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await self._prompt_sticky_role_db_action(
            guild=ctx.guild,
            channel=ctx.channel,
            role_id=role_id,
            role_name=self._role_name_for_prompt(ctx.guild, role_id),
            config_exists=config_exists,
            saved_rows=saved_rows,
            reason="manual remove command",
            requester=ctx.author,
        )

    @nhmisc_stickyroles.command(name="list")
    async def nhmisc_stickyroles_list(self, ctx: commands.Context) -> None:
        """List sticky roles configured for this server."""
        await self._require_manage_guild(ctx)
        role_ids = await self._sticky_roles.get_sticky_roles(ctx.guild.id)
        if not role_ids:
            await ctx.send("No sticky roles are configured on this server.")
            return

        lines = ["Sticky roles:"]
        for role_id in sorted(role_ids):
            lines.append(f"- {self._format_role_reference(ctx.guild, role_id)}")
        await self._send_paginated_text(ctx, "\n".join(lines))

    @nhmisc_stickyroles.command(name="scan")
    async def nhmisc_stickyroles_scan(self, ctx: commands.Context) -> None:
        """Scan sticky role DB for role IDs missing from Discord."""
        await self._require_manage_guild(ctx)
        existing_role_ids = {role.id for role in ctx.guild.roles}
        orphaned_roles = await self._sticky_roles.get_orphaned_roles(
            ctx.guild.id, existing_role_ids
        )
        if not orphaned_roles:
            await ctx.send("No sticky role DB entries need review.")
            return

        await ctx.send(
            f"Found {len(orphaned_roles)} sticky role DB entries that need review. "
            "I will ask about them one by one."
        )
        for role_id, config_exists, saved_rows in orphaned_roles:
            await self._prompt_sticky_role_db_action(
                guild=ctx.guild,
                channel=ctx.channel,
                role_id=role_id,
                role_name=None,
                config_exists=config_exists,
                saved_rows=saved_rows,
                reason="manual orphan scan",
                requester=ctx.author,
            )

    @nhmisc_stickyroles.group(name="debuglogging", invoke_without_command=True)
    async def nhmisc_stickyroles_debuglogging(self, ctx: commands.Context) -> None:
        """Configure sticky role debug logging."""
        await self._require_manage_guild(ctx)
        config = await self.config.guild(ctx.guild).all()
        embed = self._configuration_embed(
            ctx=ctx,
            title="Sticky role debug logging",
            current=(
                "Enabled: "
                + ("Yes" if config["sticky_debug_logging_enabled"] else "No"),
                "Alert channel: "
                + self._configured_channel_label(
                    ctx.guild,
                    config["alert_channel"]
                ),
            ),
            action_heading="Change it",
        )
        await ctx.send(embed=embed)

    @nhmisc_stickyroles_debuglogging.command(name="toggle")
    async def nhmisc_stickyroles_debuglogging_toggle(
        self, ctx: commands.Context, enabled: bool
    ) -> None:
        """Enable or disable sticky role debug logging."""
        await self._require_manage_guild(ctx)
        await self.config.guild(ctx.guild).sticky_debug_logging_enabled.set(enabled)
        state = "enabled" if enabled else "disabled"
        await ctx.send(f"Sticky role debug logging {state}.")

    @nhmisc.group(name="activity", invoke_without_command=True)
    async def nhmisc_activity(self, ctx: commands.Context) -> None:
        """Configure and inspect passive message activity summaries."""
        await self._require_activity_staff(ctx)
        config = await self.config.guild(ctx.guild).all()
        history_days = config["activity_history_retention_days"]
        if history_days < 0:
            history_label = "Unlimited"
        elif history_days == 0:
            history_label = "Disabled"
        else:
            history_label = f"{history_days} days"

        embed = self._configuration_embed(
            ctx=ctx,
            title="Activity tracking",
            current=(
                "Summary channel: "
                f"{self._configured_channel_label(ctx.guild, config['activity_channel'])}",
                f"Detail retention: {config['activity_detail_retention_days']} days",
                f"History retention: {history_label}",
            ),
        )
        await ctx.send(embed=embed)

    @nhmisc_activity.command(name="channel")
    async def nhmisc_activity_channel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        """Set the channel used for automatic daily activity summaries."""
        await self._require_manage_guild(ctx)
        missing_permissions = self._missing_log_permissions(ctx.guild, channel)
        if missing_permissions is not None:
            raise commands.UserFeedbackCheckFailure(missing_permissions)

        await self.config.guild(ctx.guild).activity_channel.set(channel.id)
        await ctx.send(f"Activity summary channel set to {channel.mention}.")

    @nhmisc_activity.command(name="current")
    async def nhmisc_activity_current(self, ctx: commands.Context) -> None:
        """Preview the current UTC day's activity without closing it."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        today = self._utc_today()
        summary = await self._activity_store.build_current_summary(
            ctx.guild.id, today, ctx.guild.member_count or 0
        )
        if summary is None:
            await ctx.send("No activity data has been collected for the current UTC day.")
            return

        await ctx.send(embed=self._build_daily_summary_embed(summary, title_prefix="Current day"))

    @nhmisc_activity.command(name="latest")
    async def nhmisc_activity_latest(self, ctx: commands.Context) -> None:
        """Repost the latest retained closed daily activity summary."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        summary = await self._activity_store.get_latest_summary(ctx.guild.id)
        if summary is None:
            await ctx.send("No retained daily activity summary is available.")
            return

        await ctx.send(embed=self._build_daily_summary_embed(summary, title_prefix="Latest day"))

    @nhmisc_activity.command(name="timeline")
    async def nhmisc_activity_timeline(self, ctx: commands.Context, days: int) -> None:
        """Show a compact timeline for retained closed daily summaries."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Days must be at least 1.")

        config = await self.config.guild(ctx.guild).all()
        history_retention = int(config["activity_history_retention_days"])
        if history_retention == 0:
            await ctx.send("Historical activity summaries are not retained on this server.")
            return
        if history_retention > 0 and days > history_retention:
            days = history_retention

        end_date = self._utc_today() - timedelta(days=1)
        timeline = await self._activity_store.get_timeline(ctx.guild.id, end_date, days)
        top_channels = await self._activity_store.get_timeline_top_channels(
            ctx.guild.id, end_date, days
        )
        await ctx.send(embed=self._build_timeline_embed(timeline, top_channels, days))

    @nhmisc_activity.command(name="channelstats")
    async def nhmisc_activity_channelstats(
        self, ctx: commands.Context, channel: discord.TextChannel, days: int
    ) -> None:
        """Show message activity for a channel day by day."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Days must be at least 1.")

        config = await self.config.guild(ctx.guild).all()
        history_retention = int(config["activity_history_retention_days"])
        detail_retention = max(1, int(config["activity_detail_retention_days"]))
        if history_retention == 0:
            days = min(days, detail_retention)
        elif history_retention > 0:
            days = min(days, max(history_retention, detail_retention))

        timeline = await self._activity_store.get_channel_timeline(
            ctx.guild.id,
            channel.id,
            None,
            self._utc_today(),
            days,
        )
        await ctx.send(embed=self._build_channel_timeline_embed(channel, timeline, days))

    @nhmisc_activity.command(name="verify")
    async def nhmisc_activity_verify(self, ctx: commands.Context) -> None:
        """Verify today's aggregate activity cache consistency."""
        await self._require_activity_staff(ctx)
        today = self._utc_today()
        report = await self._activity_store.verify_open_day_consistency(ctx.guild.id, today)
        await ctx.send(embed=self._build_activity_consistency_embed(report, today))

    @nhmisc_activity.command(name="dbsize")
    async def nhmisc_activity_dbsize(self, ctx: commands.Context) -> None:
        """Show activity SQLite database size and row counts."""
        await self._require_activity_staff(ctx)
        stats = await self._activity_store.get_database_stats()
        await ctx.send(embed=self._build_activity_database_stats_embed(stats))

    @nhmisc_activity.command(name="retention")
    async def nhmisc_activity_retention(self, ctx: commands.Context, days: int) -> None:
        """Set how many days of per-user/channel detail rows are retained."""
        await self._require_manage_guild(ctx)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Detail retention must be at least 1 day.")

        cutoff = self._utc_today() - timedelta(days=days - 1)
        rows_to_delete = await self._activity_store.count_detail_rows_older_than(
            ctx.guild.id, cutoff
        )
        if rows_to_delete:
            confirmed = await self._confirm_retention_delete(
                ctx,
                (
                    f"Changing detail retention to {days} days will permanently delete "
                    f"{rows_to_delete} user/channel detail rows older than {cutoff.isoformat()}.\n"
                    f"Reply with `{RETENTION_CONFIRMATION}` to continue."
                ),
            )
            if not confirmed:
                return
            deleted = await self._activity_store.prune_detail_rows_older_than(ctx.guild.id, cutoff)
            await ctx.send(f"Deleted {deleted} detail rows.")

        await self.config.guild(ctx.guild).activity_detail_retention_days.set(days)
        await ctx.send(f"Activity detail retention set to {days} days.")

    @nhmisc_activity.command(name="historyretention")
    async def nhmisc_activity_history_retention(self, ctx: commands.Context, days: int) -> None:
        """Set how many closed daily aggregate summaries are retained."""
        await self._require_manage_guild(ctx)
        if days < -1:
            raise commands.UserFeedbackCheckFailure(
                "History retention must be -1, 0, or a positive number of days."
            )

        cutoff = self._history_retention_cutoff(days)
        summary_rows = top_rows = channel_rows = 0
        if cutoff is not None:
            (
                summary_rows,
                top_rows,
                channel_rows,
            ) = await self._activity_store.count_history_rows_older_than(ctx.guild.id, cutoff)
        if summary_rows or top_rows or channel_rows:
            confirmed = await self._confirm_retention_delete(
                ctx,
                (
                    f"Changing history retention to {days} will permanently delete "
                    f"{summary_rows} daily summary rows, {top_rows} top-channel rows, "
                    f"and {channel_rows} channel summary rows "
                    f"older than {cutoff.isoformat()}.\n"
                    f"Reply with `{RETENTION_CONFIRMATION}` to continue."
                ),
            )
            if not confirmed:
                return
            (
                deleted_summary,
                deleted_top,
                deleted_channel,
            ) = await self._activity_store.prune_history_rows_older_than(ctx.guild.id, cutoff)
            await ctx.send(
                f"Deleted {deleted_summary} daily summary rows, {deleted_top} top-channel rows, "
                f"and {deleted_channel} channel summary rows."
            )

        await self.config.guild(ctx.guild).activity_history_retention_days.set(days)
        await ctx.send(f"Activity history retention set to {days}.")

    @nhmisc.group(name="usermodstats", invoke_without_command=True)
    async def nhmisc_usermodstats(
        self, ctx: commands.Context, target: str, range_text: str
    ) -> None:
        """Show moderator-only message activity stats for a user."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        user_id = self._parse_user_id(target)
        days = self._parse_range_days(range_text)
        days = await self._cap_detail_days(ctx.guild, days)
        end_date = self._utc_today()
        stats = await self._activity_store.get_user_stats(ctx.guild.id, user_id, end_date, days)

        title = f"User activity: {self._format_user_reference(ctx.guild, user_id)}"
        await ctx.send(embed=self._build_user_stats_embed(title, stats, days))

    @nhmisc_usermodstats.command(name="channel")
    async def nhmisc_usermodstats_channel(
        self,
        ctx: commands.Context,
        target: str,
        channel_text: str,
        range_text: str,
    ) -> None:
        """Show moderator-only message activity stats for a user in one channel."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        user_id = self._parse_user_id(target)
        days = self._parse_range_days(range_text)
        days = await self._cap_detail_days(ctx.guild, days)
        end_date = self._utc_today()
        channel = self._resolve_text_channel_or_thread(ctx.guild, channel_text)
        parent_channel_id = self._activity_parent_channel_id(channel)
        thread_id = self._activity_thread_id(channel)
        stats = await self._activity_store.get_user_channel_stats(
            ctx.guild.id,
            user_id,
            parent_channel_id,
            thread_id,
            thread_id is None,
            end_date,
            days,
        )

        title = (
            f"User channel activity: {self._format_user_reference(ctx.guild, user_id)} - "
            f"{self._format_activity_location(ctx.guild, parent_channel_id, thread_id)}"
        )
        await ctx.send(embed=self._build_user_channel_stats_embed(title, stats, days))

    @nhmisc_usermodstats.command(name="channels")
    async def nhmisc_usermodstats_channels(
        self, ctx: commands.Context, target: str, range_text: str
    ) -> None:
        """Show how a user's activity is distributed across channels."""
        await self._require_activity_staff(ctx)
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        user_id = self._parse_user_id(target)
        days = self._parse_range_days(range_text)
        days = await self._cap_detail_days(ctx.guild, days)
        end_date = self._utc_today()
        distribution = await self._activity_store.get_user_channel_distribution(
            ctx.guild.id, user_id, end_date, days
        )

        title = f"User channel distribution: {self._format_user_reference(ctx.guild, user_id)}"
        await ctx.send(
            embed=self._build_user_channel_distribution_embed(ctx.guild, title, distribution, days)
        )

    @nhmisc.command(name="chatchart")
    async def nhmisc_chatchart(
        self,
        ctx: commands.Context,
        target_or_days: str,
        days_or_amount: int | None = None,
        amount: int | None = None,
    ) -> None:
        """Render a chart of user activity in the selected or current channel."""
        await self._require_activity_staff(ctx)
        target, days, amount = self._resolve_chatchart_request(
            ctx,
            target_or_days,
            days_or_amount,
            amount,
        )
        if target is not ctx.channel and not target.permissions_for(ctx.author).view_channel:
            raise commands.UserFeedbackCheckFailure(
                "You cannot view that channel or thread."
            )
        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Days must be at least 1.")
        if not 1 <= amount <= MAX_CHATCHART_USER_COUNT:
            raise commands.UserFeedbackCheckFailure(
                f"Amount must be between 1 and {MAX_CHATCHART_USER_COUNT}."
            )

        days = await self._cap_detail_days(ctx.guild, days)
        channel_id = self._activity_parent_channel_id(target)
        counts = await self._activity_store.get_channel_user_counts(
            ctx.guild.id,
            channel_id,
            self._activity_thread_id(target),
            self._utc_today(),
            days,
        )
        if not counts:
            await ctx.send(f"No retained activity data for this channel in the last {days} days.")
            return

        file = self._build_chatchart_file(
            ctx.guild,
            counts,
            days,
            self._chatchart_location_label(target),
            amount,
        )
        content = "One is a bit low, no? 🤨" if amount == 1 else None
        await ctx.send(
            content,
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @nhmisc.command(name="topyapper")
    async def nhmisc_topyapper(
        self, ctx: commands.Context, days: int, amount: int
    ) -> None:
        """Show the most active users across this server."""
        await self._send_yapper_ranking(ctx, days, amount)

    @commands.command(name="selfchart")
    @commands.guild_only()
    async def selfchart(self, ctx: commands.Context) -> None:
        """Show your own simplified activity stats for the last 7 retained days."""
        days = await self._cap_detail_days(ctx.guild, 7)
        stats = await self._activity_store.get_user_stats(
            ctx.guild.id, ctx.author.id, self._utc_today(), days
        )
        embed = self._build_selfchart_embed(ctx.author, stats, days)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        """Pin the starter message for a new post in a configured forum."""
        await self._forum_autopin.handle_thread_create(thread)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Drop autopin configuration for a deleted forum."""
        await self._forum_autopin.handle_channel_delete(channel)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Snapshot configured sticky roles when a member leaves."""
        configured_roles = await self._sticky_roles.get_sticky_roles(member.guild.id)
        if not configured_roles:
            await self._send_sticky_debug_log(
                member.guild,
                (
                    "Sticky role snapshot write skipped\n"
                    f"User: {member.mention} (`{member.id}`)\n"
                    "Reason: no sticky roles are configured on this server."
                ),
            )
            return

        current_role_ids = {role.id for role in member.roles}
        saved_role_ids = configured_roles & current_role_ids
        await self._sticky_roles.replace_member_roles(
            member.guild.id,
            member.id,
            saved_role_ids,
        )
        await self._send_sticky_debug_log(
            member.guild,
            (
                "Sticky role snapshot written\n"
                f"User: {member.mention} (`{member.id}`)\n"
                f"Saved roles: {self._format_role_id_set(member.guild, saved_role_ids)}"
            ),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Restore saved sticky roles when a member rejoins."""
        saved_role_ids = await self._sticky_roles.get_member_roles(member.guild.id, member.id)
        if not saved_role_ids:
            await self._send_sticky_debug_log(
                member.guild,
                (
                    "Sticky role snapshot read\n"
                    f"User: {member.mention} (`{member.id}`)\n"
                    "Saved roles: none\n"
                    "Result: nothing to restore."
                ),
            )
            return

        configured_role_ids = await self._sticky_roles.get_sticky_roles(member.guild.id)
        roles: list[discord.Role] = []
        for role_id in sorted(saved_role_ids & configured_role_ids):
            role = member.guild.get_role(role_id)
            if role is not None and self._can_restore_role(member.guild, role):
                roles.append(role)

        restorable_role_ids = {role.id for role in roles}
        skipped_role_ids = saved_role_ids - restorable_role_ids
        if not roles:
            await self._send_sticky_debug_log(
                member.guild,
                (
                    "Sticky role snapshot read\n"
                    f"User: {member.mention} (`{member.id}`)\n"
                    f"Saved roles: {self._format_role_id_set(member.guild, saved_role_ids)}\n"
                    "Restorable roles: none\n"
                    f"Skipped roles: {self._format_role_id_set(member.guild, skipped_role_ids)}\n"
                    "Result: nothing restorable."
                ),
            )
            return

        result = "restored"
        try:
            await member.add_roles(*roles, reason="Restoring sticky roles")
        except discord.Forbidden:
            result = "failed: missing permissions"
            log.warning(
                "Missing permissions to restore sticky roles for member %s in guild %s",
                member.id,
                member.guild.id,
            )
        except discord.HTTPException:
            result = "failed: Discord API error"
            log.exception(
                "Failed to restore sticky roles for member %s in guild %s",
                member.id,
                member.guild.id,
            )
        await self._send_sticky_debug_log(
            member.guild,
            (
                "Sticky role snapshot read\n"
                f"User: {member.mention} (`{member.id}`)\n"
                f"Saved roles: {self._format_role_id_set(member.guild, saved_role_ids)}\n"
                f"Restorable roles: {self._format_role_id_set(member.guild, restorable_role_ids)}\n"
                f"Skipped roles: {self._format_role_id_set(member.guild, skipped_role_ids)}\n"
                f"Result: {result}."
            ),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """Clean up role-backed state when a Discord role is deleted."""
        definition = next(
            (
                definition
                for definition in await self._achievement_store.list_definitions(
                    role.guild.id
                )
                if definition.role_id == role.id
            ),
            None,
        )
        if definition is not None:
            await self._achievement_store.unbind_role(role.guild.id, role.id)
            await self._send_guild_alert(
                role.guild,
                f"Stopped tracking deleted role {role.name} for "
                f"{definition.display_name}",
            )

        config_exists, saved_rows = await self._sticky_roles.get_role_state(
            role.guild.id, role.id
        )
        if not config_exists and saved_rows == 0:
            return

        config = await self.config.guild(role.guild).all()
        channel = self._get_log_channel(role.guild, config["alert_channel"])
        if channel is None:
            log.warning(
                "Sticky role %s was deleted in guild %s but no alert channel is set",
                role.id,
                role.guild.id,
            )
            return

        await self._prompt_sticky_role_db_action(
            guild=role.guild,
            channel=channel,
            role_id=role.id,
            role_name=role.name,
            config_exists=config_exists,
            saved_rows=saved_rows,
            reason="Discord role deletion event",
            requester=None,
        )

    @commands.Cog.listener("on_member_join")
    async def on_role_analytics_member_join(self, member: discord.Member) -> None:
        await self._role_analytics.member_joined(
            member.guild.id,
            member,
            member.guild.default_role.id,
        )

    @commands.Cog.listener("on_member_update")
    async def on_role_analytics_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        if before_role_ids == after_role_ids:
            return
        await self._role_analytics.member_roles_changed(
            after.guild.id,
            after,
            after.guild.default_role.id,
        )

    @commands.Cog.listener("on_member_update")
    async def on_achievement_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if after.bot or not await self._achievement_store.is_bootstrapped(
            after.guild.id
        ):
            return
        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        changed_role_ids = before_role_ids ^ after_role_ids
        definitions = tuple(
            definition
            for definition in await self._achievement_store.list_definitions(
                after.guild.id
            )
            if definition.role_id is not None
        )
        protected_role_ids = set(GATE_TIER_ROLE_IDS) | {
            definition.role_id for definition in definitions
        }
        if not changed_role_ids & protected_role_ids:
            return

        corrected_roles: list[int] = []
        before_gate_roles = before_role_ids & set(GATE_TIER_ROLE_IDS)
        after_gate_roles = after_role_ids & set(GATE_TIER_ROLE_IDS)
        restored_gate = False
        if before_gate_roles != after_gate_roles:
            completed_count = await self._achievement_store.get_gate_projection(
                after.guild.id,
                after.id,
            )
            expected_gate_roles = (
                {GATE_TIER_ROLE_IDS[completed_count - 1]}
                if 0 < completed_count <= len(GATE_TIER_ROLE_IDS)
                else set()
            )
            if after_gate_roles != expected_gate_roles:
                try:
                    restored_gate = await self._restore_gate_projection(
                        after.guild,
                        after,
                        completed_count,
                        reason="Revert unauthorized Gate role change",
                    )
                except (
                    commands.UserFeedbackCheckFailure,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    log.exception(
                        "Failed to revert Gate role change for guild %s member %s",
                        after.guild.id,
                        after.id,
                    )
                    await self._send_guild_alert(
                        after.guild,
                        f"Failed to restore Gate roles for <@{after.id}>",
                    )
                    return
                if restored_gate:
                    corrected_roles.extend(after_gate_roles ^ expected_gate_roles)

        for definition in definitions:
            role_id = definition.role_id
            if role_id is None or role_id not in changed_role_ids:
                continue
            should_have_role = after.id in set(
                await self._achievement_store.projected_users_for_boolean(
                    after.guild.id,
                    definition.key,
                )
            )
            if (role_id in after_role_ids) == should_have_role:
                continue
            try:
                await self._edit_achievement_roles(
                    after.guild,
                    after,
                    add_role_ids=(role_id,) if should_have_role else (),
                    remove_role_ids=() if should_have_role else (role_id,),
                    reason="Revert unauthorized achievement role change",
                )
            except (
                commands.UserFeedbackCheckFailure,
                discord.Forbidden,
                discord.HTTPException,
            ):
                log.exception(
                    "Failed to restore achievement role %s for guild %s member %s",
                    role_id,
                    after.guild.id,
                    after.id,
                )
                await self._send_guild_alert(
                    after.guild,
                    f"Failed to restore <@&{role_id}> for <@{after.id}>",
                )
                return
            corrected_roles.append(role_id)

        if corrected_roles:
            rendered_roles = ", ".join(
                f"<@&{role_id}>" for role_id in sorted(set(corrected_roles))
            )
            await self._send_guild_alert(
                after.guild,
                f"Restored achievement roles for <@{after.id}>: {rendered_roles}",
            )
        if restored_gate:
            task = asyncio.create_task(
                self._notify_unauthorized_gate_actor(after.guild, after.id)
            )
            self._audit_log_tasks.add(task)
            task.add_done_callback(self._audit_log_tasks.discard)

    async def _notify_unauthorized_gate_actor(
        self, guild: discord.Guild, member_id: int
    ) -> None:
        permissions = getattr(guild.me, "guild_permissions", None)
        if permissions is None or not permissions.view_audit_log:
            log.warning(
                "Cannot attribute unauthorized Gate change in guild %s: "
                "View Audit Log is missing",
                guild.id,
            )
            return
        await asyncio.sleep(2)
        try:
            async for entry in guild.audit_logs(
                limit=8,
                action=discord.AuditLogAction.member_role_update,
            ):
                target = getattr(entry, "target", None)
                if target is None or target.id != member_id:
                    continue
                created_at = getattr(entry, "created_at", None)
                if created_at is None or (
                    datetime.now(timezone.utc) - created_at
                ).total_seconds() > 30:
                    continue
                actor = getattr(entry, "user", None)
                if actor is None or actor.bot:
                    continue
                delivered = await self._send_guild_alert(
                    guild,
                    f"<@{actor.id}> Gate roles must be changed through the bot. "
                    "Your manual role change was automatically reverted",
                    ping_user=actor,
                )
                if not delivered:
                    log.warning(
                        "Could not notify the moderator about a reverted Gate "
                        "change in guild %s because the alert channel is unavailable",
                        guild.id,
                    )
                return
        except (discord.Forbidden, discord.HTTPException):
            log.warning(
                "Could not notify the moderator about a reverted Gate change "
                "in guild %s",
                guild.id,
            )

    @commands.Cog.listener("on_member_remove")
    async def on_role_analytics_member_remove(self, member: discord.Member) -> None:
        await self._role_analytics.member_removed(member.guild.id, member.id)

    @commands.Cog.listener("on_guild_role_delete")
    async def on_role_analytics_role_delete(self, role: discord.Role) -> None:
        await self._role_analytics.role_deleted(role.guild.id, role.id)

    @commands.Cog.listener("on_resumed")
    async def on_role_analytics_resumed(self) -> None:
        await self._role_analytics.schedule_resumed_check(tuple(self.bot.guilds))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Passively collect message activity counters."""
        guild = message.guild
        if guild is None:
            return
        if message.author.bot or message.webhook_id is not None:
            return
        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return

        now = datetime.now(timezone.utc)
        today = now.date()
        await self._close_stale_activity_days_for_guild(guild, send_reports=True)
        await self._activity_store.record_message(
            guild.id,
            today,
            now.hour,
            message.author.id,
            self._activity_parent_channel_id(message.channel),
            self._activity_thread_id(message.channel),
            now,
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Log voice channel joins, leaves, moves, and VC jumping."""
        if before.channel == after.channel:
            return

        guild = member.guild
        config = await self.config.guild(guild).all()
        log_channel = self._get_log_channel(guild, config["voice_log_channel"])
        event_timestamp = int(time.time())

        if log_channel is not None:
            if before.channel is None and after.channel is not None:
                await self._send_voice_log(
                    log_channel,
                    (
                        f"{member.mention} ({member.id}) has joined a channel "
                        f"{after.channel.mention} at <t:{event_timestamp}:F>"
                    ),
                )
            elif before.channel is not None and after.channel is None:
                await self._send_voice_log(
                    log_channel,
                    (
                        f"{member.mention} ({member.id}) has left a channel "
                        f"{before.channel.mention} at <t:{event_timestamp}:F>"
                    ),
                )
            elif before.channel is not None and after.channel is not None:
                move_log_content = (
                    f"{member.mention} ({member.id}) has moved from "
                    f"{before.channel.mention} to {after.channel.mention} "
                    f"at <t:{event_timestamp}:F>"
                )
                move_log_message = await self._send_voice_log(
                    log_channel,
                    move_log_content,
                )
                if move_log_message is not None:
                    self._schedule_audit_log_edit(
                        move_log_message,
                        move_log_content,
                        guild,
                        member,
                        after.channel,
                        event_timestamp,
                    )

        if after.channel is None:
            return

        is_vcjumping = self._voice_visits.record_visit(
            (guild.id, member.id),
            after.channel.id,
            timestamp=time.monotonic(),
            visit_count=config["vcjumping_visit_count"],
            window_seconds=config["vcjumping_window_seconds"],
        )
        if is_vcjumping:
            alert_channel = self._get_log_channel(guild, config["alert_channel"])
            if alert_channel is None:
                return

            await self._send_voice_log(
                alert_channel,
                (
                    f"{member.mention} is VC jumping "
                    f"({config['vcjumping_visit_count']} channel entries in "
                    f"{config['vcjumping_window_seconds']} seconds)."
                ),
            )

    def _get_log_channel(
        self, guild: discord.Guild, channel_id: int | None
    ) -> discord.TextChannel | None:
        if channel_id is None:
            return None

        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    def _missing_log_permissions(
        self, guild: discord.Guild, channel: discord.TextChannel
    ) -> str | None:
        me = guild.me
        permissions = channel.permissions_for(me)
        if not permissions.view_channel:
            return f"I need permission to view {channel.mention}."
        if not permissions.send_messages:
            return f"I need permission to send messages in {channel.mention}."
        return None

    async def _send_guild_alert(
        self,
        guild: discord.Guild,
        content: str,
        *,
        ping_user: discord.abc.Snowflake | None = None,
    ) -> bool:
        """Send to the configured alert channel. False when there is none."""
        alert_channel = self._get_log_channel(
            guild, await self.config.guild(guild).alert_channel()
        )
        if alert_channel is None:
            return False

        allowed_mentions = (
            discord.AllowedMentions(
                everyone=False,
                users=[ping_user],
                roles=False,
                replied_user=False,
            )
            if ping_user is not None
            else None
        )
        return (
            await self._send_voice_log(
                alert_channel,
                content,
                allowed_mentions=allowed_mentions,
            )
            is not None
        )

    def _schedule_audit_log_edit(
        self,
        message: discord.Message,
        base_content: str,
        guild: discord.Guild,
        member: discord.Member,
        after_channel: discord.VoiceChannel | discord.StageChannel,
        event_timestamp: int,
    ) -> None:
        task = asyncio.create_task(
            self._edit_move_log_with_moderator(
                message,
                base_content,
                guild,
                member,
                after_channel,
                event_timestamp,
            )
        )
        self._audit_log_tasks.add(task)
        task.add_done_callback(self._audit_log_tasks.discard)

    async def _edit_move_log_with_moderator(
        self,
        message: discord.Message,
        base_content: str,
        guild: discord.Guild,
        member: discord.Member,
        after_channel: discord.VoiceChannel | discord.StageChannel,
        event_timestamp: int,
    ) -> None:
        for attempt in range(5):
            if attempt > 0:
                await asyncio.sleep(2)

            moved_by = await self._get_voice_move_moderator(
                guild, member, after_channel, event_timestamp
            )
            if moved_by is None:
                continue

            try:
                timestamp_suffix = f" at <t:{event_timestamp}:F>"
                edited_content = base_content.replace(
                    timestamp_suffix,
                    f" moved by {self._format_user_label(moved_by)}{timestamp_suffix}",
                    1,
                )
                await message.edit(
                    content=edited_content,
                )
            except discord.HTTPException:
                log.exception("Failed to edit voice move log message %s", message.id)
            return

    async def _get_voice_move_moderator(
        self,
        guild: discord.Guild,
        member: discord.Member,
        after_channel: discord.VoiceChannel | discord.StageChannel,
        event_timestamp: int,
    ) -> discord.User | discord.Member | None:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None

        event_time = datetime.fromtimestamp(event_timestamp, timezone.utc)
        try:
            async for entry in guild.audit_logs(
                limit=5,
                action=discord.AuditLogAction.member_move,
            ):
                created_at = entry.created_at
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)

                if abs((created_at - event_time).total_seconds()) > 15:
                    continue

                target_id = getattr(entry.target, "id", None)
                if target_id == member.id:
                    return entry.user

                extra = getattr(entry, "extra", None)
                extra_channel = getattr(extra, "channel", None)
                extra_count = getattr(extra, "count", None)
                if (
                    target_id is None
                    and getattr(extra_channel, "id", None) == after_channel.id
                    and str(extra_count) == "1"
                ):
                    return entry.user
        except discord.Forbidden:
            return None
        except discord.HTTPException:
            log.exception("Failed to read audit log for voice move in guild %s", guild.id)
        return None

    def _format_user_label(self, user: discord.User | discord.Member) -> str:
        name = getattr(user, "display_name", None) or str(user)
        return f"{name} ({user.id})"

    async def _send_voice_log(
        self,
        channel: discord.TextChannel,
        content: str,
        *,
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> discord.Message | None:
        if allowed_mentions is None:
            allowed_mentions = discord.AllowedMentions.none()
        try:
            return await channel.send(content, allowed_mentions=allowed_mentions)
        except discord.HTTPException:
            log.exception("Failed to send voice log message to channel %s", channel.id)
        return None

    async def _activity_midnight_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._close_stale_activity_days_for_all_guilds(send_reports=True)
            except Exception:
                log.exception("Failed to close stale activity days")
            try:
                now = datetime.now(timezone.utc)
                next_midnight = datetime.combine(
                    now.date() + timedelta(days=1),
                    datetime_time.min,
                    tzinfo=timezone.utc,
                ) + timedelta(seconds=5)
                await asyncio.sleep(max(1.0, (next_midnight - now).total_seconds()))
            except asyncio.CancelledError:
                raise

    async def _close_stale_activity_days_for_all_guilds(self, send_reports: bool) -> None:
        for guild in list(self.bot.guilds):
            await self._close_stale_activity_days_for_guild(guild, send_reports=send_reports)

    async def _close_stale_activity_days_for_guild(
        self, guild: discord.Guild, send_reports: bool
    ) -> None:
        today = self._utc_today()
        summaries = await self._activity_store.close_stale_days(
            guild.id, today, guild.member_count or 0
        )
        if not summaries:
            return

        config = await self.config.guild(guild).all()
        channel = self._get_log_channel(guild, config["activity_channel"])
        for summary in summaries:
            if send_reports and channel is not None:
                await self._send_activity_summary(channel, summary)
            await self._apply_activity_history_retention(
                guild.id, int(config["activity_history_retention_days"]), summary.date_utc
            )

        await self._apply_activity_detail_retention(
            guild.id, int(config["activity_detail_retention_days"])
        )

    async def _send_activity_summary(
        self, channel: discord.TextChannel, summary: DailySummary
    ) -> None:
        try:
            await channel.send(
                embed=self._build_daily_summary_embed(summary, title_prefix="Daily"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.exception("Failed to send activity summary to channel %s", channel.id)

    async def _apply_activity_detail_retention(self, guild_id: int, days: int) -> None:
        if days < 1:
            return
        cutoff = self._utc_today() - timedelta(days=days - 1)
        await self._activity_store.prune_detail_rows_older_than(guild_id, cutoff)

    async def _apply_activity_history_retention(
        self, guild_id: int, days: int, closed_date: date
    ) -> None:
        if days == -1:
            return
        if days == 0:
            await self._activity_store.delete_history_for_date(guild_id, closed_date)
            return
        cutoff = self._utc_today() - timedelta(days=days)
        await self._activity_store.prune_history_rows_older_than(guild_id, cutoff)

    async def _require_manage_guild(self, ctx: commands.Context) -> None:
        permissions = getattr(ctx.author, "guild_permissions", None)
        has_permission = bool(permissions and permissions.manage_guild)
        if has_permission or await self.bot.is_admin(ctx.author):
            return
        raise commands.UserFeedbackCheckFailure("You need Manage Server permission.")

    async def _require_activity_staff(self, ctx: commands.Context) -> None:
        permissions = getattr(ctx.author, "guild_permissions", None)
        has_permission = bool(
            permissions and (permissions.manage_messages or permissions.manage_guild)
        )
        if has_permission or await self.bot.is_admin(ctx.author):
            return
        raise commands.UserFeedbackCheckFailure(
            "You need Manage Messages or Manage Server permission."
        )

    def _prepare_role_expression(
        self, guild: discord.Guild, expression: str
    ) -> tuple[object, str, tuple[int, ...]]:
        try:
            parsed = parse_role_expression(expression)
        except RoleExpressionSyntaxError as error:
            raise commands.UserFeedbackCheckFailure(
                "Invalid role expression"
            ) from error

        for role_id in role_ids(parsed):
            role = guild.get_role(role_id)
            if role is None:
                raise commands.UserFeedbackCheckFailure(
                    "Role expression contains an unknown role"
                )
            if role_id == guild.default_role.id or role.is_default():
                raise commands.UserFeedbackCheckFailure(
                    "The @everyone role cannot be used in role expressions"
                )

        predicate_sql, parameters = compile_role_expression(parsed)
        return parsed, predicate_sql, parameters

    async def _count_role_expression(
        self, guild: discord.Guild, expression: str
    ) -> int:
        _, predicate_sql, parameters = self._prepare_role_expression(
            guild, expression
        )
        try:
            return await self._role_analytics_store.count_matching(
                guild.id, predicate_sql, parameters
            )
        except AnalyticsUnavailableError as error:
            raise commands.UserFeedbackCheckFailure(
                "Role analytics are unavailable right now"
            ) from error

    async def _count_highest_role_buckets(
        self, guild: discord.Guild, ordered_role_ids: tuple[int, ...]
    ) -> list[int]:
        counts = []
        for index, role_id in enumerate(ordered_role_ids):
            higher_role_ids = ordered_role_ids[index + 1 :]
            expression = str(role_id)
            if higher_role_ids:
                higher_roles = " OR ".join(
                    str(higher_role_id) for higher_role_id in higher_role_ids
                )
                expression = f"{expression} AND NOT ({higher_roles})"
            counts.append(await self._count_role_expression(guild, expression))
        return counts

    def _require_private_role_export_channel(self, ctx: commands.Context) -> None:
        if self._channel_is_public(ctx):
            log.info(
                "Role export refused in public channel %s for guild %s",
                getattr(ctx.channel, "id", "unknown"),
                ctx.guild.id,
            )
            raise commands.UserFeedbackCheckFailure(
                "Role export is unavailable in this channel"
            )

        if ctx.guild.me is None:
            missing_permissions = ("bot_member",)
        else:
            bot_permissions = ctx.channel.permissions_for(ctx.guild.me)
            missing_permissions = tuple(
                name
                for name in ("view_channel", "send_messages", "attach_files")
                if not bool(getattr(bot_permissions, name, False))
            )
        if missing_permissions:
            log.warning(
                "Role export refused in channel %s for guild %s; missing bot permissions: %s",
                getattr(ctx.channel, "id", "unknown"),
                ctx.guild.id,
                ", ".join(missing_permissions),
            )
            raise commands.UserFeedbackCheckFailure(
                "Role export is unavailable in this channel"
            )

    async def _repair_role_analytics_cache(self, guild: discord.Guild) -> None:
        await self._role_analytics_store.set_status(
            guild.id,
            SyncStatus.NEEDS_RECONCILIATION,
            "member_cache_mismatch",
        )
        self._role_analytics.schedule_guild_retry(guild, 0)

    def _parse_role_id(self, value: str) -> int:
        stripped = value.strip()
        if stripped.startswith("<@&") and stripped.endswith(">"):
            stripped = stripped[3:-1]
        if not stripped.isdigit():
            raise commands.UserFeedbackCheckFailure("Pass a role mention or raw Discord role ID.")
        return int(stripped)

    def _can_restore_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        me = guild.me
        if me is None:
            return False
        if role.is_default() or role.managed:
            return False
        if not me.guild_permissions.manage_roles:
            return False
        return role < me.top_role

    def _format_role_reference(self, guild: discord.Guild, role_id: int) -> str:
        role = guild.get_role(role_id)
        if role is None:
            return f"`{role_id}` (missing)"
        return f"{role.mention} (`{role_id}`)"

    def _format_role_id_set(self, guild: discord.Guild, role_ids: set[int]) -> str:
        if not role_ids:
            return "none"
        return ", ".join(
            self._format_role_reference(guild, role_id) for role_id in sorted(role_ids)
        )

    def _role_name_for_prompt(self, guild: discord.Guild, role_id: int) -> str | None:
        role = guild.get_role(role_id)
        if role is None:
            return None
        return role.name

    async def _prompt_sticky_role_db_action(
        self,
        *,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        role_id: int,
        role_name: str | None,
        config_exists: bool,
        saved_rows: int,
        reason: str,
        requester: discord.Member | discord.User | None,
    ) -> None:
        role_label = f"{role_name} (`{role_id}`)" if role_name else f"`{role_id}`"
        await channel.send(
            "Sticky role DB entry needs a decision.\n"
            f"Role: {role_label}\n"
            f"Trigger: {reason}\n"
            f"Configured as sticky: {'yes' if config_exists else 'no'}\n"
            f"Saved user-role rows: {saved_rows}\n"
            "Reply with one of:\n"
            f"`remove {role_id}` - delete this role from sticky DB and saved users\n"
            f"`keep {role_id}` - stop configuring this role as sticky, but keep saved user rows\n"
            f"`change {role_id} <role mention or ID>` - move config and saved users to another role",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        deadline = time.monotonic() + 300
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await channel.send("Sticky role DB decision timed out. No changes were made.")
                return

            def check(message: discord.Message) -> bool:
                _, target_role_id, _ = _parse_sticky_db_decision(message.content)
                return (
                    message.channel.id == channel.id
                    and not message.author.bot
                    and target_role_id == role_id
                )

            try:
                message = await self.bot.wait_for("message", check=check, timeout=remaining)
            except asyncio.TimeoutError:
                await channel.send("Sticky role DB decision timed out. No changes were made.")
                return

            if not await self._can_answer_sticky_db_prompt(message, guild, requester):
                continue

            command, _, argument = _parse_sticky_db_decision(message.content)
            if command == "remove" and not argument:
                config_removed, rows_removed = await self._sticky_roles.remove_sticky_role(
                    guild.id, role_id
                )
                await channel.send(
                    "Sticky role DB entry removed.\n"
                    f"Config row removed: {'yes' if config_removed else 'no'}\n"
                    f"Saved user-role rows removed: {rows_removed}"
                )
                return
            if command == "keep" and not argument:
                config_removed = await self._sticky_roles.unconfigure_sticky_role(
                    guild.id, role_id
                )
                await channel.send(
                    "Sticky role config removed, saved user-role rows kept.\n"
                    f"Config row removed: {'yes' if config_removed else 'no'}\n"
                    f"Saved user-role rows kept: {saved_rows}"
                )
                return
            if command == "change" and argument:
                await self._handle_sticky_role_db_change(
                    channel, guild, role_id, argument
                )
                return

            await channel.send(
                f"Invalid response. Use `remove {role_id}`, `keep {role_id}`, or "
                f"`change {role_id} <role mention or ID>`."
            )

    async def _can_answer_sticky_db_prompt(
        self,
        message: discord.Message,
        guild: discord.Guild,
        requester: discord.Member | discord.User | None,
    ) -> bool:
        if requester is not None:
            return message.author.id == requester.id

        member = message.author
        if not isinstance(member, discord.Member):
            member = guild.get_member(message.author.id)
        permissions = getattr(member, "guild_permissions", None)
        if permissions and permissions.manage_guild:
            return True
        return await self.bot.is_admin(message.author)

    async def _handle_sticky_role_db_change(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild,
        old_role_id: int,
        role_argument: str,
    ) -> None:
        if not role_argument:
            await channel.send("Missing replacement role. No changes were made.")
            return

        try:
            new_role_id = self._parse_role_id(role_argument)
        except commands.UserFeedbackCheckFailure as exc:
            await channel.send(f"{exc} No changes were made.")
            return

        if new_role_id == old_role_id:
            await channel.send("Replacement role is the same role ID. No changes were made.")
            return

        new_role = guild.get_role(new_role_id)
        if new_role is None:
            await channel.send("Replacement role does not exist on this server. No changes were made.")
            return
        if not self._can_restore_role(guild, new_role):
            await channel.send(
                "I cannot restore the replacement role. Check Manage Roles and role hierarchy. "
                "No changes were made."
            )
            return

        config_moved, old_rows_removed, new_rows_inserted = await self._sticky_roles.replace_sticky_role(
            guild.id, old_role_id, new_role_id
        )
        await channel.send(
            "Sticky role DB entry changed.\n"
            f"Replacement role: {new_role.mention} (`{new_role.id}`)\n"
            f"Config moved: {'yes' if config_moved else 'no'}\n"
            f"Old saved user-role rows removed: {old_rows_removed}\n"
            f"New saved user-role rows inserted: {new_rows_inserted}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_sticky_debug_log(self, guild: discord.Guild, content: str) -> None:
        config = await self.config.guild(guild).all()
        if not config["sticky_debug_logging_enabled"]:
            return

        channel = self._get_log_channel(guild, config["alert_channel"])
        if channel is None:
            return

        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Failed to send sticky role debug log to guild %s", guild.id)

    async def _send_paginated_text(self, ctx: commands.Context, content: str) -> None:
        page = ""
        for line in content.splitlines():
            candidate = f"{page}\n{line}" if page else line
            if len(candidate) > 1900:
                await ctx.send(page, allowed_mentions=discord.AllowedMentions.none())
                page = line
            else:
                page = candidate
        if page:
            await ctx.send(page, allowed_mentions=discord.AllowedMentions.none())

    async def _send_yapper_ranking(
        self,
        ctx: commands.Context,
        days: int,
        amount: int,
    ) -> None:
        await self._require_activity_staff(ctx)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Days must be at least 1.")
        if not 1 <= amount <= 20:
            raise commands.UserFeedbackCheckFailure(
                "Amount must be between 1 and 20."
            )

        await self._close_stale_activity_days_for_guild(ctx.guild, send_reports=True)
        days = await self._cap_detail_days(ctx.guild, days)
        end_date_utc = self._utc_today()
        counts = await self._activity_store.get_guild_user_counts(
            ctx.guild.id,
            end_date_utc,
            days,
            amount,
        )
        scope = "server"

        if not counts:
            await ctx.send(
                f"No retained activity data for this {scope} in the last {days} days.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        lines = [f"Top {len(counts)} yappers in this {scope} - last {days} days:"]
        for rank, count in enumerate(counts, start=1):
            member = ctx.guild.get_member(count.user_id)
            user = (
                f"{member.display_name} ({count.user_id})"
                if member is not None
                else str(count.user_id)
            )
            lines.append(f"{rank}. {user} — {count.message_count:,} messages")
        await self._send_paginated_text(ctx, "\n".join(lines))

    async def _confirm_retention_delete(self, ctx: commands.Context, warning: str) -> bool:
        await ctx.send(warning)

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
                and message.content == RETENTION_CONFIRMATION
            )

        try:
            await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await ctx.send("Retention change cancelled.")
            return False
        return True

    def _utc_today(self) -> date:
        return datetime.now(timezone.utc).date()

    def _history_retention_cutoff(self, days: int) -> date | None:
        if days == -1:
            return None
        if days == 0:
            return self._utc_today() + timedelta(days=1)
        return self._utc_today() - timedelta(days=days)

    async def _cap_detail_days(self, guild: discord.Guild, days: int) -> int:
        config = await self.config.guild(guild).all()
        retention = max(1, int(config["activity_detail_retention_days"]))
        return min(days, retention)

    def _parse_range_days(self, value: str) -> int:
        normalized = value.strip().lower()
        if not normalized.isdigit():
            raise commands.UserFeedbackCheckFailure("Range must be a positive number of days.")
        days = int(normalized)
        if days < 1:
            raise commands.UserFeedbackCheckFailure("Range must be at least 1 day.")
        return days

    def _parse_user_id(self, value: str) -> int:
        stripped = value.strip()
        if stripped.startswith("<@") and stripped.endswith(">"):
            stripped = stripped[2:-1]
            if stripped.startswith("!"):
                stripped = stripped[1:]
        if not stripped.isdigit():
            raise commands.UserFeedbackCheckFailure("Pass a user mention or raw Discord user ID.")
        return int(stripped)

    def _resolve_text_channel_or_thread(
        self, guild: discord.Guild, value: str
    ) -> discord.TextChannel | discord.Thread:
        stripped = value.strip()
        if stripped.startswith("<#") and stripped.endswith(">"):
            stripped = stripped[2:-1]
        if not stripped.isdigit():
            raise commands.UserFeedbackCheckFailure("Pass a channel/thread mention or raw channel ID.")

        channel_id = int(stripped)
        channel = guild.get_channel_or_thread(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        raise commands.UserFeedbackCheckFailure("Channel or thread was not found in this server.")

    def _resolve_chatchart_request(
        self,
        ctx: commands.Context,
        target_or_days: str,
        days_or_amount: int | None,
        amount: int | None,
    ) -> tuple[discord.TextChannel | discord.Thread, int, int]:
        token = str(target_or_days).strip()
        resolved_channel = None
        if token.isdigit() and len(token) >= DISCORD_SNOWFLAKE_MIN_DIGITS:
            resolved_channel = ctx.guild.get_channel_or_thread(int(token))

        is_channel_reference = (
            token.startswith("<#")
            or (token.isdigit() and len(token) >= DISCORD_SNOWFLAKE_MIN_DIGITS)
            or isinstance(resolved_channel, (discord.TextChannel, discord.Thread))
        )
        if is_channel_reference:
            target = self._resolve_text_channel_or_thread(ctx.guild, token)
            if days_or_amount is None:
                raise commands.UserFeedbackCheckFailure(
                    "Days must follow the channel or thread."
                )
            return (
                target,
                days_or_amount,
                amount if amount is not None else DEFAULT_CHATCHART_USER_COUNT,
            )

        if not token.isdigit():
            raise commands.UserFeedbackCheckFailure(
                "Pass a channel/thread mention, raw channel ID, or number of days."
            )
        if amount is not None:
            raise commands.UserFeedbackCheckFailure(
                "Too many arguments for current-channel chatchart."
            )
        return (
            ctx.channel,
            int(token),
            days_or_amount if days_or_amount is not None else DEFAULT_CHATCHART_USER_COUNT,
        )

    def _activity_parent_channel_id(self, channel: object) -> int:
        parent = getattr(channel, "parent", None)
        if isinstance(channel, discord.Thread) and parent is not None:
            return parent.id
        return channel.id

    def _activity_thread_id(self, channel: object) -> int | None:
        if isinstance(channel, discord.Thread):
            return channel.id
        return None

    def _format_channel(self, channel_id: int) -> str:
        return f"<#{channel_id}>"

    def _format_activity_location(
        self, guild: discord.Guild, channel_id: int, thread_id: int | None
    ) -> str:
        if thread_id is None:
            return self._format_channel(channel_id)
        return f"{self._format_channel(channel_id)} / {self._format_channel(thread_id)}"

    def _format_user_reference(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        if member is not None:
            return f"{member.display_name} ({user_id})"
        return f"<@{user_id}> ({user_id})"

    def _format_int(self, value: int) -> str:
        return f"{value:,}"

    def _format_percent_of_server(self, active_users: int, member_count: int) -> str:
        if member_count <= 0:
            return "n/d"
        return f"{(active_users / member_count) * 100:.1f}%"

    def _format_top_channels(self, top_channels: list[TopChannel]) -> str:
        if not top_channels:
            return "n/d"
        return "\n".join(
            (
                f"{top.rank}. {self._format_channel(top.channel_id)} - "
                f"{self._format_int(top.message_count)} messages"
            )
            for top in top_channels
        )

    def _format_activity_locations(
        self, guild: discord.Guild, locations: list[ActivityLocation], total_messages: int
    ) -> str:
        if not locations:
            return "n/d"
        lines: list[str] = []
        listed_total = 0
        for location in locations:
            listed_total += location.message_count
            percent = (
                (location.message_count / total_messages) * 100.0
                if total_messages
                else 0.0
            )
            lines.append(
                f"{location.rank}. "
                f"{self._format_activity_location(guild, location.channel_id, location.thread_id)} - "
                f"{self._format_int(location.message_count)} ({percent:.1f}%)"
            )
        other_count = total_messages - listed_total
        if other_count > 0:
            percent = (other_count / total_messages) * 100.0 if total_messages else 0.0
            lines.append(f"Other - {self._format_int(other_count)} ({percent:.1f}%)")
        return "\n".join(lines)

    def _build_daily_summary_embed(
        self, summary: DailySummary, title_prefix: str
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{title_prefix} activity summary - {summary.date_utc.isoformat()} UTC",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Messages",
            value=self._format_int(summary.total_messages),
            inline=True,
        )
        embed.add_field(
            name="Active users",
            value=(
                f"{self._format_int(summary.active_users)} "
                f"({self._format_percent_of_server(summary.active_users, summary.member_count_at_close)})"
            ),
            inline=True,
        )
        embed.add_field(
            name="Thresholds",
            value=(
                f"10+: {self._format_int(summary.users_10_plus)}\n"
                f"50+: {self._format_int(summary.users_50_plus)}\n"
                f"100+: {self._format_int(summary.users_100_plus)}"
            ),
            inline=True,
        )
        peak_hour = self._format_peak_hour(summary)
        embed.add_field(
            name="Channels",
            value=(
                f"Active: {self._format_int(summary.channels_with_activity)}\n"
                f"Peak hour: {peak_hour}\n"
                f"Avg/user: {summary.messages_per_active_user:.1f}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Top channels",
            value=self._format_top_channels(summary.top_channels),
            inline=False,
        )
        return embed

    def _format_peak_hour(self, summary: DailySummary) -> str:
        if summary.peak_hour_utc is None:
            return "n/d"
        peak_time = datetime(
            summary.date_utc.year,
            summary.date_utc.month,
            summary.date_utc.day,
            summary.peak_hour_utc,
            tzinfo=timezone.utc,
        )
        return f"<t:{int(peak_time.timestamp())}:t>"

    def _build_timeline_embed(
        self, timeline: list[TimelineDay], top_channels: list[TopChannel], days: int
    ) -> discord.Embed:
        include_percent = days <= 7
        header = "Date       Msgs  Users  %Srv  10+ 50+ 100+" if include_percent else "Date       Msgs  Users  10+ 50+ 100+"
        lines = [header]
        summaries: list[DailySummary] = []
        for day in timeline:
            summary = day.summary
            if summary is None:
                if include_percent:
                    lines.append(f"{day.date_utc.isoformat()} n/d   n/d    n/d   n/d n/d n/d")
                else:
                    lines.append(f"{day.date_utc.isoformat()} n/d   n/d    n/d n/d n/d")
                continue
            summaries.append(summary)
            if include_percent:
                lines.append(
                    f"{day.date_utc.isoformat()} "
                    f"{summary.total_messages:<5} {summary.active_users:<6} "
                    f"{self._format_percent_of_server(summary.active_users, summary.member_count_at_close):<5} "
                    f"{summary.users_10_plus:<3} {summary.users_50_plus:<3} {summary.users_100_plus:<4}"
                )
            else:
                lines.append(
                    f"{day.date_utc.isoformat()} "
                    f"{summary.total_messages:<5} {summary.active_users:<6} "
                    f"{summary.users_10_plus:<3} {summary.users_50_plus:<3} {summary.users_100_plus:<4}"
                )

        table = "\n".join(lines)
        if len(table) > 3900:
            visible_lines = lines[:120]
            visible_lines.append("...")
            table = "\n".join(visible_lines)

        embed = discord.Embed(
            title=f"Activity timeline - last {days} closed days",
            color=discord.Color.blue(),
            description=f"```text\n{table}\n```",
        )
        if summaries:
            avg_messages = sum(summary.total_messages for summary in summaries) / len(summaries)
            avg_users = sum(summary.active_users for summary in summaries) / len(summaries)
            best = max(summaries, key=lambda summary: summary.total_messages)
            embed.add_field(
                name="Range",
                value=(
                    f"Avg/day: {avg_messages:.0f} msgs\n"
                    f"Avg active users: {avg_users:.0f}\n"
                    f"Best day: {best.date_utc.isoformat()} "
                    f"({self._format_int(best.total_messages)} msgs)"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="Range", value="n/d", inline=False)
        embed.add_field(
            name="Top channels in range",
            value=self._format_top_channels(top_channels),
            inline=False,
        )
        return embed

    def _build_channel_timeline_embed(
        self, channel: discord.TextChannel, timeline: list[ChannelTimelineDay], days: int
    ) -> discord.Embed:
        lines = ["Date       Msgs"]
        numeric_counts: list[int] = []
        for day in timeline:
            if day.message_count is None:
                value = "n/d"
            else:
                numeric_counts.append(day.message_count)
                value = str(day.message_count)
            lines.append(f"{day.date_utc.isoformat()} {value}")

        table = "\n".join(lines)
        if len(table) > 3900:
            visible_lines = lines[:120]
            visible_lines.append("...")
            table = "\n".join(visible_lines)

        embed = discord.Embed(
            title=f"Channel activity - {channel.name} - last {days} days",
            color=discord.Color.blue(),
            description=f"```text\n{table}\n```",
        )
        if numeric_counts:
            total = sum(numeric_counts)
            active_days = sum(1 for value in numeric_counts if value > 0)
            embed.add_field(name="Total messages", value=self._format_int(total), inline=True)
            embed.add_field(name="Active days", value=self._format_int(active_days), inline=True)
            embed.add_field(
                name="Average per active day",
                value=f"{(total / active_days) if active_days else 0.0:.1f}",
                inline=True,
            )
        else:
            embed.add_field(name="Total messages", value="n/d", inline=True)
        return embed

    def _build_activity_consistency_embed(
        self, report: ActivityConsistencyReport, day: date
    ) -> discord.Embed:
        ok = report.user_day_mismatches == 0 and report.channel_day_mismatches == 0
        embed = discord.Embed(
            title=f"Activity consistency - {day.isoformat()} UTC",
            color=discord.Color.green() if ok else discord.Color.red(),
        )
        embed.add_field(
            name="Canonical rows",
            value=self._format_int(report.canonical_rows),
            inline=True,
        )
        embed.add_field(
            name="Canonical messages",
            value=self._format_int(report.canonical_messages),
            inline=True,
        )
        embed.add_field(
            name="User cache mismatches",
            value=self._format_int(report.user_day_mismatches),
            inline=True,
        )
        embed.add_field(
            name="Channel cache mismatches",
            value=self._format_int(report.channel_day_mismatches),
            inline=True,
        )
        embed.add_field(name="Status", value="OK" if ok else "Mismatch detected", inline=False)
        return embed

    def _build_activity_database_stats_embed(
        self, stats: ActivityDatabaseStats
    ) -> discord.Embed:
        file_mib = stats.file_size_bytes / 1024 / 1024
        sqlite_mib = stats.sqlite_size_bytes / 1024 / 1024
        lines = [f"{name} {self._format_int(count)}" for name, count in stats.table_rows]
        table = "\n".join(lines)
        if len(table) > 1000:
            table = "\n".join(lines[:18] + ["..."])

        embed = discord.Embed(title="Activity database size", color=discord.Color.blue())
        embed.add_field(
            name="File size",
            value=f"{self._format_int(stats.file_size_bytes)} bytes ({file_mib:.2f} MiB)",
            inline=False,
        )
        embed.add_field(
            name="SQLite pages",
            value=(
                f"{self._format_int(stats.page_count)} pages x "
                f"{self._format_int(stats.page_size)} bytes = {sqlite_mib:.2f} MiB"
            ),
            inline=False,
        )
        embed.add_field(name="Rows", value=f"```text\n{table}\n```", inline=False)
        embed.set_footer(text=stats.path)
        return embed

    def _build_user_stats_embed(self, title: str, stats: UserStats, days: int) -> discord.Embed:
        embed = discord.Embed(title=title, color=discord.Color.blue())
        embed.add_field(name="Range", value=f"last {days} days", inline=True)
        embed.add_field(name="Total messages", value=self._format_int(stats.total_messages), inline=True)
        embed.add_field(name="Active days", value=self._format_int(stats.active_days), inline=True)
        embed.add_field(
            name="Average per active day",
            value=f"{stats.average_per_active_day:.1f}",
            inline=True,
        )
        embed.add_field(
            name="Top channels",
            value=self._format_top_channels(stats.top_channels),
            inline=False,
        )
        embed.add_field(
            name="Daily breakdown",
            value=f"```text\n{self._format_daily_rows(stats.date_rows)}\n```",
            inline=False,
        )
        return embed

    def _build_user_channel_stats_embed(
        self, title: str, stats: UserStats, days: int
    ) -> discord.Embed:
        embed = discord.Embed(title=title, color=discord.Color.blue())
        embed.add_field(name="Range", value=f"last {days} days", inline=True)
        embed.add_field(name="Total messages", value=self._format_int(stats.total_messages), inline=True)
        embed.add_field(name="Active days", value=self._format_int(stats.active_days), inline=True)
        embed.add_field(
            name="Average per active day",
            value=f"{stats.average_per_active_day:.1f}",
            inline=True,
        )
        embed.add_field(
            name="Daily breakdown",
            value=f"```text\n{self._format_daily_rows(stats.date_rows)}\n```",
            inline=False,
        )
        return embed

    def _build_user_channel_distribution_embed(
        self,
        guild: discord.Guild,
        title: str,
        distribution: UserChannelDistribution,
        days: int,
    ) -> discord.Embed:
        embed = discord.Embed(title=title, color=discord.Color.blue())
        embed.add_field(name="Range", value=f"last {days} days", inline=True)
        embed.add_field(
            name="Total messages",
            value=self._format_int(distribution.total_messages),
            inline=True,
        )
        embed.add_field(
            name="Active days",
            value=self._format_int(distribution.active_days),
            inline=True,
        )
        embed.add_field(
            name="Locations used",
            value=self._format_int(distribution.locations_used),
            inline=True,
        )
        top_location = distribution.top_locations[0] if distribution.top_locations else None
        embed.add_field(
            name="Top location",
            value=(
                self._format_activity_location(guild, top_location.channel_id, top_location.thread_id)
                if top_location
                else "n/d"
            ),
            inline=True,
        )
        embed.add_field(
            name="Top locations in range",
            value=self._format_activity_locations(
                guild, distribution.top_locations, distribution.total_messages
            ),
            inline=False,
        )
        embed.add_field(
            name="Daily dominant location",
            value=self._format_daily_dominant_location_rows(guild, distribution.date_rows),
            inline=False,
        )
        return embed

    def _build_selfchart_embed(
        self, member: discord.Member | discord.User, stats: UserStats, days: int
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"Your activity - last {days} days",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Total messages", value=self._format_int(stats.total_messages), inline=True)
        top_channel = stats.top_channels[0] if stats.top_channels else None
        embed.add_field(
            name="Top channel",
            value=(
                f"{self._format_channel(top_channel.channel_id)} - "
                f"{self._format_int(top_channel.message_count)} messages"
                if top_channel
                else "n/d"
            ),
            inline=True,
        )
        embed.add_field(
            name="Daily messages",
            value=f"```text\n{self._format_daily_rows(stats.date_rows)}\n```",
            inline=False,
        )
        return embed

    def _format_daily_rows(self, rows: list[tuple[date, int | None]]) -> str:
        lines = ["Date       Msgs"]
        for day, count in rows:
            value = "n/d" if count is None else str(count)
            lines.append(f"{day.isoformat()} {value}")
        return "\n".join(lines)

    def _format_daily_dominant_location_rows(
        self, guild: discord.Guild, rows: list[DailyDominantLocation]
    ) -> str:
        lines: list[str] = []
        for row in rows:
            if row.total_messages is None:
                lines.append(f"{row.date_utc.isoformat()}: n/d")
            elif row.total_messages == 0:
                lines.append(f"{row.date_utc.isoformat()}: 0")
            elif row.channel_id is not None and row.location_messages is not None:
                percent = (row.location_messages / row.total_messages) * 100.0
                lines.append(
                    f"{row.date_utc.isoformat()}: "
                    f"{self._format_int(row.total_messages)} msgs, "
                    f"{self._format_activity_location(guild, row.channel_id, row.thread_id)} "
                    f"{self._format_int(row.location_messages)} ({percent:.1f}%)"
                )
            else:
                lines.append(f"{row.date_utc.isoformat()}: {self._format_int(row.total_messages)} msgs")
        return self._join_limited_lines(lines)

    def _join_limited_lines(self, lines: list[str], limit: int = 1000) -> str:
        output: list[str] = []
        current_length = 0
        for line in lines:
            extra_length = len(line) + (1 if output else 0)
            if current_length + extra_length > limit:
                output.append("...")
                break
            output.append(line)
            current_length += extra_length
        return "\n".join(output) if output else "n/d"

    def _chatchart_location_label(self, channel: object) -> str:
        """Name the charted channel or thread for display inside the image."""
        name = getattr(channel, "name", None) or "unknown-channel"
        if self._activity_thread_id(channel) is None:
            return f"#{name}"
        parent_name = getattr(getattr(channel, "parent", None), "name", None)
        return f"#{parent_name} / {name}" if parent_name else name

    def _draw_chatchart_donut(
        self,
        donut_axis,
        values: list[int],
        bar_colors: list[str],
        other_count: int,
        total_count: int,
    ) -> None:
        donut_values = list(values)
        donut_colors = list(bar_colors)
        if other_count:
            donut_values.append(other_count)
            donut_colors.append(CHATCHART_OTHER_COLOR)
        donut_labels = [""] * len(donut_values)
        if other_count:
            donut_labels[-1] = "Other"
        _wedges, outside_labels, _percentages = donut_axis.pie(
            donut_values,
            labels=donut_labels,
            colors=donut_colors,
            autopct=lambda percent: f"{percent:.0f}%" if percent >= 6 else "",
            pctdistance=0.79,
            labeldistance=1.08,
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 0.38, "edgecolor": "white", "linewidth": 2},
            textprops={"color": "white", "fontsize": 10, "fontweight": "bold"},
        )
        for outside_label in outside_labels:
            outside_label.set_color("#52514e")
            outside_label.set_fontweight("normal")
        donut_axis.text(
            0,
            0,
            f"{total_count:,}\nmessages",
            ha="center",
            va="center",
            fontsize=11,
        )
        donut_axis.set_title("Share by user", pad=12)
        donut_axis.axis("equal")

    def _build_chatchart_file(
        self,
        guild: discord.Guild,
        counts: list[ChannelUserCount],
        days: int,
        location_label: str,
        amount: int,
    ) -> discord.File:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise commands.UserFeedbackCheckFailure(
                "Matplotlib is required for chatchart but is not installed."
            ) from exc

        top_counts = counts[:amount]
        top_count = sum(count.message_count for count in top_counts)
        other_count = sum(count.message_count for count in counts[len(top_counts):])
        total_count = top_count + other_count
        labels: list[str] = []
        values: list[int] = []
        for count in top_counts:
            member = guild.get_member(count.user_id)
            name = member.display_name if member is not None else str(count.user_id)
            if len(name) > 32:
                name = f"{name[:29]}..."
            labels.append(name)
            values.append(count.message_count)

        bar_colors = list(CHATCHART_SERIES_COLORS[: len(values)])

        figure_height = max(5.5, 1.5 + len(top_counts) * 0.5)
        figure = plt.figure(figsize=(13, figure_height))
        grid = figure.add_gridspec(1, 2, width_ratios=(3, 1.35), wspace=0.02)
        ranking_axis = figure.add_subplot(grid[0, 0])
        donut_axis = figure.add_subplot(grid[0, 1])

        positions = list(range(len(values)))
        ranking_axis.barh(positions, values, color=bar_colors, height=0.68)
        ranking_axis.set_yticks(positions, labels=labels)
        ranking_axis.invert_yaxis()
        ranking_axis.xaxis.set_visible(False)
        ranking_axis.tick_params(axis="y", length=0)
        for spine in ranking_axis.spines.values():
            spine.set_visible(False)

        largest_value = max(values)
        ranking_axis.set_xlim(0, largest_value * 1.24)
        for position, value in zip(positions, values, strict=True):
            percentage = value / total_count * 100
            ranking_axis.text(
                value + largest_value * 0.025,
                position,
                f"{value:,} · {percentage:.1f}%",
                va="center",
                fontsize=9,
            )

        self._draw_chatchart_donut(
            donut_axis, values, bar_colors, other_count, total_count
        )

        title_y = 0.97
        figure.suptitle(
            f"Messages by user - last {days} days",
            fontsize=16,
            y=title_y,
            va="center",
        )
        figure.text(
            0.008,
            title_y,
            location_label,
            ha="left",
            va="center",
            fontsize=12,
            color="#52514e",
        )
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", bbox_inches="tight", dpi=160)
        plt.close(figure)
        buffer.seek(0)
        return discord.File(buffer, filename="chatchart.png")
