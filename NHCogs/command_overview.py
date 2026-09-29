from __future__ import annotations

import typing

import discord
from redbot.core import commands

Translate = typing.Callable[[str], str]
ConfigurationSender = typing.Callable[[], typing.Awaitable[None]]
MAX_FIELD_VALUE_LENGTH = 1024


def channel_is_private(guild: typing.Any, channel: typing.Any) -> bool:
    default_role = getattr(guild, "default_role", None)
    permissions_for = getattr(channel, "permissions_for", None)
    if default_role is None or not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(default_role)
    except (AttributeError, TypeError):
        return False
    return not bool(getattr(permissions, "view_channel", True))


def group_overview_is_private(ctx: commands.Context) -> bool:
    return channel_is_private(
        getattr(ctx, "guild", None),
        getattr(ctx, "channel", None),
    )


def _is_hidden(command: typing.Any) -> bool:
    return bool(getattr(command, "hidden", False))


def _visible_children(command: typing.Any) -> list[typing.Any]:
    return [
        child
        for child in getattr(command, "commands", ())
        if not _is_hidden(child)
    ]


def public_usage(command: typing.Any) -> str:
    """Usage mods should see.

    ``usage`` is the public form. A nullable setting documents
    ``[channel|clear]`` there even when the callback parameter is only named
    ``channel``. Hidden ``set`` aliases are not part of that form.
    """
    usage = getattr(command, "usage", None)
    if isinstance(usage, str) and usage.strip():
        return usage.strip()
    signature = getattr(command, "signature", "") or ""
    return signature.strip() if isinstance(signature, str) else ""


def descendant_leaf_commands(parent: commands.Group) -> typing.Iterator[typing.Any]:
    """Visible command lines under a nested group.

    A group with a usage string is itself a public command. That keeps a
    nullable value on one leaf: omit the argument to show it, pass the value
    to store it, and pass ``clear`` to remove it. A hidden ``set`` child can
    keep the old longer path working without appearing here. Visible leaves
    such as ``reset`` are still listed. An argument-free group is only a
    category, so the overview lists its visible descendants instead.

    Toggles stay a separate nested command. Collections stay add/remove
    (and rename where it exists) under an argument-free group. A scalar that
    is only assigned stays a required-argument leaf; its parent overview is
    the show screen. A show-or-set leaf stays optional when checking that one
    setting is the normal action.
    """
    for child in _visible_children(parent):
        usage = public_usage(child)
        visible_descendants = _visible_children(child)
        if usage:
            yield child
        if visible_descendants:
            yield from descendant_leaf_commands(child)
        elif not usage:
            yield child


def overview_embeds(
    title: str,
    description: str,
    fields: list[tuple[str, str]],
) -> list[discord.Embed]:
    max_fields = 25
    max_embed_text = 6000
    embeds: list[discord.Embed] = []
    field_index = 0
    while field_index < len(fields) or not embeds:
        embed = discord.Embed(
            title=title,
            description=description if not embeds else None,
        )
        text_length = len(title) + (len(description) if not embeds else 0)
        field_count = 0
        while field_index < len(fields):
            field_name, field_value = fields[field_index]
            field_length = len(field_name) + len(field_value)
            if field_count and (
                field_count >= max_fields
                or text_length + field_length > max_embed_text
            ):
                break
            embed.add_field(
                name=field_name,
                value=field_value,
                inline=False,
            )
            field_index += 1
            field_count += 1
            text_length += field_length
        embeds.append(embed)
    return embeds


async def send_group_overview(
    ctx: commands.Context,
    config_sender: ConfigurationSender | None = None,
    *,
    include_descendants: bool = True,
    title: str | None = None,
    translate: Translate = lambda text: text,
) -> None:
    private = group_overview_is_private(ctx)
    if private and config_sender is not None:
        await config_sender()

    command = ctx.command
    overview_title = title or command.name.replace("_", " ").title()
    description = getattr(command, "short_doc", "") or ""
    if not include_descendants:
        description = (
            f"{description}\n\n"
            + translate("Run a category below to see its complete command list")
        )
    fields: list[tuple[str, str]] = []
    if not private and config_sender is not None:
        fields.append(
            (
                translate("Current configuration"),
                translate(
                    "Current values are hidden in channels visible to regular members\n"
                    "Run this command in a private moderator channel to view them"
                ),
            )
        )

    children = (
        descendant_leaf_commands(command)
        if include_descendants
        else iter(getattr(command, "commands", ()))
    )
    command_lines = []
    for child in children:
        if getattr(child, "hidden", False):
            continue
        signature = public_usage(child)
        short_doc = getattr(child, "short_doc", "") or ""
        usage = f"{ctx.clean_prefix}{child.qualified_name}"
        if isinstance(signature, str) and signature.strip():
            usage = f"{usage} {signature.strip()}"
        command_lines.append(f"`{usage}` - {short_doc}")

    chunks: list[str] = []
    current: list[str] = []
    for line in command_lines:
        candidate = "\n".join((*current, line))
        if current and len(candidate) > MAX_FIELD_VALUE_LENGTH:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))

    for index, chunk in enumerate(chunks):
        fields.append(
            (
                translate("Commands")
                if index == 0
                else translate("Commands (continued)"),
                chunk,
            )
        )

    for embed in overview_embeds(overview_title, description, fields):
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
