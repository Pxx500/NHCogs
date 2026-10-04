"""Private history serialization without copying source databases or evidence files."""

import json
import zipfile
from collections.abc import Iterable, Mapping
from typing import BinaryIO


def write_archive(
    destination: BinaryIO,
    *,
    manifest: dict,
    datasets: Mapping[str, Iterable[dict]],
    max_bytes: int,
) -> None:
    """Write complete JSONL datasets and reject exports exceeding the upload limit."""
    counts = {}
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, records in datasets.items():
            count = 0
            with archive.open(f"{name}.jsonl", "w") as output:
                for record in records:
                    output.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
                    count += 1
            counts[name] = count
        archive.writestr("manifest.json", json.dumps({**manifest, "counts": counts}, ensure_ascii=False, indent=2))
    if destination.tell() > max_bytes:
        raise ValueError("The complete history export exceeds this server's upload limit. No records were omitted")
    destination.seek(0)
