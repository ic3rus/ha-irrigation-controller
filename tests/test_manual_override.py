"""Manual override end to end, through the real setup path (Story 3.4).

The HA-glue half: a real `state_changed` on a governed switch, carrying a
context the integration never issued, reaches the standing watch in the switch
adapter, crosses the runner and pauses the engine — and the operator sees it
on one coarse attribute and nowhere else. No Repairs issue, no push, no bus
event: a pause is normal operation, and the nominal-week zero-notification
counter-metric holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import EVENT_STATE_CHANGED, STATE_OFF, STATE_ON
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import (
    DOMAIN,
    EVENT_HA_IRRIGATION_CONTROLLER,
)
from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.runs import CycleStatus
from tests.common import (
    CONTROLLER_OPTIONS,
    PUMP,
    VALVE_1,
    VALVE_2,
    add_zone,
    fire_at,
    register_notify_domain,
    register_switch_domain,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant, ServiceCall, State


def one_zone_entry(*, two: bool = False) -> MockConfigEntry:
    """Build the controller the suite drives: morning 07:00, ten minutes a zone."""
    zones = [zone_subentry_data("Zone A", VALVE_1)]
    if two:
        zones.append(zone_subentry_data("Zone B", VALVE_2))
    return MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=zones,
    )


def commands(calls: list[ServiceCall]) -> list[tuple[str, str]]:
    """Return the (service, entity_id) sequence the fake hardware received."""
    return [(call.service, call.data["entity_id"]) for call in calls]


def cycle_status(hass: HomeAssistant, entry: MockConfigEntry) -> State:
    """Return the Cycle status sensor's state object."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_cycle_status",
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


def our_issues(hass: HomeAssistant) -> list[str]:
    """Return the ids of every Repairs issue this integration has open."""
    return [
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    ]


def record_events(hass: HomeAssistant) -> list[Event]:
    """Collect every event of the ONE bus type, in order."""
    events: list[Event] = []

    @callback
    def _record(event: Event) -> None:
        events.append(event)

    hass.bus.async_listen(EVENT_HA_IRRIGATION_CONTROLLER, _record)
    return events


async def flip(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    moment: str,
    entity_id: str,
    state: str,
) -> None:
    """Flip a governed switch BY HAND at `moment` — a context we never issued.

    `hass.states.async_set` with no context mints a fresh one, which is
    exactly what a physical switch or a UI toggle produces: nothing links it
    to a service call of ours.
    """
    freezer.move_to(moment)
    hass.states.async_set(entity_id, state)
    await hass.async_block_till_done(wait_background_tasks=True)


async def setup_entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    *,
    now: str = "2026-07-31 06:50:00+02:00",
    two: bool = False,
) -> tuple[MockConfigEntry, list[ServiceCall]]:
    """Set the controller up while HA is RUNNING, then register the hardware.

    ORDER CONTRACT: the hardware is registered AFTER the entry, so the fake
    stays the last-wins handler. Its initial OFF states are the entities
    APPEARING (`old_state is None`) and are inert for the watch.
    """
    freezer.move_to(now)
    entry = one_zone_entry(two=two)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = register_switch_domain(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry, calls


async def unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Unload the entry and settle the loop."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_hand_opened_valve_defers_the_start_and_its_close_waters_it(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 1-3 end to end, with the counter-metric asserted throughout.

    06:55 the operator opens zone A's valve on site; 07:00 the daily start
    fires into a queue instead of a run; 07:10 the morning window closes with
    the watchdog silent; 07:15 the valve goes off and the cycle waters at
    once, on freshly quoted durations.
    """
    pushes = register_notify_domain(hass)
    events = record_events(hass)
    entry, calls = await setup_entry(hass, freezer)
    sequencer = entry.runtime_data.sequencer

    await flip(hass, freezer, "2026-07-31 06:55:00+02:00", VALVE_1, STATE_ON)

    assert sequencer.manual_override is True
    assert cycle_status(hass, entry).attributes["manual_override"] is True
    assert calls == []

    # The daily start fires into the pause: queued, not dispatched.
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)
    assert calls == []

    # The window closes: the watchdog must not make up what is merely late.
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    assert our_issues(hass) == []
    assert pushes == []
    assert events == []
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)

    # The operator closes it again: the queue drains and waters in this call.
    await flip(hass, freezer, "2026-07-31 07:15:00+02:00", VALVE_1, STATE_OFF)

    assert sequencer.manual_override is False
    assert cycle_status(hass, entry).attributes["manual_override"] is False
    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.cycle_id == "2026-07-31-morning"
    assert run.late_rerun is False
    assert sequencer.deferred_kinds == ()

    # The re-armed boundary finishes it ten minutes later.
    await fire_at(hass, freezer, "2026-07-31 07:25:00+02:00")

    assert commands(calls)[2:] == [("turn_off", VALVE_1), ("turn_off", PUMP)]
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    # Nothing about a pause is a failure: not one issue, push or event.
    assert our_issues(hass) == []
    assert pushes == []
    assert events == []

    await unload(hass, entry)


async def test_our_own_cycle_never_pauses_itself(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 6: every valve and pump transition of a normal cycle is attributed to us."""
    entry, calls = await setup_entry(hass, freezer, two=True)
    sequencer = entry.runtime_data.sequencer

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert sequencer.manual_override is False

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert sequencer.manual_override is False

    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    assert sequencer.manual_override is False
    assert commands(calls) == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_on", VALVE_2),
        ("turn_off", VALVE_2),
        ("turn_off", PUMP),
    ]
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED

    await unload(hass, entry)


async def test_a_flip_during_a_running_cycle_never_fights_the_operator(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 4: the cycle runs to completion and starts nothing more while paused."""
    entry, calls = await setup_entry(hass, freezer, two=True)
    sequencer = entry.runtime_data.sequencer

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    live = sequencer.current_run
    assert live is not None

    # Zone B's valve opened by hand while zone A waters.
    await flip(hass, freezer, "2026-07-31 07:02:00+02:00", VALVE_2, STATE_ON)

    assert sequencer.manual_override is True
    assert sequencer.current_run is live
    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]

    # The cycle runs on unchanged through both zone boundaries. Zone B's own
    # slot opens its valve as usual (a no-op on an already-open valve, which
    # the adapter confirms without a state change) and closes it at 07:20 —
    # and THAT close, ours though it is, is what ends the pause: any observed
    # off releases the entity, whoever commanded it.
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert sequencer.manual_override is True

    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert sequencer.current_run is None
    assert sequencer.manual_override is False
    assert our_issues(hass) == []

    await unload(hass, entry)


async def test_a_switch_going_unavailable_and_back_never_pauses(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 6 / matrix row 12: a booting or flapping switch is not an operator."""
    entry, _ = await setup_entry(hass, freezer)
    sequencer = entry.runtime_data.sequencer

    await flip(hass, freezer, "2026-07-31 06:51:00+02:00", VALVE_1, "unavailable")
    await flip(hass, freezer, "2026-07-31 06:52:00+02:00", VALVE_1, STATE_ON)

    assert sequencer.manual_override is False

    await unload(hass, entry)


async def test_the_watch_is_gone_after_an_unload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The standing watch rides `entry.async_on_unload`: the listener itself goes.

    Asserted on the listener count rather than on the engine, because the
    runner's own shutdown guard would make an "and nothing was observed"
    assertion pass even with the subscription still live.
    """
    baseline = hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)
    entry, _ = await setup_entry(hass, freezer)
    assert hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0) > baseline

    await unload(hass, entry)

    assert hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0) == baseline


async def test_a_held_switch_going_unavailable_releases_the_pause(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """A pause must never outlive the operator because a switch dropped off.

    The valve is hand-opened at 06:55 and its integration drops it to
    `unavailable` a minute later. Were that ignored, the scheduler would stay
    paused for ever — no watering, no anomaly — even after the valve came
    back off. The release is reported instead, and the day's cycles water.
    """
    pushes = register_notify_domain(hass)
    entry, calls = await setup_entry(hass, freezer)
    sequencer = entry.runtime_data.sequencer

    await flip(hass, freezer, "2026-07-31 06:55:00+02:00", VALVE_1, STATE_ON)
    assert sequencer.manual_override is True

    await flip(hass, freezer, "2026-07-31 06:56:00+02:00", VALVE_1, "unavailable")

    assert sequencer.manual_override is False

    # It comes back off — which pauses nothing either (leaving doubt never can).
    await flip(hass, freezer, "2026-07-31 06:57:00+02:00", VALVE_1, STATE_OFF)
    assert sequencer.manual_override is False

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]
    assert our_issues(hass) == []
    assert pushes == []

    await unload(hass, entry)


async def test_a_held_switch_removed_releases_the_pause(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The same for an entity that disappears: gone is not held open."""
    entry, _ = await setup_entry(hass, freezer)
    sequencer = entry.runtime_data.sequencer
    await flip(hass, freezer, "2026-07-31 06:55:00+02:00", VALVE_1, STATE_ON)
    assert sequencer.manual_override is True

    hass.states.async_remove(VALVE_1)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert sequencer.manual_override is False

    await unload(hass, entry)


async def test_run_now_is_honoured_while_paused(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Decision 2: the operator asking is not the scheduler guessing."""
    entry, calls = await setup_entry(hass, freezer)
    sequencer = entry.runtime_data.sequencer
    # The PUMP by hand: governed, and not re-commanded while the cycle runs
    # (the open is a no-op the adapter confirms without a state change).
    await flip(hass, freezer, "2026-07-31 06:55:00+02:00", PUMP, STATE_ON)
    assert sequencer.manual_override is True

    await hass.services.async_call(
        DOMAIN,
        "run_now",
        {"cycle": "morning"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]
    assert sequencer.manual_override is True

    await unload(hass, entry)


async def test_a_pending_run_is_handed_back_when_a_pause_opens(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Matrix row 4: no PENDING scheduled run survives the start of a pause.

    The morning start is requested a minute early through the service-free
    path a daily tracker uses, so the run exists but has commanded nothing.
    """
    entry, calls = await setup_entry(hass, freezer, now="2026-07-31 06:58:00+02:00")
    sequencer = entry.runtime_data.sequencer
    await sequencer.request_cycle(CycleKind.MORNING, dt_util.now())
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.PENDING

    await flip(hass, freezer, "2026-07-31 06:59:00+02:00", VALVE_1, STATE_ON)

    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)
    assert calls == []
    assert cycle_status(hass, entry).state == "idle"

    await unload(hass, entry)


async def test_a_zone_added_mid_cycle_is_watched_before_the_reload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The governed set moves with the plan: the busy path re-arms the watch.

    A zone added while a cycle waters swaps `sequencer.plan` in place and
    defers the reload until the cycle ends (Story 1.7). The new valve is part
    of the governed set from that moment, so the watch has to be re-armed
    over it — exactly like the registry tracker's re-subscription.
    """
    entry, _ = await setup_entry(hass, freezer)
    sequencer = entry.runtime_data.sequencer
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert sequencer.current_run is not None

    await add_zone(hass, entry, name="Zone B", valve=VALVE_2)
    assert entry.runtime_data.runner.reload_pending is True

    await flip(hass, freezer, "2026-07-31 07:02:00+02:00", VALVE_2, STATE_ON)

    assert sequencer.manual_override is True

    await unload(hass, entry)
