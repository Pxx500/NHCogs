from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ExactResponsePresentation:
    description: str | None
    attachment: bytes | None


def present_exact_response(content: str) -> ExactResponsePresentation:
    if "```" in content:
        return ExactResponsePresentation(
            description=None,
            attachment=content.encode("utf-8"),
        )
    return ExactResponsePresentation(
        description=f"```\n{content}\n```",
        attachment=None,
    )


def build_response_transcript(responses: Sequence[str]) -> bytes:
    sections = []
    for index, content in enumerate(responses, start=1):
        encoded = content.encode("utf-8")
        marker = f"===== Response {index}: {len(encoded)} bytes =====\n".encode()
        sections.append(marker + encoded)
    return b"\n".join(sections)
