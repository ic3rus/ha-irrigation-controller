"""Timing adapter — the clock and the ONE re-armed timer (AD-3, AC 1).

Two responsibilities, deliberately split the way AD-3 splits time intents:

- **Daily starts** are NOT `next_wakeup()` intents. They arrive from outside
  via `sequencer.request_cycle`, driven by `async_track_time_change` — the
  only DST-safe way to express a daily wall-clock start.
- **Intra-cycle wake-ups** (zone boundaries; later the watchdog and the
  manual-valve timeout) are ALL `next_wakeup()` intents served by exactly ONE
  `async_track_point_in_time` registration. A second registration anywhere
  breaks AD-3 and the Epic 3 stories that hang their intents off this
  callback.

No tick loop, no `asyncio.sleep`, no feature-owned timers.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from ..const import engine_state_signal  # noqa: TID252
from ..engine.plan import CycleKind  # noqa: TID252

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant

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
        self._sequencer = sequencer
        self._clock = clock
        self._signal = engine_state_signal(entry.entry_id)
        self._unsub_point: CALLBACK_TYPE | None = None
        self._unsub_daily: list[CALLBACK_TYPE] = []
        self._shutdown = False

    async def async_start(self) -> None:
        """Arm the daily cycle starts from the plan's configured times.

        Read off `plan.morning_start`/`plan.evening_start` (`datetime.time`):
        `async_track_time_change` re-computes the next occurrence in local
        time after every fire, which is what survives a DST transition.
        Edited start times reach these timers for free — a config change
        reloads the entry and rebuilds this runner (FR8's restart-free half).
        """
        plan = self._sequencer.plan
        # The evening cycle always runs; the morning one is opt-in (spring is
        # evening-only), and a disabled morning start is simply never armed.
        kinds = [CycleKind.EVENING]
        if plan.morning_enabled:
            kinds.append(CycleKind.MORNING)
        for kind in kinds:
            start = plan.start_time(kind)
            self._unsub_daily.append(
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

        After a cancel `next_wakeup()` is `None`, so `_rearm` cancels the
        point-in-time handle — that IS the "re-armed to nothing" half of AC 4.
        """
        try:
            return await self._sequencer.async_cancel_cycle(self._clock.now())
        finally:
            self._rearm()
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
            self._async_push_state()

    @callback
    def async_shutdown(self) -> None:
        """Cancel every timer this runner armed, idempotently.

        Registered through `entry.async_on_unload`: a surviving timer after
        unload fails PHCC's `verify_cleanup`, and the reload-per-config-change
        regime runs this constantly.
        """
        self._shutdown = True
        if self._unsub_point is not None:
            self._unsub_point()
            self._unsub_point = None
        for unsubscribe in self._unsub_daily:
            unsubscribe()
        self._unsub_daily.clear()

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
        when = self._sequencer.next_wakeup()
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
