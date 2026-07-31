"""Run objects — first-class, status-enum records of cycle execution.

Runs are created and mutated by the sequencer ONLY (AD-6); everything outside
the engine reads them. They snapshot their parameters from the plan at cycle
start as owned copies (AD-8), so a mid-cycle plan change never touches a
running cycle — Story 1.7 relies on exactly this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from .plan import irrigation_day

if TYPE_CHECKING:
    from .plan import CycleKind


class CycleStatus(StrEnum):
    """Lifecycle of a cycle run.

    COMPLETED is the only terminal status today; a future CANCELLED
    (Story 1.6) joins as a second terminal status without redesign — nothing
    below assumes "terminal == completed".
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


class ZoneRunStatus(StrEnum):
    """Lifecycle of one zone's slot within a cycle run.

    FAILED means the open was never confirmed — the slot is still consumed
    (fail-wet, AD-4) and the shortfall becomes ledger material in Epic 2.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _utc_iso(moment: datetime | None) -> str | None:
    """Serialize an aware datetime as a UTC ISO-8601 string (conventions).

    The engine works in HA-local time; UTC conversion happens ONLY here, at
    the serialization boundary.
    """
    return None if moment is None else moment.astimezone(UTC).isoformat()


@dataclass(slots=True)
class ZoneRun:
    """One zone's slot in a cycle run — parameters snapshotted from the plan."""

    zone_id: str
    name: str
    valve_entity_id: str
    duration_s: int
    planned_start: datetime
    planned_end: datetime
    status: ZoneRunStatus = ZoneRunStatus.PENDING
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    # Commanded vs verified: None = not commanded yet; True/False = the
    # confirmation outcome the switch port reported (verification mechanics
    # are the Story 1.5 adapter's job).
    open_confirmed: bool | None = None
    close_confirmed: bool | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a plain serializable snapshot of this zone run."""
        return {
            "zone_id": self.zone_id,
            "name": self.name,
            "valve_entity_id": self.valve_entity_id,
            "duration_s": self.duration_s,
            "planned_start": _utc_iso(self.planned_start),
            "planned_end": _utc_iso(self.planned_end),
            "status": self.status.value,
            "actual_start": _utc_iso(self.actual_start),
            "actual_end": _utc_iso(self.actual_end),
            "open_confirmed": self.open_confirmed,
            "close_confirmed": self.close_confirmed,
        }


@dataclass(slots=True)
class CycleRun:
    """One cycle run: stable id, snapshotted parameters, per-zone runs."""

    cycle_id: str
    kind: CycleKind
    scheduled_start: datetime
    pump_entity_id: str
    zone_runs: tuple[ZoneRun, ...]
    status: CycleStatus = CycleStatus.PENDING
    pump_on_confirmed: bool | None = None
    pump_off_confirmed: bool | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a plain serializable snapshot of this cycle run."""
        return {
            "cycle_id": self.cycle_id,
            "kind": self.kind.value,
            "scheduled_start": _utc_iso(self.scheduled_start),
            "pump_entity_id": self.pump_entity_id,
            "status": self.status.value,
            "pump_on_confirmed": self.pump_on_confirmed,
            "pump_off_confirmed": self.pump_off_confirmed,
            "zones": [zone.as_dict() for zone in self.zone_runs],
        }


def cycle_id_for(kind: CycleKind, scheduled_start: datetime) -> str:
    """Return the stable cycle id: irrigation day + kind, e.g. 2026-07-31-evening.

    Deterministic on purpose so Epic 2's settlement can key idempotent writes
    off it (AD-5); a run-now variant joins in Epic 2.
    """
    return f"{irrigation_day(scheduled_start).isoformat()}-{kind.value}"
