import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

from tests.harness import _MISSING, _isolated_honeypot_modules

PACKAGE_DIR = Path(__file__).parents[1] / "NHCogs" / "githubtickets"
MODULE_NAMES = (
    "NHCogs.githubtickets",
    "NHCogs.githubtickets.models",
    "NHCogs.githubtickets.store",
    "NHCogs.githubtickets.settings",
    "NHCogs.githubtickets.presentation",
    "NHCogs.githubtickets.githubtickets",
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def isolated_githubtickets_modules(data_path: Path):
    previous = {name: sys.modules.get(name, _MISSING) for name in MODULE_NAMES}
    with _isolated_honeypot_modules(data_path):
        package = ModuleType("NHCogs.githubtickets")
        package.__path__ = [str(PACKAGE_DIR)]
        sys.modules["NHCogs.githubtickets"] = package
        try:
            loaded = {}
            for short_name in ("models", "store", "settings", "presentation"):
                loaded[short_name] = _load_module(
                    f"NHCogs.githubtickets.{short_name}",
                    PACKAGE_DIR / f"{short_name}.py",
                )
            loaded["githubtickets"] = _load_module(
                "NHCogs.githubtickets.githubtickets",
                PACKAGE_DIR / "githubtickets.py",
            )
            yield SimpleNamespace(**loaded)
        finally:
            for name, previous_module in previous.items():
                if previous_module is _MISSING:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous_module
