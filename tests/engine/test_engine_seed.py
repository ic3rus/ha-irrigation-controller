"""AD-1 seed tests: the engine package is importable without Home Assistant.

The real engine suite (virtual clock, sequencer) arrives with Story 1.4.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from custom_components.ha_irrigation_controller.engine.ports import Clock

REPO_ROOT = Path(__file__).resolve().parents[2]

# Runs in a fresh interpreter: any homeassistant.* import anywhere under
# engine/ (even transitive) raises and fails the subprocess.
_HASS_FREE_IMPORT_PROBE = """
import importlib.abc
import sys

class HomeAssistantBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "homeassistant" or fullname.startswith("homeassistant."):
            msg = "engine/ must not import " + fullname + " (AD-1)"
            raise ImportError(msg)
        return None

sys.meta_path.insert(0, HomeAssistantBlocker())
sys.path.insert(0, r"@INTEGRATION_DIR@")

import engine
import engine.ports
"""


def test_engine_imports_without_homeassistant() -> None:
    """engine/ imports in an interpreter where homeassistant.* is forbidden."""
    integration_dir = REPO_ROOT / "custom_components" / "ha_irrigation_controller"
    probe = _HASS_FREE_IMPORT_PROBE.replace("@INTEGRATION_DIR@", str(integration_dir))

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"engine/ failed to import hass-free (AD-1 violation?):\n{result.stderr}"
    )


def test_clock_port_accepts_virtual_clock() -> None:
    """A trivial virtual clock satisfies the Clock port — time is injected."""

    class VirtualClock:
        def __init__(self, start: datetime) -> None:
            self._now = start

        def now(self) -> datetime:
            return self._now

        def advance_to(self, moment: datetime) -> None:
            self._now = moment

    start = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)
    clock: Clock = VirtualClock(start)
    assert clock.now() == start
