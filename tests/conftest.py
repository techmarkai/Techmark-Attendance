"""Test setup for two different worlds.

The pure modules (stream/hr/stats/store/report/processor/isapi) are meant to be
testable without the Home Assistant runtime. But
`custom_components/techmark_attendance/__init__.py` imports Home Assistant, so
importing the package normally would drag in the whole runtime just to test a
parser.

So: when Home Assistant is *not* installed, register the package with an
explicit `__path__`, which lets the submodules and their relative imports
resolve while `__init__.py` is never executed.

When Home Assistant *is* installed (the `.venv` used for the runtime tests),
the stub must not be registered. Home Assistant's own loader imports
`custom_components.techmark_attendance` for real and looks up
`async_setup_entry` on it; a stub module whose `__init__.py` never ran has no
such attribute, and every runtime test would fail for a reason that has nothing
to do with the integration.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

HA_INSTALLED = importlib.util.find_spec("homeassistant") is not None

if HA_INSTALLED:
    # Let the real package be imported, and make sure it is importable at all
    # (pytest's rootdir is not automatically on sys.path).
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    pytest_plugins = "pytest_homeassistant_custom_component"

    @pytest.fixture(autouse=True)
    def _allow_custom_integrations(enable_custom_integrations):
        """Home Assistant refuses to load custom components in tests without this."""
        return None

else:
    for name, path in (
        ("custom_components", ROOT / "custom_components"),
        (
            "custom_components.techmark_attendance",
            ROOT / "custom_components" / "techmark_attendance",
        ),
    ):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [str(path)]  # type: ignore[attr-defined]
            sys.modules[name] = module
