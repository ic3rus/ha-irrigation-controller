"""Engine ports — Protocol seams between the hass-free core and its adapters.

No ``homeassistant.*`` imports are allowed in this module (AD-1). The engine
only ever consumes these shapes; the real adapters (service calls with state
verification, the Store journal, the anomaly manager) arrive in Story 1.5.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class Clock(Protocol):
    """Source of 'now' for the engine — injected, never read from the wall."""

    def now(self) -> datetime:
        """Return the current engine time (timezone-aware)."""
        ...


class SwitchPort(Protocol):
    """Actuation seam for valves and the pump.

    Implementations command the entity and report a confirmation outcome.
    The verification mechanics (service call + state watch under a timeout,
    ``Context()``) are the Story 1.5 adapter's job — the engine only consumes
    the confirmed/not-confirmed outcome and never aborts on failure (AD-4).
    """

    async def async_turn_on(self, entity_id: str) -> bool:
        """Command `entity_id` on; return True iff the actuation confirmed."""
        ...

    async def async_turn_off(self, entity_id: str) -> bool:
        """Command `entity_id` off; return True iff the actuation confirmed."""
        ...


class JournalPort(Protocol):
    """Persistence seam for the engine's serializable state snapshot.

    The engine's contract is to call save on EVERY state transition (NFR2);
    whether a save hits disk immediately or after a short debounce is the
    Story 1.5 adapter's policy, invisible here.
    """

    async def async_save(self, snapshot: dict[str, object]) -> None:
        """Persist the given snapshot."""
        ...


class AnomalyKind(StrEnum):
    """What went wrong — the vocabulary the anomaly seam speaks (seed of AD-9).

    The four *_UNCONFIRMED kinds cover a port that reported failure AND a port
    that raised: an adapter is supposed to translate its own exceptions into a
    False outcome, but the engine cannot let a leaked one abort a cycle (AD-4),
    so it treats a raise as "not confirmed" and adds an `error` key to the
    context. JOURNAL_SAVE_FAILED has no confirmation equivalent — a journal
    write either happened or did not.
    """

    PUMP_ON_UNCONFIRMED = "pump_on_unconfirmed"
    PUMP_OFF_UNCONFIRMED = "pump_off_unconfirmed"
    VALVE_OPEN_UNCONFIRMED = "valve_open_unconfirmed"
    VALVE_CLOSE_UNCONFIRMED = "valve_close_unconfirmed"
    JOURNAL_SAVE_FAILED = "journal_save_failed"


class AnomalyPort(Protocol):
    """Reporting seam for anomalies — no feature ever notifies directly (AD-9).

    In 1.4 tests this is a recording fake; Story 1.5 connects it to the
    anomaly-manager seed (notify once, persist until seen).
    """

    def report(self, kind: AnomalyKind, context: dict[str, object]) -> None:
        """Report one anomaly with its context payload."""
        ...
