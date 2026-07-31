"""Event-scheduled sequencer state machine (AD-3 compatible by construction).

The engine never sleeps, loops on wall time or arms timers. It exposes two
methods:

- ``next_wakeup()`` — the earliest pending time intent on requested/running
  work, ``None`` when idle. Daily cycle starts are NOT a wakeup intent: they
  arrive from outside through ``request_cycle`` (tests call it directly;
  Story 1.5 calls it from ``async_track_time_change``, per AD-3's split).
- ``advance(now)`` — perform EVERY transition due at ``now``: command ports,
  update run objects, journal. Idempotent for a given state and ``now``.

Story 1.5's adapter arms exactly ONE re-armed ``async_track_point_in_time``
at ``next_wakeup()`` and calls ``advance(dt_util.now())`` when it fires;
virtual-clock tests drive the identical loop with ``advance_to()``.

Every port call is defended: a port that raises must never abort a cycle and
leave a valve open with no completion path (AD-4). Adapters are expected to
translate their own failures into a not-confirmed outcome; the engine treats
a leaked exception as exactly that and reports it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final

from .plan import derive_schedule, irrigation_day, zone_windows
from .ports import AnomalyKind
from .runs import (
    CycleRun,
    CycleStatus,
    ZoneRun,
    ZoneRunStatus,
    cycle_id_for,
    utc_iso,
)

if TYPE_CHECKING:
    from datetime import datetime

    from .plan import ControllerPlan, CycleKind
    from .ports import AnomalyPort, JournalPort, SwitchPort

# Journal snapshot schema — code-owned (the spine defers it) but versioned
# from day one so the Story 1.5 adapter and future migrations can tell what
# they are loading.
JOURNAL_SCHEMA_VERSION: Final = 1


class Sequencer:
    """Strictly sequential cycle executor with pump orchestration (FR2, FR3).

    One cycle at a time: requesting a cycle while one is active defers the new
    one until the active one completes, then starts it immediately — never
    skipped (AD-4, runtime half of the overlap defense).
    """

    def __init__(
        self,
        plan: ControllerPlan,
        *,
        switches: SwitchPort,
        journal: JournalPort,
        anomalies: AnomalyPort,
    ) -> None:
        """Wire the sequencer to its plan and ports."""
        # Public and replaceable on purpose: a new plan applies to the NEXT
        # requested cycle; the running cycle only ever reads its own snapshot
        # (AD-8 — Story 1.7's reload deferral builds on this seam).
        self.plan = plan
        self._switches = switches
        self._journal = journal
        self._anomalies = anomalies
        self._run: CycleRun | None = None
        self._last_run: CycleRun | None = None
        # Each deferred entry keeps the reference instant of its ORIGINAL
        # request: that is what its configured start (and therefore its
        # irrigation day) is derived from, so a cycle deferred across midnight
        # stays filed under the day it was requested for.
        self._deferred: list[tuple[CycleKind, datetime]] = []
        self._zone_index = 0
        # How many runs of each (irrigation day, kind) have been created —
        # feeds the cycle id's occurrence suffix. Journalled, because a
        # counter that resets on restart would recreate the id collisions the
        # suffix exists to remove.
        self._cycle_counts: dict[str, int] = {}
        # The state machine mutates run state across `await` boundaries. Two
        # overlapping calls — a re-armed timer firing while a slow verified
        # service call is still in flight — would interleave those mutations,
        # duplicate hardware commands and desynchronize `_zone_index`. The
        # second caller waits and then drains whatever is still due.
        self._lock = asyncio.Lock()

    @property
    def current_run(self) -> CycleRun | None:
        """Return the active (pending or running) cycle run, read-only (AD-6)."""
        return self._run

    @property
    def last_run(self) -> CycleRun | None:
        """Return the most recently completed cycle run, read-only (AD-6)."""
        return self._last_run

    @property
    def deferred_kinds(self) -> tuple[CycleKind, ...]:
        """Return the cycle kinds waiting for the active run to complete."""
        return tuple(kind for kind, _ in self._deferred)

    async def request_cycle(self, kind: CycleKind, now: datetime) -> None:
        """Request a cycle start; defers if another run is active.

        The actual transitions (pump on, first zone open) happen in
        ``advance`` — this only records the intent, so the caller's next step
        is always the same: re-arm on ``next_wakeup()``.
        """
        async with self._lock:
            if self._run is not None:
                self._deferred.append((kind, now))
            else:
                self._create_run(kind, now)
            await self._save()

    def next_wakeup(self) -> datetime | None:
        """Return the earliest pending time intent, or None when idle."""
        run = self._run
        if run is None:
            return None
        if run.status is CycleStatus.PENDING:
            return run.scheduled_start
        if self._zone_index >= len(run.zone_runs):
            # Transient window held open across an `await`: the last zone has
            # closed (or the run has no zones at all) and the cycle completes
            # within the same `advance` call. No time intent remains, and this
            # accessor is the adapter's re-arm contract — it must be safe to
            # call at every instant the engine yields.
            return None
        return run.zone_runs[self._zone_index].planned_end

    async def advance(self, now: datetime) -> None:
        """Perform every transition due at `now` (state machine step).

        A single late call drains all missed boundaries: the loop keeps
        transitioning until the next intent lies in the future. Calling twice
        with the same ``now`` performs nothing the second time.
        """
        async with self._lock:
            while (run := self._run) is not None:
                if run.status is CycleStatus.PENDING:
                    if now < run.scheduled_start:
                        return
                    await self._start_cycle(run, now)
                # No bounds guard needed on the index here (unlike in
                # `next_wakeup`, which outside callers reach mid-transition):
                # a run whose last zone has closed is completed and cleared
                # before control returns to this loop, and a zero-zone run
                # never reaches RUNNING with the loop still holding it.
                elif now >= (zone := run.zone_runs[self._zone_index]).planned_end:
                    await self._finish_zone(run, zone, now)
                else:
                    return

    async def _start_cycle(self, run: CycleRun, now: datetime) -> None:
        """Start the cycle: pump on (FR3), journal, then open the first zone."""
        if not run.zone_runs:
            # Nothing to water — never run the pump against a closed manifold.
            run.status = CycleStatus.RUNNING
            await self._save()
            await self._complete_cycle(run, now, pump_was_on=False)
            return
        confirmed, error = await self._command(on=True, entity_id=run.pump_entity_id)
        run.pump_on_confirmed = confirmed
        if not confirmed:
            # Fail-wet (AD-4/FR4): pressure is doubtful, zones still run
            # their slots; aborting would be the one unforgivable branch.
            self._report(
                AnomalyKind.PUMP_ON_UNCONFIRMED,
                {"cycle_id": run.cycle_id, "entity_id": run.pump_entity_id},
                error,
            )
        run.status = CycleStatus.RUNNING
        await self._save()
        await self._open_zone(run, run.zone_runs[0], now)

    async def _open_zone(self, run: CycleRun, zone: ZoneRun, now: datetime) -> None:
        """Open one zone's valve and record the commanded-vs-verified outcome."""
        zone.actual_start = now
        confirmed, error = await self._command(on=True, entity_id=zone.valve_entity_id)
        zone.open_confirmed = confirmed
        if confirmed:
            zone.status = ZoneRunStatus.RUNNING
        else:
            # The slot is still consumed: its planned end stands, and the
            # shortfall becomes ledger material in Epic 2 (AD-4).
            zone.status = ZoneRunStatus.FAILED
            self._report(
                AnomalyKind.VALVE_OPEN_UNCONFIRMED,
                {
                    "cycle_id": run.cycle_id,
                    "zone_id": zone.zone_id,
                    "entity_id": zone.valve_entity_id,
                },
                error,
            )
        await self._save()

    async def _finish_zone(self, run: CycleRun, zone: ZoneRun, now: datetime) -> None:
        """Close one zone, then open the next or complete the cycle.

        The close is ALWAYS commanded before the next open — FR2 governs the
        commanded sequence. An unconfirmed close (possibly a stuck-open valve)
        raises the anomaly and the sequence proceeds: bounded over-watering is
        consequence-free (fail-wet), stalling the cycle is not.
        """
        confirmed, error = await self._command(on=False, entity_id=zone.valve_entity_id)
        zone.close_confirmed = confirmed
        zone.actual_end = now
        if not confirmed:
            self._report(
                AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
                {
                    "cycle_id": run.cycle_id,
                    "zone_id": zone.zone_id,
                    "entity_id": zone.valve_entity_id,
                },
                error,
            )
        if zone.status is not ZoneRunStatus.FAILED:
            zone.status = ZoneRunStatus.COMPLETED
        self._zone_index += 1
        await self._save()
        if self._zone_index < len(run.zone_runs):
            await self._open_zone(run, run.zone_runs[self._zone_index], now)
        else:
            await self._complete_cycle(run, now, pump_was_on=True)

    async def _complete_cycle(
        self,
        run: CycleRun,
        now: datetime,
        *,
        pump_was_on: bool,
    ) -> None:
        """Pump off after the last zone closes (FR3), then release the slot.

        A deferred cycle is re-created immediately, dispatched at completion —
        ``advance``'s loop starts it in the same call, so a deferred cycle is
        delayed, never skipped (AD-4).
        """
        if pump_was_on:
            confirmed, error = await self._command(
                on=False,
                entity_id=run.pump_entity_id,
            )
            run.pump_off_confirmed = confirmed
            if not confirmed:
                self._report(
                    AnomalyKind.PUMP_OFF_UNCONFIRMED,
                    {"cycle_id": run.cycle_id, "entity_id": run.pump_entity_id},
                    error,
                )
        run.status = CycleStatus.COMPLETED
        await self._save()
        self._last_run = run
        self._run = None
        self._zone_index = 0
        if self._deferred:
            kind, reference = self._deferred.pop(0)
            self._create_run(kind, reference, dispatch_at=now)
            await self._save()

    def _create_run(
        self,
        kind: CycleKind,
        reference: datetime,
        *,
        dispatch_at: datetime | None = None,
    ) -> None:
        """Snapshot the plan into a new pending run (AD-8: owned copies).

        The run's windows come from the plan's configured start time on
        `reference`'s day — the plan is the single source of the schedule
        (AD-6/FR1), so the operator's start time is what the engine executes,
        not whatever instant the caller happened to fire at. A deferred cycle
        passes `dispatch_at` to start right away instead of waiting for its
        configured start, while still keeping that start for its identity.
        """
        schedule = derive_schedule(self.plan, kind, reference)
        scheduled_start = schedule.start if dispatch_at is None else dispatch_at
        windows = (
            schedule.zones
            if dispatch_at is None
            else zone_windows(self.plan, kind, dispatch_at)
        )
        self._run = CycleRun(
            cycle_id=cycle_id_for(
                kind,
                schedule.start,
                self._next_occurrence(kind, schedule.start),
            ),
            kind=kind,
            configured_start=schedule.start,
            scheduled_start=scheduled_start,
            pump_entity_id=self.plan.pump_entity_id,
            zone_runs=tuple(
                ZoneRun(
                    zone_id=spec.zone_id,
                    name=spec.name,
                    valve_entity_id=spec.valve_entity_id,
                    duration_s=spec.duration_s(kind),
                    planned_start=window.start,
                    planned_end=window.end,
                )
                for spec, window in zip(self.plan.zones, windows, strict=True)
            ),
        )
        self._zone_index = 0

    def _next_occurrence(self, kind: CycleKind, configured_start: datetime) -> int:
        """Return (and record) this run's occurrence number for its day+kind."""
        key = f"{irrigation_day(configured_start).isoformat()}-{kind.value}"
        occurrence = self._cycle_counts.get(key, 0) + 1
        self._cycle_counts[key] = occurrence
        return occurrence

    async def _command(self, *, on: bool, entity_id: str) -> tuple[bool, str | None]:
        """Command one entity, converting a leaked port exception into failure.

        Returns (confirmed, error). An adapter is supposed to report failure
        as False; if one raises instead, letting it propagate would abandon
        the cycle mid-flight with a valve open and no re-arm — the one branch
        AD-4 forbids.
        """
        try:
            if on:
                return await self._switches.async_turn_on(entity_id), None
            return await self._switches.async_turn_off(entity_id), None
        except Exception as err:  # noqa: BLE001 — see docstring: never abort a cycle
            return False, repr(err)

    def _report(
        self,
        kind: AnomalyKind,
        context: dict[str, object],
        error: str | None = None,
    ) -> None:
        """Report one anomaly, tolerating an anomaly port that itself fails."""
        if error is not None:
            context = {**context, "error": error}
        # Nothing left to report the failure to, and a broken notification
        # seam must not be what stops the water.
        with contextlib.suppress(Exception):
            self._anomalies.report(kind, context)

    async def _save(self) -> None:
        """Journal the current state — called on EVERY transition (NFR2).

        The engine never touches storage: it awaits the port and moves on;
        debounce/immediacy is the adapter's policy.

        The snapshot carries everything needed to rebuild the machine, not
        just to display it: `zone_index` says which zone is open (a FAILED
        zone looks identical whether it is the current slot or a finished
        one), `last_run` keeps the completed cycle that would otherwise be
        overwritten the moment a deferred cycle is created, and the deferred
        queue keeps each entry's reference instant.
        """
        last_run = self._last_run
        snapshot: dict[str, object] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "run": self._run.as_dict() if self._run is not None else None,
            "zone_index": self._zone_index,
            "last_run": last_run.as_dict() if last_run is not None else None,
            "cycle_counts": dict(self._cycle_counts),
            "deferred": [
                {"kind": kind.value, "reference": utc_iso(reference)}
                for kind, reference in self._deferred
            ],
        }
        try:
            await self._journal.async_save(snapshot)
        except Exception as err:  # noqa: BLE001 — a failed save must not stop the water
            self._report(AnomalyKind.JOURNAL_SAVE_FAILED, {}, repr(err))
