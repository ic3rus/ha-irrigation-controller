"""Passive entity projections of engine state (AC 5, AD-6).

The sensors READ the engine and never mutate it: no polling, no
`RestoreEntity`, no second authority. Updates arrive by dispatcher after each
engine step.
"""

from __future__ import annotations

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

from custom_components.ha_irrigation_controller.const import (
    CONF_MORNING_ENABLED,
    CONF_VALVE_SWITCH,
    DOMAIN,
    SUBENTRY_TYPE_ZONE,
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
    assert device is not None
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
    assert state.attributes[ATTR_OPTIONS] == [
        "idle",
        "pending",
        "running",
        "completed",
    ]
    assert ATTR_STATE_CLASS not in state.attributes
    assert ATTR_UNIT_OF_MEASUREMENT not in state.attributes
    assert state.attributes["cycle_id"] is None
    assert state.attributes["current_zone"] is None

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    state = state_of(hass, status)
    assert state.state in state.attributes[ATTR_OPTIONS]
    assert state.attributes["cycle_id"] == "2026-07-31-morning"
    assert state.attributes["current_zone"] == "Zone A"

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
