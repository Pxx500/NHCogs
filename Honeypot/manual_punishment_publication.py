"""Private audit and public notification publication for manual punishments."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import discord

DISCORD_MESSAGE_LIMIT = 2_000
MAX_REASON_LENGTH = 500


@dataclass
class PrivateAudit:
    primary: Any
    parts: tuple[Any, ...]
    content_external: bool
    attachment_failures: tuple[str, ...]
    channel: Any | None = None
    text_parts: tuple[Any, ...] = ()


@dataclass(frozen=True)
class PublicNotificationResult:
    channel_id: int | None
    status: str


@dataclass(frozen=True)
class PunishmentOutcome:
    kind: str
    status: str
    detail: str
    role_id: int | None = None
    role_name: str | None = None
    notification_channel_id: int | None = None
    duration_label: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @classmethod
    def role_succeeded(
        cls,
        role_id: int,
        role_name: str,
        *,
        notification_channel_id: int | None,
    ) -> PunishmentOutcome:
        return cls(
            "role",
            "succeeded",
            f"Role n’t {role_name}: Applied",
            role_id=role_id,
            role_name=role_name,
            notification_channel_id=notification_channel_id,
        )
    @classmethod
    def mute_succeeded(cls, duration_label: str) -> PunishmentOutcome:
        return cls(
            "mute",
            "succeeded",
            f"Mute: Applied for {duration_label}",
            duration_label=duration_label,
        )

    @classmethod
    def succeeded_action(cls, kind: str, detail: str) -> PunishmentOutcome:
        return cls(kind, "succeeded", detail)

    @classmethod
    def failed(cls, kind: str, detail: str) -> PunishmentOutcome:
        return cls(kind, "failed", detail)

    @classmethod
    def planned(
        cls,
        kind: str,
        detail: str,
        *,
        role_id: int | None = None,
        role_name: str | None = None,
        notification_channel_id: int | None = None,
        duration_label: str | None = None,
    ) -> PunishmentOutcome:
        return cls(
            kind,
            "planned",
            detail,
            role_id=role_id,
            role_name=role_name,
            notification_channel_id=notification_channel_id,
            duration_label=duration_label,
        )

    @classmethod
    def already_applied(
        cls,
        role_id: int,
        role_name: str,
        *,
        notification_channel_id: int | None,
    ) -> PunishmentOutcome:
        return cls(
            "role",
            "already_applied",
            f"Role n’t {role_name}: Already applied",
            role_id=role_id,
            role_name=role_name,
            notification_channel_id=notification_channel_id,
        )


def _content_chunks(content: str) -> tuple[str, ...]:
    chunks = []
    remaining = content
    while len(remaining) > DISCORD_MESSAGE_LIMIT:
        split_at = remaining.rfind("\n", 0, DISCORD_MESSAGE_LIMIT + 1)
        if split_at <= 0:
            split_at = DISCORD_MESSAGE_LIMIT
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    chunks.append(remaining)
    return tuple(chunks)


def _selection_labels(selection: Any, role_names: tuple[str, ...]) -> list[str]:
    labels = [f"Role n’t {name}" for name in role_names]
    action = selection.member_action.value
    if action != "none":
        labels.append(action.title())
    return labels


def _audit_reserve(selection: Any, role_names: tuple[str, ...]) -> int:
    action_count = len(role_names) + int(selection.member_action.value != "none")
    return MAX_REASON_LENGTH + action_count * 180 + 500


def _render_private_audit(
    source_message: Any,
    moderator: Any,
    selection: Any,
    *,
    reason: str | None,
    mute_duration_label: str | None,
    role_names: tuple[str, ...],
    source_deletion: str,
    content_override: str | None = None,
    attachment_failures: tuple[str, ...] = (),
    outcomes: tuple[PunishmentOutcome, ...] = (),
    notification_result: PublicNotificationResult | None = None,
) -> str:
    labels = _selection_labels(selection, role_names)
    selected = ", ".join(labels) if labels else "None"
    timestamp = int(source_message.created_at.timestamp())
    evidence_status = "Saved" if selection.capture_evidence else "Not saved"
    lines = [
        "**Manual punishment**",
        f"Author: {source_message.author.display_name} ({source_message.author.id})",
        f"Moderator: {moderator.display_name} ({moderator.id})",
        f"Source: {source_message.channel.mention} ({source_message.channel.id})",
        f"Created: <t:{timestamp}:F>",
        f"Source message ID: {source_message.id}",
        f"Evidence: {evidence_status}",
        f"Selected punishments: {selected}",
    ]
    if reason is not None:
        lines.append(f"Reason: {reason}")
    if mute_duration_label is not None:
        lines.append(f"Mute duration: {mute_duration_label}")
    lines.append(f"Source deletion: {source_deletion}")
    if attachment_failures:
        lines.append(
            "Attachment capture failures: " + ", ".join(attachment_failures)
        )
    if outcomes:
        lines.extend(("", "**Action results**"))
        lines.extend(outcome.detail for outcome in outcomes)
    if notification_result is not None:
        lines.append(
            "Public notification: "
            f"{notification_result.status}"
            + (
                f" in channel {notification_result.channel_id}"
                if notification_result.channel_id is not None
                else ""
            )
        )
    if selection.capture_evidence:
        content = content_override or source_message.content or "[No text content]"
        lines.extend(("", "**Content**", content))
    return "\n".join(lines)


async def create_private_audit(
    evidence_channel: Any,
    source_message: Any,
    moderator: Any,
    selection: Any,
    *,
    reason: str | None,
    mute_duration_label: str | None,
    role_names: tuple[str, ...] = (),
) -> PrivateAudit:
    rendered = _render_private_audit(
        source_message,
        moderator,
        selection,
        reason=reason,
        mute_duration_label=mute_duration_label,
        role_names=role_names,
        source_deletion="Pending",
    )
    content_external = (
        selection.capture_evidence
        and len(rendered) + _audit_reserve(selection, role_names)
        > DISCORD_MESSAGE_LIMIT
    )
    files = []
    if content_external:
        files.append(
            discord.File(
                io.BytesIO(source_message.content.encode("utf-8")),
                filename="message.txt",
            )
        )
        rendered = _render_private_audit(
            source_message,
            moderator,
            selection,
            reason=reason,
            mute_duration_label=mute_duration_label,
            role_names=role_names,
            source_deletion="Pending",
            content_override="[Stored in message.txt]",
        )
    published = []
    text_parts = []
    evidence_parts = []
    attachment_failures = []
    try:
        rendered_chunks = _content_chunks(rendered)
        primary = await evidence_channel.send(
            content=rendered_chunks[0],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        published.append(primary)
        for chunk in rendered_chunks[1:]:
            part = await evidence_channel.send(
                content=chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            text_parts.append(part)
            published.append(part)
        if selection.capture_evidence:
            for attachment in source_message.attachments:
                try:
                    files.append(await attachment.to_file(use_cached=True))
                except (discord.HTTPException, OSError):
                    attachment_failures.append(
                        getattr(attachment, "filename", "unknown attachment")
                    )
        for start in range(0, len(files), 10):
            part = await evidence_channel.send(
                content=f"Evidence files for source message {source_message.id}.",
                files=files[start : start + 10],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            evidence_parts.append(part)
            published.append(part)
    except (discord.HTTPException, OSError):
        for message in reversed(published):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        raise
    return PrivateAudit(
        primary=primary,
        parts=tuple(evidence_parts),
        content_external=content_external,
        attachment_failures=tuple(attachment_failures),
        channel=evidence_channel,
        text_parts=tuple(text_parts),
    )


async def finalize_private_audit(
    audit: PrivateAudit,
    source_message: Any,
    moderator: Any,
    selection: Any,
    *,
    reason: str | None,
    mute_duration_label: str | None,
    role_names: tuple[str, ...],
    source_deletion: str,
    outcomes: tuple[PunishmentOutcome, ...],
    notification_result: PublicNotificationResult | None,
) -> None:
    rendered = _render_private_audit(
            source_message,
            moderator,
            selection,
            reason=reason,
            mute_duration_label=mute_duration_label,
            role_names=role_names,
            source_deletion=source_deletion,
            content_override="[Stored in message.txt]" if audit.content_external else None,
            attachment_failures=audit.attachment_failures,
            outcomes=outcomes,
            notification_result=notification_result,
    )
    chunks = _content_chunks(rendered)
    messages = [audit.primary, *audit.text_parts]
    for index, chunk in enumerate(chunks):
        if index < len(messages):
            await messages[index].edit(
                content=chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            continue
        if audit.channel is None:
            raise RuntimeError("Private audit channel is unavailable")
        message = await audit.channel.send(
            content=chunk,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        messages.append(message)
    for message in messages[len(chunks) :]:
        await message.delete()
    audit.text_parts = tuple(messages[1 : len(chunks)])


def _safe_role_name(name: str) -> str:
    return name.replace("@", "@\u200b")


def _joined_role_names(outcomes: list[PunishmentOutcome]) -> str:
    return " and ".join(
        _safe_role_name(outcome.role_name or "Role n’t") for outcome in outcomes
    )


def _public_content(
    target: Any,
    moderator: Any,
    outcomes: tuple[PunishmentOutcome, ...],
    reason: str,
) -> str | None:
    successful = [outcome for outcome in outcomes if outcome.succeeded]
    if not successful:
        return None
    roles = [outcome for outcome in successful if outcome.kind == "role"]
    mute = next((outcome for outcome in successful if outcome.kind == "mute"), None)
    kick = next((outcome for outcome in successful if outcome.kind == "kick"), None)
    ban = next((outcome for outcome in successful if outcome.kind == "ban"), None)
    def render(role_label: str | None) -> str | None:
        if ban is not None:
            sentence = f"<@{target.id}> was banned by <@{moderator.id}>."
        elif kick is not None:
            sentence = f"<@{target.id}> was kicked by <@{moderator.id}>."
        elif role_label is not None and mute is not None:
            sentence = (
                f"<@{target.id}> received {role_label} and was muted "
                f"by <@{moderator.id}> for {mute.duration_label}."
            )
        elif role_label is not None:
            sentence = (
                f"<@{target.id}> received {role_label} from <@{moderator.id}>."
            )
        elif mute is not None:
            sentence = (
                f"<@{target.id}> was muted by <@{moderator.id}> for "
                f"{mute.duration_label}."
            )
        else:
            return None
        return f"{sentence}\nReason: {reason}"

    content = render(_joined_role_names(roles) if roles else None)
    if content is None or len(content) <= DISCORD_MESSAGE_LIMIT:
        return content
    role_count = len(roles)
    compact_roles = f"{role_count} Role n’t {'role' if role_count == 1 else 'roles'}"
    return render(compact_roles)


def _public_destination(
    guild: Any,
    source_channel: Any,
    outcomes: tuple[PunishmentOutcome, ...],
) -> tuple[Any | None, int | None]:
    successful_roles = [
        outcome
        for outcome in outcomes
        if outcome.succeeded and outcome.kind == "role"
    ]
    if not successful_roles:
        return source_channel, source_channel.id
    if any(
        outcome.notification_channel_id in (None, source_channel.id)
        for outcome in successful_roles
    ):
        return source_channel, source_channel.id
    destination_id = successful_roles[0].notification_channel_id
    return guild.get_channel(destination_id), destination_id


async def publish_public_result(
    guild: Any,
    source_channel: Any,
    target: Any,
    moderator: Any,
    outcomes: tuple[PunishmentOutcome, ...],
    *,
    reason: str,
) -> PublicNotificationResult | None:
    content = _public_content(target, moderator, outcomes, reason)
    if content is None:
        return None
    destination, destination_id = _public_destination(
        guild, source_channel, outcomes
    )
    if destination is None:
        return PublicNotificationResult(
            destination_id,
            "failed because the configured channel is unavailable",
        )
    try:
        await destination.send(
            content,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[target, moderator],
                replied_user=False,
            ),
        )
    except discord.HTTPException:
        return PublicNotificationResult(destination_id, "failed")
    return PublicNotificationResult(destination_id, "sent")
