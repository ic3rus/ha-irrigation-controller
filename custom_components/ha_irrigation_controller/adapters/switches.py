"""Verified switch adapter — the ONE implementation of `SwitchPort` (AD-7).

Every valve and pump command goes through here: one `switch` service call
carrying the cycle's `Context()`, then a watch for the expected state change
under a timeout. The adapter only returns confirmed/not-confirmed — anomaly
reporting for unconfirmed actuations is the ENGINE's job (`Sequencer._command`
already raises the right `*_UNCONFIRMED` kind); reporting here too would
produce two anomalies per failure, each fanned out to a Repairs issue and a
push notification by Story 3.1.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

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

    from homeassistant.core import EventStateChangedData, HomeAssistant


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
    ) -> None:
        """Wire the adapter to hass, its timeout and the cycle id source."""
        self._hass = hass
        self._timeout_s = timeout_s
        self._cycle_id_provider = cycle_id_provider
        self._context_cycle_id: str | None = None
        self._context: Context | None = None
        # Renames followed since this adapter was built: stored id → the id
        # the registry now knows the entity by (Story 1.7). A running cycle
        # keeps commanding the id in its AD-8 snapshot; this map is the one
        # place that knows Home Assistant renamed the target underneath it.
        # Cleared for free on every reload (the adapter is rebuilt).
        self._aliases: dict[str, str] = {}

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
            return Context()
        if self._context is None or self._context_cycle_id != cycle_id:
            self._context = Context()
            self._context_cycle_id = cycle_id
        return self._context

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
            unsubscribe()
