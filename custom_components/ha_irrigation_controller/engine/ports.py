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

    async def async_is_on(self, entity_id: str) -> bool | None:
        """Read `entity_id`'s ACTUAL state: True on, False off, None doubtful.

        The resync input of Story 3.2's reconciler (AD-11): after a restart
        the journal says what was intended, this says what the hardware is
        doing. `None` covers an entity that is missing, `unknown` or
        `unavailable` (governed switches may still be booting when the
        integration loads) — the engine reads it as "not OFF" for the orphan
        pass and as "watering unproven" for the credit rule, both fail-wet.
        """
        ...


class RainPort(Protocol):
    """Read seam for the rain gauge (FR14, FR15) — the rain credit's ONE input.

    `total_mm()` returns the gauge's CUMULATIVE precipitation total in
    millimetres, or `None` when the reading is doubtful — missing entity,
    unknown/unavailable, non-numeric, non-finite, negative, or an unknown
    unit. Units and validation are the ADAPTER's job (`adapters/rain.py`
    converts `cm` and `in` to mm); the engine never sees a unit. `None` means
    "no modulation for this quote", never an anomaly: fail-wet (AD-4) reads a
    doubtful gauge as "it did not rain".

    `source_id` is the gauge's IDENTITY (Story 2.5) — an opaque string the
    engine compares for equality and nothing else. The ledger banks its rain
    baselines under the source that produced them, and a total from any
    other source is not comparable to them: baselines banked under one
    source never credit another. So swapping the gauge for one whose
    cumulative total is higher credits nothing (instead of its whole
    lifetime total) and simply re-banks under the new source — the fail-wet
    direction. The adapter answers with the configured entity id, so a
    rename costs one banking cycle too; accepted.

    Synchronous on purpose: a Home Assistant state read is synchronous, and
    the sequencer quotes inside `_build_run`, which is synchronous too. The
    engine reads the port exactly once per QUOTE (`_build_run` — a scheduled
    dispatch that ends waived and a deferred pop are quotes too) and
    snapshots the value AND the source on the run (AD-8) — never during or
    after a cycle.
    """

    @property
    def source_id(self) -> str:
        """Return the opaque identity of the gauge this port reads."""
        ...

    def total_mm(self) -> float | None:
        """Return the cumulative rain total in mm, or None when doubtful."""
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

    Two kinds (Story 1.7) are raised about the CONFIGURATION rather than about
    a command, so that the anomaly seam stays the one vocabulary every fault
    speaks (AD-9) instead of growing a second channel per feature:

    - CONFIGURED_ENTITY_MISSING — a pump, valve or sensor the entry stores has
      been removed from (or disabled in) Home Assistant's entity registry.
      Nothing is skipped or unscheduled: the stored id stays, and the next
      cycle's commands raise the usual *_UNCONFIRMED kinds (fail-wet). Context:
      `entity_id`, `role` (the option / subentry key) and `zone_id` for a
      valve.
    - CYCLE_INTERRUPTED — an active cycle was suspended because the entry was
      unloaded from outside the engine's control (integration disabled or
      removed, a forced reload). The live valve and the pump were commanded
      off but the run itself was NOT completed or cancelled: the journal
      keeps its RUNNING/PENDING intent for Story 3.2's reconciler. Context:
      `cycle_id`, `kind`, `zone_id` (the live zone, None when PENDING).
    - CYCLE_RECOVERED (Story 3.2) — the reconciler found an in-flight run in
      the journal at startup and acted on it. Context: `cycle_id`, `kind`,
      `zone_id` (the zone that was live at the crash, None when none was)
      and `outcome`: `resumed` (same irrigation day — the cycle continues on
      its journaled durations), `closed` (a later day, or a PENDING run
      under season OFF — the run is filed `interrupted` and its shortfalls
      reach the ledger) or `discarded` (the journaled run was unreadable —
      every configured switch was commanded off and nothing was booked).
      Controller-level, like CYCLE_INTERRUPTED, and NEVER cleared by the
      engine: a recovery is news the operator acknowledges.
    - MISSED_CYCLE (Story 3.3) — a scheduled cycle whose window closed with
      no record of it at all: Home Assistant was down over the window, the
      daily tracker never fired (a start inside the spring-forward gap), or
      the dispatch itself raised. The watchdog files the cycle to history as
      `missed` and, unless a later cycle of the same irrigation day has
      already run, re-dispatches it at once as a late re-run — at most one
      per missed cycle, ever. Context: `cycle_id`, `kind` and `outcome`:
      `rerun` (the cycle is watering now, on freshly quoted durations) or
      `recorded` (no re-run; each zone's shortfall was booked to the ledger,
      capped at one base duration). Controller-level, like CYCLE_RECOVERED,
      and NEVER cleared by the engine: a missed cycle is news the operator
      acknowledges. A PERMITTED non-watering cause is never reported here —
      the season being off, a disabled morning cycle, a plan with no zones,
      a cycle already in history in any status (completed, waived, cancelled
      or interrupted), a live or deferred run for that day and kind, and an
      unconsumed day credit are all silent.
    - MANUAL_VALVE_TIMEOUT (Story 3.5) — a governed switch the operator opened
      BY HAND held the scheduler paused for the configured safety timeout, so
      the engine closed it itself through the verified port and released the
      pause. Context: `entity_id` (the switch that was closed) and `zone_id`
      (the zone whose valve it is, None for the pump and for a valve whose
      zone was deleted while it was held). Raised per switch, once per manual
      open: expiry never retries and never re-arms. An unconfirmed close
      raises VALVE_CLOSE_UNCONFIRMED / PUMP_OFF_UNCONFIRMED alongside it and
      the switch is released anyway — a pause must never survive its own
      timeout. NEVER cleared by the engine, like CYCLE_RECOVERED and
      MISSED_CYCLE: it is news the operator acknowledges. Silent in two
      cases: a switch closed BY HAND before its deadline (nothing commanded,
      nothing reported), and a switch the RUNNING cycle itself holds open —
      the cycle's claim wins, the deadline is spent and the slot's own close
      releases it.
    """

    PUMP_ON_UNCONFIRMED = "pump_on_unconfirmed"
    PUMP_OFF_UNCONFIRMED = "pump_off_unconfirmed"
    VALVE_OPEN_UNCONFIRMED = "valve_open_unconfirmed"
    VALVE_CLOSE_UNCONFIRMED = "valve_close_unconfirmed"
    JOURNAL_SAVE_FAILED = "journal_save_failed"
    CONFIGURED_ENTITY_MISSING = "configured_entity_missing"
    CYCLE_INTERRUPTED = "cycle_interrupted"
    CYCLE_RECOVERED = "cycle_recovered"
    MISSED_CYCLE = "missed_cycle"
    MANUAL_VALVE_TIMEOUT = "manual_valve_timeout"


class AnomalyPort(Protocol):
    """Reporting seam for anomalies — no feature ever notifies directly (AD-9).

    In the engine suites this is a recording fake; the adapter behind it is
    the anomaly manager (notify once, persist until seen — Story 3.1).

    `clear` is the OTHER half of the seam: the engine says "healthy again"
    about a subject it just confirmed, and the manager closes whatever it has
    open for that kind and subject. The engine calls it on every confirmed
    actuation, every successful save and every terminal cycle status — it
    never knows whether anything was open, so clearing nothing must be a
    no-op on the adapter side. Contexts carry the same keys as the matching
    `report` (`entity_id`, `zone_id`, `cycle_id`), which is how the adapter
    finds the subject.
    """

    def report(self, kind: AnomalyKind, context: dict[str, object]) -> None:
        """Report one anomaly with its context payload."""
        ...

    def clear(self, kind: AnomalyKind, context: dict[str, object]) -> None:
        """Declare `kind` healthy again for the subject `context` names."""
        ...
