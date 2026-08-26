"""Load the shared storage module without importing Red's cog package."""

import sys
import types
from importlib import util
from pathlib import Path

_NHCogs_ROOT = Path(__file__).resolve().parents[1] / "NHCogs"
_STORAGE_PATH = _NHCogs_ROOT / "storage.py"


def load_shared_storage():
    """Return ``NHCogs.storage`` while keeping tests independent of Red."""
    loaded = sys.modules.get("NHCogs.storage")
    if loaded is not None:
        return loaded

    package = sys.modules.get("NHCogs")
    if package is None:
        package = types.ModuleType("NHCogs")
        package.__path__ = [str(_NHCogs_ROOT)]
        sys.modules["NHCogs"] = package

    spec = util.spec_from_file_location("NHCogs.storage", _STORAGE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("the shared storage interface is missing")
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
