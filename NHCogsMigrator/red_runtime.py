from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .inventory import (
    SuiteInventory,
    snapshot_global_inventory,
    snapshot_suite_inventory,
)


class RedRuntimeError(RuntimeError):
    pass


class RedRuntime:
    """Version-pinned access to Red's extension and package authority."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._validate_contract()

    async def persisted_packages(self) -> tuple[str, ...]:
        packages = await self._bot._config.packages()
        return tuple(str(package) for package in packages)

    async def replace_persisted_packages(
        self,
        expected: tuple[str, ...],
        replacement: tuple[str, ...],
    ) -> None:
        if len(replacement) != len(set(replacement)):
            raise RedRuntimeError("replacement package list contains duplicates")
        async with self._bot._config.packages() as packages:
            current = tuple(str(package) for package in packages)
            if current != expected:
                raise RedRuntimeError(
                    "persisted package list changed before migration commit"
                )
            packages[:] = list(replacement)

    async def load_extension(self, name: str) -> str:
        if self._extension_key_for_module(name) is not None:
            raise RedRuntimeError(f"extension {name} is already loaded")
        spec = await self._bot._cog_mgr.find_cog(name)
        if spec is None:
            raise RedRuntimeError(f"extension {name} is not installed")
        await self._bot.load_extension(spec)
        extension_key = self._extension_key_for_module(spec.name)
        if extension_key is None:
            raise RedRuntimeError(
                f"Red loaded {name} without publishing an extension entry"
            )
        return extension_key

    async def find_extension_spec(self, name: str) -> Any:
        return await self._bot._cog_mgr.find_cog(name)

    async def probe_suite_identity(self) -> dict[str, dict[str, object]]:
        spec = await self.find_extension_spec("NHCogs")
        origin = None if spec is None else getattr(spec, "origin", None)
        if origin is None:
            raise RedRuntimeError("NHCogs has no importable module origin")
        package_parent = await asyncio.to_thread(
            lambda: str(Path(origin).resolve().parent.parent)
        )
        environment = os.environ.copy()
        python_paths = [str(path) for path in sys.path if path]
        configured_python_path = environment.get("PYTHONPATH")
        if configured_python_path:
            python_paths.extend(configured_python_path.split(os.pathsep))
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))
        script = (
            "import importlib,json,sys;"
            "sys.path.insert(0,sys.argv[1]);"
            "m=importlib.import_module('NHCogs');"
            "print(json.dumps({"
            "'NHMisc':{'class_name':m.NHMisc.__name__,"
            "'module':m.NHMisc.__module__,"
            "'config_identifier':m.NHMisc.CONFIG_IDENTIFIER,"
            "'quiescent_unload_version':m.NHMisc.QUIESCENT_UNLOAD_VERSION,"
            "'runtime_health_version':m.NHMisc.RUNTIME_HEALTH_VERSION},"
            "'Honeypot':{'class_name':m.Honeypot.__name__,"
            "'module':m.Honeypot.__module__,"
            "'config_identifier':m.Honeypot.CONFIG_IDENTIFIER,"
            "'quiescent_unload_version':m.Honeypot.QUIESCENT_UNLOAD_VERSION,"
            "'runtime_health_version':m.Honeypot.RUNTIME_HEALTH_VERSION}}))"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            package_parent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RedRuntimeError("NHCogs import probe timed out") from None
        if process.returncode != 0:
            raise RedRuntimeError(
                "NHCogs import probe failed: "
                + stderr.decode("utf-8", errors="replace").strip()
            )
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise RedRuntimeError("NHCogs import probe returned invalid JSON") from error
        if not isinstance(result, dict):
            raise RedRuntimeError("NHCogs import probe returned an invalid identity")
        return result

    async def unload_extension(self, extension_key: str) -> None:
        if extension_key not in self._bot.extensions:
            raise RedRuntimeError(f"extension {extension_key} is not loaded")
        await self._bot.unload_extension(extension_key)

    def extension_key_for_cog(self, cog_name: str) -> str:
        cog = self._bot.get_cog(cog_name)
        if cog is None:
            raise RedRuntimeError(f"cog {cog_name} is not loaded")
        cog_module = type(cog).__module__
        for key, module in self._bot.extensions.items():
            module_name = getattr(module, "__name__", "")
            if cog_module == module_name or cog_module.startswith(f"{module_name}."):
                return str(key)
        raise RedRuntimeError(
            f"could not resolve the extension owning cog {cog_name}"
        )

    def extension_key_for_module(self, module_name: str) -> str | None:
        return self._extension_key_for_module(module_name)

    def loaded_cog(self, name: str) -> Any:
        cog = self._bot.get_cog(name)
        if cog is None:
            raise RedRuntimeError(f"cog {name} is not loaded")
        return cog

    def config_for_cog(self, name: str, identifier: int) -> Any:
        config_type = importlib.import_module("redbot.core").Config
        return config_type.get_conf(None, identifier, cog_name=name)

    def suite_inventory(self, cog_names: tuple[str, ...]) -> SuiteInventory:
        return snapshot_suite_inventory(self._bot, cog_names)

    def legacy_global_inventory(self, cog_names: tuple[str, ...]) -> SuiteInventory:
        return snapshot_global_inventory(self._bot, cog_names)

    def background_health_issues(self, cog_names: tuple[str, ...]) -> tuple[str, ...]:
        issues = []
        for name in cog_names:
            cog = self.loaded_cog(name)
            if getattr(type(cog), "RUNTIME_HEALTH_VERSION", None) != 1:
                issues.append(f"{name}: runtime health contract is unavailable")
                continue
            checker = getattr(cog, "runtime_health_issues", None)
            if not callable(checker):
                issues.append(f"{name}: runtime health checker is unavailable")
                continue
            observed = checker()
            if not isinstance(observed, tuple) or not all(
                isinstance(issue, str) for issue in observed
            ):
                issues.append(f"{name}: runtime health checker returned invalid data")
                continue
            issues.extend(f"{name}: {issue}" for issue in observed)
        return tuple(issues)

    def data_path_for_cog(self, name: str) -> Path:
        data_manager = importlib.import_module("redbot.core.data_manager")
        return Path(data_manager.cog_data_path(self.loaded_cog(name)))

    async def installed_module(self, name: str) -> Any:
        downloader = self._bot.get_cog("Downloader")
        if downloader is None:
            raise RedRuntimeError("Downloader must be loaded for migration preflight")
        ready = getattr(downloader, "_ready", None)
        if ready is not None:
            await ready.wait()
        ready_error = getattr(downloader, "_ready_raised", None)
        if isinstance(ready_error, BaseException):
            raise RedRuntimeError("Downloader initialization failed") from ready_error
        if ready_error:
            raise RedRuntimeError("Downloader initialization failed")
        installed, module = await downloader.is_installed(name)
        if not installed or module is None:
            raise RedRuntimeError(f"extension {name} is not Downloader-installed")
        return module

    def _extension_key_for_module(self, module_name: str) -> str | None:
        for key, module in self._bot.extensions.items():
            loaded_name = getattr(module, "__name__", "")
            if loaded_name == module_name:
                return str(key)
        return None

    def _validate_contract(self) -> None:
        missing = []
        for owner, attribute in (
            (self._bot, "_config"),
            (self._bot, "_cog_mgr"),
            (self._bot, "extensions"),
            (self._bot, "get_cog"),
            (self._bot, "load_extension"),
            (self._bot, "unload_extension"),
        ):
            if not hasattr(owner, attribute):
                missing.append(attribute)
        if missing:
            raise RedRuntimeError(
                "Red runtime is missing required migration interfaces: "
                + ", ".join(missing)
            )
