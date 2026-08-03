"""Verified switch adapter: service call + state watch under a timeout (AC 2, 3).

The adapter is the ONE implementation of `engine.ports.SwitchPort` (AD-7): it
commands the `switch` service with the cycle's `Context()` and reports a
confirmed/not-confirmed outcome — it never reports anomalies itself (the
engine owns that; double-reporting would double every notification in 3.1).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.ha_irrigation_controller.adapters.switches import (
    VerifiedSwitchAdapter,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant, ServiceCall

VALVE = "switch.zone_1_valve"


def make_adapter(
    hass: HomeAssistant,
    *,
    timeout_s: int = 5,
    cycle_id: str | None = "2026-07-31-morning",
) -> VerifiedSwitchAdapter:
    """Build an adapter with a fixed cycle id provider."""
    return VerifiedSwitchAdapter(
        hass,
        timeout_s=timeout_s,
        cycle_id_provider=lambda: cycle_id,
    )


async def test_confirmed_via_a_state_change(hass: HomeAssistant) -> None:
    """The command confirms when the watched entity reaches the target state."""
    calls = async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set(VALVE, STATE_OFF)
    adapter = make_adapter(hass)

    task = hass.async_create_task(adapter.async_turn_on(VALVE))
    for _ in range(10):
        await asyncio.sleep(0)
    # Commanded but unconfirmed: the adapter is watching for the state change.
    assert not task.done()
    assert len(calls) == 1
    assert calls[0].data[ATTR_ENTITY_ID] == VALVE

    hass.states.async_set(VALVE, STATE_ON)
    assert await task is True


async def test_already_in_target_state_confirms_without_a_state_change(
    hass: HomeAssistant,
) -> None:
    """Commanding an already-on valve confirms — a no-op fires no state_changed.

    This is not a corner case: a failed close leaves the next open in exactly
    this state, and without the post-call `is_state` check it would produce a
    guaranteed false timeout.
    """
    async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set(VALVE, STATE_ON)
    adapter = make_adapter(hass)

    assert await adapter.async_turn_on(VALVE) is True


async def test_unconfirmed_command_times_out_to_false(hass: HomeAssistant) -> None:
    """No state change within the timeout → False, and the listener is gone.

    Cleanup is doubly enforced: PHCC's `verify_cleanup` fails the test on a
    leaked listener, and the late state change below must land on nobody.
    """
    async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set(VALVE, STATE_OFF)
    adapter = make_adapter(hass, timeout_s=0)

    assert await adapter.async_turn_on(VALVE) is False

    # A state change arriving after the timeout resolves nothing and raises
    # nothing — the watch was unsubscribed in `finally`.
    hass.states.async_set(VALVE, STATE_ON)
    await hass.async_block_till_done()


async def test_the_timeout_is_the_configured_duration_not_a_flag(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A confirmation inside the window still confirms; past it, it does not.

    The other timeout tests use `timeout_s=0`, which is below the configurable
    minimum and only proves that the branch exists. This pins the value as a
    real duration — the clock moves from inside the service call, because
    `asyncio.timeout` runs on the loop clock and the freezer holds it still.
    """
    configured_s = 10

    def _handler_ticking(
        seconds: int,
    ) -> Callable[[ServiceCall], Coroutine[Any, Any, None]]:
        async def _handle(call: ServiceCall) -> None:
            freezer.tick(timedelta(seconds=seconds))
            if seconds < configured_s:
                hass.states.async_set(call.data[ATTR_ENTITY_ID], STATE_ON)

        return _handle

    hass.services.async_register("switch", "turn_on", _handler_ticking(5))
    hass.states.async_set(VALVE, STATE_OFF)
    assert await make_adapter(hass, timeout_s=configured_s).async_turn_on(VALVE) is True

    hass.services.async_remove("switch", "turn_on")
    hass.services.async_register("switch", "turn_on", _handler_ticking(11))
    hass.states.async_set(VALVE, STATE_OFF)
    adapter = make_adapter(hass, timeout_s=configured_s)

    task = hass.async_create_task(adapter.async_turn_on(VALVE))
    for _ in range(10):
        await asyncio.sleep(0)

    assert await task is False


async def test_service_error_returns_false(hass: HomeAssistant) -> None:
    """An adapter translates its own failures: HomeAssistantError → False."""

    async def broken_service(call: ServiceCall) -> None:
        msg = "switch integration is wedged"
        raise HomeAssistantError(msg)

    hass.services.async_register("switch", "turn_off", broken_service)
    hass.states.async_set(VALVE, STATE_ON)
    adapter = make_adapter(hass)

    assert await adapter.async_turn_off(VALVE) is False


async def test_unknown_entity_times_out_to_false(hass: HomeAssistant) -> None:
    """A missing entity needs no special branch: no-op call, watch, timeout.

    That IS the "never a silent skip" behavior — the engine turns the False
    into a `*_UNCONFIRMED` anomaly with the entity in its context.
    """
    async_mock_service(hass, "switch", "turn_on")
    adapter = make_adapter(hass, timeout_s=0)

    assert await adapter.async_turn_on("switch.does_not_exist") is False


async def test_commands_within_one_cycle_share_one_context(
    hass: HomeAssistant,
) -> None:
    """AD-7: ONE `Context()` per cycle id; a new cycle mints a new one."""
    on_calls = async_mock_service(hass, "switch", "turn_on")
    off_calls = async_mock_service(hass, "switch", "turn_off")
    hass.states.async_set(VALVE, STATE_ON)
    current: dict[str, str | None] = {"cycle": "2026-07-31-morning"}
    adapter = VerifiedSwitchAdapter(
        hass,
        timeout_s=5,
        cycle_id_provider=lambda: current["cycle"],
    )

    hass.states.async_set(VALVE, STATE_ON)
    assert await adapter.async_turn_on(VALVE) is True
    hass.states.async_set(VALVE, STATE_OFF)
    assert await adapter.async_turn_off(VALVE) is True
    assert on_calls[0].context.id == off_calls[0].context.id

    current["cycle"] = "2026-07-31-evening"
    hass.states.async_set(VALVE, STATE_ON)
    assert await adapter.async_turn_on(VALVE) is True
    assert on_calls[1].context.id != on_calls[0].context.id


async def test_commands_outside_a_cycle_get_fresh_contexts(
    hass: HomeAssistant,
) -> None:
    """No active cycle (provider returns None) → a fresh Context per command."""
    calls = async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set(VALVE, STATE_ON)
    adapter = make_adapter(hass, cycle_id=None)

    assert await adapter.async_turn_on(VALVE) is True
    assert await adapter.async_turn_on(VALVE) is True
    assert calls[0].context.id != calls[1].context.id
