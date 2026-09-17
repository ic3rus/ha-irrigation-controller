"""Timing adapter — the clock and the ONE re-armed timer (AD-3, AC 1).

Two responsibilities, deliberately split the way AD-3 splits time intents:

- **Daily starts** are NOT `next_wakeup()` intents. They arrive from outside
  via `sequencer.request_cycle`, driven by `async_track_time_change` — the
  only DST-safe way to express a daily wall-clock start.
- **Intra-cycle wake-ups** (zone boundaries, the missed-cycle watchdog's
  window-end deadline since Story 3.3; later the manual-valve timeout) are
  ALL `next_wakeup(now)` intents served by exactly ONE
  `async_track_point_in_time` registration. A second registration anywhere
  breaks AD-3 and the Epic 3 stories that hang their intents off this
  callback.

Neither is armed before the engine has RECONCILED (Story 3.2, AD-11):
`async_start` runs `sequencer.async_reconcile` first — at once when Home
Assistant is already running, otherwise on `EVENT_HOMEASSISTANT_STARTED`, so
the governed switches have had their chance to report a real state — and
only then arms the daily starts and the point-in-time re-arm.

No tick loop, no `asyncio.sleep`, no feature-owned timers.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from ..const import LOGGER, engine_state_signal  # noqa: TID252
from ..engine.plan import CycleKind  # noqa: TID252

if TYPE_CHECKING:
    from datetime import datetime, time

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant

    from ..engine.sequencer import Sequencer  # noqa: TID252


class HaClock:
    """Source of 'now' for the engine: aware and HA-local (`engine.ports.Clock`).

    Never `datetime.now()`: the engine's whole contract is aware, HA-local
    datetimes, and `dt_util.now()` is the one accessor that honours the
    instance's configured timezone.
    """

    def now(self) -> datetime:
        """Return the current HA-local, timezone-aware time."""
        return dt_util.now()


class CycleRunner:
    """Own the sequencer's timers: the daily starts and the single re-arm.

    Everything the engine needs from Home Assistant's event loop lives here,
    so the engine itself stays hass-free (AD-1) and virtual-clock testable.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        sequencer: Sequencer,
        clock: HaClock,
    ) -> None:
        """Wire the runner to its sequencer, clock and entry-scoped signal."""
        self._hass = hass
        self._entry = entry
        self._sequencer = sequencer
        self._clock = clock
        self._entry_id = entry.entry_id
        self._signal = engine_state_signal(entry.entry_id)
        self._unsub_point: CALLBACK_TYPE | None = None
        # The `EVENT_HOMEASSISTANT_STARTED` listener while reconciliation
        # waits for boot to finish (Story 3.2); cancelled by `async_shutdown`
        # so an unload before HA started leaves no dangling listener.
        self._unsub_started: CALLBACK_TYPE | None = None
        # One daily tracker per enabled cycle kind, with the start it was
        # armed for — `async_replan` only touches the kinds whose start
        # changed (see there for why).
        self._daily: dict[CycleKind, tuple[time, CALLBACK_TYPE]] = {}
        self._shutdown = False
        # A config change landed while a cycle was active (Story 1.7): the
        # reload it needs waits for the cycle to complete, and several edits
        # coalesce into this ONE flag — hence one reload.
        self._reload_pending = False

    @property
    def reload_pending(self) -> bool:
        """Return whether a config change is waiting for the cycle to end.

        Read-only: the cycle-status sensor projects it as
        `config_change_pending` (AD-6 — a projection, never a second state).
        """
        return self._reload_pending

    async def async_start(self) -> None:
        """Reconcile the journal against the hardware, THEN arm the timers.

        Reconciliation (Story 3.2, AD-11) runs exactly once per setup and
        before anything is armed: nothing may step a restored run before it
        has been resynced. It runs at once when Home Assistant is already
        RUNNING (a reload, the forced one of Story 1.7 included); during
        boot — `CoreState.starting`, which `hass.is_running` also reports
        as running, hence the explicit state check — it waits for
        `EVENT_HOMEASSISTANT_STARTED`, because the governed switches'
        integrations may still be loading and would read `unknown` — which
        the engine would treat as "not OFF" and command off, and as
        "watering unproven". Until then no daily start and no point-in-time
        handle exists, so `_async_daily_start`/`_async_fire` cannot run; the
        engine's own `_reconciled` gate is the belt to that suspender for
        the services.

        The daily starts are read off `plan.morning_start`/`plan.evening_start`
        (`datetime.time`): `async_track_time_change` re-computes the next
        occurrence in local time after every fire, which is what survives a
        DST transition. Edited start times reach these timers through a
        reload (FR8's restart-free half) or, while a reload is deferred
        behind a running cycle, through `async_replan`.
        """
        if self._hass.state is CoreState.running:
            await self._async_reconcile()
            return
        self._unsub_started = self._hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED,
            self._async_on_started,
        )

    @callback
    def _async_on_started(self, _event: Event) -> None:
        """Reconcile once Home Assistant has started — the boot-time path.

        Synchronous on purpose: the one-time listener has already removed
        itself when this runs, so the handle is dropped HERE, in the same
        loop iteration, before `async_shutdown` could ever call a remove
        that would log "unknown job listener". The reconciliation itself is
        an entry-owned task: it awaits verified service calls, and an unload
        landing meanwhile cancels it with the entry.
        """
        self._unsub_started = None
        if self._shutdown:
            return
        self._entry.async_create_task(
            self._hass,
            self._async_reconcile(),
            name=f"{self._entry_id} startup reconciliation",
        )

    async def _async_reconcile(self) -> None:
        """Run the engine's reconciliation, then arm and push — the start tail.

        The arming rides a `finally`, like every other tail: a port that
        raises out of the reconciler must not leave the entry with no daily
        start at all. `_arm_daily` is skipped after shutdown (an unload can
        land while a verified close is in flight); `_rearm` and
        `_async_maybe_reload` carry their own guards. The push is what makes
        a recovery visible on the sensors at once — a resumed run's status,
        a closed run's deficits, the health entity's new issue.
        """
        if self._shutdown:
            return
        try:
            await self._sequencer.async_reconcile(self._clock.now())
        except Exception:  # noqa: BLE001 — see below: setup must not fail for this
            # The engine defends every port call, so this is a defect, not a
            # hardware fault — and the one outcome AD-4 forbids is the
            # integration refusing to load over it: the season would end
            # silently. Logged with its traceback; the arming below still
            # runs, so the daily starts water.
            LOGGER.exception("Startup reconciliation failed; scheduling continues")
        finally:
            if not self._shutdown:
                self._arm_daily()
            self._rearm()
            self._async_maybe_reload()
            self._async_push_state()

    @callback
    def async_replan(self) -> None:
        """Re-arm the daily starts from the CURRENT plan (Story 1.7).

        Called by the update listener right after it swapped
        `sequencer.plan` during a deferral: the daily trackers were armed
        from the old plan, and a start time edited to an instant that passes
        before the deferred reload lands would otherwise be skipped for the
        day with no anomaly. Still the same two primitives and still the one
        point-in-time registration — only daily handles are replaced.

        ONLY the kinds whose start (or enabled state) changed are re-armed.
        `async_track_time_change` fires at once when armed within the very
        second its pattern matches, so blindly re-arming an unchanged tracker
        during the second of its own daily start would request that cycle a
        second time (and a duration edit landing right at 07:00 is exactly
        the mid-cycle case this story exists for).

        A no-op after shutdown: nothing may be armed behind an unload.
        """
        if self._shutdown:
            return
        self._arm_daily()

    @callback
    def _arm_daily(self) -> None:
        """Bring the daily trackers in line with the CURRENT plan.

        One `async_track_time_change` per enabled cycle kind; a tracker whose
        start is unchanged is left alone, one whose start changed (or whose
        kind is now disabled) is cancelled first.
        """
        plan = self._sequencer.plan
        # The evening cycle always runs; the morning one is opt-in (spring is
        # evening-only), and a disabled morning start is simply never armed.
        wanted = {CycleKind.EVENING: plan.start_time(CycleKind.EVENING)}
        if plan.morning_enabled:
            wanted[CycleKind.MORNING] = plan.start_time(CycleKind.MORNING)
        for kind, (armed_start, unsubscribe) in list(self._daily.items()):
            if wanted.get(kind) != armed_start:
                unsubscribe()
                del self._daily[kind]
        for kind, start in wanted.items():
            if kind in self._daily:
                continue
            self._daily[kind] = (
                start,
                async_track_time_change(
                    self._hass,
                    partial(self._async_daily_start, kind),
                    hour=start.hour,
                    minute=start.minute,
                    second=start.second,
                ),
            )

    async def async_cancel_cycle(self) -> bool:
        """Cancel the active cycle and re-arm; False when nothing was running.

        Every command goes through the runner rather than reaching the
        sequencer directly: `_rearm()` and the dispatcher push are the
        runner's to own, and a mutating call that skipped them would leave the
        ONE timer pointing at a boundary the engine no longer has.

        After a cancel nothing of the cancelled cycle is left to serve, so
        `_rearm` never points at one of its boundaries — that IS the
        "re-armed to nothing" half of AC 4. Since Story 3.3 the handle itself
        usually survives, holding the watchdog's next window-end deadline
        instead; it is cancelled outright only when the engine answers `None`
        (the season is off, or the plan has no zones).
        """
        try:
            return await self._sequencer.async_cancel_cycle(self._clock.now())
        finally:
            self._rearm()
            self._async_maybe_reload()
            self._async_push_state()

    async def async_run_now(self, kind: CycleKind) -> bool:
        """Start `kind` now on operator demand; False when the engine refused.

        Mirrors `async_cancel_cycle`: a mutating command reaches the sequencer
        through the runner and nowhere else, so `_rearm`, the deferred reload
        and the dispatcher push keep their single home — a call that skipped
        them would leave the ONE timer pointing at a boundary the engine no
        longer has, or (here) at nothing while a cycle is running.

        The `advance` is `_async_daily_start`'s, for the same reason: the new
        run is dispatched at `now`, so advancing starts it in this very tick
        instead of waiting for the re-armed timer to fire on an instant that
        is already in the past. It is called unconditionally because it is
        idempotent — on the refused paths it either finds no run at all, or
        performs exactly the transitions the re-armed timer would have served
        a moment later, and it never touches the live run's identity.

        ONE clock read for both calls: a single operator action must not derive
        the run's windows from one instant and the decision to start it from a
        later one.
        """
        now = self._clock.now()
        try:
            started = await self._sequencer.async_run_now(kind, now)
            await self._sequencer.advance(now)
        finally:
            self._rearm()
            self._async_maybe_reload()
            self._async_push_state()
        return started

    async def async_switch_observed(
        self,
        entity_id: str,
        *,
        is_on: bool,
        manual: bool,
    ) -> None:
        """Hand one governed-switch observation to the engine (Story 3.4).

        The switch adapter's standing watch is a synchronous `@callback`; it
        reaches this through `entry.async_create_background_task`, so the
        observation is served on the entry's own task and an unload cancels
        it with the entry.

        Mirrors every other mutating command: ONE clock read, the engine
        call, then the pinned `finally` triple. The engine may have installed
        a deferred cycle and started it (a resume), so the ONE timer has to
        be re-pointed and the entities pushed; `_async_maybe_reload` keeps
        its place in the middle, because a resume that completes a cycle is
        as good a moment to fire a deferred reload as any other.

        No timer of its own, and no `asyncio`: the pause is unbounded in this
        story (Story 3.5's safety timeout is what bounds it) and every future
        time intent hangs off `next_wakeup` (AD-3).

        A no-op after shutdown, and that guard is load-bearing rather than
        defensive: `async_suspend` calls `async_shutdown()` FIRST and then
        commands the live valve and the pump off. Each of those closes is an
        observation, and one of them releasing the last hand-opened switch
        would resume the scheduler — draining the deferred queue and opening
        a valve — on an entry that is being torn down, with no timer left to
        close it again.
        """
        if self._shutdown:
            return
        now = self._clock.now()
        try:
            await self._sequencer.async_switch_observed(
                entity_id,
                is_on=is_on,
                manual=manual,
                now=now,
            )
        finally:
            self._rearm()
            self._async_maybe_reload()
            self._async_push_state()

    async def async_set_season(self, *, enabled: bool) -> None:
        """Turn the season on or off and re-arm (FR10, AC 1).

        The daily `async_track_time_change` trackers are deliberately NOT torn
        down or re-registered here: they stay armed and the gate lives in the
        engine. A second arming path would break AD-3 and the "exactly one
        live registration" invariant the tests measure.
        """
        try:
            await self._sequencer.async_set_season(
                enabled=enabled,
                now=self._clock.now(),
            )
        finally:
            self._rearm()
            self._async_maybe_reload()
            self._async_push_state()

    @callback
    def async_request_reload(self) -> None:
        """Reload the entry now if idle, otherwise once the cycle completes.

        The ONE update listener calls this on every config change that
        matters (Story 1.7). A reload tears the engine down, and a running
        cycle torn down mid-flight leaves its valve and the pump energized
        with no timer left to close them — so while a run is active (PENDING
        counts: it holds a snapshot too) the request only raises the flag,
        and `_async_maybe_reload` fires it from the `finally` tail of the
        step that completes the cycle. Several requests raise one flag.

        A no-op after shutdown: the entry is already on its way out, and a
        reload scheduled from a dying runner would race the one replacing it.
        """
        if self._shutdown:
            return
        if self._sequencer.current_run is None:
            self._hass.config_entries.async_schedule_reload(self._entry_id)
            return
        if not self._reload_pending:
            self._reload_pending = True
            # Pushed at once, not at the next engine step: the sensor's
            # `config_change_pending` must flip the moment the edit lands.
            self._async_push_state()

    @callback
    def _async_maybe_reload(self) -> None:
        """Fire the deferred reload once nothing is running (Story 1.7).

        Sits in every `finally` tail AFTER `_rearm` and BEFORE the push: the
        last push of a completed cycle then already reads
        `config_change_pending: false`. The shutdown guard is the same one
        `_rearm` has — an in-flight step finishing after unload must schedule
        nothing.
        """
        if (
            self._reload_pending
            and not self._shutdown
            and self._sequencer.current_run is None
        ):
            self._reload_pending = False
            self._hass.config_entries.async_schedule_reload(self._entry_id)

    async def async_suspend(self) -> bool:
        """Cancel the timers, then make the hardware safe — the forced-unload hook.

        `async_shutdown()` runs FIRST: it cancels the point-in-time and daily
        handles and sets the shutdown flag, so an `advance` still in flight
        when the unload landed cannot re-arm a timer (or schedule a reload)
        from its `finally` while the engine is closing the valve — a re-armed
        boundary would reopen a valve on a manifold whose pump was just
        switched off. Only then does the engine command the live valve and
        the pump off; the run and the journal stay untouched (see
        `Sequencer.async_suspend`).

        Awaited from `async_unload_entry` as an ordinary lifecycle step, NOT
        from an HA-stop handler: AD-11's "nothing closes valves in shutdown
        handlers" is untouched, because HA stop never unloads entries.
        """
        self.async_shutdown()
        return await self._sequencer.async_suspend(self._clock.now())

    @callback
    def async_shutdown(self) -> None:
        """Cancel every timer this runner armed, idempotently.

        Registered through `entry.async_on_unload`: a surviving timer after
        unload fails PHCC's `verify_cleanup`, and the reload-per-config-change
        regime runs this constantly.
        """
        self._shutdown = True
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        if self._unsub_point is not None:
            self._unsub_point()
            self._unsub_point = None
        for _, unsubscribe in self._daily.values():
            unsubscribe()
        self._daily.clear()

    async def _async_daily_start(self, kind: CycleKind, _now: datetime) -> None:
        """Request `kind` and drive it: the daily-start half of AD-3.

        The callback's own `now` is ignored: it arrives in UTC, while the
        engine's contract is HA-local aware datetimes. Advancing immediately
        starts the cycle in this same tick; the re-arm still catches whatever
        boundary is left.

        The re-arm rides a `finally` for the reason spelled out in
        `_async_fire`: an escaping exception must never be what disarms the
        ONE timer.
        """
        try:
            await self._sequencer.request_cycle(kind, self._clock.now())
            await self._sequencer.advance(self._clock.now())
        finally:
            self._rearm()
            self._async_maybe_reload()
            self._async_push_state()

    async def _async_fire(self, _now: datetime) -> None:
        """Serve the due time intent, then re-arm on the next one.

        The spent handle is NOT cleared here: `_rearm` unsubscribes it (a
        no-op on an already-fired timer) so that cancel-then-arm stays the one
        path every registration goes through — which is what makes "exactly
        one live registration" checkable rather than merely intended.

        The re-arm and the push ride a `finally`: this handle has ALREADY
        fired, so it is the re-arm alone that keeps the loop alive. Anything
        the engine does not swallow — a `CancelledError` (which its blanket
        `except Exception` does not catch), a malformed stored history once
        Story 3.2 loads one — would otherwise leave the cycle with no timer
        at all, and the open valve and the pump energized until a daily start
        that only DEFERS the next cycle rather than closing them.
        """
        try:
            await self._sequencer.advance(self._clock.now())
        finally:
            self._rearm()
            self._async_maybe_reload()
            self._async_push_state()

    @callback
    def _rearm(self) -> None:
        """Point the ONE timer at the engine's next intent (AC 1).

        Synchronous on purpose — no `await` anywhere between cancel and arm:
        the daily timer and the point-in-time timer can both reach this, and
        an await in the middle would let a stale wake-up win the race.

        `advance()` is never called from here: re-arming always goes through
        the timer, or a long cycle would recurse. A `next_wakeup()` already in
        the past is fine — `async_track_point_in_time` fires it immediately
        and the loop converges.

        The clock read is handed to the engine (Story 3.3): while a cycle is
        active the answer is its own boundary, and while the machine is idle
        it is the watchdog's next cycle-window end — which is why the handle
        now normally exists between cycles too, still exactly one of it.
        """
        if self._unsub_point is not None:
            self._unsub_point()
            self._unsub_point = None
        if self._shutdown:
            # An unload can land while an advance is still in flight (a
            # verified service call takes real time). Without this guard that
            # in-flight step would arm a fresh timer AFTER shutdown ran, and
            # the entry would leak a timer per reload.
            return
        when = self._sequencer.next_wakeup(self._clock.now())
        if when is None:
            return
        self._unsub_point = async_track_point_in_time(
            self._hass,
            self._async_fire,
            when,
        )

    @callback
    def _async_push_state(self) -> None:
        """Tell the passive entity projections that engine state moved (AD-6)."""
        async_dispatcher_send(self._hass, self._signal)
