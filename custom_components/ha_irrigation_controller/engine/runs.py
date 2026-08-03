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


def utc_iso(moment: datetime | None) -> str | None:
    """Serialize an aware datetime as a UTC ISO-8601 string (conventions).

    The engine works in HA-local time; UTC conversion happens ONLY here, at
    the serialization boundary. Public because the sequencer's own snapshot
    (the deferred queue's reference instants) serializes through it too.
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
            "planned_start": utc_iso(self.planned_start),
            "planned_end": utc_iso(self.planned_end),
            "status": self.status.value,
            "actual_start": utc_iso(self.actual_start),
            "actual_end": utc_iso(self.actual_end),
            "open_confirmed": self.open_confirmed,
            "close_confirmed": self.close_confirmed,
        }


def effective_seconds(zone_run: ZoneRun) -> int:
    """Return the seconds this zone actually watered — THE single helper.

    Zero when the open never confirmed (status FAILED: the valve never opened,
    so nothing was watered) and zero while either instant is still missing.
    Otherwise the elapsed open-to-close time — an unconfirmed CLOSE still
    watered its slot (possibly longer, which fail-wet accepts, AD-4).

    Epic 2's deficit input and Story 1.5's per-zone sensor both read THIS
    function: AD-5's "no feature re-implements the math" starts here.
    """
    if zone_run.status is ZoneRunStatus.FAILED:
        return 0
    if zone_run.actual_start is None or zone_run.actual_end is None:
        return 0
    return int((zone_run.actual_end - zone_run.actual_start).total_seconds())


@dataclass(slots=True)
class CycleRun:
    """One cycle run: stable id, snapshotted parameters, per-zone runs.

    Two start instants, deliberately distinct:

    - `configured_start` is the plan's derived start for this cycle on its
      reference day — the operator's intent. It NEVER moves, so it is what
      the cycle id and the irrigation day key off.
    - `scheduled_start` is when this run is actually dispatched. It equals
      `configured_start` for a cycle that starts on time, and the completion
      instant of the preceding cycle for a deferred one (AD-4: delayed, never
      skipped). Collapsing the two would either make a deferred cycle wait for
      tomorrow's configured start or file a past-midnight cycle under the
      wrong irrigation day.
    """

    cycle_id: str
    kind: CycleKind
    configured_start: datetime
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
            "configured_start": utc_iso(self.configured_start),
            "scheduled_start": utc_iso(self.scheduled_start),
            "pump_entity_id": self.pump_entity_id,
            "status": self.status.value,
            "pump_on_confirmed": self.pump_on_confirmed,
            "pump_off_confirmed": self.pump_off_confirmed,
            "zones": [zone.as_dict() for zone in self.zone_runs],
        }


def cycle_id_for(
    kind: CycleKind,
    configured_start: datetime,
    occurrence: int = 1,
) -> str:
    """Return the cycle id: irrigation day + kind, e.g. 2026-07-31-evening.

    Deterministic on purpose so Epic 2's settlement can key idempotent writes
    off it (AD-5) — which requires it to be UNIQUE as well. The deferral queue
    accepts repeated requests for the same kind, so the second and later runs
    of a kind on one irrigation day carry an occurrence suffix
    (`2026-07-31-morning-2`). The first run of a kind keeps the bare id, so the
    scheduled cycle every other feature keys off never changes shape.
    """
    base = f"{irrigation_day(configured_start).isoformat()}-{kind.value}"
    return base if occurrence <= 1 else f"{base}-{occurrence}"
