"""Make the pure modules importable without Home Assistant installed.

`custom_components/techmark_attendance/__init__.py` imports Home Assistant, so
importing the package normally would need the whole HA runtime just to test a
parser. Registering the package with an explicit `__path__` lets the submodules
and their relative imports resolve while `__init__.py` is never executed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

for name, path in (
    ("custom_components", ROOT / "custom_components"),
    ("custom_components.techmark_attendance", ROOT / "custom_components" / "techmark_attendance"),
):
    if name not in sys.modules:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = module
