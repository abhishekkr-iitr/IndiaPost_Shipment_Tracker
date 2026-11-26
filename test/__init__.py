"""Compatibility package forwarding imports from 'test' to 'tests'."""
import importlib
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_TESTS_DIR = str(Path(__file__).resolve().parent.parent / "tests")

if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def __getattr__(name: str):
    """Dynamically route attribute and submodule lookups to 'tests'."""
    try:
        mod = importlib.import_module(f"tests.{name}")
        sys.modules[f"test.{name}"] = mod
        return mod
    except ImportError:
        pass
    try:
        mod = importlib.import_module(name)
        sys.modules[f"test.{name}"] = mod
        return mod
    except ImportError:
        raise AttributeError(f"module 'test' has no attribute '{name}'")
