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

import pytest
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context
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
PUMP = "switch.pool_pump"


def make_adapter(
    hass: HomeAssistant,
    *,
    timeout_s: int = 5,
    cycle_id: str | None = "2026-07-31-morning",
    governed: tuple[str, ...] | None = None,
    observed: list[tuple[str, bool, bool]] | None = None,
) -> VerifiedSwitchAdapter:
    """Build an adapter with a fixed cycle id provider.

    `governed` and `observed` wire the Story 3.4 watch: the set to subscribe
    to and the list every report is appended to. Left out, the adapter is the
    pure `SwitchPort` every command-level test below needs.
    """
    return VerifiedSwitchAdapter(
        hass,
        timeout_s=timeout_s,
        cycle_id_provider=lambda: cycle_id,
        governed_entities=None if governed is None else (lambda: governed),
        on_observed=(
            None
            if observed is None
            else (
                lambda entity_id, is_on, manual: observed.append(
                    (entity_id, is_on, manual),
                )
            )
        ),
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


async def test_a_renamed_entity_is_commanded_and_watched_under_its_new_id(
    hass: HomeAssistant,
) -> None:
    """The alias resolves BEFORE the service call and the state watch (Story 1.7).

    A running cycle's snapshot keeps naming the old id; the adapter is the
    one place that knows the registry renamed it. Both directions, both
    halves: the call carries the new id and the confirmation is read off it.
    """
    on_calls = async_mock_service(hass, "switch", "turn_on")
    off_calls = async_mock_service(hass, "switch", "turn_off")
    adapter = make_adapter(hass)
    adapter.async_rename(VALVE, "switch.front_valve")

    # Confirmed off the NEW id's state — the old id has no state at all.
    hass.states.async_set("switch.front_valve", STATE_ON)
    assert await adapter.async_turn_on(VALVE) is True
    assert on_calls[0].data[ATTR_ENTITY_ID] == "switch.front_valve"

    hass.states.async_set("switch.front_valve", STATE_OFF)
    assert await adapter.async_turn_off(VALVE) is True
    assert off_calls[0].data[ATTR_ENTITY_ID] == "switch.front_valve"

    # The watch is on the new id too: a late confirmation lands there.
    hass.states.async_set("switch.front_valve", STATE_OFF)
    task = hass.async_create_task(adapter.async_turn_on(VALVE))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()
    hass.states.async_set("switch.front_valve", STATE_ON)
    assert await task is True


async def test_unaliased_ids_pass_through_untouched(hass: HomeAssistant) -> None:
    """An alias for one entity changes nothing for the others."""
    calls = async_mock_service(hass, "switch", "turn_on")
    adapter = make_adapter(hass)
    adapter.async_rename("switch.other_valve", "switch.renamed_valve")
    hass.states.async_set(VALVE, STATE_ON)

    assert await adapter.async_turn_on(VALVE) is True

    assert calls[0].data[ATTR_ENTITY_ID] == VALVE


async def test_chained_renames_resolve_to_the_last_id(hass: HomeAssistant) -> None:
    """Chained a → b → c: snapshot id `a` reaches `c`, so does `b`, `c` is itself."""
    calls = async_mock_service(hass, "switch", "turn_on")
    adapter = make_adapter(hass)
    adapter.async_rename(VALVE, "switch.b")
    adapter.async_rename("switch.b", "switch.c")
    hass.states.async_set("switch.c", STATE_ON)

    assert await adapter.async_turn_on(VALVE) is True
    assert await adapter.async_turn_on("switch.b") is True
    assert await adapter.async_turn_on("switch.c") is True

    assert [call.data[ATTR_ENTITY_ID] for call in calls] == ["switch.c"] * 3


async def test_renaming_back_to_an_earlier_id_resolves_to_itself(
    hass: HomeAssistant,
) -> None:
    """Renamed a → b → a: the re-pointing loop yields `a → a`, `b → a`."""
    calls = async_mock_service(hass, "switch", "turn_on")
    adapter = make_adapter(hass)
    adapter.async_rename(VALVE, "switch.b")
    adapter.async_rename("switch.b", VALVE)
    hass.states.async_set(VALVE, STATE_ON)

    assert await adapter.async_turn_on(VALVE) is True
    assert await adapter.async_turn_on("switch.b") is True

    assert [call.data[ATTR_ENTITY_ID] for call in calls] == [VALVE, VALVE]


async def test_swapping_two_ids_keeps_each_snapshot_on_its_own_hardware(
    hass: HomeAssistant,
) -> None:
    """Swap a → tmp, b → a, tmp → b: snapshot `a` reaches `b`, `b` reaches `a`.

    Dropping the alias for an id just renamed onto would send snapshot `a` to
    the OTHER valve with no anomaly — which is why no alias is ever removed.
    """
    calls = async_mock_service(hass, "switch", "turn_on")
    adapter = make_adapter(hass)
    adapter.async_rename("switch.a", "switch.tmp")
    adapter.async_rename("switch.b", "switch.a")
    adapter.async_rename("switch.tmp", "switch.b")
    hass.states.async_set("switch.a", STATE_ON)
    hass.states.async_set("switch.b", STATE_ON)

    assert await adapter.async_turn_on("switch.a") is True
    assert await adapter.async_turn_on("switch.b") is True

    assert [call.data[ATTR_ENTITY_ID] for call in calls] == ["switch.b", "switch.a"]


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


# --------------------------------------------------------------------------
# The resync input (Story 3.2): `async_is_on`
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (STATE_ON, True),
        (STATE_OFF, False),
        (STATE_UNKNOWN, None),
        (STATE_UNAVAILABLE, None),
        ("opening", None),
    ],
)
async def test_is_on_reads_the_actual_state_and_doubts_anything_else(
    hass: HomeAssistant,
    state: str,
    expected: bool | None,  # noqa: FBT001 — a parametrized expectation
) -> None:
    """`on` → True, `off` → False, everything else → None (fail-wet: "not OFF")."""
    hass.states.async_set(VALVE, state)

    assert await make_adapter(hass).async_is_on(VALVE) is expected


async def test_is_on_of_an_entity_with_no_state_is_none(hass: HomeAssistant) -> None:
    """A switch whose integration has not loaded yet has no state at all: doubtful."""
    assert await make_adapter(hass).async_is_on("switch.not_loaded_yet") is None


async def test_is_on_follows_a_rename(hass: HomeAssistant) -> None:
    """The read goes through the alias map the commands use (Story 1.7 + 3.2)."""
    adapter = make_adapter(hass)
    adapter.async_rename(VALVE, "switch.front_valve")
    hass.states.async_set("switch.front_valve", STATE_ON)

    assert await adapter.async_is_on(VALVE) is True
    assert await adapter.async_is_on("switch.front_valve") is True


# --------------------------------------------------------------------------
# Manual-override detection (Story 3.4): the ONE standing watch
# --------------------------------------------------------------------------


def watching(
    hass: HomeAssistant,
    *,
    governed: tuple[str, ...] = (VALVE, PUMP),
) -> tuple[VerifiedSwitchAdapter, list[tuple[str, bool, bool]]]:
    """Build an adapter with the standing watch armed; return it and its reports."""
    reports: list[tuple[str, bool, bool]] = []
    adapter = make_adapter(hass, governed=governed, observed=reports)
    adapter.async_start_watch()
    return adapter, reports


async def test_a_foreign_context_turn_on_is_reported_manual(
    hass: HomeAssistant,
) -> None:
    """AC 1: nobody of ours issued that context and nothing is in flight."""
    hass.states.async_set(VALVE, STATE_OFF)
    adapter, reports = watching(hass)

    hass.states.async_set(VALVE, STATE_ON)
    await hass.async_block_till_done()

    assert reports == [(VALVE, True, True)]

    adapter.async_stop_watch()


async def test_an_off_is_reported_too_and_manual_is_its_own_answer(
    hass: HomeAssistant,
) -> None:
    """The adapter reports every governed transition; the engine decides."""
    hass.states.async_set(VALVE, STATE_ON)
    adapter, reports = watching(hass)

    hass.states.async_set(VALVE, STATE_OFF)
    await hass.async_block_till_done()

    assert reports == [(VALVE, False, True)]

    adapter.async_stop_watch()


async def test_our_own_command_is_not_manual(hass: HomeAssistant) -> None:
    """AC 6: a governed change carrying a context THIS adapter minted is ours.

    The fake hardware propagates `call.context` onto the resulting state, the
    way a well-behaved switch integration does.
    """
    hass.states.async_set(VALVE, STATE_OFF)
    adapter, reports = watching(hass)

    async def _flip(call: ServiceCall) -> None:
        hass.states.async_set(
            call.data[ATTR_ENTITY_ID],
            STATE_ON,
            context=call.context,
        )

    hass.services.async_register("switch", "turn_on", _flip)

    assert await adapter.async_turn_on(VALVE) is True
    await hass.async_block_till_done()

    assert reports == [(VALVE, True, False)]

    adapter.async_stop_watch()


async def test_a_child_context_is_ours_too(hass: HomeAssistant) -> None:
    """Some integrations make the service context the PARENT of the state change."""
    hass.states.async_set(VALVE, STATE_OFF)
    adapter, reports = watching(hass)

    async def _flip(call: ServiceCall) -> None:
        hass.states.async_set(
            call.data[ATTR_ENTITY_ID],
            STATE_ON,
            context=Context(parent_id=call.context.id),
        )

    hass.services.async_register("switch", "turn_on", _flip)

    assert await adapter.async_turn_on(VALVE) is True
    await hass.async_block_till_done()

    assert reports == [(VALVE, True, False)]

    adapter.async_stop_watch()


async def test_a_child_context_is_ours_after_the_command_has_returned(
    hass: HomeAssistant,
) -> None:
    """The parent-id clause alone, with the in-flight guard out of the way.

    In the test above the state change is emitted from INSIDE the service
    call, so the entity is still in `_in_flight` and the report would be
    non-manual whatever the context said. Here the command has already
    returned — a switch that reports its real state a moment later, or one
    whose integration echoes an earlier call — so the parent link is the only
    thing that can attribute this change to us.
    """
    calls = async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set(VALVE, STATE_ON)
    adapter, reports = watching(hass)

    # Confirms on the no-op branch: the command is over, nothing in flight.
    assert await adapter.async_turn_on(VALVE) is True
    reports.clear()

    child = Context(parent_id=calls[0].context.id)
    hass.states.async_set(VALVE, STATE_OFF, context=child)
    await hass.async_block_till_done()

    assert reports == [(VALVE, False, False)]

    adapter.async_stop_watch()


async def test_a_switch_that_drops_our_context_is_still_not_manual(
    hass: HomeAssistant,
) -> None:
    """The in-flight guard is the fail-safe half: no context, still ours.

    A switch integration that does not propagate the service context would
    otherwise make every one of our own commands read as manual, and the
    engine would pause itself the instant it opened a valve.
    """
    hass.states.async_set(VALVE, STATE_OFF)
    adapter, reports = watching(hass)

    async def _flip(call: ServiceCall) -> None:
        # A FRESH context: nothing links this state change to our call.
        hass.states.async_set(call.data[ATTR_ENTITY_ID], STATE_ON)

    hass.services.async_register("switch", "turn_on", _flip)

    assert await adapter.async_turn_on(VALVE) is True
    await hass.async_block_till_done()

    assert reports == [(VALVE, True, False)]

    adapter.async_stop_watch()


async def test_the_in_flight_entry_is_cleared_after_the_command(
    hass: HomeAssistant,
) -> None:
    """A leaked in-flight entry would hide that entity's every later manual act."""
    async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set(VALVE, STATE_ON)
    adapter, reports = watching(hass)

    assert await adapter.async_turn_on(VALVE) is True
    hass.states.async_set(VALVE, STATE_OFF)
    await hass.async_block_till_done()

    assert reports == [(VALVE, False, True)]

    adapter.async_stop_watch()


async def test_a_context_from_an_earlier_cycle_is_still_ours(
    hass: HomeAssistant,
) -> None:
    """`_issued` is a BOUNDED SET, not just the current cycle's one context.

    The adapter mints one context per cycle id, and the watch sees the close
    of a zone whose cycle has already been replaced by the next one's.
    """
    hass.states.async_set(VALVE, STATE_OFF)
    reports: list[tuple[str, bool, bool]] = []
    current: dict[str, str | None] = {"cycle": "2026-07-31-morning"}
    adapter = VerifiedSwitchAdapter(
        hass,
        timeout_s=5,
        cycle_id_provider=lambda: current["cycle"],
        governed_entities=lambda: (VALVE,),
        on_observed=lambda entity_id, is_on, manual: reports.append(
            (entity_id, is_on, manual),
        ),
    )
    adapter.async_start_watch()
    calls = async_mock_service(hass, "switch", "turn_on")

    # Both commands confirm on the no-op branch (the valve is already on), so
    # each still mints and records its cycle's context.
    hass.states.async_set(VALVE, STATE_ON)
    assert await adapter.async_turn_on(VALVE) is True
    first_context = calls[0].context
    current["cycle"] = "2026-07-31-evening"
    assert await adapter.async_turn_on(VALVE) is True
    assert calls[1].context.id != first_context.id

    reports.clear()
    # The morning cycle's context, long after its cycle id was replaced.
    hass.states.async_set(VALVE, STATE_OFF, context=first_context)
    await hass.async_block_till_done()

    assert reports == [(VALVE, False, False)]

    adapter.async_stop_watch()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (STATE_UNAVAILABLE, STATE_ON),
        (STATE_UNKNOWN, STATE_OFF),
        (STATE_UNAVAILABLE, STATE_UNKNOWN),
    ],
)
async def test_transitions_out_of_a_doubtful_state_are_ignored(
    hass: HomeAssistant,
    first: str,
    second: str,
) -> None:
    """A booting or reconnecting switch is not an operator — never a pause."""
    hass.states.async_set(VALVE, first)
    adapter, reports = watching(hass)

    hass.states.async_set(VALVE, second)
    await hass.async_block_till_done()

    assert reports == []

    adapter.async_stop_watch()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (STATE_ON, STATE_UNAVAILABLE),
        (STATE_ON, STATE_UNKNOWN),
        (STATE_OFF, STATE_UNAVAILABLE),
    ],
)
async def test_a_governed_switch_going_doubtful_is_reported_as_a_release(
    hass: HomeAssistant,
    first: str,
    second: str,
) -> None:
    """The rules are ASYMMETRIC: they may never pause, they must always release.

    An entity the engine is holding in its paused set that drops off the bus
    would otherwise stay held for ever — the scheduler paused with no
    watering and no anomaly. `is_on=False` can only release, never add, and
    `manual=False` keeps it out of "the operator did this".
    """
    hass.states.async_set(VALVE, first)
    adapter, reports = watching(hass)

    hass.states.async_set(VALVE, second)
    await hass.async_block_till_done()

    assert reports == [(VALVE, False, False)]

    adapter.async_stop_watch()


async def test_an_entity_appearing_is_ignored(hass: HomeAssistant) -> None:
    """`old_state is None`: a switch showing up at boot is not a manual act."""
    adapter, reports = watching(hass)

    hass.states.async_set(VALVE, STATE_ON)
    await hass.async_block_till_done()

    assert reports == []

    adapter.async_stop_watch()


async def test_an_entity_removed_is_reported_as_a_release(
    hass: HomeAssistant,
) -> None:
    """`new_state is None`: gone is not open, so a held entity is released."""
    hass.states.async_set(VALVE, STATE_ON)
    adapter, reports = watching(hass)

    hass.states.async_remove(VALVE)
    await hass.async_block_till_done()

    assert reports == [(VALVE, False, False)]

    adapter.async_stop_watch()


async def test_an_entity_removed_while_already_doubtful_is_ignored(
    hass: HomeAssistant,
) -> None:
    """Nothing was holding it open: the release was reported when it went doubtful."""
    hass.states.async_set(VALVE, STATE_UNAVAILABLE)
    adapter, reports = watching(hass)

    hass.states.async_remove(VALVE)
    await hass.async_block_till_done()

    assert reports == []

    adapter.async_stop_watch()


async def test_an_attribute_only_write_is_ignored(hass: HomeAssistant) -> None:
    """The state did not change: `state_changed` fires all the same."""
    hass.states.async_set(VALVE, STATE_ON)
    adapter, reports = watching(hass)

    hass.states.async_set(VALVE, STATE_ON, {"friendly_name": "Zone 1"})
    await hass.async_block_till_done()

    assert reports == []

    adapter.async_stop_watch()


async def test_an_ungoverned_entity_is_never_watched(hass: HomeAssistant) -> None:
    """The watch covers the governed set and nothing else."""
    hass.states.async_set("switch.kitchen_light", STATE_OFF)
    adapter, reports = watching(hass)

    hass.states.async_set("switch.kitchen_light", STATE_ON)
    await hass.async_block_till_done()

    assert reports == []

    adapter.async_stop_watch()


async def test_start_watch_replaces_the_previous_subscription(
    hass: HomeAssistant,
) -> None:
    """Re-arming after a plan swap leaves exactly ONE live registration."""
    hass.states.async_set(VALVE, STATE_OFF)
    hass.states.async_set(PUMP, STATE_OFF)
    adapter, reports = watching(hass)

    adapter.async_start_watch()
    hass.states.async_set(VALVE, STATE_ON)
    await hass.async_block_till_done()

    assert reports == [(VALVE, True, True)]

    adapter.async_stop_watch()


async def test_stop_watch_is_idempotent_and_really_stops(
    hass: HomeAssistant,
) -> None:
    """The unload path: no report after it, and a second call is a no-op."""
    hass.states.async_set(VALVE, STATE_OFF)
    adapter, reports = watching(hass)

    adapter.async_stop_watch()
    adapter.async_stop_watch()
    hass.states.async_set(VALVE, STATE_ON)
    await hass.async_block_till_done()

    assert reports == []


async def test_an_adapter_with_no_providers_arms_nothing(
    hass: HomeAssistant,
) -> None:
    """A plain `SwitchPort` is complete without the watch — and registers none."""
    hass.states.async_set(VALVE, STATE_OFF)
    baseline = hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)
    adapter = make_adapter(hass)

    adapter.async_start_watch()

    assert hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0) == baseline

    adapter.async_stop_watch()


async def test_an_armed_watch_really_registers_a_state_listener(
    hass: HomeAssistant,
) -> None:
    """The counterpart: with providers, one listener appears and unloads cleanly."""
    baseline = hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)
    adapter, _ = watching(hass)

    assert hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0) > baseline

    adapter.async_stop_watch()

    assert hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0) == baseline


async def test_an_empty_governed_set_subscribes_to_nothing(
    hass: HomeAssistant,
) -> None:
    """A controller with nothing configured yet has nothing to watch."""
    adapter, reports = watching(hass, governed=())

    hass.states.async_set(VALVE, STATE_ON)
    await hass.async_block_till_done()

    assert reports == []

    adapter.async_stop_watch()
