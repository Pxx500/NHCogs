from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_RUN_ID = re.compile(r"[A-Za-z0-9_-]+")


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest_sha256: str
    file_count: int
    total_bytes: int
    database_count: int


async def create_verified_backup(
    run_id: str,
    *,
    data_directories: dict[str, Path],
    backup_root: Path,
    config_exports: dict[str, object],
    metadata: dict[str, object],
) -> BackupResult:
    worker = asyncio.create_task(
        asyncio.to_thread(
            _create_verified_backup_sync,
            run_id,
            data_directories,
            backup_root,
            config_exports,
            metadata,
        )
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise


async def restore_verified_backup(
    backup_path: Path,
    data_directories: dict[str, Path],
) -> None:
    worker = asyncio.create_task(
        asyncio.to_thread(
            _restore_verified_backup_sync,
            backup_path,
            data_directories,
        )
    )
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise


def _create_verified_backup_sync(
    run_id: str,
    data_directories: dict[str, Path],
    backup_root: Path,
    config_exports: dict[str, object],
    metadata: dict[str, object],
) -> BackupResult:
    if _RUN_ID.fullmatch(run_id) is None:
        raise BackupError("run ID may contain only letters, numbers, underscores, and hyphens")
    root = backup_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".{run_id}.tmp"
    final = root / run_id
    if temporary.parent != root or final.parent != root:
        raise BackupError("backup path escaped the configured backup root")
    if temporary.exists() or final.exists():
        raise BackupError(f"backup path for run {run_id} already exists")
    temporary.mkdir()

    try:
        data_root = temporary / "data"
        sqlite_root = temporary / "sqlite"
        config_root = temporary / "config"
        data_root.mkdir()
        sqlite_root.mkdir()
        config_root.mkdir()
        database_count = 0
        for name, source_path in data_directories.items():
            source = source_path.resolve()
            if not source.is_dir():
                raise BackupError(f"data directory is missing: {source}")
            destination = data_root / name
            shutil.copytree(source, destination, copy_function=_copy_file)
            for database in sorted(source.rglob("*.sqlite"), key=str):
                relative = database.relative_to(source)
                sqlite_destination = sqlite_root / name / relative
                sqlite_destination.parent.mkdir(parents=True, exist_ok=True)
                _backup_sqlite(database, sqlite_destination)
                _copy_file(sqlite_destination, destination / relative)
                database_count += 1

        for name, export in config_exports.items():
            _write_json(config_root / f"{name}.json", export)
        _write_json(temporary / "metadata.json", metadata)

        manifest = _build_manifest(temporary)
        manifest_path = temporary / "manifest.json"
        _write_json(manifest_path, manifest)
        _verify_manifest(temporary, manifest)
        manifest_sha256 = _sha256(manifest_path)
        _write_bytes(
            temporary / "manifest.sha256",
            f"{manifest_sha256}  manifest.json\n".encode(),
        )
        temporary.replace(final)
        files = cast(dict[str, dict[str, int | str]], manifest["files"])
        return BackupResult(
            path=final,
            manifest_sha256=manifest_sha256,
            file_count=len(files),
            total_bytes=sum(int(entry["size"]) for entry in files.values()),
            database_count=database_count,
        )
    except Exception as error:
        if temporary.exists():
            shutil.rmtree(temporary)
        if isinstance(error, BackupError):
            raise
        raise BackupError(f"backup creation failed: {error}") from error


def _restore_verified_backup_sync(
    backup_path: Path,
    data_directories: dict[str, Path],
) -> None:
    backup = backup_path.resolve()
    manifest = _load_verified_manifest(backup)
    _reset_interrupted_restore(backup, data_directories)
    staged, previous = _stage_data_restore(backup, manifest, data_directories)
    moved: list[str] = []
    try:
        _swap_data_restore(data_directories, staged, previous, moved)
    except Exception as error:
        _recover_data_restore(data_directories, staged, previous, moved)
        if isinstance(error, BackupError):
            raise
        raise BackupError(f"backup restore failed: {error}") from error

    cleanup_errors = []
    for old in previous.values():
        try:
            shutil.rmtree(old)
        except OSError as error:
            cleanup_errors.append(f"{old}: {error}")
    if cleanup_errors:
        raise BackupError(
            "backup restore completed but old data cleanup failed: "
            + "; ".join(cleanup_errors)
        )


def _reset_interrupted_restore(
    backup: Path,
    data_directories: dict[str, Path],
) -> None:
    run_id = backup.name
    for name, raw_target in data_directories.items():
        target = raw_target.resolve()
        stage = target.parent / f".{target.name}.restore-{run_id}"
        old = target.parent / f".{target.name}.pre-restore-{run_id}"
        if old.exists():
            if target.exists():
                if not target.is_dir():
                    raise BackupError(
                        f"interrupted restore target is not a directory for {name}"
                    )
                shutil.rmtree(target)
            old.replace(target)
        elif not target.exists() and stage.exists():
            stage.replace(target)
        if stage.exists():
            shutil.rmtree(stage)
        if not target.is_dir():
            raise BackupError(
                f"could not reconstruct interrupted restore target for {name}"
            )


def _load_verified_manifest(backup: Path) -> dict[str, object]:
    manifest_path = backup / "manifest.json"
    checksum_path = backup / "manifest.sha256"
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError(f"could not read backup manifest: {error}") from error
    if not isinstance(manifest, dict):
        raise BackupError("backup manifest is not an object")
    _verify_manifest(backup, manifest)
    try:
        expected_manifest_hash = checksum_path.read_text("utf-8").split()[0]
    except (OSError, IndexError) as error:
        raise BackupError(f"could not read manifest checksum: {error}") from error
    if _sha256(manifest_path) != expected_manifest_hash:
        raise BackupError("backup manifest checksum does not match")
    return manifest


def _stage_data_restore(
    backup: Path,
    manifest: dict[str, object],
    data_directories: dict[str, Path],
) -> tuple[dict[str, Path], dict[str, Path]]:
    staged: dict[str, Path] = {}
    previous: dict[str, Path] = {}
    run_id = backup.name
    try:
        for name, raw_target in data_directories.items():
            target = raw_target.resolve()
            source = backup / "data" / name
            if not source.is_dir():
                raise BackupError(f"backup data directory is missing: {source}")
            if not target.is_dir():
                raise BackupError(f"live data directory is missing: {target}")
            stage = target.parent / f".{target.name}.restore-{run_id}"
            old = target.parent / f".{target.name}.pre-restore-{run_id}"
            if stage.exists() or old.exists():
                raise BackupError(f"restore staging path already exists for {name}")
            staged[name] = stage
            previous[name] = old
            shutil.copytree(source, stage, copy_function=_copy_file)
            _verify_restored_tree(backup, manifest, name, stage)
    except Exception:
        for stage in staged.values():
            if stage.exists():
                shutil.rmtree(stage)
        raise
    return staged, previous


def _swap_data_restore(
    data_directories: dict[str, Path],
    staged: dict[str, Path],
    previous: dict[str, Path],
    moved: list[str],
) -> None:
    for name, raw_target in data_directories.items():
        target = raw_target.resolve()
        target.replace(previous[name])
        moved.append(name)
        staged[name].replace(target)


def _recover_data_restore(
    data_directories: dict[str, Path],
    staged: dict[str, Path],
    previous: dict[str, Path],
    moved: list[str],
) -> None:
    for name in reversed(moved):
        target = data_directories[name].resolve()
        if target.exists():
            shutil.rmtree(target)
        if previous[name].exists():
            previous[name].replace(target)
    for path in (*staged.values(), *previous.values()):
        if path.exists():
            shutil.rmtree(path)


def _backup_sqlite(source: Path, destination: Path) -> None:
    uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with (
            closing(sqlite3.connect(uri, uri=True, timeout=5)) as source_connection,
            closing(sqlite3.connect(destination, timeout=5)) as destination_connection,
        ):
            source_connection.backup(destination_connection)
            destination_connection.commit()
        with closing(sqlite3.connect(destination, timeout=5)) as verification:
            results = tuple(
                str(row[0]) for row in verification.execute("PRAGMA integrity_check")
            )
        if results != ("ok",):
            raise BackupError(
                f"SQLite backup integrity failed for {source}: {', '.join(results)}"
            )
        _fsync_path(destination)
    except sqlite3.Error as error:
        raise BackupError(f"SQLite backup failed for {source}: {error}") from error


def _build_manifest(root: Path) -> dict[str, object]:
    files = {}
    for path in sorted((path for path in root.rglob("*") if path.is_file()), key=str):
        relative = path.relative_to(root).as_posix()
        files[relative] = {
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return {"files": files}


def _verify_manifest(root: Path, manifest: dict[str, object]) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise BackupError("backup manifest has no file map")
    for relative, raw_entry in files.items():
        if not isinstance(relative, str) or not isinstance(raw_entry, dict):
            raise BackupError("backup manifest contains an invalid entry")
        path = root / Path(relative)
        if not path.is_file():
            raise BackupError(f"backup artifact is missing: {relative}")
        if path.stat().st_size != raw_entry.get("size"):
            raise BackupError(f"backup artifact size changed: {relative}")
        if _sha256(path) != raw_entry.get("sha256"):
            raise BackupError(f"backup artifact checksum changed: {relative}")


def _verify_restored_tree(
    backup: Path,
    manifest: dict[str, object],
    name: str,
    restored: Path,
) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise BackupError("backup manifest has no file map")
    prefix = f"data/{name}/"
    expected = {
        relative.removeprefix(prefix): entry
        for relative, entry in files.items()
        if isinstance(relative, str) and relative.startswith(prefix)
    }
    actual = {
        path.relative_to(restored).as_posix()
        for path in restored.rglob("*")
        if path.is_file()
    }
    if actual != set(expected):
        raise BackupError(f"restored file inventory does not match for {name}")
    for relative, entry in expected.items():
        if not isinstance(entry, dict):
            raise BackupError(f"invalid backup manifest entry for {name}/{relative}")
        path = restored / relative
        if path.stat().st_size != entry.get("size"):
            raise BackupError(f"restored file size changed: {name}/{relative}")
        if _sha256(path) != entry.get("sha256"):
            raise BackupError(f"restored file checksum changed: {name}/{relative}")


def _write_json(path: Path, value: object) -> None:
    _write_bytes(
        path,
        json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def _copy_file(source: str | Path, destination: str | Path) -> str:
    source_path = Path(source)
    destination_path = Path(destination)
    with (
        source_path.open("rb") as source_file,
        destination_path.open("wb") as destination_file,
    ):
        shutil.copyfileobj(source_file, destination_file, length=1024 * 1024)
        destination_file.flush()
        os.fsync(destination_file.fileno())
    shutil.copystat(source_path, destination_path)
    return str(destination_path)


def _fsync_path(path: Path) -> None:
    with path.open("r+b") as file:
        file.flush()
        os.fsync(file.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
