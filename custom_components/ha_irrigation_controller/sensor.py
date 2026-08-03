"""Sensor platform entry point.

MUST live at the package ROOT: HA's loader imports `<pkg_path>.sensor`
(`loader.Integration._import_platform`), so a platform module under
`entities/` is never found. The implementation stays in `entities/sensor.py`
with the other platforms — this module is only the re-export that satisfies
the loader.
"""

from __future__ import annotations

from typing import Final

from .entities.sensor import async_setup_entry

# Zero: these entities are pushed by the engine-state dispatcher and never
# poll, so HA has no update calls to serialize (the platform convention wants
# the constant declared either way).
PARALLEL_UPDATES: Final = 0

__all__ = ["async_setup_entry"]
