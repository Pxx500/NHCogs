from __future__ import annotations

from pathlib import Path

_TRANSIENT_SUFFIXES = ("-wal", "-shm", "-journal")


def is_transient_sqlite_sidecar(path: Path) -> bool:
    name = path.name.casefold()
    return any(
        name.endswith(suffix) and name[: -len(suffix)].endswith(".sqlite")
        for suffix in _TRANSIENT_SUFFIXES
    )
