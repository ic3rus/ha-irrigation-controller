"""Engine tests run on plain pytest — zero Home Assistant fixtures (AD-1).

Overrides the root autouse fixture, which would otherwise drag the whole PHCC
harness (bluetooth, zeroconf and network mocks, translation and bcrypt fixtures)
into the hass-free suite and make it depend on Home Assistant being installed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

try:
    import homeassistant  # noqa: F401
except ImportError:
    # The engine-hass-free CI job runs this suite with NO homeassistant
    # installed — that absence is the proof of AD-1. But the test modules
    # import the engine by its full dotted path, and importing
    # `custom_components.ha_irrigation_controller.engine.plan` would first
    # execute the integration's `__init__.py`, which imports homeassistant.
    # Register namespace-style parent stubs whose `__path__` points at the
    # real directories: the engine submodules then import normally while the
    # integration's `__init__.py` never executes. With homeassistant present
    # (the PHCC suite) this block is inert and the real packages load.
    _integration_dir = (
        Path(__file__).resolve().parents[2]
        / "custom_components"
        / "ha_irrigation_controller"
    )
    for _name, _dir in (
        ("custom_components", _integration_dir.parent),
        ("custom_components.ha_irrigation_controller", _integration_dir),
    ):
        if _name not in sys.modules:
            _module = types.ModuleType(_name)
            _module.__path__ = [str(_dir)]
            sys.modules[_name] = _module


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """Neutralise the root HA fixture for the engine suite."""
