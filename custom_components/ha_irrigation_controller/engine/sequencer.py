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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from .plan import zone_windows
from .ports import AnomalyKind
from .runs import CycleRun, CycleStatus, ZoneRun, ZoneRunStatus, cycle_id_for

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
        self._deferred: list[CycleKind] = []
        self._zone_index = 0

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
        return tuple(self._deferred)

    async def request_cycle(self, kind: CycleKind, now: datetime) -> None:
        """Request a cycle start; defers if another run is active.

        The actual transitions (pump on, first zone open) happen in
        ``advance`` — this only records the intent, so the caller's next step
        is always the same: re-arm on ``next_wakeup()``.
        """
        if self._run is not None:
            self._deferred.append(kind)
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
        return run.zone_runs[self._zone_index].planned_end

    async def advance(self, now: datetime) -> None:
        """Perform every transition due at `now` (state machine step).

        A single late call drains all missed boundaries: the loop keeps
        transitioning until the next intent lies in the future. Calling twice
        with the same ``now`` performs nothing the second time.
        """
        while (run := self._run) is not None:
            if run.status is CycleStatus.PENDING:
                if now < run.scheduled_start:
                    return
                await self._start_cycle(run, now)
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
        confirmed = await self._switches.async_turn_on(run.pump_entity_id)
        run.pump_on_confirmed = confirmed
        if not confirmed:
            # Fail-wet (AD-4/FR4): pressure is doubtful, zones still run
            # their slots; aborting would be the one unforgivable branch.
            self._anomalies.report(
                AnomalyKind.PUMP_ON_UNCONFIRMED,
                {"cycle_id": run.cycle_id, "entity_id": run.pump_entity_id},
            )
        run.status = CycleStatus.RUNNING
        await self._save()
        await self._open_zone(run, run.zone_runs[0], now)

    async def _open_zone(self, run: CycleRun, zone: ZoneRun, now: datetime) -> None:
        """Open one zone's valve and record the commanded-vs-verified outcome."""
        zone.actual_start = now
        confirmed = await self._switches.async_turn_on(zone.valve_entity_id)
        zone.open_confirmed = confirmed
        if confirmed:
            zone.status = ZoneRunStatus.RUNNING
        else:
            # The slot is still consumed: its planned end stands, and the
            # shortfall becomes ledger material in Epic 2 (AD-4).
            zone.status = ZoneRunStatus.FAILED
            self._anomalies.report(
                AnomalyKind.VALVE_OPEN_UNCONFIRMED,
                {
                    "cycle_id": run.cycle_id,
                    "zone_id": zone.zone_id,
                    "entity_id": zone.valve_entity_id,
                },
            )
        await self._save()

    async def _finish_zone(self, run: CycleRun, zone: ZoneRun, now: datetime) -> None:
        """Close one zone, then open the next or complete the cycle.

        The close is ALWAYS commanded before the next open — FR2 governs the
        commanded sequence. An unconfirmed close (possibly a stuck-open valve)
        raises the anomaly and the sequence proceeds: bounded over-watering is
        consequence-free (fail-wet), stalling the cycle is not.
        """
        confirmed = await self._switches.async_turn_off(zone.valve_entity_id)
        zone.close_confirmed = confirmed
        zone.actual_end = now
        if not confirmed:
            self._anomalies.report(
                AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
                {
                    "cycle_id": run.cycle_id,
                    "zone_id": zone.zone_id,
                    "entity_id": zone.valve_entity_id,
                },
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

        A deferred cycle is re-created immediately, scheduled at completion —
        ``advance``'s loop starts it in the same call, so a deferred cycle is
        delayed, never skipped (AD-4).
        """
        if pump_was_on:
            confirmed = await self._switches.async_turn_off(run.pump_entity_id)
            run.pump_off_confirmed = confirmed
            if not confirmed:
                self._anomalies.report(
                    AnomalyKind.PUMP_OFF_UNCONFIRMED,
                    {"cycle_id": run.cycle_id, "entity_id": run.pump_entity_id},
                )
        run.status = CycleStatus.COMPLETED
        await self._save()
        self._last_run = run
        self._run = None
        self._zone_index = 0
        if self._deferred:
            self._create_run(self._deferred.pop(0), now)
            await self._save()

    def _create_run(self, kind: CycleKind, now: datetime) -> None:
        """Snapshot the plan into a new pending run (AD-8: owned copies)."""
        windows = zone_windows(self.plan, kind, now)
        self._run = CycleRun(
            cycle_id=cycle_id_for(kind, now),
            kind=kind,
            scheduled_start=now,
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

    async def _save(self) -> None:
        """Journal the current state — called on EVERY transition (NFR2).

        The engine never touches storage: it awaits the port and moves on;
        debounce/immediacy is the adapter's policy.
        """
        run = self._run if self._run is not None else self._last_run
        await self._journal.async_save(
            {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "run": run.as_dict() if run is not None else None,
                "deferred": [kind.value for kind in self._deferred],
            },
        )
