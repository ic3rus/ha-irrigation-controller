"""Event-scheduled sequencer state machine (AD-3 compatible by construction).

The engine never sleeps, loops on wall time or arms timers. It exposes two
methods:

- ``next_wakeup(now)`` — the earliest pending time intent: a running cycle's
  own boundary while one is active, otherwise the watchdog's next window-end
  deadline (Story 3.3). ``None`` in three cases: before reconciliation, when
  the watchdog has nothing it could act on (season off, or a plan with no
  zones), and in the transient window inside ``advance`` where the last zone
  has closed but the cycle has not completed yet. Daily cycle starts are NOT
  a wakeup intent: they arrive from outside through ``request_cycle`` (tests
  call it directly; Story 1.5 calls it from ``async_track_time_change``, per
  AD-3's split).
- ``advance(now)`` — perform EVERY transition due at ``now``: command ports,
  update run objects, journal. Idempotent for a given state and ``now``.

Story 1.5's adapter arms exactly ONE re-armed ``async_track_point_in_time``
at ``next_wakeup(now)`` and calls ``advance(dt_util.now())`` when it fires;
virtual-clock tests drive the identical loop with ``advance_to()``. Every
``advance`` starts with the missed-cycle check (Story 3.3), so the watchdog
needs no timer of its own — AD-3's one callback serves it too.

Every port call is defended: a port that raises must never abort a cycle and
leave a valve open with no completion path (AD-4). Adapters are expected to
translate their own failures into a not-confirmed outcome; the engine treats
a leaked exception as exactly that and reports it.

A third entry point, ``async_reconcile(now)`` (Story 3.2, AD-11), runs ONCE
per setup before the adapter arms anything: it resyncs the run the journal
restored against the actual switch states, closes orphans, then resumes or
closes the cycle. Until it has run, ``advance`` is a no-op and
``request_cycle`` only defers — nothing acts on a restored run before it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final

from .history import history_entry, prune_history
from .ledger import Ledger
from .plan import derive_schedule, irrigation_day, zone_windows
from .ports import AnomalyKind
from .reconcile import RecoveryOutcome, plan_recovery
from .runs import (
    CycleRun,
    CycleStatus,
    ZoneRun,
    ZoneRunStatus,
    cycle_id_for,
    live_zone,
    utc_iso,
)
from .watchdog import missed_cycles, next_deadline

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from .plan import ControllerPlan, CycleKind
    from .ports import AnomalyPort, JournalPort, RainPort, SwitchPort

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

    def __init__(  # noqa: PLR0913 — four injected ports plus the journal seeds; collapsing them into a config object would hide which are ports and which are state
        self,
        plan: ControllerPlan,
        *,
        switches: SwitchPort,
        journal: JournalPort,
        anomalies: AnomalyPort,
        rain: RainPort | None = None,
        history: list[dict[str, object]] | None = None,
        season_enabled: bool = True,
        ledger: dict[str, object] | None = None,
        run: CycleRun | None = None,
        last_run: CycleRun | None = None,
        deferred: Sequence[tuple[CycleKind, datetime]] | None = None,
        run_unreadable: Mapping[str, object] | None = None,
        watchdog_since: datetime | None = None,
    ) -> None:
        """Wire the sequencer to its plan, its ports and any prior state.

        `rain` (Story 2.4) is the gauge seam, read ONCE per cycle in
        `_build_run`; `None` means there is no gauge and every quote is
        unmodulated — the engine never distinguishes "no gauge" from "gauge
        doubtful", both quote full durations.

        `history`, `season_enabled` and `ledger` are seeds that are NOT
        machine state: outcome records are not, a season flag is a runtime
        MODE, and the water-debt ledger is ACCOUNTING between cycles. Without
        the history seed the first `_save` of every reload would overwrite
        the stored 7-day section with an empty list, and without the ledger
        seed a deficit would not survive the reload regime that runs on every
        config change — let alone an HA restart.

        `run`, `last_run`, `deferred` and `run_unreadable` (Story 3.2) ARE
        machine state — the journal's intent, validated by the adapter
        (`CycleRun.from_dict` is the trust boundary; the engine trusts what
        reaches here). Seeding them does not act on them: `async_reconcile`
        is the ONLY recovery path (AD-11), and until it has run `advance` is
        a no-op and `request_cycle` only defers. A restored `run` whose
        status is already terminal is the completion write that filed it
        under `run` before the release (`_complete_cycle` saves, THEN moves
        it to `last_run`): it is not in flight, so it becomes `last_run` and
        the restart is a nominal one. `run_unreadable` is the raw `run`
        document the adapter refused (or `{}` when it was not even a
        mapping): the reconciler then closes every configured switch and
        reports the discard without ever trusting it.

        `watchdog_since` (Story 3.3) is the instant this controller first
        became observable — the journal's stamp, `None` on a first install or
        when it could not be read. It is the watchdog's FLOOR: a cycle window
        that closed at or before it was never ours to run. `async_reconcile`
        is the one place it is ever set, so a machine driven directly (every
        virtual-clock suite) keeps `None` and no floor at all.

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
        self._rain = rain
        self._run: CycleRun | None = None
        self._last_run: CycleRun | None = last_run
        if run is not None:
            if run.status.is_terminal:
                self._last_run = run
            else:
                self._run = run
        # Story 3.3's watchdog floor, journalled with the rest of the state
        # and stamped by the first reconcile that finds it unset. Windows
        # that closed at or before it belong to a controller that did not
        # exist yet.
        self._watchdog_since = watchdog_since
        # The raw run document the adapter could not validate — see
        # `async_reconcile`. Consumed there, never trusted.
        self._unreadable_run = run_unreadable
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
        self._deferred: list[tuple[CycleKind, datetime]] = (
            [] if deferred is None else list(deferred)
        )
        # The switches an operator is holding open BY HAND (Story 3.4).
        # Non-empty IS the paused state — a set rather than a flag, so two
        # hand-opened valves need two closes before the scheduler moves
        # again. LIVE state on purpose: it is never journalled and never
        # seeded, so a reload or a restart loses the pause (the start it
        # deferred survives, in `_deferred`, and `async_reconcile` drains it).
        self._manual_on: set[str] = set()
        self._zone_index = 0
        # The Story 3.2 gate: False while a restored in-flight run (or an
        # unreadable one) awaits `async_reconcile`. A machine with nothing to
        # recover has nothing to protect and starts reconciled — which is
        # also what keeps every virtual-clock suite driving `request_cycle`
        # and `advance` directly, without a reconcile step, honest.
        self._reconciled = self._run is None and run_unreadable is None
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
        """Return the cycle kinds queued to start later — late, never lost.

        Three things put a kind here: another run was active when it was
        requested, reconciliation had not run yet (Story 3.2), or a manual
        override was holding a governed switch open (Story 3.4). The
        watchdog reads this to tell "pending" from "missed".
        """
        return tuple(kind for kind, _ in self._deferred)

    @property
    def manual_override(self) -> bool:
        """Return whether the operator is holding a governed switch open (3.4).

        THE paused state, read-only (AD-6): the cycle-status sensor projects
        it, and `request_cycle`, `_pop_deferred` and `_check_missed` gate on
        it. True from the first hand-opened switch until the last one is
        observed off, whoever closed it.
        """
        return bool(self._manual_on)

    @property
    def season_enabled(self) -> bool:
        """Return whether scheduling is active at all, read-only (AD-6)."""
        return self._season_enabled

    @property
    def reconciled(self) -> bool:
        """Return whether startup reconciliation has run (or was not needed)."""
        return self._reconciled

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

        Turning the season ON forgets the rain (Story 2.5): an actual OFF→ON
        change calls `Ledger.forget_rain()`, clearing every banked baseline
        and the gauge source they came from, and journals. The switch is the
        operator's statement that a new accumulation period begins, and the
        rain banked before it — a winter's worth, on a gauge that never
        resets — must not skip the first cycle of the season. That first
        cycle quotes no credit, waters in full and re-banks at settlement;
        the one after modulates. A mid-season OFF/ON loses the credit for
        that gap's rain, which is the fail-wet direction. Season OFF, a
        no-op toggle and a run-now leave the baselines alone; deficits and
        the day credit are never touched here. This is the ONLY caller of
        `forget_rain`.

        `now` is accepted for symmetry with every other engine entry point and
        is deliberately unused: the engine never reads a clock of its own
        (AD-1), and a later season-transition record would take its instant
        from here rather than from a new parameter.
        """
        async with self._lock:
            if self._season_enabled == enabled:
                return False
            self._season_enabled = enabled
            if enabled:
                self._ledger.forget_rain()
            else:
                self._deferred.clear()
            await self._save()
            return True

    async def async_switch_observed(
        self,
        entity_id: str,
        *,
        is_on: bool,
        manual: bool,
        now: datetime,
    ) -> None:
        """Take one governed-switch observation and own the pause (Story 3.4).

        The adapter watches every governed entity and REPORTS — entity, on or
        off, ours or not (AD-7). This method is the whole decision, and the
        set `self._manual_on` is the state: non-empty is paused. A manual ON
        adds; ANY observed OFF removes, whoever commanded it — that is what
        lets the engine's own close of a hand-opened valve end the pause, and
        it is the hook Story 3.5's safety timeout will pull.

        A manual OFF on its own never pauses (Decision 1): nothing is being
        held open, so there is nothing to step aside from. The operator
        closing the valve of the LIVE slot is exactly that case — the cycle
        runs on, the valve is never re-opened (the engine only commands at
        slot boundaries) and the slot's own close later confirms trivially.

        `_manual_on` is mutated HERE, before the lock, and the lock is taken
        only for the EFFECTS — the hand-back on the first open, the drain on
        the last close. Two reasons, and both are load-bearing:

        - an observation that changes nothing takes no lock at all. Every
          valve and pump command is issued from inside `advance`, which holds
          the lock while the actuation verifies, and each generates a
          `state_changed`; queueing those behind it would serialize the whole
          watch onto the state machine for no effect — and, since a caller's
          `block_till_done` does not wait for the entry's background tasks,
          would make the daily start that follows queue behind them too;
        - the set is then never stale. A set read before the lock and
          mutated after it would drop the OFF of a switch flipped on and
          straight off again — that OFF testing an `_manual_on` its own ON
          had not reached yet — and leave the entity held for ever with the
          switch physically off. Mutating it here is safe precisely because
          each mutation is one synchronous statement: nothing interleaves
          with it, which is the one thing the lock is not needed for.

        `_reconciled` is read before the lock for the same reason, and only
        ever goes False→True: a no-op until reconciliation has run, exactly
        like `advance` and `async_run_now`. A valve found open at boot is the
        reconciler's orphan pass, not the pause's business.

        On the FIRST hand-opened switch a PENDING run — one that has
        commanded nothing — is handed back to `_deferred` as
        `(kind, configured_start)`: the invariant is that no PENDING
        scheduled run survives the start of a pause. A RUNNING cycle is
        untouched; it finishes its zones.

        On the LAST close the queue is drained the way every deferral is:
        entries whose irrigation day is not today are dropped (the rule
        `async_reconcile` already applies), the rest are popped through
        `_pop_deferred`, and `_advance_locked` then starts the popped cycle
        in this same call and re-runs the watchdog check that returned early
        while the pause held.
        """
        if not self._reconciled:
            return
        if manual and is_on:
            if entity_id in self._manual_on:
                return
            first = not self._manual_on
            self._manual_on.add(entity_id)
            if not first:
                return
            async with self._lock:
                await self._defer_pending_run()
            return
        # An ON that is not manual can only ever be ours, and a release can
        # only concern an entity the set holds: both are no-ops here, which
        # is what keeps our own commands off the lock entirely.
        if is_on or entity_id not in self._manual_on:
            return
        self._manual_on.discard(entity_id)
        if self._manual_on:
            return
        async with self._lock:
            await self._resume(now)

    async def _defer_pending_run(self) -> None:
        """Hand a PENDING scheduled run back to `_deferred` (Story 3.4).

        Called from inside the lock, on the FIRST hand-opened switch only —
        a second one arriving later finds no PENDING run, because
        `request_cycle` has been deferring since the pause opened (it reads
        the set, which this method's caller already mutated).

        A run-now is NEVER handed back, PENDING though it is. The invariant
        is that no PENDING *scheduled* run survives the start of a pause, and
        `_deferred` holds scheduled work only: queueing a manual cycle there
        would strip its `manual` marker, make it waivable on a day credit and
        let it consume one — and `async_run_now` refuses to queue for exactly
        that reason. The window is real: the runner acquires the lock twice
        (`async_run_now`, then `advance`), and an observation can land
        between them. Honouring run-now while paused is Decision 2.

        A RUNNING cycle is untouched: it finishes its zones.
        """
        run = self._run
        if run is None or run.status is not CycleStatus.PENDING or run.manual:
            return
        self._deferred.append((run.kind, run.configured_start))
        self._run = None
        self._zone_index = 0
        await self._save()

    async def _resume(self, now: datetime) -> None:
        """Drain and restart after the LAST hand-opened switch closed (3.4).

        Called from inside the lock. `manual_override` is re-read by
        `_pop_deferred`, so a switch hand-opened again between the release
        and this lock acquisition simply holds the queue where it is.

        The save at the end covers the ONE case `_pop_deferred` cannot: a
        stale-day deferral dropped while nothing was popped after it — the
        queue ended empty, or a cycle is still running and holds the drain.
        Every pop journals itself, so the length test is what tells the two
        apart. The pause itself is live state and is never journalled.
        """
        today = irrigation_day(now)
        kept = [entry for entry in self._deferred if irrigation_day(entry[1]) == today]
        dropped = len(kept) != len(self._deferred)
        self._deferred = kept
        queued = len(self._deferred)
        await self._pop_deferred(now)
        if dropped and len(self._deferred) == queued:
            await self._save()
        await self._advance_locked(now)

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

        Before `async_reconcile` has run (Story 3.2) the request is deferred
        too, whatever `self._run` holds: the restored run is not yet
        resynced, and the reconciler's completion path pops the queue.

        Deferred as well while a manual override holds (Story 3.4): the
        operator has a governed switch open by hand, and a scheduled start
        dispatched on top of them is exactly the fight this pause exists to
        avoid. The queue is the point — the cycle is delayed, never skipped,
        and the watchdog reads it as pending rather than missed. The season
        gate stays FIRST: season off is a permitted non-watering cause and
        must not queue anything.
        """
        async with self._lock:
            if not self._season_enabled:
                # A permitted non-watering cause (AD-4): no run, no defer, no
                # save and — the part a watchdog must not mistake for a miss —
                # no anomaly. The daily tracker stays armed; the gate is here.
                return
            if self._run is not None or not self._reconciled or self.manual_override:
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

        Refused too while `async_reconcile` has not run (Story 3.2): the
        journal's run is not resynced yet, and only the reconciler may act
        on it. That window is Home Assistant's own boot.
        """
        async with self._lock:
            if self._run is not None or not self.plan.zones or not self._reconciled:
                return False
            self._create_run(kind, now, dispatch_at=now, manual=True)
            await self._save()
            return True

    def next_wakeup(self, now: datetime) -> datetime | None:
        """Return the earliest pending time intent, or None when there is none.

        THE re-arm contract of the one point-in-time callback (AD-3), and
        since Story 3.3 the home of TWO sources of intent:

        - a cycle is active → its own boundary wins, exactly as before. The
          watchdog has nothing to serve while the machine is watering, and a
          deadline that pre-empted a zone boundary would leave a valve open.
        - the machine is idle → `next_deadline(self.plan, now)`, the next
          cycle window end. That is the watchdog's timer, and it is armed
          permanently: a nominal window end fires, finds nothing missing,
          writes nothing and re-arms on the following one. Except when the
          watchdog provably cannot act: the season being off and a plan with
          no zones both make `missed_cycles` return nothing whatever the
          instant, so an off-season controller must not re-arm a timer twice
          a day for a check that can only do nothing. Both are read live, so
          turning the season back on re-arms it through the ordinary tail.

        None while `async_reconcile` has not run (Story 3.2): a restored
        run's boundaries are not the engine's intent until it has been
        resynced — `advance` would serve nothing, and a boundary already in
        the past would otherwise fire, do nothing and be re-armed at once.
        The watchdog is gated by the same flag for the reason AC 3 names:
        boot must not re-run a cycle before recovery has decided what the
        journal's run was.

        `now` is the caller's clock read; the engine keeps none of its own
        (AD-1). It is used ONLY for the idle deadline — a run's boundaries
        are absolute instants already.
        """
        if not self._reconciled:
            return None
        run = self._run
        if run is None:
            if not self._season_enabled or not self.plan.zones:
                return None
            return next_deadline(self.plan, now)
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

        False too while `async_reconcile` has not run (Story 3.2): a restored
        run is the reconciler's to close or resume, and it runs during boot.
        """
        async with self._lock:
            run = self._run
            if run is None or not self._reconciled:
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
                zone = live_zone(run)
                if zone is not None:
                    confirmed, error = await self._command(
                        on=False,
                        entity_id=zone.valve_entity_id,
                    )
                    self._valve_outcome(
                        run,
                        zone,
                        confirmed=confirmed,
                        error=error,
                        kind=AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
                    )
                confirmed, error = await self._command(
                    on=False,
                    entity_id=run.pump_entity_id,
                )
                self._pump_outcome(
                    run,
                    confirmed=confirmed,
                    error=error,
                    kind=AnomalyKind.PUMP_OFF_UNCONFIRMED,
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

        The live zone is found by `live_zone`'s "started but not finished"
        rule, never by status: a FAILED zone is indistinguishable by status
        from a finished one and still owns the open slot.

        The next zone is deliberately NOT opened and `_zone_index` is NOT
        bumped: the zones never reached keep PENDING, so `effective_seconds`
        returns 0 for them and Epic 2's deficit reads the shortfall from the
        ONE helper that owns the math (AD-5).
        """
        zone = live_zone(run)
        if zone is None:
            return
        confirmed, error = await self._command(on=False, entity_id=zone.valve_entity_id)
        zone.close_confirmed = confirmed
        zone.actual_end = now
        # Exactly what `_finish_zone` does: a failing valve reports and the
        # sequence proceeds — it must never be what wedges a cancel.
        self._valve_outcome(
            run,
            zone,
            confirmed=confirmed,
            error=error,
            kind=AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        )
        if zone.status is not ZoneRunStatus.FAILED:
            zone.status = ZoneRunStatus.COMPLETED

    async def advance(self, now: datetime) -> None:
        """Perform every transition due at `now` (state machine step).

        A single late call drains all missed boundaries: the loop keeps
        transitioning until the next intent lies in the future. Calling twice
        with the same ``now`` performs nothing the second time.

        A no-op before `async_reconcile` has run (Story 3.2): a restored run
        is resynced against the hardware before anything steps it.
        """
        async with self._lock:
            if not self._reconciled:
                return
            await self._advance_locked(now)

    async def _advance_locked(self, now: datetime) -> None:
        """Run the `advance` loop for a caller that already holds the lock.

        The missed-cycle check (Story 3.3) runs FIRST, before any transition:
        a late re-run it dispatches is a PENDING run scheduled at `now`, so
        the loop below starts it in this very call — the same "delayed, never
        skipped" tail a deferred cycle gets.
        """
        await self._check_missed(now)
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

    async def async_reconcile(self, now: datetime) -> None:
        """Resync the journal's intent against the hardware, ONCE, at startup.

        THE recovery path (Story 3.2, AD-11) and the only one: the adapter
        calls it before arming any timer, and nothing else acts on a restored
        run. Under the lock, in this order:

        1. history is pruned to the retention window from `now` and every
           deferred entry whose reference is on another irrigation day is
           dropped (the watchdog owns missed cycles — Story 3.3) — a save
           follows only if either changed, so a nominal restart with fresh
           history writes nothing; and the watchdog's floor is stamped with
           `now` if the journal carried none (Story 3.3). THE one place it is
           ever set: the adapter reconciles before it arms anything, so by
           the time `_check_missed` can run the machine always knows the
           instant it became observable — and every window that closed
           before it is one nobody was there to run;
        2. an UNREADABLE run closes EVERY configured valve, then the pump,
           reports `CYCLE_RECOVERED{outcome: "discarded"}` and is dropped
           from the journal — nothing is booked, the ledger is untouched;
        3. a restored in-flight run goes through `_recover`: the orphan pass
           (each zone valve, then the pump — whatever is not OFF is commanded
           off through the verified port), then `plan_recovery`'s pure
           decision, then its application: resume through the same helpers a
           live cycle uses, or close through `_complete_cycle` so the ledger
           books the shortfalls exactly once.

        The gate lifts in a `finally`: whatever a port did, the machine must
        not stay frozen. A resumed PENDING run (or a deferred cycle popped by
        a close) is then started in the same call through the ordinary
        `advance` loop, so the caller's re-arm finds the next boundary.

        A same-day deferral restored from the journal IS popped here (Story
        3.4, Decision 5), right after the gate lifts and before the
        `advance`: an idle restart commands nothing *unless a deferral is
        waiting*. Such an entry waits for a completion that will never come
        — the run it was queued behind, and the manual pause that deferred
        it, both died with the process — so leaving it would strand the
        cycle. Popping it is the fail-wet direction (AD-4), and the ordinary
        `advance` below starts it in this same call.

        A nominal restart — no run, no deferral, nothing to prune —
        commands nothing, reports nothing and writes nothing.
        """
        async with self._lock:
            try:
                today = irrigation_day(now)
                pruned = prune_history(self._history, today)
                kept = [
                    entry
                    for entry in self._deferred
                    if irrigation_day(entry[1]) == today
                ]
                changed = len(pruned) != len(self._history) or len(kept) != len(
                    self._deferred
                )
                self._history = pruned
                self._deferred = kept
                if self._watchdog_since is None:
                    self._watchdog_since = now
                    changed = True
                if self._unreadable_run is not None:
                    await self._discard_unreadable(self._unreadable_run)
                elif (run := self._run) is not None:
                    await self._recover(run, now)
                elif changed:
                    await self._save()
            finally:
                self._reconciled = True
            # Story 3.4, Decision 5: a deferral restored from the journal is
            # drained here when nothing is running and nothing is held open
            # by hand. The pause that deferred it is LIVE state and did not
            # survive the restart, so the queue would otherwise wait for a
            # completion that is never coming — stranded, on a day the
            # watchdog reads as "late, not lost" until the window closes.
            # Fail-wet (AD-4). The `manual_override` gate inside is empty
            # here today; it keeps this call correct the day something seeds
            # the set before reconciliation.
            #
            # The watchdog runs FIRST, before the drain rather than through
            # the `_advance_locked` below: a popped cycle is `self._run` the
            # instant it is installed, and `missed_cycles` reads a live run
            # as "another cycle is watering" — so the day's OTHER kind would
            # be filed `recorded` instead of re-run, burning that
            # `(day, kind)` for ever. Asking before installing anything lets
            # the miss take the machine it is entitled to; the deferral then
            # waits for that re-run's completion, like every deferral.
            await self._check_missed(now)
            await self._pop_deferred(now)
            await self._advance_locked(now)

    async def _discard_unreadable(self, raw: Mapping[str, object]) -> None:
        """Make the hardware safe after a run the adapter could not trust.

        The journal said a cycle was in flight but not which valve, so EVERY
        configured valve is commanded off (plan order), then the pump — the
        run's own snapshot is exactly what cannot be read. The raw document
        is consulted for the anomaly's `cycle_id` and `kind` only, and only
        when they are strings; then the save drops it from the journal.
        """
        self._unreadable_run = None
        cycle_id = raw.get("cycle_id")
        kind = raw.get("kind")
        context: dict[str, object] = {
            "cycle_id": cycle_id if isinstance(cycle_id, str) else None,
            "kind": kind if isinstance(kind, str) else None,
        }
        for spec in self.plan.zones:
            confirmed, error = await self._command(
                on=False, entity_id=spec.valve_entity_id
            )
            self._outcome(
                AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
                {**context, "zone_id": spec.zone_id, "entity_id": spec.valve_entity_id},
                confirmed=confirmed,
                error=error,
            )
        confirmed, error = await self._command(
            on=False,
            entity_id=self.plan.pump_entity_id,
        )
        self._outcome(
            AnomalyKind.PUMP_OFF_UNCONFIRMED,
            {**context, "entity_id": self.plan.pump_entity_id},
            confirmed=confirmed,
            error=error,
        )
        # Superseded like any recovery: the interrupted cycle is gone for good.
        self._clear(AnomalyKind.CYCLE_INTERRUPTED, {"cycle_id": context["cycle_id"]})
        self._report(
            AnomalyKind.CYCLE_RECOVERED,
            {**context, "zone_id": None, "outcome": RecoveryOutcome.DISCARDED.value},
        )
        await self._save()

    async def _recover(self, run: CycleRun, now: datetime) -> None:
        """Orphan pass, pure decision, application — for one restored run.

        The orphan pass records confirmation outcomes through the same
        `_valve_outcome`/`_pump_outcome` a live cycle uses (an unconfirmed
        close reports `VALVE_CLOSE_UNCONFIRMED`/`PUMP_OFF_UNCONFIRMED`, a
        confirmed one clears it) but mutates NO zone field: the live zone may
        be about to be re-opened, and its `close_confirmed` belongs to the
        close that ends its slot.

        A CLOSED decision completes the run as INTERRUPTED with
        `pump_was_on=False` — the orphan pass has just commanded the pump
        off, and `_complete_cycle` clears `CYCLE_INTERRUPTED`, settles (the
        ledger refuses an id it already settled, so a replay books nothing
        twice), files history and pops the deferred queue. A RESUMED decision
        re-opens the machine: pump on through the shared helper, then either
        the live zone re-opened on its kept `actual_start` (its window is
        already what it always was), or the next slot opened through
        `_open_zone` exactly as a boundary would — and a resume with no slot
        left completes the run COMPLETED. `CYCLE_INTERRUPTED` is cleared as
        superseded and `CYCLE_RECOVERED` reported LAST, once the hardware
        outcome is known, so the one push describes the final state.
        """
        self._follow_renames(run)
        states = await self._read_states(run)
        for zone in run.zone_runs:
            if states.get(zone.valve_entity_id) is not False:
                confirmed, error = await self._command(
                    on=False,
                    entity_id=zone.valve_entity_id,
                )
                self._valve_outcome(
                    run,
                    zone,
                    confirmed=confirmed,
                    error=error,
                    kind=AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
                )
        if states.get(run.pump_entity_id) is not False:
            confirmed, error = await self._command(
                on=False, entity_id=run.pump_entity_id
            )
            self._pump_outcome(
                run,
                confirmed=confirmed,
                error=error,
                kind=AnomalyKind.PUMP_OFF_UNCONFIRMED,
            )
        recovery = plan_recovery(
            run,
            now=now,
            states=states,
            season_enabled=self._season_enabled,
        )
        run.recovery = recovery.outcome.value
        context: dict[str, object] = {
            "cycle_id": run.cycle_id,
            "kind": run.kind.value,
            "zone_id": recovery.live_zone_id,
            "outcome": recovery.outcome.value,
        }
        if recovery.outcome is RecoveryOutcome.CLOSED:
            await self._complete_cycle(
                run,
                now,
                pump_was_on=False,
                status=CycleStatus.INTERRUPTED,
            )
            self._report(AnomalyKind.CYCLE_RECOVERED, context)
            return
        self._zone_index = recovery.slot
        self._clear(AnomalyKind.CYCLE_INTERRUPTED, {"cycle_id": run.cycle_id})
        if run.status is CycleStatus.PENDING:
            # Re-timed to start at `now`: the `advance` loop that follows
            # takes the ordinary `_start_cycle` path.
            await self._save()
        elif recovery.slot >= len(run.zone_runs):
            await self._complete_cycle(run, now, pump_was_on=False)
        else:
            await self._pump_on(run)
            await self._save()
            zone = run.zone_runs[recovery.slot]
            if zone.status is ZoneRunStatus.RUNNING:
                await self._reopen_zone(run, zone)
            else:
                await self._open_zone(run, zone, now)
        self._report(AnomalyKind.CYCLE_RECOVERED, context)

    def _follow_renames(self, run: CycleRun) -> None:
        """Point the journaled run at the entity ids the plan knows NOW.

        The registry tracker (Story 1.7) rewrites a renamed entity into the
        stored options and subentries and into the running adapter's alias
        map — the latter dies with the process. A run journaled before a
        rename and restored after it would read, close and re-open a dead
        id while the real valve stays on. The plan built at this setup is
        the current truth for every zone still in it (same `zone_id`) and
        for the pump; a zone no longer in the plan keeps its journaled id,
        the only one anybody has. Mutated in place, so the remapped ids are
        what the journal carries from here on.
        """
        current = {spec.zone_id: spec.valve_entity_id for spec in self.plan.zones}
        for zone in run.zone_runs:
            zone.valve_entity_id = current.get(zone.zone_id, zone.valve_entity_id)
        run.pump_entity_id = self.plan.pump_entity_id

    async def _read_states(self, run: CycleRun) -> dict[str, bool | None]:
        """Read every governed switch of `run`; a port that raises reads as doubtful."""
        states: dict[str, bool | None] = {}
        for entity_id in (
            *(zone.valve_entity_id for zone in run.zone_runs),
            run.pump_entity_id,
        ):
            try:
                states[entity_id] = await self._switches.async_is_on(entity_id)
            except Exception:  # noqa: BLE001 — a doubtful read is "not OFF", never a crash
                states[entity_id] = None
        return states

    async def _reopen_zone(self, run: CycleRun, zone: ZoneRun) -> None:
        """Re-open the live zone the orphan pass just closed; `actual_start` kept.

        `_open_zone` would stamp a new `actual_start` and lose the proven
        watering since the original one — the whole point of the credit rule.
        The confirmation outcome is recorded exactly as an open's.
        """
        confirmed, error = await self._command(on=True, entity_id=zone.valve_entity_id)
        zone.open_confirmed = confirmed
        zone.status = ZoneRunStatus.RUNNING if confirmed else ZoneRunStatus.FAILED
        self._valve_outcome(
            run,
            zone,
            confirmed=confirmed,
            error=error,
            kind=AnomalyKind.VALVE_OPEN_UNCONFIRMED,
        )
        await self._save()

    async def _start_cycle(self, run: CycleRun, now: datetime) -> None:
        """Start the cycle: pump on (FR3), journal, then open the first zone.

        A run with nothing to water — no zones at all, or every zone quoted
        at ZERO because rain covered it (Story 2.4) — completes at once
        without the pump: running it against a closed manifold is the one
        thing the skip exists to avoid. The all-covered zones are filed
        SKIPPED so history says why nothing flowed; the cycle itself still
        COMPLETES (and a run-now among them credits the day, Story 2.3).
        """
        if not any(zone.duration_s > 0 for zone in run.zone_runs):
            # Nothing to water — never run the pump against a closed manifold.
            for zone in run.zone_runs:
                zone.status = ZoneRunStatus.SKIPPED
            run.status = CycleStatus.RUNNING
            await self._save()
            await self._complete_cycle(run, now, pump_was_on=False)
            return
        await self._pump_on(run)
        run.status = CycleStatus.RUNNING
        await self._save()
        await self._open_zone(run, run.zone_runs[0], now)

    async def _pump_on(self, run: CycleRun) -> None:
        """Command the pump on and record the outcome — shared by start and resume.

        Fail-wet (AD-4/FR4): pressure is doubtful, zones still run their
        slots; aborting would be the one unforgivable branch.
        """
        confirmed, error = await self._command(on=True, entity_id=run.pump_entity_id)
        run.pump_on_confirmed = confirmed
        self._pump_outcome(
            run,
            confirmed=confirmed,
            error=error,
            kind=AnomalyKind.PUMP_ON_UNCONFIRMED,
        )

    async def _open_zone(self, run: CycleRun, zone: ZoneRun, now: datetime) -> None:
        """Open one zone's valve and record the commanded-vs-verified outcome.

        A zone quoted at ZERO seconds (Story 2.4: rain covered its whole base
        and it owed nothing) is SKIPPED instead: no open, no close, no
        anomaly, no instants (so `effective_seconds` reads 0 and the ledger
        books no deficit — its quote was 0), and the slot advances at once
        within this same call. Opening and closing it back to back would
        still command the valve twice and risk a spurious
        `VALVE_*_UNCONFIRMED` on a slow relay for water that must not flow.
        """
        if zone.duration_s == 0:
            zone.status = ZoneRunStatus.SKIPPED
            await self._advance_slot(run, now)
            return
        zone.actual_start = now
        confirmed, error = await self._command(on=True, entity_id=zone.valve_entity_id)
        zone.open_confirmed = confirmed
        # An unconfirmed open still consumes the slot: its planned end stands,
        # and the shortfall is ledger material (AD-4).
        zone.status = ZoneRunStatus.RUNNING if confirmed else ZoneRunStatus.FAILED
        self._valve_outcome(
            run,
            zone,
            confirmed=confirmed,
            error=error,
            kind=AnomalyKind.VALVE_OPEN_UNCONFIRMED,
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
        self._valve_outcome(
            run,
            zone,
            confirmed=confirmed,
            error=error,
            kind=AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        )
        if zone.status is not ZoneRunStatus.FAILED:
            zone.status = ZoneRunStatus.COMPLETED
        await self._advance_slot(run, now)

    async def _advance_slot(self, run: CycleRun, now: datetime) -> None:
        """Move past the current slot: open the next zone or complete the cycle.

        The tail shared by a closed zone (`_finish_zone`) and a skipped one
        (`_open_zone`, Story 2.4). The index is bumped BEFORE the save, so
        `next_wakeup()` — which the journal observer may call inside that
        save — already points at the next slot (or at nothing). ONE window
        does show a zero-duration slot as current: `_start_cycle`'s RUNNING
        save, where `_zone_index` is 0 and slot 0 may be about to be skipped
        — `next_wakeup()` then returns its planned end, which equals `now`,
        and the skip happens in this same `advance` call, so a re-arm there
        is harmless (the fire finds nothing due). Once a skip has been
        passed, no save ever points back at it. Reached with the
        pump ON in both cases: a skip happens inside a started cycle, and an
        all-skipped run never gets here (`_start_cycle` completes it
        without the pump).
        """
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
        the normal path, CANCELLED for `async_cancel_cycle`, INTERRUPTED for
        a run the startup reconciler could not resume (Story 3.2). All go
        through here so the pump-off, the settlement, the history append,
        the prune and the release have exactly one implementation.

        The ledger is settled right after the status turns terminal and
        BEFORE the snapshot is saved (Story 2.2): the journal written for this
        completion already carries the deficits it produced, and the deferred
        cycle created further down is quoted against them. Both terminal
        statuses settle — a cancelled run's un-reached zones owe their whole
        slot. One of the TWO places `settle` is called: the other is
        `_check_missed`, which settles a missed cycle nothing is going to
        water (Story 3.3). The settlement
        is also where every zone's RAIN BASELINE advances to the run's
        quote-time gauge reading (Story 2.4), so the deferred cycle quoted
        below is credited only for rain the gauge has counted since this
        run was quoted — never the same millimetres twice.

        Called from inside the lock by `advance`, `async_reconcile` and
        `async_cancel_cycle`, which already hold it — this method must NOT
        acquire the lock itself.
        """
        if pump_was_on:
            confirmed, error = await self._command(
                on=False,
                entity_id=run.pump_entity_id,
            )
            run.pump_off_confirmed = confirmed
            self._pump_outcome(
                run,
                confirmed=confirmed,
                error=error,
                kind=AnomalyKind.PUMP_OFF_UNCONFIRMED,
            )
        run.status = status
        # A cycle that reaches a terminal status supersedes an interrupted
        # one (Story 3.1): `CYCLE_INTERRUPTED` is controller-level, so ANY
        # completed or cancelled cycle is the proof the machine runs again.
        self._clear(AnomalyKind.CYCLE_INTERRUPTED, {"cycle_id": run.cycle_id})
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
        await self._pop_deferred(now)

    async def _pop_deferred(self, now: datetime) -> None:
        """Install the next deferred cycle, unless a manual pause holds (3.4).

        THE drain, with three call sites: the completion that releases the
        run, the resume that ends a pause, and `async_reconcile`. Each pop is
        an ordinary SCHEDULED decision — `_dispatch_scheduled` asks the
        ledger whether a run-now already credited the day — and each is
        saved, so a crash between two pops loses neither.

        The loop stops the moment something is installed: one cycle at a
        time. It also keeps going past a WAIVED pop, which installs nothing
        and leaves `self._run` empty, so the entry after it gets its turn in
        the same call.

        The pause gate is the whole reason this is a method rather than four
        lines: while the operator holds a governed switch open, the scheduler
        starts nothing — the queue is left exactly as it is and drained by
        the last close.

        Called from inside the lock by every one of its call sites; it must
        NOT acquire the lock itself.
        """
        if self.manual_override:
            return
        while self._deferred and self._run is None:
            kind, reference = self._deferred.pop(0)
            self._dispatch_scheduled(kind, reference, dispatch_at=now, now=now)
            await self._save()

    async def _check_missed(self, now: datetime) -> None:
        """File, report and make up every cycle of today that never ran (Story 3.3).

        THE watchdog. Called at the top of every `_advance_locked` — so at
        each window end the re-armed callback serves, and at every cycle
        transition — and once explicitly from `async_reconcile`, after the
        gate lifts and BEFORE its deferred-queue drain (see there).
        `watchdog.missed_cycles` makes the whole decision (pure, on the
        virtual clock); this method is the effects.

        A window that closed at or before the watchdog's floor
        (`watchdog_since`) is never a miss: the controller did not exist yet,
        so nobody skipped anything. That is what keeps a first install at
        16:00 from watering the morning cycle it was never configured for.

        A nominal restart and a nominal window end reach here, find nothing
        and return at once: no record, no anomaly, no command, and — the
        counter-metric — NO journal write. The single `_save` at the end runs
        only when something really changed.

        Per miss, in this order:

        1. build the record with `_build_run` (pure: the same snapshot code,
           the same ledger quote and the same gauge read a real cycle gets)
           and stamp it `MISSED`;
        2. SETTLE it — but only when nothing else is watering AND it will not
           be re-run (Decision 2). Every zone is PENDING, so
           `effective_seconds` reads 0 and each deficit clamps to `base_s`:
           exactly the "deficits at one base duration" cap, since the quote
           already folded in whatever was carried and `settle` REPLACES the
           ledger with this run's zones. Two cases never settle, for the same
           reason — the ledger they wrote would be thrown away minutes later:
           a miss that IS re-run (the re-run waters in full and its own
           completion settles it, and settling first would put the same
           shortfall into the re-run's quote as well), and a miss recorded
           while another cycle is live (that cycle's completion replaces the
           ledger with ITS zones). The live-run case therefore records the
           miss without booking anything: the record is the truth kept, the
           debt is not;
        3. append the record to history and prune — the same two lines
           `_dispatch_scheduled` uses. Filing BEFORE the dispatch is what
           makes `_next_occurrence` count the record (the re-run takes the
           `-2` id) and what makes this `(irrigation day, kind)` undetectable
           forever after: the record IS the at-most-once marker;
        4. report exactly one `MISSED_CYCLE`, controller-level and
           acknowledge-only — the engine never clears it;
        5. dispatch the late re-run through the ORDINARY scheduled path, at
           `now` on the cycle's own configured start, so the snapshot, the
           quote, the waiver rule and the verified actuation are all code
           this story never touches.

        The re-run is dispatched only while the machine is IDLE. At most one
        miss per check is rerunnable (the latest-starting one), so two misses
        never race; the guard covers the other case — a cycle of the other
        kind still watering — where installing a run would abandon a live one
        with its valve open. Such a miss is recorded and nothing else: no
        run, and no ledger write either, since the live cycle's own
        settlement would replace it before the next quote ever read it.

        A crash between the record and the debounced save re-detects the miss
        on the next check — the same bounded window `_dispatch_scheduled`
        already accepts for the day credit.

        A manual override (Story 3.4) returns before `missed_cycles` is even
        asked: a late re-run would fight the operator who is holding a valve
        open, and the record it files would burn this `(day, kind)`'s one
        detection for good. The check re-runs on resume, from the
        `_advance_locked` the last close performs — so a pause inside one
        irrigation day costs nothing. A pause that spans MIDNIGHT does: the
        lookback is today only, so a window that closed before the day turned
        is no longer visible to `missed_cycles`, and the start deferred
        behind the pause is dropped as stale at the resume. Neither leaves a
        record. That is the accepted cost of an unbounded pause, and it is
        what Story 3.5's safety timeout exists to bound.
        """
        if not self._reconciled or self.manual_override:
            return
        misses = missed_cycles(
            self.plan,
            now=now,
            history=self._history,
            season_enabled=self._season_enabled,
            active=self._run,
            deferred_kinds=self.deferred_kinds,
            day_credit=self._ledger.day_credit,
            since=self._watchdog_since,
        )
        if not misses:
            return
        for miss in misses:
            live = self._run is not None
            rerun = miss.rerunnable and not live
            record = self._build_run(miss.kind, miss.configured_start)
            record.status = CycleStatus.MISSED
            if not rerun and not live:
                self._ledger.settle(record)
            self._history.append(history_entry(record, now))
            self._history = prune_history(self._history, irrigation_day(now))
            self._report(
                AnomalyKind.MISSED_CYCLE,
                {
                    "cycle_id": record.cycle_id,
                    "kind": miss.kind.value,
                    "outcome": "rerun" if rerun else "recorded",
                },
            )
            if rerun:
                self._dispatch_scheduled(
                    miss.kind,
                    miss.configured_start,
                    dispatch_at=now,
                    now=now,
                    late_rerun=True,
                )
        await self._save()

    def _dispatch_scheduled(
        self,
        kind: CycleKind,
        reference: datetime,
        *,
        dispatch_at: datetime | None,
        now: datetime,
        late_rerun: bool = False,
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

        `late_rerun` (Story 3.3) marks the run the watchdog dispatched to make
        up for a missed cycle. It is a MARKER and nothing more — the decision,
        the quote, the waiver and every command are identical, which is the
        point: a late re-run is an ordinary scheduled cycle that happens to
        start after its window. A credited day still waives it.
        """
        run = self._build_run(
            kind, reference, dispatch_at=dispatch_at, late_rerun=late_rerun
        )
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
        late_rerun: bool = False,
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

        `manual` marks Story 2.1's run-now and `late_rerun` Story 3.3's
        watchdog make-up cycle. Both default to False so every existing
        caller stays unchanged, and both only ever reach the run object — the
        id, the windows and the occurrence counter are derived
        identically either way, which is what keeps a run-now (or a late
        re-run) indistinguishable from a scheduled cycle everywhere except in
        the accounting.

        Durations are QUOTED through the ledger (Story 2.2, AD-5), whichever
        caller is creating the run: a scheduled start, a deferred pop and a
        run-now all apply the carried deficit — and the rain credit (Story
        2.4) — the same way — FR11's "current durations" are the current
        EFFECTIVE durations, so a run-now after rain is rain-reduced like any
        cycle. The windows accumulate the quoted durations, so a carried
        deficit shifts every following zone and `next_wakeup()` with it.
        Quoting is pure; the ledger is only written when this run settles in
        `_complete_cycle`.

        The RAIN GAUGE is read HERE and nowhere else, exactly once per run,
        and the reading is snapshotted on the run next to the credit it
        produced (AD-8): a resumed, deferred-then-started or running cycle is
        never re-quoted, and rain during a cycle credits the next one. A
        port that raises is treated like a doubtful reading — `None`, full
        durations, no anomaly — the same defence `_command` gives the switch
        port, because a broken gauge must never be what stops the water.
        Doubt stays SILENT however long it lasts: no counter, no anomaly
        kind — a gauge unavailable for weeks waters in full with the
        adapter's DEBUG logs only, and the operator reads `rain_total_mm:
        null` in history. The registry tracker's CONFIGURED_ENTITY_MISSING
        on a removed or disabled sensor is the one fault surfaced.

        The gauge's IDENTITY (`RainPort.source_id`, Story 2.5) is snapshotted
        next to the reading and handed to the quote with it: the ledger
        credits nothing from a source other than the one its baselines were
        banked under, and settlement stamps the run's source as the new one.
        A late-arriving total (FR16) needs nothing special here: whatever
        the gauge says at THIS quote is compared to the baselines banked by
        the previous settlement, so a backfilled jump credits the next
        unquoted cycle in full — a completed run's snapshot, record and
        settlement are never revisited.
        """
        schedule = derive_schedule(self.plan, kind, reference)
        scheduled_start = schedule.start if dispatch_at is None else dispatch_at
        rain_total_mm = self._rain_total_mm()
        rain_source = None if self._rain is None else self._rain.source_id
        quotes = self._ledger.quote(self.plan.zones, kind, rain_total_mm, rain_source)
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
                    rain_credit_s=quote.rain_credit_s,
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
            rain_total_mm=rain_total_mm,
            rain_source=rain_source,
            late_rerun=late_rerun,
        )

    def _rain_total_mm(self) -> float | None:
        """Read the gauge once for a quote; None without a gauge or on a raise.

        The adapter is supposed to translate every doubtful reading into
        `None` itself (`RainPort`); a leaked exception is read the same way
        rather than propagated out of `request_cycle` or `async_run_now` —
        fail-wet (AD-4): the cycle is quoted on full durations.
        """
        if self._rain is None:
            return None
        try:
            return self._rain.total_mm()
        except Exception:  # noqa: BLE001 — see docstring: a broken gauge quotes full durations
            return None

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

    def _outcome(
        self,
        kind: AnomalyKind,
        context: dict[str, object],
        *,
        confirmed: bool,
        error: str | None,
    ) -> None:
        """Report `kind` when not confirmed, clear it when confirmed — no run needed.

        The discard pass of `async_reconcile` commands switches from the
        PLAN, not from a run (the run is what could not be read), so it
        cannot go through `_pump_outcome`/`_valve_outcome`; the rule is the
        same.
        """
        if confirmed:
            self._clear(kind, context)
        else:
            self._report(kind, context, error)

    def _pump_outcome(
        self,
        run: CycleRun,
        *,
        confirmed: bool,
        error: str | None,
        kind: AnomalyKind,
    ) -> None:
        """Turn one pump actuation outcome into a report or a clear (Story 3.1).

        Not confirmed → report `kind`. Confirmed → clear `kind`, and ONLY
        `kind`: a confirmation proves exactly the command it confirmed. The
        switch adapter reads "already in the target state" as confirmed, so
        the no-op OFF of a pump that never turned on would otherwise erase
        the `PUMP_ON_UNCONFIRMED` it just raised. The confirmation is the
        engine's "healthy again", said by the engine, never by Home
        Assistant.
        """
        context: dict[str, object] = {
            "cycle_id": run.cycle_id,
            "entity_id": run.pump_entity_id,
        }
        if confirmed:
            self._clear(kind, context)
        else:
            self._report(kind, context, error)

    def _valve_outcome(
        self,
        run: CycleRun,
        zone: ZoneRun,
        *,
        confirmed: bool,
        error: str | None,
        kind: AnomalyKind,
    ) -> None:
        """Turn one valve outcome into a report or a clear; the subject is the zone."""
        context: dict[str, object] = {
            "cycle_id": run.cycle_id,
            "zone_id": zone.zone_id,
            "entity_id": zone.valve_entity_id,
        }
        if confirmed:
            self._clear(kind, context)
        else:
            self._report(kind, context, error)

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

    def _clear(self, kind: AnomalyKind, context: dict[str, object]) -> None:
        """Declare `kind` healthy again, with `_report`'s exact defence.

        Called on every confirmation, every successful save and every
        terminal status whether or not anything is open: the engine keeps no
        record of what it reported (a reload rebuilds it empty while the
        manager may still hold the open issue), so "clear if open" is the
        adapter's decision, not the engine's.
        """
        with contextlib.suppress(Exception):
            self._anomalies.clear(kind, context)

    async def _save(self) -> None:
        """Journal the current state — called on EVERY transition (NFR2).

        The engine never touches storage: it awaits the port and moves on;
        debounce/immediacy is the adapter's policy.

        The snapshot carries everything needed to rebuild the machine, not
        just to display it: `last_run` keeps the completed cycle that would
        otherwise be overwritten the moment a deferred cycle is created, and
        the deferred queue keeps each entry's reference instant. `ledger` is
        the water debt between cycles (Story 2.2) — seeded back on setup,
        which is how a deficit survives a restart. `zone_index` is written
        for the record but NOT read back (Story 3.2): the reconciler derives
        the live slot from the zone statuses and instants, which a hand edit
        cannot desynchronize from the index. `watchdog_since` (Story 3.3) is
        the floor below which no window is ours to make up — the one field
        whose absence on read means LESS watering, not more.
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
            "watchdog_since": utc_iso(self._watchdog_since),
        }
        try:
            await self._journal.async_save(snapshot)
        except Exception as err:  # noqa: BLE001 — a failed save must not stop the water
            self._report(AnomalyKind.JOURNAL_SAVE_FAILED, {}, repr(err))
        else:
            # Storage is back: the write that just landed is the proof.
            self._clear(AnomalyKind.JOURNAL_SAVE_FAILED, {})
