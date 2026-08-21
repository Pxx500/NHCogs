from __future__ import annotations

import importlib.util
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from .inventory import SuiteInventory
from .preflight import PersistedDataReport, inspect_persisted_data

_COG_NAMES = ("NHMisc", "Honeypot")
_DEPENDENCIES = {
    "matplotlib": ("matplotlib", "matplotlib"),
    "AAA3A_utils": ("AAA3A_utils", "AAA3A_utils"),
    "Pillow": ("PIL", "Pillow"),
    "pillow-avif-plugin": ("pillow_avif", "pillow-avif-plugin"),
}


@dataclass(frozen=True)
class MigrationPreflightPlan:
    original_packages: tuple[str, ...]
    source_commit: str
    installed_commits: dict[str, str]
    dependency_versions: dict[str, str]
    suite_identity: dict[str, dict[str, object]]
    legacy_extension_keys: dict[str, str]
    data_directories: dict[str, str]
    config_guild_counts: dict[str, int]
    inventory: SuiteInventory
    persisted_data: PersistedDataReport
    blocking_issues: tuple[str, ...]

    def validations(self) -> dict[str, object]:
        return {
            "legacy_extension_keys": dict(self.legacy_extension_keys),
            "suite_identity": self.suite_identity,
            "installed_commits": dict(self.installed_commits),
            "dependency_versions": dict(self.dependency_versions),
            "data_directories": dict(self.data_directories),
            "config_guild_counts": dict(self.config_guild_counts),
            "inventory": self.inventory.as_dict(),
            "persisted_data": asdict(self.persisted_data),
            "blocking_issues": list(self.blocking_issues),
        }


async def build_preflight_plan(
    runtime: Any,
    *,
    backup_root: Path,
) -> MigrationPreflightPlan:
    packages = await runtime.persisted_packages()
    blocking = _package_issues(packages)
    legacy_keys, data_directories, config_guild_counts, legacy_issues = (
        await _inspect_legacy(runtime)
    )
    blocking.extend(legacy_issues)
    blocking.extend(await _extension_issues(runtime))
    installed_commits, source_issues = await _source_commits(runtime)
    source_commit = installed_commits.get("NHCogs", "")
    blocking.extend(source_issues)
    suite_identity, identity_issues = await _suite_identity(runtime)
    blocking.extend(identity_issues)
    dependency_versions, dependency_issues = _dependency_versions()
    blocking.extend(dependency_issues)
    inventory, inventory_issues = _inventory(runtime)
    blocking.extend(inventory_issues)

    persisted_data = await inspect_persisted_data(
        data_directories,
        backup_root=backup_root,
    )
    blocking.extend(persisted_data.blocking_issues)
    return MigrationPreflightPlan(
        original_packages=packages,
        source_commit=source_commit,
        installed_commits=installed_commits,
        dependency_versions=dependency_versions,
        suite_identity=suite_identity,
        legacy_extension_keys=legacy_keys,
        data_directories={name: str(path) for name, path in data_directories.items()},
        config_guild_counts=config_guild_counts,
        inventory=inventory,
        persisted_data=persisted_data,
        blocking_issues=tuple(blocking),
    )


def _package_issues(packages: tuple[str, ...]) -> list[str]:
    issues = []
    if any(packages.count(name) != 1 for name in (*_COG_NAMES, "NHCogsMigrator")):
        issues.append(
            "Persisted package list must contain NHMisc, Honeypot, and "
            "NHCogsMigrator exactly once"
        )
    if "NHCogs" in packages:
        issues.append("Persisted package list already contains NHCogs")
    return issues


async def _inspect_legacy(
    runtime: Any,
) -> tuple[dict[str, str], dict[str, Path], dict[str, int], list[str]]:
    keys: dict[str, str] = {}
    paths: dict[str, Path] = {}
    guild_counts: dict[str, int] = {}
    issues: list[str] = []
    for name in _COG_NAMES:
        try:
            cog = runtime.loaded_cog(name)
            if type(cog).__name__ != name:
                issues.append(
                    f"Loaded {name} cog class is {type(cog).__name__}, expected {name}"
                )
            if not type(cog).__module__.startswith(f"{name}."):
                issues.append(
                    f"Loaded {name} cog does not originate from the legacy extension"
                )
            if getattr(type(cog), "QUIESCENT_UNLOAD_VERSION", None) != 1:
                issues.append(
                    f"Loaded {name} does not provide the migration release's quiescent unload"
                )
            keys[name] = runtime.extension_key_for_cog(name)
            path = runtime.data_path_for_cog(name).resolve()
            paths[name] = path
            if path.name != name:
                issues.append(f"{name} data path must end in {name}, got {path}")
            guild_counts[name] = len(await cog.config.all_guilds())
        except Exception as error:
            issues.append(f"Could not inspect legacy {name}: {error}")
    return keys, paths, guild_counts, issues


async def _extension_issues(runtime: Any) -> list[str]:
    issues = []
    for extension in ("NHCogs", "NHCogsMigrator"):
        try:
            if await runtime.find_extension_spec(extension) is None:
                issues.append(f"Extension {extension} is not installed")
        except Exception as error:
            issues.append(f"Could not locate extension {extension}: {error}")
    return issues


async def _source_commits(runtime: Any) -> tuple[dict[str, str], list[str]]:
    commits = {}
    issues = []
    for name in (*_COG_NAMES, "NHCogs", "NHCogsMigrator"):
        try:
            installed = await runtime.installed_module(name)
            commit = str(getattr(installed, "commit", ""))
            if not commit:
                issues.append(f"Downloader did not report the {name} source commit")
                continue
            commits[name] = commit
        except Exception as error:
            issues.append(f"Could not inspect the Downloader {name} install: {error}")
    if commits and len(set(commits.values())) != 1:
        issues.append(
            "NHMisc, Honeypot, NHCogs, and NHCogsMigrator are not installed "
            "from the same release commit"
        )
    return commits, issues


def _dependency_versions() -> tuple[dict[str, str], list[str]]:
    versions = {}
    issues = []
    for requirement, (module, distribution) in _DEPENDENCIES.items():
        if importlib.util.find_spec(module) is None:
            issues.append(f"Required package is not importable: {requirement}")
            continue
        try:
            versions[requirement] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            issues.append(f"Required package version is unavailable: {requirement}")
    return versions, issues


async def _suite_identity(
    runtime: Any,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    expected = {
        "NHMisc": {
            "class_name": "NHMisc",
            "module": "NHCogs.nhmisc.nhmisc",
            "config_identifier": 8597423150612235807,
            "quiescent_unload_version": 1,
            "runtime_health_version": 1,
        },
        "Honeypot": {
            "class_name": "Honeypot",
            "module": "NHCogs.honeypot.honeypot",
            "config_identifier": 205192943327321000143939875896557571750,
            "quiescent_unload_version": 1,
            "runtime_health_version": 1,
        },
    }
    try:
        observed = await runtime.probe_suite_identity()
    except Exception as error:
        return {}, [f"Could not import-probe NHCogs: {error}"]
    if observed != expected:
        return observed, [
            "NHCogs class, Config, or unload identity does not match the migration contract"
        ]
    return observed, []


def _inventory(runtime: Any) -> tuple[SuiteInventory, list[str]]:
    try:
        return runtime.suite_inventory(_COG_NAMES), []
    except Exception as error:
        return SuiteInventory((), (), (), ()), [
            f"Could not snapshot suite inventory: {error}"
        ]
