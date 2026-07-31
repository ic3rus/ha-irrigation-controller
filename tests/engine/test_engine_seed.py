"""AD-1 seed tests: the engine package is importable without Home Assistant.

These three guards are structural and apply to every module ever added under
`engine/` — the import-blocker walk and the AST scan pick up new modules with
no edit here. The behavioural engine suite lives beside them in
`test_plan.py`, `test_builder.py` and `test_sequencer.py`.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tests.engine.common import VirtualClock

if TYPE_CHECKING:
    from custom_components.ha_irrigation_controller.engine.ports import Clock

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "ha_irrigation_controller"
ENGINE_DIR = INTEGRATION_DIR / "engine"

# Runs in a fresh interpreter that takes the integration dir from argv (never
# templated into the source, which would break on a path containing a quote or a
# trailing backslash) and installs a meta-path hook refusing every homeassistant
# import. It walks EVERY module under engine/ so modules added by later stories
# cannot slip past the guard, and it proves the blocker actually blocks before
# trusting that walk — a broken MetaPathFinder would make this vacuously pass.
_HASS_FREE_IMPORT_PROBE = """
import importlib
import importlib.abc
import pkgutil
import sys


class HomeAssistantBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "homeassistant" or fullname.startswith("homeassistant."):
            msg = "engine/ must not import " + fullname + " (AD-1)"
            raise ImportError(msg)
        return None


sys.path.insert(0, sys.argv[1])
sys.meta_path.insert(0, HomeAssistantBlocker())

try:
    import homeassistant
except ImportError:
    pass
else:
    raise AssertionError("blocker is broken: 'import homeassistant' succeeded")

import engine

walked = []
for module in pkgutil.walk_packages(engine.__path__, "engine."):
    importlib.import_module(module.name)
    walked.append(module.name)

print(" ".join(sorted(walked)))
"""


def test_engine_imports_without_homeassistant() -> None:
    """Every module under engine/ imports where homeassistant.* is forbidden."""
    result = subprocess.run(
        [sys.executable, "-c", _HASS_FREE_IMPORT_PROBE, str(INTEGRATION_DIR)],
        capture_output=True,
        text=True,
        check=False,
        # PYTHONPATH is deliberately not inherited: which `engine` package the
        # probe imports must be decided by argv alone.
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, (
        f"engine/ failed to import hass-free (AD-1 violation?):\n{result.stderr}"
    )
    assert "engine.ports" in result.stdout.split(), (
        f"probe walked no engine modules: {result.stdout!r}"
    )


def test_engine_sources_never_import_homeassistant() -> None:
    """No engine module imports homeassistant, deferred or under TYPE_CHECKING.

    The import-time probe above cannot observe these; only the AST can.
    """
    offenders: list[str] = []
    for path in sorted(ENGINE_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue
            offenders += [
                f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports {name}"
                for name in names
                if name == "homeassistant" or name.startswith("homeassistant.")
            ]

    assert not offenders, f"AD-1 violation: {offenders}"


def test_clock_port_accepts_virtual_clock() -> None:
    """The suite's own virtual clock satisfies the Clock port — time is injected.

    Protocol conformance is a static claim: mypy (CI `Lint` workflow) is what
    actually enforces the annotation below. It type-checks the ONE clock the
    behavioural suites drive, so a change to that double cannot silently stop
    satisfying the port.
    """
    start = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)
    clock: Clock = VirtualClock(start)
    assert clock.now() == start
