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

from .history import history_entry, prune_history
from .ledger import Ledger
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

    def __init__(  # noqa: PLR0913 — three injected ports plus the three seeds; collapsing them into a config object would hide which are ports and which are state
        self,
        plan: ControllerPlan,
        *,
        switches: SwitchPort,
        journal: JournalPort,
        anomalies: AnomalyPort,
        history: list[dict[str, object]] | None = None,
        season_enabled: bool = True,
        ledger: dict[str, object] | None = None,
    ) -> None:
        """Wire the sequencer to its plan, its ports and any prior state.

        `history`, `season_enabled` and `ledger` are the ONLY state this
        constructor accepts back, and none is machine state: outcome records
        are not, a season flag is a runtime MODE, and the water-debt ledger is
        ACCOUNTING between cycles, not an in-flight cycle. Seeding them is
        therefore not resuming a cycle (AD-11's recovery path stays Story
        3.2's, and it restores `run`/`last_run`/`deferred`). Without the
        history seed the first `_save` of every reload would overwrite the
        stored 7-day section with an empty list, and without the ledger seed a
        deficit would not survive the reload regime that runs on every config
        change — let alone an HA restart.

        `season_enabled` defaults to True — fail-wet (AD-4): doubt about the
        stored value resolves toward watering, so an absent or unreadable key
        waters rather than silently ending the season. `ledger` is the
        section exactly as `Ledger.as_dict` wrote it, already validated by the
        journal adapter; `None` (a journal written before Story 2.2) is an
        empty ledger and the first cycle is quoted on base durations.
        """
        # Public and replaceable on purpose: a new plan applies to the NEXT
        # requested cycle; the running cycle only ever reads its own snapshot
        # (AD-8 — Story 1.7's reload deferral builds on this seam). The update
        # listener swaps it here while a reload is deferred behind a running
        # cycle, so a cycle popped from the deferred queue inside
        # `_complete_cycle` is already created from the edited plan.
        self.plan = plan
        self._switches = switches
        self._journal = journal
        self._anomalies = anomalies
        self._run: CycleRun | None = None
        self._last_run: CycleRun | None = None
        # Completed-cycle outcomes, oldest→newest, pruned to the retention
        # window on every completion. Journalled with the rest of the state:
        # Epic 4's state view reads it from here, never from storage (AD-14).
        self._history: list[dict[str, object]] = (
            [] if history is None else list(history)
        )
        # FR10's one-action season switch. Journalled with the rest of the
        # state rather than kept in `entry.options`: an options write fires the
        # update listener, and the reload it schedules would tear down a
        # running cycle mid-flight — which AC 1 forbids.
        self._season_enabled = season_enabled
        # THE water-debt ledger (AD-5): every cycle is quoted through it in
        # `_build_run`, settled through it in `_complete_cycle`, and every
        # SCHEDULED dispatch asks it for the day credit in
        # `_dispatch_scheduled` (Story 2.3) — those three are its only call
        # sites. Journalled with the rest of the state.
        self._ledger = Ledger.from_dict(ledger)
        # Each deferred entry keeps the reference instant of its ORIGINAL
        # request: that is what its configured start (and therefore its
        # irrigation day) is derived from, so a cycle deferred across midnight
        # stays filed under the day it was requested for.
        self._deferred: list[tuple[CycleKind, datetime]] = []
        self._zone_index = 0
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

    @property
    def season_enabled(self) -> bool:
        """Return whether scheduling is active at all, read-only (AD-6)."""
        return self._season_enabled

    @property
    def ledger(self) -> Ledger:
        """Return the water-debt ledger for READING (AD-6).

        The zone sensors project `deficit_s` from it. Writing goes through
        `_complete_cycle` only — nothing outside the engine settles.
        """
        return self._ledger

    async def async_set_season(self, *, enabled: bool, now: datetime) -> bool:  # noqa: ARG002
        """Turn the season on or off; return True iff the value changed.

        Setting it to the value it already has is NOT an error and writes
        nothing: the switch entity and the service both reach this, and the
        reload regime must not churn the journal over a no-op.

        Disabling clears the deferred queue — a deferred cycle is scheduled
        work, and AC 1's "suspends all scheduling in one action" has to be
        true the instant the operator flips the switch. `self._run` is
        deliberately NOT touched whatever its status: a cycle in progress
        completes, and only `async_cancel_cycle` stops one.

        `now` is accepted for symmetry with every other engine entry point and
        is deliberately unused: the engine never reads a clock of its own
        (AD-1), and a later season-transition record would take its instant
        from here rather than from a new parameter.
        """
        async with self._lock:
            if self._season_enabled == enabled:
                return False
            self._season_enabled = enabled
            if not enabled:
                self._deferred.clear()
            await self._save()
            return True

    async def request_cycle(self, kind: CycleKind, now: datetime) -> None:
        """Request a cycle start; defers if another run is active.

        The actual transitions (pump on, first zone open) happen in
        ``advance`` — this only records the intent, so the caller's next step
        is always the same: re-arm on ``next_wakeup()``.

        Story 2.1's `run_now` does NOT reuse this method: it runs with the
        season OFF by design, so it has its own entry point
        (`async_run_now`) rather than bypassing the guard from inside here.

        A request that would create a run is a SCHEDULED dispatch decision
        (Story 2.3): it goes through `_dispatch_scheduled`, which asks the
        ledger whether a completed run-now has already credited the day. A
        deferred request is not decided here — it is decided when it is
        popped, in `_complete_cycle`, so the credit the running run-now is
        about to record can excuse it.
        """
        async with self._lock:
            if not self._season_enabled:
                # A permitted non-watering cause (AD-4): no run, no defer, no
                # save and — the part a watchdog must not mistake for a miss —
                # no anomaly. The daily tracker stays armed; the gate is here.
                return
            if self._run is not None:
                self._deferred.append((kind, now))
            else:
                self._dispatch_scheduled(kind, now, dispatch_at=None, now=now)
            await self._save()

    async def async_run_now(self, kind: CycleKind, now: datetime) -> bool:
        """Start `kind` immediately on operator demand; False when refused.

        Story 2.1's run-now, and deliberately the SIBLING of `request_cycle`
        rather than a flag inside it: that method's first statement is the
        season gate, and an explicit operator action is independent of
        scheduling — running with the season OFF is the point here, not an
        edge case. Everything after the decision is the scheduled path
        verbatim: `_create_run(..., dispatch_at=now)` builds the same
        snapshot, so windows, pump orchestration, actuation verification,
        journalling and history all come from code this story never touches.
        The run is tagged `manual` so history — and Story 2.3's day credit —
        can tell it apart without a third `CycleKind`. It calls `_create_run`
        DIRECTLY, never `_dispatch_scheduled`: a manual cycle is never waived,
        and it never consumes the credit either — the operator asked.

        A disabled morning cycle is not a refusal either, for the same reason
        the season is not: `morning_enabled` suppresses the daily START, and
        this is not one. `run_now(morning)` on such a plan waters every zone on
        its morning durations, from the configured morning start's windows —
        the operator asked for that cycle by name.

        Two refusals, both returning False rather than raising: the engine
        speaks no user-facing errors (it must stay importable with no Home
        Assistant), so the SERVICE is what turns each into its translated
        `ServiceValidationError`, exactly as `async_cancel_cycle` does.

        - A cycle is already active. PENDING counts, not just RUNNING: it
          holds its own AD-8 snapshot and a scheduled start, and overwriting
          it would abandon a cycle nobody cancelled. Nothing here closes,
          cancels, journals or even reads that run.
        - The plan has no zones. The scheduled path files such a cycle as a
          completed zero-length run (`_start_cycle`'s no-zones branch), which
          is honest for a daily start nobody asked for; here it would put a
          phantom "manual run" in history for water that never flowed, and
          Story 2.3 would credit the irrigation day for it.

        A refused run-now is never queued. The deferral queue exists so a
        *scheduled* cycle is delayed rather than skipped (AD-4); an operator
        command that cannot run now is an error to report, not work to
        remember — the operator can see the cycle running and call again.
        """
        async with self._lock:
            if self._run is not None or not self.plan.zones:
                return False
            self._create_run(kind, now, dispatch_at=now, manual=True)
            await self._save()
            return True

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

    async def async_cancel_cycle(self, now: datetime) -> bool:
        """Cancel the active cycle; return False when there is nothing to cancel.

        The ONLY thing that stops a running cycle (AC 1, AD-4). It leaves the
        hardware safe and the accounting honest, and it raises NOTHING: the
        engine must stay importable with no Home Assistant and speaks no
        user-facing errors, so the caller (the service) is what turns a False
        into a `ServiceValidationError`.
        """
        async with self._lock:
            run = self._run
            if run is None:
                return False
            # Cancelling "the cycle" must not immediately start the next one:
            # emptied BEFORE `_complete_cycle`, whose deferral branch is then
            # a no-op.
            self._deferred.clear()
            # PENDING means nothing was ever commanded, so nothing has to be
            # un-commanded — and the pump was never started.
            pump_was_on = run.status is CycleStatus.RUNNING
            if pump_was_on:
                await self._close_live_zone(run, now)
            # Through the EXISTING completion path so the pump-off, the
            # history append, the prune and the release stay in ONE place.
            await self._complete_cycle(
                run,
                now,
                pump_was_on=pump_was_on,
                status=CycleStatus.CANCELLED,
            )
            return True

    async def async_suspend(self, now: datetime) -> bool:  # noqa: ARG002
        """Make the hardware safe for a forced unload; return False when idle.

        The entry is being unloaded by something other than the engine's own
        deferred reload — the integration disabled or removed, a forced
        `async_reload` — while a cycle is active. The live valve is closed
        first and the pump second (the same valve-before-pump order as a
        cancel), each through the verified port, and `CYCLE_INTERRUPTED` is
        reported so the loss is never silent (AD-4).

        Deliberately NOT a cancel: nothing here mutates the run, bumps the
        zone index, appends history or saves. The journal therefore still
        holds the RUNNING (or PENDING) intent exactly as the last transition
        wrote it, which is what Story 3.2's reconciler resumes from (AD-11).
        Completing or cancelling the run here would turn an interrupted cycle
        into an honest-looking finished one and steal that recovery.

        A PENDING run has commanded nothing, so nothing is un-commanded — but
        it is still a scheduled cycle that will be lost pre-3.2, hence the
        anomaly. `now` is accepted for symmetry with every other engine entry
        point and is unused: no timestamp is written because nothing is.

        Never raises: the caller is the unload path, and an exception there
        would leave the entry in FAILED_UNLOAD with the timers already gone.
        """
        async with self._lock:
            run = self._run
            if run is None:
                return False
            zone: ZoneRun | None = None
            if run.status is CycleStatus.RUNNING:
                zone = _live_zone(run)
                if zone is not None:
                    confirmed, error = await self._command(
                        on=False,
                        entity_id=zone.valve_entity_id,
                    )
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
                confirmed, error = await self._command(
                    on=False,
                    entity_id=run.pump_entity_id,
                )
                if not confirmed:
                    self._report(
                        AnomalyKind.PUMP_OFF_UNCONFIRMED,
                        {"cycle_id": run.cycle_id, "entity_id": run.pump_entity_id},
                        error,
                    )
            self._report(
                AnomalyKind.CYCLE_INTERRUPTED,
                {
                    "cycle_id": run.cycle_id,
                    "kind": run.kind.value,
                    "zone_id": None if zone is None else zone.zone_id,
                },
            )
            return True

    async def _close_live_zone(self, run: CycleRun, now: datetime) -> None:
        """Close the zone whose slot is open, if any — the cancel's valve half.

        The live zone is found by `_live_zone`'s "started but not finished"
        rule, never by status: a FAILED zone is indistinguishable by status
        from a finished one and still owns the open slot.

        The next zone is deliberately NOT opened and `_zone_index` is NOT
        bumped: the zones never reached keep PENDING, so `effective_seconds`
        returns 0 for them and Epic 2's deficit reads the shortfall from the
        ONE helper that owns the math (AD-5).
        """
        zone = _live_zone(run)
        if zone is None:
            return
        confirmed, error = await self._command(on=False, entity_id=zone.valve_entity_id)
        zone.close_confirmed = confirmed
        zone.actual_end = now
        if not confirmed:
            # Exactly what `_finish_zone` does: a failing valve reports and the
            # sequence proceeds — it must never be what wedges a cancel.
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
        status: CycleStatus = CycleStatus.COMPLETED,
    ) -> None:
        """Pump off after the last zone closes (FR3), then release the slot.

        A deferred cycle is re-created immediately, dispatched at completion —
        ``advance``'s loop starts it in the same call, so a deferred cycle is
        delayed, never skipped (AD-4). The pop is a scheduled dispatch
        DECISION (Story 2.3): it runs after this run's settlement, so a
        scheduled cycle that fell due behind a run-now is waived by the credit
        that run-now just recorded, in this same call. A waived pop leaves
        `self._run` empty, so the loop lets the next deferred entry through
        rather than stranding it behind a cycle that never existed.

        `status` is the TERMINAL status to file the run under: COMPLETED for
        the normal path, CANCELLED for `async_cancel_cycle`. Both go through
        here so the pump-off, the settlement, the history append, the prune
        and the release have exactly one implementation.

        The ledger is settled right after the status turns terminal and
        BEFORE the snapshot is saved (Story 2.2): the journal written for this
        completion already carries the deficits it produced, and the deferred
        cycle created further down is quoted against them. Both terminal
        statuses settle — a cancelled run's un-reached zones owe their whole
        slot — and this is the ONLY place `settle` is called.

        Called from inside the lock by `advance` and directly by
        `async_cancel_cycle`, which already holds it — this method must NOT
        acquire the lock itself.
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
        run.status = status
        self._ledger.settle(run)
        # Appended BEFORE the save and before the deferral branch below: a
        # deferred cycle is created in this same call, and the snapshot taken
        # then must already carry this cycle's outcome.
        self._history.append(history_entry(run, now))
        self._history = prune_history(self._history, irrigation_day(now))
        await self._save()
        self._last_run = run
        self._run = None
        self._zone_index = 0
        while self._deferred and self._run is None:
            kind, reference = self._deferred.pop(0)
            self._dispatch_scheduled(kind, reference, dispatch_at=now, now=now)
            await self._save()

    def _dispatch_scheduled(
        self,
        kind: CycleKind,
        reference: datetime,
        *,
        dispatch_at: datetime | None,
        now: datetime,
    ) -> None:
        """Decide a SCHEDULED cycle: install it, or file it waived (Story 2.3).

        The only two scheduled call sites — `request_cycle` creating a run and
        the deferred pop in `_complete_cycle` — come through here; a run-now
        never does. The run is built first, by the same snapshot code as a
        real run, so a waived record carries the quoted durations and an id
        from `_next_occurrence` exactly as if it had run. Then the LEDGER
        decides (AD-5): `waive` returns the crediting run-now's id iff its
        credit is for this cycle's irrigation day — `configured_start`'s, the
        ONE definition — and clears the credit either way.

        Credit → the run is filed to history as `waived` naming the run-now,
        pruned like any record, and DROPPED: never `current_run` (so nothing
        is commanded and `next_wakeup()` stays None), never `last_run` (the
        zone sensors keep showing the real last watering), never settled
        (the quote was pure, so an outstanding deficit stays for the next
        real cycle), and no anomaly — a credited day is a permitted
        non-watering cause (AD-4). The caller saves; the record is already in
        `_history` by then. That debounced save is the decision's ONLY
        persistence: a crash inside its window re-seeds the credit on the next
        setup, and the day's other scheduled cycle is waived instead — bounded
        by construction, since the run-now really did water the day.
        """
        run = self._build_run(kind, reference, dispatch_at=dispatch_at)
        credit = self._ledger.waive(irrigation_day(run.configured_start))
        if credit is None:
            self._run = run
            self._zone_index = 0
            return
        run.status = CycleStatus.WAIVED
        self._history.append(history_entry(run, now, waived_by=credit))
        self._history = prune_history(self._history, irrigation_day(now))

    def _create_run(
        self,
        kind: CycleKind,
        reference: datetime,
        *,
        dispatch_at: datetime | None = None,
        manual: bool = False,
    ) -> None:
        """Install a new pending run built from the plan (see `_build_run`).

        The run-now path: `async_run_now` installs unconditionally, because a
        manual cycle is never waived. Scheduled callers go through
        `_dispatch_scheduled`, which builds the same run and asks the ledger
        first.
        """
        self._run = self._build_run(
            kind, reference, dispatch_at=dispatch_at, manual=manual
        )
        self._zone_index = 0

    def _build_run(
        self,
        kind: CycleKind,
        reference: datetime,
        *,
        dispatch_at: datetime | None = None,
        manual: bool = False,
    ) -> CycleRun:
        """Snapshot the plan into a new pending run (AD-8: owned copies).

        PURE apart from the ledger's own pure quote: it assigns nothing on
        `self`, so a run built here and then waived leaves the machine
        exactly as it found it.

        The run's windows come from the plan's configured start time on
        `reference`'s day — the plan is the single source of the schedule
        (AD-6/FR1), so the operator's start time is what the engine executes,
        not whatever instant the caller happened to fire at. A deferred cycle
        passes `dispatch_at` to start right away instead of waiting for its
        configured start, while still keeping that start for its identity.

        `manual` marks Story 2.1's run-now. It defaults to False so both
        scheduled callers stay unchanged, and it only ever reaches the run
        object — the id, the windows and the occurrence counter are derived
        identically either way, which is what keeps a run-now indistinguishable
        from a scheduled cycle everywhere except in the accounting.

        Durations are QUOTED through the ledger (Story 2.2, AD-5), whichever
        caller is creating the run: a scheduled start, a deferred pop and a
        run-now all apply the carried deficit the same way — FR11's "current
        durations" are the current EFFECTIVE durations. The windows accumulate
        the quoted durations, so a carried deficit shifts every following
        zone and `next_wakeup()` with it. Quoting is pure; the ledger is only
        written when this run settles in `_complete_cycle`.
        """
        schedule = derive_schedule(self.plan, kind, reference)
        scheduled_start = schedule.start if dispatch_at is None else dispatch_at
        quotes = self._ledger.quote(self.plan.zones, kind)
        windows = zone_windows(
            self.plan,
            kind,
            scheduled_start,
            [quote.quoted_s for quote in quotes],
        )
        return CycleRun(
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
                    duration_s=quote.quoted_s,
                    base_s=quote.base_s,
                    carried_s=quote.carried_s,
                    planned_start=window.start,
                    planned_end=window.end,
                )
                for spec, quote, window in zip(
                    self.plan.zones,
                    quotes,
                    windows,
                    strict=True,
                )
            ),
            manual=manual,
        )

    def _next_occurrence(self, kind: CycleKind, configured_start: datetime) -> int:
        """Return this run's occurrence number for its irrigation day and kind.

        COUNTED FROM HISTORY, not from a counter of its own. A private counter
        is journalled but never seeded back (`JournalSeed` restores history and
        the season flag, nothing else, and Story 3.2 owns the rest), so it
        resets on every reload — and the reload regime runs on every config
        change. The second run of a kind after a reload would then be handed
        the id the first one already has, which is precisely the collision the
        suffix exists to remove: Epic 2 keys idempotent settlement off the
        cycle id. History is the one piece of this state that already survives
        a reload, so the count comes from there.

        Every terminal run reaches history, cancelled and waived ones
        included (a waived cycle consumed an id, so the next one must not
        reuse it), and `_complete_cycle` appends BEFORE it pops the deferred
        queue — so the count is already right for a cycle created inside that
        same call.
        Retention cannot interfere: pruning is by irrigation DAY over a 7-day
        window, so a run's own day is never the day that ages out.
        """
        day = irrigation_day(configured_start).isoformat()
        return 1 + sum(
            1
            for entry in self._history
            if entry.get("irrigation_day") == day and entry.get("kind") == kind.value
        )

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
        queue keeps each entry's reference instant. `ledger` is the water
        debt between cycles (Story 2.2) — seeded back on setup, which is how a
        deficit survives a restart.
        """
        last_run = self._last_run
        snapshot: dict[str, object] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "run": self._run.as_dict() if self._run is not None else None,
            "zone_index": self._zone_index,
            "last_run": last_run.as_dict() if last_run is not None else None,
            "deferred": [
                {"kind": kind.value, "reference": utc_iso(reference)}
                for kind, reference in self._deferred
            ],
            "history": list(self._history),
            "season_enabled": self._season_enabled,
            "ledger": self._ledger.as_dict(),
        }
        try:
            await self._journal.async_save(snapshot)
        except Exception as err:  # noqa: BLE001 — a failed save must not stop the water
            self._report(AnomalyKind.JOURNAL_SAVE_FAILED, {}, repr(err))


def _live_zone(run: CycleRun) -> ZoneRun | None:
    """Return the zone whose slot is currently open, or None.

    Identified by "started but not finished" rather than by status: a FAILED
    zone is indistinguishable by status from a finished one, and it is still
    the live slot until its planned end (fail-wet consumes the slot, AD-4).

    Shared by the cancel (which closes and mutates it) and the suspend (which
    closes and mutates nothing). `entities/sensor.py::_live_zone` encodes the
    SAME rule for the projection — the engine may not import from
    `entities/`, so the two must be edited together.
    """
    return next(
        (
            zone
            for zone in run.zone_runs
            if zone.actual_start is not None and zone.actual_end is None
        ),
        None,
    )
