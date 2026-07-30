"""Engine ports — Protocol seams between the hass-free core and its adapters.

Placeholder seed: the real port surface is defined by Story 1.4.
No ``homeassistant.*`` imports are allowed in this module (AD-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class Clock(Protocol):
    """Source of 'now' for the engine — injected, never read from the wall."""

    def now(self) -> datetime:
        """Return the current engine time (timezone-aware)."""
        ...
