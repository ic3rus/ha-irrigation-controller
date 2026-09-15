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
from typing import TYPE_CHECKING, Final

from .plan import irrigation_day

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .plan import CycleKind

# The serialized key carrying Story 2.1's manual marker, in BOTH the run
# snapshot and the history record. Named once so the journal adapter, the
# history reader and Story 2.3's day credit cannot spell it three ways.
MANUAL_KEY: Final = "manual"


class CycleStatus(StrEnum):
    """Lifecycle of a cycle run.

    Three TERMINAL statuses: COMPLETED (the cycle ran its course), CANCELLED
    (Story 1.6's explicit operator cancel, the only thing that stops a running
    cycle) and WAIVED (Story 2.3: a scheduled cycle the ledger's day credit
    excused because a completed run-now had already watered its irrigation
    day). WAIVED is terminal AT BIRTH — such a run is built, filed to history
    and dropped in one step; it is never `current_run`, never `last_run`,
    never settled and commands nothing. Nothing anywhere assumes "terminal ==
    completed" — the completion path is shared and takes the terminal status
    as a parameter, and every reader keys off the value rather than off the
    absence of a run.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    WAIVED = "waived"


class ZoneRunStatus(StrEnum):
    """Lifecycle of one zone's slot within a cycle run.

    FAILED means the open was never confirmed — the slot is still consumed
    (fail-wet, AD-4) and the shortfall becomes ledger material in Epic 2.

    SKIPPED (Story 2.4) is a slot quoted at ZERO seconds: real rain covered
    the zone's whole base duration and it owed nothing. The valve is never
    commanded (no open, no close, no `*_UNCONFIRMED` anomaly), the slot has
    no instants so `effective_seconds` reads 0, and its quote was 0 so the
    ledger books no deficit. It is a ZONE status, not a cycle status: a cycle
    with one skipped zone still COMPLETES, and a cycle whose zones are all
    skipped completes without ever starting the pump. Epic 3's watchdog reads
    a skipped zone with `rain_credit_s > 0` as a PERMITTED non-watering
    cause, never as a miss.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


def utc_iso(moment: datetime | None) -> str | None:
    """Serialize an aware datetime as a UTC ISO-8601 string (conventions).

    The engine works in HA-local time; UTC conversion happens ONLY here, at
    the serialization boundary. Public because the sequencer's own snapshot
    (the deferred queue's reference instants) serializes through it too.
    """
    return None if moment is None else moment.astimezone(UTC).isoformat()


@dataclass(slots=True)
class ZoneRun:
    """One zone's slot in a cycle run — parameters snapshotted from the plan.

    Four durations, all seconds, all frozen at quote time (Stories 2.2, 2.4):

    - `base_s` is the plan's duration for this cycle kind — the snapshot the
      ledger settles against, never the live plan (AD-8).
    - `carried_s` is the deficit the ledger applied to this run (≤ `base_s`).
    - `rain_credit_s` is the rain credit the ledger subtracted from the base
      for this run (Story 2.4): `floor(mm since the zone's previous settled
      cycle * rain_factor * 60)`, 0 for a sheltered zone, a zone with no
      baseline yet, a factor of 0 or a doubtful gauge. It may exceed
      `base_s`; the formula clamps, this field does not — it records what
      the rain was worth, which is what history shows (Epic 4).
    - `duration_s` is the QUOTED effective duration the slot actually runs:
      `max(0, base_s - rain_credit_s) + carried_s`. The planned window is
      derived from it, so a carried deficit really does extend the run — and
      a zero here means the slot is SKIPPED, never commanded.
    """

    zone_id: str
    name: str
    valve_entity_id: str
    duration_s: int
    base_s: int
    planned_start: datetime
    planned_end: datetime
    carried_s: int = 0
    rain_credit_s: int = 0
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
            "base_s": self.base_s,
            "carried_s": self.carried_s,
            "rain_credit_s": self.rain_credit_s,
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

    ROUNDED to the nearest second, not truncated: both instants are timer-fire
    wall-clock reads, so a healthy 600 s slot whose open fired a few ms later
    than its close spans 599.99 s — truncation would read 599 and the ledger
    would book a spurious 1 s deficit on roughly every other zone.

    Clamped at zero: `now` comes from the wall clock, which is not monotonic —
    an NTP correction, a manual time change or a VM snapshot restore between a
    zone's open and its close would otherwise produce a negative DURATION on a
    `MEASUREMENT` sensor and a negative deficit input.
    """
    if zone_run.status is ZoneRunStatus.FAILED:
        return 0
    if zone_run.actual_start is None or zone_run.actual_end is None:
        return 0
    return max(0, round((zone_run.actual_end - zone_run.actual_start).total_seconds()))


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

    `manual` is Story 2.1's run-now marker — a FIELD, deliberately not a third
    `CycleKind`: the kind is also the cycle-id component and the per-zone
    duration-map key, and a manual cycle waters a real morning or evening
    kind. It defaults to False, so every run created by the scheduled path is
    scheduled without that path mentioning it.

    `rain_total_mm` (Story 2.4) is the gauge's cumulative total AS READ AT
    QUOTE TIME, or None when the gauge was doubtful (or there is none). It is
    frozen with the rest of the snapshot (AD-8): a resumed or running cycle
    is never re-quoted, and this is the value the ledger advances every
    settled zone's rain baseline to — so rain falling DURING the cycle is
    banked for the next one rather than lost. Settlement needs no second
    gauge read because of it.

    `rain_source` (Story 2.5) is the identity of the gauge that produced
    `rain_total_mm` — the rain port's `source_id`, frozen at quote time next
    to the total and serialized with it, None when there is no gauge. The
    ledger compares it to the source its baselines were banked under when
    it quotes (a foreign gauge credits nothing) and stamps it as the new
    source when the run settles with a readable total. Snapshotting it here
    is what lets settlement re-bank under the right gauge without a second
    port read, even after a restart or a mid-cycle sensor change.
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
    manual: bool = False
    rain_total_mm: float | None = None
    rain_source: str | None = None

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
            MANUAL_KEY: self.manual,
            "rain_total_mm": self.rain_total_mm,
            "rain_source": self.rain_source,
            "zones": [zone.as_dict() for zone in self.zone_runs],
        }


def is_manual(record: Mapping[str, object]) -> bool:
    """Return whether a SERIALIZED run or history record is a manual run.

    THE reader of the marker (AD-5: one home for the rule). The journal
    carries `JOURNAL_SCHEMA_VERSION` with no migration, so a document written
    before Story 2.1 simply has no such key — and a restored backup or a hand
    edit can put anything at all there. Both read as scheduled: only a real
    `True` marks a manual run, which is the conservative direction (Story
    2.3's day credit is a reason NOT to water, and doubt waters — AD-4).
    """
    return record.get(MANUAL_KEY) is True


def cycle_id_for(
    kind: CycleKind,
    configured_start: datetime,
    occurrence: int = 1,
) -> str:
    """Return the cycle id: irrigation day + kind, e.g. 2026-07-31-evening.

    Deterministic on purpose so Epic 2's settlement can key idempotent writes
    off it (AD-5) — which requires it to be UNIQUE as well. The deferral queue
    accepts repeated requests for the same kind, and Story 2.1's run-now adds
    another way to run one twice, so the second and later runs of a kind on one
    irrigation day carry an occurrence suffix (`2026-07-31-morning-2`).

    The suffix promises uniqueness and NOTHING about which run gets which
    number: occurrences are handed out in creation order, so a run-now fired
    before the morning cycle is due takes the bare id and files that day's
    scheduled morning cycle as `-2`. Nothing may key off the bare form.
    """
    base = f"{irrigation_day(configured_start).isoformat()}-{kind.value}"
    return base if occurrence <= 1 else f"{base}-{occurrence}"
