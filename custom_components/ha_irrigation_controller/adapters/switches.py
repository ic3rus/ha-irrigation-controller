"""Verified switch adapter — the ONE implementation of `SwitchPort` (AD-7).

Every valve and pump command goes through here: one `switch` service call
carrying the cycle's `Context()`, then a watch for the expected state change
under a timeout. The adapter only returns confirmed/not-confirmed — anomaly
reporting for unconfirmed actuations is the ENGINE's job (`Sequencer._command`
already raises the right `*_UNCONFIRMED` kind); reporting here too would
produce two anomalies per failure, each fanned out to a Repairs issue and a
push notification by Story 3.1.

Since Story 3.4 the adapter also holds ONE standing state watch over the
governed switches (the pump and every zone valve) and REPORTS what it sees —
entity, on or off, ours or not. It decides nothing: the paused state belongs
to the engine, which owns the deferral and the resume. Detection is
best-effort by AD-7 and its failure direction is fixed: it may miss a manual
act, it must never invent one.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Final

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    Platform,
)
from homeassistant.core import Context, Event, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from ..const import LOGGER  # noqa: TID252

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import CALLBACK_TYPE, EventStateChangedData, HomeAssistant

# How many recently issued command contexts the manual-override watch keeps.
# `self._context` alone is not enough: the adapter mints one `Context` per
# cycle id, and the watch sees the CLOSE of a zone whose cycle has already
# been replaced by the next one's context — plus every out-of-cycle command
# (the reconciler's orphan pass, Story 3.5's timeout close) gets a fresh one.
# The events we care about all arrive within the actuation timeout, so a
# handful of ids is plenty; the bound is what keeps this from growing for the
# lifetime of the entry.
ISSUED_CONTEXTS: Final = 32


class VerifiedSwitchAdapter:
    """Command a switch entity and verify the actuation via its state change.

    Implements `engine.ports.SwitchPort`. The verification sequence is pinned
    (each step defends a real race — see the story's Dev Notes):

    1. Register the state watch BEFORE the service call (a fast switch flips
       before a late watcher starts, producing a false timeout).
    2. The service call runs INSIDE the timeout (`blocking=True` on a wedged
       integration must not hang the cycle with a valve open).
    3. Check `is_state` after the call returns (commanding an already-on
       valve fires NO `state_changed` — a no-op must confirm, not time out).
    4. Otherwise await the watched state change.
    5. Timeouts and `HomeAssistantError` are the adapter's own failures,
       translated to False — commanded, unconfirmed.
    6. Always unsubscribe (a leaked listener per command accumulates forever).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        timeout_s: int,
        cycle_id_provider: Callable[[], str | None],
        governed_entities: Callable[[], tuple[str, ...]] | None = None,
        on_observed: Callable[[str, bool, bool], None] | None = None,
    ) -> None:
        """Wire the adapter to hass, its timeout and its late-bound providers.

        `governed_entities` and `on_observed` are the manual-override watch
        (Story 3.4), late-bound exactly like `cycle_id_provider`: the plan is
        swapped under a running engine (AD-8) and the sequencer the callback
        reaches does not exist yet when the adapter is built. Both default to
        None — an adapter with no watch to arm is still a complete
        `SwitchPort`, which is what every command-level test builds.
        """
        self._hass = hass
        self._timeout_s = timeout_s
        self._cycle_id_provider = cycle_id_provider
        self._governed_entities = governed_entities
        self._on_observed = on_observed
        self._context_cycle_id: str | None = None
        self._context: Context | None = None
        # Renames followed since this adapter was built: stored id → the id
        # the registry now knows the entity by (Story 1.7). A running cycle
        # keeps commanding the id in its AD-8 snapshot; this map is the one
        # place that knows Home Assistant renamed the target underneath it.
        # Cleared for free on every reload (the adapter is rebuilt).
        self._aliases: dict[str, str] = {}
        # The ids of the contexts this adapter has issued, newest last and
        # bounded (see ISSUED_CONTEXTS). A `deque` for the eviction order and
        # a `set` for the O(1) membership the state callback needs on every
        # governed transition.
        self._issued_order: deque[str] = deque(maxlen=ISSUED_CONTEXTS)
        self._issued: set[str] = set()
        # Entities with a command in flight right now. The FAIL-SAFE half of
        # "not integration-issued": a switch integration that does not
        # propagate the service context onto the resulting state would make
        # every one of our own commands read as manual, and the engine would
        # pause itself the moment it opened a valve.
        self._in_flight: set[str] = set()
        # The ONE standing state watch over the governed set.
        self._unsub_watch: CALLBACK_TYPE | None = None

    async def async_turn_on(self, entity_id: str) -> bool:
        """Command `entity_id` on; return True iff the actuation confirmed."""
        return await self._command(entity_id, target_state=STATE_ON)

    async def async_turn_off(self, entity_id: str) -> bool:
        """Command `entity_id` off; return True iff the actuation confirmed."""
        return await self._command(entity_id, target_state=STATE_OFF)

    async def async_is_on(self, entity_id: str) -> bool | None:
        """Read the ACTUAL state of `entity_id`: True on, False off, else None.

        The reconciler's resync input (Story 3.2). Read through the same
        alias map the commands use, so a valve renamed while its cycle ran
        is read under the id the registry knows now. Anything that is not a
        plain `on`/`off` — no state at all, `unknown`, `unavailable` (the
        switch's integration may still be booting) — is `None`: the engine
        reads doubt as "not OFF, watering unproven", the fail-wet direction.

        A coroutine for the port's sake only (the engine awaits every switch
        read the way it awaits every command); the read itself is the
        synchronous state-machine lookup.
        """
        state = self._hass.states.get(self._aliases.get(entity_id, entity_id))
        if state is None:
            return None
        if state.state == STATE_ON:
            return True
        if state.state == STATE_OFF:
            return False
        return None

    @callback
    def async_rename(self, old_entity_id: str, new_entity_id: str) -> None:
        """Route every future command for `old_entity_id` to `new_entity_id`.

        Earlier aliases that resolved to `old_entity_id` are re-pointed too,
        so a valve renamed twice during one cycle (a → b → c) still closes
        under its snapshot id `a`, and a → b → a yields the identity `a → a`
        by the same loop. No alias is ever dropped: when two configured ids
        are SWAPPED (a → tmp, b → a, tmp → b) the snapshot `a` must keep
        following its own hardware to `b`, and deleting the entry for the id
        just renamed onto would send it to the other valve instead.
        """
        for stored, current in self._aliases.items():
            if current == old_entity_id:
                self._aliases[stored] = new_entity_id
        self._aliases[old_entity_id] = new_entity_id

    def _command_context(self) -> Context:
        """Return the cycle's one `Context`, minted lazily per cycle id (AD-7).

        Only the current cycle's context is cached (replaced on change);
        commands issued outside a cycle get a fresh `Context()` each.
        """
        cycle_id = self._cycle_id_provider()
        if cycle_id is None:
            return self._remember(Context())
        if self._context is None or self._context_cycle_id != cycle_id:
            self._context = self._remember(Context())
            self._context_cycle_id = cycle_id
        return self._context

    def _remember(self, context: Context) -> Context:
        """Record `context` as ours and return it (Story 3.4).

        Every context this adapter mints passes through here, so the watch's
        "was this ours?" test is a membership check and nothing else. The
        deque's own `maxlen` does the eviction; the set follows it.
        """
        if len(self._issued_order) == self._issued_order.maxlen:
            self._issued.discard(self._issued_order[0])
        self._issued_order.append(context.id)
        self._issued.add(context.id)
        return context

    @callback
    def async_start_watch(self) -> None:
        """Arm the ONE standing watch over the governed switches (Story 3.4).

        Cancels any previous subscription first, so re-arming after the plan
        was swapped under a running cycle (Story 1.7's deferred reload) never
        leaves two live registrations — the `ConfiguredEntityTracker.async_start`
        shape, and for the same reason: the governed set moves with the plan.

        The set is read LIVE from the provider, so this is also the only
        place that needs re-running when it changes. A plan with no governed
        entity at all (nothing configured yet) subscribes to nothing.
        """
        self.async_stop_watch()
        if self._governed_entities is None or self._on_observed is None:
            return
        entities = self._governed_entities()
        if not entities:
            return
        self._unsub_watch = async_track_state_change_event(
            self._hass,
            list(entities),
            self._async_observed,
        )

    @callback
    def async_stop_watch(self) -> None:
        """Drop the standing watch, idempotently (the unload path)."""
        if self._unsub_watch is not None:
            self._unsub_watch()
            self._unsub_watch = None

    @callback
    def _async_observed(self, event: Event[EventStateChangedData]) -> None:
        """Report one governed transition — and decide nothing (AD-7).

        The ignore rules are ASYMMETRIC on purpose. Only a plain `on`↔`off`
        transition can ever make the engine ENTER a pause, because detection's
        failure direction is fixed: missing a manual act is acceptable,
        inventing a pause is not. But a switch the engine is holding in its
        paused set must always be releasable, or an entity that goes
        `unavailable` (or is removed) while held open would pause the
        scheduler for ever, with no watering and no anomaly. So:

        - **dropped entirely** — `old_state is None`, an entity APPEARING
          (boot, an integration reloading, a newly created switch): nobody
          touched anything, and there is nothing to release either. Same for
          an unchanged state (an attribute-only write, which fires
          `state_changed` just the same) and for anything LEAVING
          `unknown`/`unavailable`: a booting switch is not an operator, and
          `unavailable`→`on` would otherwise read as a hand-opened valve
          every time the hardware reconnects.
        - **reported as a non-manual OFF** — a governed entity going from a
          plain `on`/`off` INTO `unknown`/`unavailable`, or removed
          (`new_state is None`). We can no longer see it open, so the engine
          must not keep holding it: `is_on=False` can never add to the paused
          set, only release from it, and `manual=False` keeps it out of
          Story 3.5's "the operator did this" accounting.

        "Not integration-issued" is then two tests, and BOTH must pass for a
        change to count as manual: the event's context is neither one this
        adapter minted nor the child of one (a service call's context becomes
        the parent of the state change in some integrations), AND there is no
        command of ours in flight on that entity.
        """
        if self._on_observed is None:
            return
        old_state, new_state = event.data["old_state"], event.data["new_state"]
        entity_id = event.data["entity_id"]
        if old_state is None or old_state.state not in (STATE_ON, STATE_OFF):
            return
        if new_state is None:
            self._on_observed(entity_id, False, False)  # noqa: FBT003 — removed: release only
            return
        if new_state.state == old_state.state:
            return
        if new_state.state not in (STATE_ON, STATE_OFF):
            self._on_observed(entity_id, False, False)  # noqa: FBT003 — doubtful: release only
            return
        context = event.context
        ours = context.id in self._issued or (
            context.parent_id is not None and context.parent_id in self._issued
        )
        manual = not ours and entity_id not in self._in_flight
        self._on_observed(entity_id, new_state.state == STATE_ON, manual)

    async def _command(self, entity_id: str, *, target_state: str) -> bool:
        """Run the pinned command-and-verify sequence for one entity.

        The alias is resolved FIRST, before the watch and the call: both must
        address the id the registry knows now, or a renamed valve would be
        commanded under a dead id (a no-op call) and watched under it too (a
        guaranteed timeout).
        """
        entity_id = self._aliases.get(entity_id, entity_id)
        future: asyncio.Future[None] = self._hass.loop.create_future()

        @callback
        def _state_changed(event: Event[EventStateChangedData]) -> None:
            # `new_state` is None for an entity removed mid-command; resolving
            # a done future raises InvalidStateError inside a callback, where
            # nothing catches it — a switch can emit several state_changed
            # events before the unsubscribe runs.
            new_state = event.data["new_state"]
            if (
                new_state is not None
                and new_state.state == target_state
                and not future.done()
            ):
                future.set_result(None)

        unsubscribe = async_track_state_change_event(
            self._hass,
            [entity_id],
            _state_changed,
        )
        self._in_flight.add(entity_id)
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._hass.services.async_call(
                    Platform.SWITCH,
                    SERVICE_TURN_ON if target_state == STATE_ON else SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: entity_id},
                    blocking=True,
                    context=self._command_context(),
                )
                # A no-op command (already in the target state) fires no
                # state_changed event: the state itself is the confirmation.
                if self._hass.states.is_state(entity_id, target_state):
                    return True
                await future
                return True
        except TimeoutError:
            # A timeout during the blocking call cancels it — the semantics
            # stay exactly right: commanded, unconfirmed.
            LOGGER.debug(
                "Actuation of %s to %r unconfirmed after %s s",
                entity_id,
                target_state,
                self._timeout_s,
            )
            return False
        except HomeAssistantError as err:
            LOGGER.debug(
                "Actuation of %s to %r failed: %s",
                entity_id,
                target_state,
                err,
            )
            return False
        finally:
            # Both in the ONE existing `finally`: a leaked listener
            # accumulates for ever, and a leaked in-flight entry would make
            # that entity's every future manual change read as ours.
            self._in_flight.discard(entity_id)
            unsubscribe()
