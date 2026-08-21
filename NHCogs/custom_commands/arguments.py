from __future__ import annotations

import re
from dataclasses import dataclass

import discord
from redbot.core import commands

PLACEHOLDER_PATTERN = re.compile(r"{([^{}]+)}")
NUMERIC_PREFIX_PATTERN = re.compile(r"^(\d+)(.*)$")
BUILTIN_CONVERTERS = {
    "bool",
    "complex",
    "float",
    "frozenset",
    "int",
    "list",
    "query",
    "set",
    "str",
    "tuple",
}
MAX_ARGUMENT_INDEX = 9


class ArgumentSignatureError(ValueError):
    pass


@dataclass(frozen=True)
class NumericPlaceholder:
    token: str
    index: int
    converter: str | None
    attribute: str | None


def numeric_placeholders(response: str) -> tuple[NumericPlaceholder, ...]:
    placeholders: list[NumericPlaceholder] = []
    for match in PLACEHOLDER_PATTERN.finditer(response):
        token = match.group(1)
        field, separator, format_spec = token.partition(":")
        numeric = NUMERIC_PREFIX_PATTERN.match(field)
        if numeric is None:
            continue
        raw_index, field_suffix = numeric.groups()
        attribute = field_suffix if field_suffix.startswith(".") else None
        converter = None
        if separator:
            converter, dot, format_attribute = format_spec.partition(".")
            if attribute is None and dot:
                attribute = f".{format_attribute}"
        placeholders.append(
            NumericPlaceholder(
                token=token,
                index=int(raw_index),
                converter=converter or None,
                attribute=attribute,
            )
        )
    return tuple(placeholders)


def normalized_converter_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    name = raw[:-9] if raw.casefold().endswith("converter") else raw
    if not name or name.startswith("_"):
        return None
    folded = name.casefold()
    if folded in BUILTIN_CONVERTERS:
        return folded
    try:
        converter_type = getattr(discord, name)
        getattr(commands, converter_type.__name__ + "Converter")
    except (AttributeError, ImportError):
        return None
    return converter_type.__name__.casefold()


def argument_signature(response: str) -> tuple[str | None, ...]:
    """Return the normalized positional converter contract used by Red."""
    placeholders = numeric_placeholders(response)
    if not placeholders:
        return ()

    first_index = min(item.index for item in placeholders)
    relative_indices = {item.index - first_index for item in placeholders}
    final_index = max(relative_indices)
    if final_index > MAX_ARGUMENT_INDEX:
        raise ArgumentSignatureError("Too many arguments")
    expected_indices = set(range(final_index + 1))
    missing = expected_indices - relative_indices
    if missing:
        rendered = ", ".join(str(index + first_index) for index in sorted(missing))
        raise ArgumentSignatureError(f"Arguments must be sequential. Missing: {rendered}")

    signature: list[str | None] = [None] * (final_index + 1)
    for placeholder in placeholders:
        position = placeholder.index - first_index
        converter = normalized_converter_name(placeholder.converter)
        existing = signature[position]
        if existing is not None and converter is not None and existing != converter:
            raise ArgumentSignatureError(
                f"Conflicting converters for argument {placeholder.index}: "
                f"{existing} and {converter}"
            )
        if converter is not None:
            signature[position] = converter
    return tuple(signature)
