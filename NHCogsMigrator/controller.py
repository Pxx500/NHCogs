from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .backup import BackupResult, create_verified_backup, restore_verified_backup
from .inventory import SuiteInventory
from .plan import MigrationPreflightPlan
from .preflight import inspect_persisted_data
from .state import MigrationRun, MigrationState, MigrationStateStore

_COG_NAMES = ("NHMisc", "Honeypot")
_CONFIG_IDENTIFIERS = {
    "NHMisc": 8597423150612235807,
    "Honeypot": 205192943327321000143939875896557571750,
}


class MigrationApplyError(RuntimeError):
    pass


class MigrationController:
    def __init__(
        self,
        runtime: Any,
        store: MigrationStateStore,
        backup_root: Path,
        process_token: str = "unknown-process",
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._backup_root = backup_root
        self._process_token = process_token
        self._operation_lock = asyncio.Lock()

    async def apply(
        self,
        run_id: str,
        plan: MigrationPreflightPlan,
    ) -> MigrationRun:
        async with self._operation_lock:
            return await self._apply_once(run_id, plan)

    async def _apply_once(
        self,
        run_id: str,
        plan: MigrationPreflightPlan,
    ) -> MigrationRun:
        if plan.blocking_issues:
            raise MigrationApplyError("migration preflight contains blocking issues")
        run = await self._store.latest_run()
        if run is None or run.run_id != run_id or run.state is not MigrationState.PLANNED:
            raise MigrationApplyError("migration run is not ready to apply")

        data_directories = {
            name: Path(path) for name, path in plan.data_directories.items()
        }
        configs = {
            name: self._runtime.config_for_cog(name, _CONFIG_IDENTIFIERS[name])
            for name in _COG_NAMES
        }
        backup: BackupResult | None = None
        exports: dict[str, object] = {}
        replacement = _replacement_packages(plan.original_packages)
        try:
            await self._store.transition(
                run_id,
                MigrationState.PLANNED,
                MigrationState.QUIESCING,
            )
            for name in reversed(_COG_NAMES):
                await self._runtime.unload_extension(plan.legacy_extension_keys[name])
            for name, config in configs.items():
                exports[name] = await config.all_guilds()

            backup = await create_verified_backup(
                run_id,
                data_directories=data_directories,
                backup_root=self._backup_root,
                config_exports=exports,
                metadata={
                    "original_packages": list(plan.original_packages),
                    "source_commit": plan.source_commit,
                    "installed_commits": dict(plan.installed_commits),
                    "dependency_versions": dict(plan.dependency_versions),
                    "inventory": plan.inventory.as_dict(),
                },
            )
            await self._store.transition(
                run_id,
                MigrationState.QUIESCING,
                MigrationState.BACKUP_COMPLETE,
                artifacts={"backup_path": str(backup.path)},
                checksums={"manifest.json": backup.manifest_sha256},
            )
            await self._store.transition(
                run_id,
                MigrationState.BACKUP_COMPLETE,
                MigrationState.LOADING_SUITE,
            )
            await self._runtime.load_extension("NHCogs")
            await self._validate_loaded_suite(plan, data_directories)
            await self._store.transition(
                run_id,
                MigrationState.LOADING_SUITE,
                MigrationState.VALIDATED,
            )
            await self._runtime.replace_persisted_packages(
                plan.original_packages,
                replacement,
            )
            return await self._store.transition(
                run_id,
                MigrationState.VALIDATED,
                MigrationState.COMMITTED,
                validations={"committed_process": self._process_token},
            )
        except BaseException as error:
            if await self._runtime.persisted_packages() == replacement:
                current = await self._store.latest_run()
                if current is not None and current.state is MigrationState.VALIDATED:
                    try:
                        return await self._store.transition(
                            run_id,
                            MigrationState.VALIDATED,
                            MigrationState.COMMITTED,
                            validations={"committed_process": self._process_token},
                        )
                    except Exception as state_error:
                        raise MigrationApplyError(
                            "package authority committed but migration state could not be recorded"
                        ) from state_error
                raise MigrationApplyError(
                    "package authority committed but migration completion is uncertain"
                ) from error
            await self._rollback(
                run_id,
                plan=plan,
                data_directories=data_directories,
                configs=configs,
                exports=exports,
                backup=backup,
            )
            if isinstance(error, asyncio.CancelledError):
                raise
            raise MigrationApplyError(f"migration rolled back: {error}") from error

    async def verify_restart(self, run_id: str) -> MigrationRun:
        run = await self._store.latest_run()
        if run is None or run.run_id != run_id or run.state is not MigrationState.COMMITTED:
            raise MigrationApplyError("migration run is not waiting for restart verification")
        if run.validations.get("committed_process") == self._process_token:
            raise MigrationApplyError("a normal bot restart is required before verification")
        expected_packages = _replacement_packages(run.original_packages)
        if await self._runtime.persisted_packages() != expected_packages:
            raise MigrationApplyError("persisted package authority does not match NHCogs")
        data_directories = _stored_data_directories(run)
        expected_inventory = _stored_inventory(run)
        expected_guild_counts = _stored_config_guild_counts(run)
        for name in _COG_NAMES:
            cog = self._runtime.loaded_cog(name)
            if not type(cog).__module__.startswith(f"NHCogs.{name.casefold()}"):
                raise MigrationApplyError(f"restarted {name} does not originate from NHCogs")
            if self._runtime.extension_key_for_cog(name) != "NHCogs":
                raise MigrationApplyError(f"restarted {name} has the wrong extension owner")
            if self._runtime.data_path_for_cog(name).resolve() != data_directories[name]:
                raise MigrationApplyError(f"restarted {name} resolved a different data path")
            await _validate_config_identity(
                name,
                cog,
                expected_guild_counts[name],
                phase="restart",
            )
        await _require_background_health(self._runtime, phase="restart")
        if self._runtime.suite_inventory(_COG_NAMES) != expected_inventory:
            raise MigrationApplyError("restarted suite inventory differs from preflight")
        data_report = await inspect_persisted_data(
            data_directories,
            backup_root=self._backup_root,
        )
        _require_database_continuity(
            _stored_database_paths(run),
            _database_paths(data_report.databases),
            phase="restart",
        )
        data_issues = tuple(
            issue
            for issue in data_report.blocking_issues
            if not issue.startswith("Insufficient backup space")
        )
        if data_issues:
            raise MigrationApplyError("restart data validation failed: " + "; ".join(data_issues))
        return await self._store.transition(
            run_id,
            MigrationState.COMMITTED,
            MigrationState.RESTART_VERIFIED,
            validations={"restart_process": self._process_token},
        )

    async def finalize(self, run_id: str) -> MigrationRun:
        run = await self._store.latest_run()
        if (
            run is None
            or run.run_id != run_id
            or run.state
            not in {MigrationState.RESTART_VERIFIED, MigrationState.FINALIZED}
        ):
            raise MigrationApplyError(
                "migration run has not passed restart verification"
            )
        expected = _replacement_packages(run.original_packages)
        if expected.count("NHCogsMigrator") != 1:
            raise MigrationApplyError(
                "persisted package list does not contain NHCogsMigrator exactly once"
            )
        finalized_packages = tuple(
            package for package in expected if package != "NHCogsMigrator"
        )
        current_packages = await self._runtime.persisted_packages()
        if current_packages not in {expected, finalized_packages}:
            raise MigrationApplyError(
                "persisted package authority does not match finalization state"
            )
        if run.state is MigrationState.RESTART_VERIFIED:
            run = await self._store.transition(
                run_id,
                MigrationState.RESTART_VERIFIED,
                MigrationState.FINALIZED,
            )
        if current_packages == expected:
            await self._runtime.replace_persisted_packages(expected, finalized_packages)
        return run

    async def recover_interrupted(self, run_id: str) -> MigrationRun:
        async with self._operation_lock:
            return await self._recover_interrupted_once(run_id)

    async def _recover_interrupted_once(self, run_id: str) -> MigrationRun:
        run = await self._store.latest_run()
        recoverable = {
            MigrationState.QUIESCING,
            MigrationState.BACKUP_COMPLETE,
            MigrationState.LOADING_SUITE,
            MigrationState.VALIDATED,
            MigrationState.ROLLING_BACK,
            MigrationState.MANUAL_INTERVENTION,
        }
        if run is None or run.run_id != run_id or run.state not in recoverable:
            raise MigrationApplyError("migration run is not recoverable as legacy authority")
        packages = await self._runtime.persisted_packages()
        committed_packages = _replacement_packages(run.original_packages)
        if run.state is MigrationState.VALIDATED and packages == committed_packages:
            await self._store.transition(
                run_id,
                MigrationState.VALIDATED,
                MigrationState.COMMITTED,
                validations={
                    "committed_process": "interrupted-before-commit-state",
                    "recovered_commit": True,
                },
            )
            return await self.verify_restart(run_id)
        if packages != run.original_packages:
            raise MigrationApplyError(
                "interrupted pre-commit run no longer has legacy package authority"
            )
        data_directories = _stored_data_directories(run)
        if run.state is not MigrationState.ROLLING_BACK:
            await self._store.transition(
                run_id,
                run.state,
                MigrationState.ROLLING_BACK,
            )
        try:
            configs = {
                name: self._runtime.config_for_cog(name, _CONFIG_IDENTIFIERS[name])
                for name in _COG_NAMES
            }
            await self._quiesce_suite_runtimes()
            raw_backup_path = run.artifacts.get("backup_path")
            if raw_backup_path is not None:
                backup_path = Path(str(raw_backup_path))
                await restore_verified_backup(backup_path, data_directories)
                exports = _load_config_exports(backup_path)
                await _restore_config(configs, exports)
            for name in _COG_NAMES:
                if self._runtime.extension_key_for_module(name) is None:
                    await self._runtime.load_extension(name)
            if self._runtime.suite_inventory(_COG_NAMES) != _stored_inventory(run):
                raise MigrationApplyError("recovered legacy inventory differs from preflight")
            return await self._store.transition(
                run_id,
                MigrationState.ROLLING_BACK,
                MigrationState.ROLLED_BACK,
                validations={"recovered_process": self._process_token},
            )
        except Exception as error:
            current = await self._store.latest_run()
            if current is not None and current.state is MigrationState.ROLLING_BACK:
                await self._store.transition(
                    run_id,
                    MigrationState.ROLLING_BACK,
                    MigrationState.MANUAL_INTERVENTION,
                    validations={"recovery_error": str(error)},
                )
            raise MigrationApplyError(f"interrupted migration recovery failed: {error}") from error

    async def _validate_loaded_suite(
        self,
        plan: MigrationPreflightPlan,
        data_directories: dict[str, Path],
    ) -> None:
        if await self._runtime.persisted_packages() != plan.original_packages:
            raise MigrationApplyError("package authority changed before validation")
        for legacy_extension in _COG_NAMES:
            if self._runtime.extension_key_for_module(legacy_extension) is not None:
                raise MigrationApplyError(
                    f"legacy extension {legacy_extension} is still loaded"
                )
        for name in _COG_NAMES:
            cog = self._runtime.loaded_cog(name)
            if type(cog).__name__ != name:
                raise MigrationApplyError(f"loaded suite has the wrong {name} class")
            expected_module = f"NHCogs.{name.casefold()}"
            if not type(cog).__module__.startswith(expected_module):
                raise MigrationApplyError(f"loaded {name} does not originate from NHCogs")
            if self._runtime.data_path_for_cog(name).resolve() != data_directories[name].resolve():
                raise MigrationApplyError(f"loaded {name} resolved a different data path")
            await _validate_config_identity(
                name,
                cog,
                plan.config_guild_counts[name],
                phase="loaded suite",
            )
        await _require_background_health(self._runtime, phase="loaded suite")
        if self._runtime.suite_inventory(_COG_NAMES) != plan.inventory:
            raise MigrationApplyError("loaded suite inventory differs from preflight")
        data_report = await inspect_persisted_data(
            data_directories,
            backup_root=self._backup_root,
        )
        _require_database_continuity(
            _database_paths(plan.persisted_data.databases),
            _database_paths(data_report.databases),
            phase="loaded suite",
        )
        data_issues = tuple(
            issue
            for issue in data_report.blocking_issues
            if not issue.startswith("Insufficient backup space")
        )
        if data_issues:
            raise MigrationApplyError("loaded suite data validation failed: " + "; ".join(data_issues))

    async def _rollback(
        self,
        run_id: str,
        *,
        plan: MigrationPreflightPlan,
        data_directories: dict[str, Path],
        configs: dict[str, Any],
        exports: dict[str, object],
        backup: BackupResult | None,
    ) -> None:
        try:
            current = await self._store.latest_run()
            if current is None:
                raise MigrationApplyError("migration state disappeared during rollback")
            if current.state is not MigrationState.ROLLING_BACK:
                await self._store.transition(
                    run_id,
                    current.state,
                    MigrationState.ROLLING_BACK,
                )
            await self._quiesce_suite_runtimes()
            if backup is not None:
                await restore_verified_backup(backup.path, data_directories)
                await _restore_config(configs, exports)
            for name in _COG_NAMES:
                if self._runtime.extension_key_for_module(name) is None:
                    await self._runtime.load_extension(name)
            if await self._runtime.persisted_packages() != plan.original_packages:
                raise MigrationApplyError("legacy package authority changed during rollback")
            if self._runtime.suite_inventory(_COG_NAMES) != plan.inventory:
                raise MigrationApplyError("legacy inventory did not recover")
            await self._store.transition(
                run_id,
                MigrationState.ROLLING_BACK,
                MigrationState.ROLLED_BACK,
            )
        except Exception as rollback_error:
            current = await self._store.latest_run()
            if current is not None and current.state is not MigrationState.MANUAL_INTERVENTION:
                try:
                    await self._store.transition(
                        run_id,
                        current.state,
                        MigrationState.MANUAL_INTERVENTION,
                        validations={"rollback_error": str(rollback_error)},
                    )
                except Exception:
                    pass
            raise MigrationApplyError(
                f"automatic rollback failed: {rollback_error}"
            ) from rollback_error

    async def _quiesce_suite_runtimes(self) -> None:
        for module_name in ("NHCogs", *_COG_NAMES):
            key = self._runtime.extension_key_for_module(module_name)
            if key is not None:
                await self._runtime.unload_extension(key)
        for module_name in ("NHCogs", *_COG_NAMES):
            if self._runtime.extension_key_for_module(module_name) is not None:
                raise MigrationApplyError(
                    f"could not quiesce {module_name} before data restore"
                )


async def _restore_config(
    configs: dict[str, Any],
    exports: dict[str, object],
) -> None:
    for name, config in configs.items():
        raw_export = exports.get(name)
        if not isinstance(raw_export, dict):
            raise MigrationApplyError(f"Config export is missing for {name}")
        await config.clear_all_guilds()
        for raw_guild_id, values in raw_export.items():
            if not isinstance(values, dict):
                raise MigrationApplyError(f"Config guild export is invalid for {name}")
            await config.guild_from_id(int(raw_guild_id)).set(values)


def _replacement_packages(original: tuple[str, ...]) -> tuple[str, ...]:
    legacy = {"NHMisc", "Honeypot"}
    if any(original.count(name) != 1 for name in legacy) or "NHCogs" in original:
        raise MigrationApplyError("package list is not eligible for NHCogs replacement")
    first_legacy = min(original.index(name) for name in legacy)
    replacement = [name for name in original if name not in legacy]
    replacement.insert(first_legacy, "NHCogs")
    return tuple(replacement)


def _stored_data_directories(run: MigrationRun) -> dict[str, Path]:
    raw = run.validations.get("data_directories")
    if not isinstance(raw, dict):
        raise MigrationApplyError("migration run has no stored data directories")
    paths = {str(name): Path(str(path)).resolve() for name, path in raw.items()}
    if set(paths) != set(_COG_NAMES):
        raise MigrationApplyError("migration run data directory set is incomplete")
    return paths


def _stored_inventory(run: MigrationRun) -> SuiteInventory:
    raw = run.validations.get("inventory")
    if not isinstance(raw, dict):
        raise MigrationApplyError("migration run has no stored inventory")
    try:
        return SuiteInventory(
            prefix_commands=tuple(raw["prefix_commands"]),
            listeners=tuple(raw["listeners"]),
            application_commands=tuple(raw["application_commands"]),
            persistent_view_custom_ids=tuple(raw["persistent_view_custom_ids"]),
        )
    except (KeyError, TypeError) as error:
        raise MigrationApplyError("migration run inventory is invalid") from error


def _stored_config_guild_counts(run: MigrationRun) -> dict[str, int]:
    raw = run.validations.get("config_guild_counts")
    if not isinstance(raw, dict):
        raise MigrationApplyError("stored Config guild counts are missing")
    try:
        return {name: int(raw[name]) for name in _COG_NAMES}
    except (KeyError, TypeError, ValueError) as error:
        raise MigrationApplyError("stored Config guild counts are invalid") from error


async def _validate_config_identity(
    name: str,
    cog: Any,
    expected_guild_count: int,
    *,
    phase: str,
) -> None:
    if getattr(type(cog), "QUIESCENT_UNLOAD_VERSION", None) != 1:
        raise MigrationApplyError(f"{phase} {name} has no quiescent unload contract")
    if getattr(type(cog), "CONFIG_IDENTIFIER", None) != _CONFIG_IDENTIFIERS[name]:
        raise MigrationApplyError(f"{phase} {name} has the wrong Config identifier")
    guilds = await cog.config.all_guilds()
    if len(guilds) != expected_guild_count:
        raise MigrationApplyError(
            f"{phase} {name} Config guild count changed from "
            f"{expected_guild_count} to {len(guilds)}"
        )


async def _require_background_health(runtime: Any, *, phase: str) -> None:
    await asyncio.sleep(0)
    issues = runtime.background_health_issues(_COG_NAMES)
    if issues:
        raise MigrationApplyError(
            f"{phase} background health validation failed: " + "; ".join(issues)
        )


def _stored_database_paths(run: MigrationRun) -> set[Path]:
    persisted_data = run.validations.get("persisted_data")
    if not isinstance(persisted_data, dict):
        raise MigrationApplyError("stored persisted-data validation is missing")
    databases = persisted_data.get("databases")
    if not isinstance(databases, (list, tuple)):
        raise MigrationApplyError("stored database inventory is missing")
    paths = set()
    for database in databases:
        if not isinstance(database, dict) or not isinstance(database.get("path"), str):
            raise MigrationApplyError("stored database inventory is invalid")
        paths.add(Path(database["path"]).resolve())
    return paths


def _require_database_continuity(
    expected: set[Path],
    observed: set[Path],
    *,
    phase: str,
) -> None:
    missing = sorted(str(path) for path in expected - observed)
    if missing:
        raise MigrationApplyError(
            f"{phase} lost persisted SQLite databases: " + ", ".join(missing)
        )


def _database_paths(databases: Any) -> set[Path]:
    return {Path(database.path).resolve() for database in databases}


def _load_config_exports(backup_path: Path) -> dict[str, object]:
    exports = {}
    for name in _COG_NAMES:
        path = backup_path / "config" / f"{name}.json"
        try:
            exports[name] = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MigrationApplyError(f"could not read Config backup for {name}") from error
    return exports
