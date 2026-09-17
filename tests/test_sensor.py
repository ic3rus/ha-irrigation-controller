"""Passive entity projections of engine state (AC 5, AD-6).

The sensors READ the engine and never mutate it: no polling, no
`RestoreEntity`, no second authority. Updates arrive by dispatcher after each
engine step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.sensor import (
    ATTR_OPTIONS,
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    UnitOfTime,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.ha_irrigation_controller
from custom_components.ha_irrigation_controller.adapters.journal import STORAGE_KEY
from custom_components.ha_irrigation_controller.const import (
    ATTR_CYCLE,
    CONF_MORNING_ENABLED,
    CONF_RAIN_EXPOSED,
    CONF_VALVE_SWITCH,
    DOMAIN,
    SERVICE_CANCEL_CYCLE,
    SERVICE_RUN_NOW,
    SUBENTRY_TYPE_ZONE,
)
from custom_components.ha_irrigation_controller.engine.sequencer import (
    JOURNAL_SCHEMA_VERSION,
)
from tests.common import CONTROLLER_OPTIONS, fire_at, zone_subentry_data

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant, ServiceCall, State
    from homeassistant.helpers.device_registry import DeviceEntry
    from homeassistant.helpers.entity_registry import RegistryEntry

PUMP = "switch.pool_pump"
VALVE_A = "switch.zone_a_valve"
VALVE_B = "switch.zone_b_valve"


def register_switches(
    hass: HomeAssistant,
    *,
    failing: str | None = None,
) -> list[ServiceCall]:
    """Register switch services that flip state; `failing` refuses to turn on.

    A refusing valve is how a real integration reports a wedged switch: the
    adapter translates the error into not-confirmed, the engine files the zone
    FAILED, and the zone's effective seconds are zero.
    """
    calls: list[ServiceCall] = []

    async def _handle(call: ServiceCall) -> None:
        calls.append(call)
        entity_id = call.data[ATTR_ENTITY_ID]
        if entity_id == failing and call.service == "turn_on":
            msg = f"{entity_id} is not responding"
            raise HomeAssistantError(msg)
        state = STATE_ON if call.service == "turn_on" else STATE_OFF
        hass.states.async_set(entity_id, state, context=call.context)

    hass.services.async_register("switch", "turn_on", _handle)
    hass.services.async_register("switch", "turn_off", _handle)
    for entity_id in (PUMP, VALVE_A, VALVE_B):
        hass.states.async_set(entity_id, STATE_OFF)
    return calls


def controller_with_zones(*zones: Any) -> MockConfigEntry:
    """Build an entry whose morning cycle runs the given zones."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options={**CONTROLLER_OPTIONS, CONF_MORNING_ENABLED: True},
        subentries_data=list(zones),
    )


def zone_of(entry: MockConfigEntry, title: str) -> ConfigSubentry:
    """Return the live subentry carrying `title`."""
    subentries: dict[str, ConfigSubentry] = entry.subentries
    return next(subentry for subentry in subentries.values() if subentry.title == title)


def entity_id_for(hass: HomeAssistant, unique_id: str) -> str:
    """Resolve a sensor's entity_id from its unique_id (never from its name)."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    """Return an existing entity's state (mypy strict: never the None branch)."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state


def registry_entry(hass: HomeAssistant, entity_id: str) -> RegistryEntry:
    """Return an existing entity's registry entry."""
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    return entry


def device_of(hass: HomeAssistant, entity: RegistryEntry) -> DeviceEntry:
    """Return the device an entity is attached to."""
    assert entity.device_id is not None
    device = dr.async_get(hass).async_get(entity.device_id)
    # HA 2026.9 returns `DeviceEntry | ChildDeviceEntry`; ours are top-level.
    assert isinstance(device, dr.DeviceEntry)
    return device


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> MockConfigEntry:
    """Set up a one-zone controller just before its morning start."""
    freezer.move_to("2026-07-31 06:59:00+02:00")
    config_entry = controller_with_zones(zone_subentry_data("Zone A", VALVE_A))
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


async def test_cycle_status_goes_idle_running_idle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """The controller sensor projects the engine's cycle status (AC 5)."""
    register_switches(hass)
    status = entity_id_for(hass, f"{entry.entry_id}_cycle_status")
    assert state_of(hass, status).state == "idle"

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert state_of(hass, status).state == "running"

    # One zone at ten morning minutes: the cycle ends at 07:10.
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert state_of(hass, status).state == "idle"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_cycle_status_is_an_enum_with_a_coarse_summary(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """ENUM device class, every reported state in `options`, coarse attributes.

    AD-10 forbids the full timeline in attributes; an ENUM sensor must carry
    no state_class and no unit, and HA raises on a state outside `options`.
    """
    register_switches(hass)
    status = entity_id_for(hass, f"{entry.entry_id}_cycle_status")

    state = state_of(hass, status)
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENUM
    # `cancelled` (Story 1.6), `waived` (Story 2.3) and `interrupted` (Story
    # 3.2) ride in through the CycleStatus comprehension. No push point can
    # surface any of them today — the
    # dispatcher fires after `advance` returns, by which point `current_run`
    # is released, and a waived cycle is never `current_run` at all — so what
    # is asserted is that the sensor DECLARES them (HA raises on an unlisted
    # state) and that they are translated;
    # `test_cancelled_is_a_declared_and_translated_state` covers the
    # translation half.
    assert state.attributes[ATTR_OPTIONS] == [
        "idle",
        "pending",
        "running",
        "completed",
        "cancelled",
        "waived",
        "interrupted",
    ]
    assert ATTR_STATE_CLASS not in state.attributes
    assert ATTR_UNIT_OF_MEASUREMENT not in state.attributes
    assert state.attributes["cycle_id"] is None
    assert state.attributes["current_zone"] is None
    assert state.attributes["config_change_pending"] is False
    assert state.attributes["day_credit"] is None

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    state = state_of(hass, status)
    assert state.state in state.attributes[ATTR_OPTIONS]
    assert state.attributes["cycle_id"] == "2026-07-31-morning"
    assert state.attributes["current_zone"] == "Zone A"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_config_change_pending_follows_the_deferred_reload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """Decision 2: the pending reload is observable — false, true, false.

    False while idle, true the moment an edit lands during a cycle (pushed at
    once, not at the next engine step), and false again once the cycle
    completes and the reload fires — the rebuilt entity starts clean.
    """
    register_switches(hass)
    status = entity_id_for(hass, f"{entry.entry_id}_cycle_status")
    assert state_of(hass, status).attributes["config_change_pending"] is False

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert state_of(hass, status).attributes["config_change_pending"] is False

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_MORNING_ENABLED: False},
    )
    await hass.async_block_till_done()
    assert state_of(hass, status).state == "running"
    assert state_of(hass, status).attributes["config_change_pending"] is True

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    assert state_of(hass, status).state == "idle"
    assert state_of(hass, status).attributes["config_change_pending"] is False
    assert entry.runtime_data.plan.morning_enabled is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_zone_sensor_reports_its_effective_seconds_after_the_cycle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """The per-zone duration comes from the ONE `effective_seconds` helper."""
    register_switches(hass)
    zone = zone_of(entry, "Zone A")
    duration = entity_id_for(
        hass,
        f"{entry.entry_id}_{zone.subentry_id}_last_watering_duration",
    )
    # Never watered yet: unknown, not zero.
    assert state_of(hass, duration).state == "unknown"

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    state = state_of(hass, duration)
    assert state.state == "600"
    assert state.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.DURATION
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTime.SECONDS
    assert state.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_last_watering_sensor_keeps_its_value_across_a_reload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """Story 3.2: `last_run` is restored from the journal — no `unknown` flap on reload.

    The reload-per-config-change regime rebuilds the entity constantly; a
    MEASUREMENT sensor resetting to `unknown` each time would pollute its
    long-term statistics. The figure comes from the restored run, not from
    `RestoreEntity` (AD-2: the journal is the one authority).
    """
    register_switches(hass)
    zone = zone_of(entry, "Zone A")
    duration = entity_id_for(
        hass,
        f"{entry.entry_id}_{zone.subentry_id}_last_watering_duration",
    )
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert state_of(hass, duration).state == "600"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    state = state_of(hass, duration)
    assert state.state == "600"
    assert state.attributes["carried_deficit"] == 0
    assert state.attributes["pending_deficit"] == 0
    assert state.attributes["rain_credit"] == 0
    assert hass.states.is_state(PUMP, STATE_OFF)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_failed_open_zone_reports_zero_seconds(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """A zone whose valve never confirmed watered nothing — the slot still ran."""
    register_switches(hass, failing=VALVE_A)
    zone = zone_of(entry, "Zone A")
    duration = entity_id_for(
        hass,
        f"{entry.entry_id}_{zone.subentry_id}_last_watering_duration",
    )

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    assert state_of(hass, duration).state == "0"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_cancelled_is_a_declared_and_translated_state(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """An ENUM state without a translation renders as the raw string.

    `CycleStatus.CANCELLED` lands in `_attr_options` for free through the
    existing comprehension, so the drift risk is the translation file, not the
    sensor — and only this assertion would catch it.
    """
    status = entity_id_for(hass, f"{entry.entry_id}_cycle_status")
    assert "cancelled" in state_of(hass, status).attributes[ATTR_OPTIONS]

    translations = json.loads(
        (
            Path(custom_components.ha_irrigation_controller.__file__).parent
            / "translations"
            / "en.json"
        ).read_text(encoding="utf-8"),
    )
    states = translations["entity"]["sensor"]["cycle_status"]["state"]
    assert set(states) == set(state_of(hass, status).attributes[ATTR_OPTIONS])

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_day_credit_attribute_shows_the_credit_until_it_is_consumed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.3: the credit is observable on the cycle-status sensor, coarsely.

    None before anything ran; the ISO day once the 05:00 run-now completes;
    None again once the 07:00 morning has been waived by it — and the sensor
    is `idle` throughout that decision, because a waived cycle is never
    `current_run`. Same coarse-attribute precedent as Story 2.2.
    """
    freezer.move_to("2026-07-31 05:00:00+02:00")
    entry = controller_with_zones(zone_subentry_data("Zone A", VALVE_A))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switches(hass)
    status = entity_id_for(hass, f"{entry.entry_id}_cycle_status")
    assert state_of(hass, status).attributes["day_credit"] is None

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RUN_NOW,
        {ATTR_CYCLE: "morning"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert state_of(hass, status).state == "running"
    assert state_of(hass, status).attributes["day_credit"] is None  # not settled yet

    await fire_at(hass, freezer, "2026-07-31 05:10:00+02:00")

    state = state_of(hass, status)
    assert state.state == "idle"
    assert state.attributes["day_credit"] == "2026-07-31"

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    state = state_of(hass, status)
    assert state.state == "idle"
    assert state.attributes["cycle_id"] is None
    assert state.attributes["day_credit"] is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_entities_follow_the_naming_and_device_conventions(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Stable unique ids, has_entity_name, and each zone entity on its device."""
    freezer.move_to("2026-07-31 06:59:00+02:00")
    config_entry = controller_with_zones(
        zone_subentry_data("Zone A", VALVE_A),
        zone_subentry_data("Zone B", VALVE_B),
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    status = registry_entry(
        hass,
        entity_id_for(hass, f"{config_entry.entry_id}_cycle_status"),
    )
    assert status.has_entity_name is True
    assert status.translation_key == "cycle_status"
    assert device_of(hass, status).identifiers == {(DOMAIN, config_entry.entry_id)}

    for title, valve in (("Zone A", VALVE_A), ("Zone B", VALVE_B)):
        zone = zone_of(config_entry, title)
        entity = registry_entry(
            hass,
            entity_id_for(
                hass,
                f"{config_entry.entry_id}_{zone.subentry_id}_last_watering_duration",
            ),
        )
        assert entity.has_entity_name is True
        assert entity.config_subentry_id == zone.subentry_id
        device = device_of(hass, entity)
        assert device.identifiers == {(DOMAIN, zone.subentry_id)}
        assert device.name == title
        # The entity really sits on the device of the zone driving THIS valve,
        # not merely on some zone device — the pairing is what a subentry mix-up
        # would break, and titles alone would not catch it.
        assert zone.data[CONF_VALVE_SWITCH] == valve

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_removing_a_zone_removes_its_entity(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """A zone entity lives and dies with its subentry (added under it)."""
    zone = zone_of(entry, "Zone A")
    unique_id = f"{entry.entry_id}_{zone.subentry_id}_last_watering_duration"
    assert entity_id_for(hass, unique_id)

    hass.config_entries.async_remove_subentry(entry, zone.subentry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id) is None
    assert not entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_cancelled_cycle_reports_zero_for_the_zones_it_never_reached(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """A CANCELLED run is an ANSWER for un-reached zones, not missing data.

    `_close_live_zone` leaves the zones the cancel never reached with no
    `actual_end`, and that run becomes `last_run` — the first time `last_run`
    can hold a zone with no end instant. Gating on `actual_end` alone would
    flap every one of those MEASUREMENT sensors to `unknown` and pollute its
    long-term statistics, while the README documents the opposite ("zones the
    cycle never reached are recorded as having watered zero seconds").
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = controller_with_zones(
        zone_subentry_data("Zone A", VALVE_A),
        zone_subentry_data("Zone B", VALVE_B),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switches(hass)

    duration_b = entity_id_for(
        hass,
        f"{entry.entry_id}_{zone_of(entry, 'Zone B').subentry_id}"
        "_last_watering_duration",
    )
    duration_a = entity_id_for(
        hass,
        f"{entry.entry_id}_{zone_of(entry, 'Zone A').subentry_id}"
        "_last_watering_duration",
    )
    assert state_of(hass, duration_b).state == "unknown"

    # Zone A opens at 07:00 and is still the live slot at 07:05, so Zone B's
    # 07:10-07:20 window is never reached.
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    freezer.move_to("2026-07-31 07:05:00+02:00")
    await hass.services.async_call(DOMAIN, SERVICE_CANCEL_CYCLE, blocking=True)
    await hass.async_block_till_done()

    assert state_of(hass, duration_a).state == "300"
    assert state_of(hass, duration_b).state == "0"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Water debt attributes (Story 2.2)
# --------------------------------------------------------------------------


async def test_zone_sensor_debt_attributes_read_zero_with_no_run_and_no_debt(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """A zone that never watered and owes nothing has NO debt — 0, not unknown.

    Pinned before any cycle (state unknown) and after a full one (state 600):
    neither shows a deficit, because none was carried and none is owed.
    """
    register_switches(hass)
    zone = zone_of(entry, "Zone A")
    duration = entity_id_for(
        hass,
        f"{entry.entry_id}_{zone.subentry_id}_last_watering_duration",
    )
    state = state_of(hass, duration)
    assert state.state == "unknown"
    assert state.attributes["carried_deficit"] == 0
    assert state.attributes["pending_deficit"] == 0
    assert state.attributes["rain_credit"] == 0

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    state = state_of(hass, duration)
    assert state.state == "600"
    assert state.attributes["carried_deficit"] == 0
    assert state.attributes["pending_deficit"] == 0
    assert state.attributes["rain_credit"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_zone_sensor_shows_the_deficit_a_run_produced_then_carried(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """AC 4, end to end: `pending_deficit` is the ledger, `carried_deficit` the run.

    The failed cycle shows 0 s watered, carried 0 (it applied no debt) and
    pending 600 (it produced one). The run-now that follows is quoted 1200 s
    — the debt is applied but still pending while the cycle runs — and once
    it completes the sensor shows 1200 s, carried 600, pending 0.
    """
    register_switches(hass, failing=VALVE_A)
    zone = zone_of(entry, "Zone A")
    duration = entity_id_for(
        hass,
        f"{entry.entry_id}_{zone.subentry_id}_last_watering_duration",
    )

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    state = state_of(hass, duration)
    assert state.state == "0"
    assert state.attributes["carried_deficit"] == 0
    assert state.attributes["pending_deficit"] == 600

    # The valve recovers: the fake hardware is re-registered without the
    # failure (`async_register` is last-wins), and the operator fires a
    # run-now — quoted like any cycle, so it carries the 600 s.
    register_switches(hass)
    freezer.move_to("2026-07-31 07:30:00+02:00")
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RUN_NOW,
        {ATTR_CYCLE: "morning"},
        blocking=True,
    )
    await hass.async_block_till_done()
    state = state_of(hass, duration)
    assert state.state == "0"  # the running slot has not closed: still the old run
    assert state.attributes["carried_deficit"] == 0
    assert state.attributes["pending_deficit"] == 600  # quoting writes nothing

    await fire_at(hass, freezer, "2026-07-31 07:50:00+02:00")

    state = state_of(hass, duration)
    assert state.state == "1200"
    assert state.attributes["carried_deficit"] == 600
    assert state.attributes["pending_deficit"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_zone_sensor_shows_a_pending_deficit_with_no_run_at_all(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """A deficit seeded from the journal is visible before any cycle has run.

    The restart case: the ledger owes 300 s from a previous life, nothing has
    watered yet in this one — the state is unknown and the debt is pending.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [],
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {"zone-a": 300},
            },
        },
    }
    entry = controller_with_zones(
        {**zone_subentry_data("Zone A", VALVE_A), "subentry_id": "zone-a"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    duration = entity_id_for(hass, f"{entry.entry_id}_zone-a_last_watering_duration")

    state = state_of(hass, duration)
    assert state.state == "unknown"
    assert state.attributes["carried_deficit"] == 0
    assert state.attributes["pending_deficit"] == 300

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Rain credit attribute (Story 2.4)
# --------------------------------------------------------------------------


def set_gauge(hass: HomeAssistant, total: str) -> None:
    """Put a cumulative total in mm on the representative entry's rain sensor."""
    hass.states.async_set("sensor.rain_gauge", total, {"unit_of_measurement": "mm"})


async def test_zone_sensor_shows_the_rain_credit_of_the_run_it_reports(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.4 AC 1, end to end: two cycles, the second reduced, the credit visible.

    The morning at 12.0 mm waters its full 600 s (no baseline yet) and shows
    `rain_credit: 0`. By the evening the gauge reads 15.5: the 15 min slot
    is quoted 690 s and, once it closes, the sensor shows 690 s watered with
    `rain_credit: 210` — the same slot both figures describe.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    set_gauge(hass, "12.0")
    entry = controller_with_zones(zone_subentry_data("Zone A", VALVE_A))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switches(hass)
    zone = zone_of(entry, "Zone A")
    duration = entity_id_for(
        hass,
        f"{entry.entry_id}_{zone.subentry_id}_last_watering_duration",
    )

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    state = state_of(hass, duration)
    assert state.state == "600"
    assert state.attributes["rain_credit"] == 0

    set_gauge(hass, "15.5")
    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    # The evening slot is still open: the sensor keeps showing the morning.
    state = state_of(hass, duration)
    assert state.state == "600"
    assert state.attributes["rain_credit"] == 0

    await fire_at(hass, freezer, "2026-07-31 20:11:30+02:00")

    state = state_of(hass, duration)
    assert state.state == "690"
    assert state.attributes["rain_credit"] == 210
    assert state.attributes["carried_deficit"] == 0
    assert state.attributes["pending_deficit"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_rain_skipped_zone_reports_zero_seconds_and_its_credit(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """A skip is an ANSWER — 0 s — not missing data, and the credit says why.

    Zone A's baseline is 0.0 and the gauge reads 12.0: the 10 min slot is
    fully covered, the zone is skipped the instant the cycle starts, and the
    MEASUREMENT sensor reads `0` (never `unknown`) with `rain_credit` 720 —
    while zone B, sheltered, waters its 600 s.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    set_gauge(hass, "12.0")
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [],
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {},
                "day_credit": None,
                "rain_baselines": {"zone-a": 0.0, "zone-b": 0.0},
                "rain_source": "sensor.rain_gauge",
            },
        },
    }
    entry = controller_with_zones(
        {**zone_subentry_data("Zone A", VALVE_A), "subentry_id": "zone-a"},
        {
            **zone_subentry_data("Zone B", VALVE_B, **{CONF_RAIN_EXPOSED: False}),
            "subentry_id": "zone-b",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = register_switches(hass)
    duration_a = entity_id_for(hass, f"{entry.entry_id}_zone-a_last_watering_duration")
    duration_b = entity_id_for(hass, f"{entry.entry_id}_zone-b_last_watering_duration")
    assert state_of(hass, duration_a).state == "unknown"

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    # Skipped in the same tick the cycle started: already an answer.
    state = state_of(hass, duration_a)
    assert state.state == "0"
    assert state.attributes["rain_credit"] == 720
    assert state.attributes["carried_deficit"] == 0
    assert state_of(hass, duration_b).state == "unknown"  # still watering
    assert [call.data[ATTR_ENTITY_ID] for call in calls] == [PUMP, VALVE_B]

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    state = state_of(hass, duration_a)
    assert state.state == "0"
    assert state.attributes["rain_credit"] == 720
    assert state.attributes["pending_deficit"] == 0
    state = state_of(hass, duration_b)
    assert state.state == "600"
    assert state.attributes["rain_credit"] == 0
    assert [call.data[ATTR_ENTITY_ID] for call in calls] == [
        PUMP,
        VALVE_B,
        VALVE_B,
        PUMP,
    ]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
