"""Binary sensor platform entry point.

MUST live at the package ROOT: HA's loader imports `<pkg_path>.binary_sensor`
(`loader.Integration._import_platform`), so a platform module under
`entities/` is never found. The implementation stays in
`entities/binary_sensor.py` with the other platforms — this module is only
the re-export that satisfies the loader.
"""

from __future__ import annotations

from typing import Final

from .entities.binary_sensor import async_setup_entry

# Zero: the health entity is pushed by the engine-state dispatcher and never
# polls, so HA has no update calls to serialize (the platform convention wants
# the constant declared either way).
PARALLEL_UPDATES: Final = 0

__all__ = ["async_setup_entry"]
