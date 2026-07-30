"""Tests for the zone config subentry flow (Story 1.3).

The controller flows stay in test_config_flow.py; everything zone-shaped —
add/reconfigure/remove flows, zone devices, reload-on-change, zone order —
lives here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import (
    CONF_EVENING_DURATION,
    CONF_MORNING_DURATION,
    CONF_RAIN_EXPOSED,
    CONF_RAIN_FACTOR,
    CONF_VALVE_SWITCH,
    DOMAIN,
    SUBENTRY_TYPE_ZONE,
)
from tests.common import (
    CONTROLLER_OPTIONS,
    ZONE_DATA,
    ZONE_INPUT,
    add_zone,
    controller_entry,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def _setup_controller(hass: HomeAssistant) -> MockConfigEntry:
    """Add and set up a representative controller entry."""
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_add_zone_creates_subentry(hass: HomeAssistant) -> None:
    """The add-zone flow creates a typed subentry; name is title, not data (AC 1)."""
    entry = await _setup_controller(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=dict(ZONE_INPUT),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert len(entry.subentries) == 1
    subentry = next(iter(entry.subentries.values()))
    assert subentry.subentry_type == SUBENTRY_TYPE_ZONE
    assert subentry.title == "Front Lawn"
    assert subentry.unique_id is None
    assert subentry.data == ZONE_DATA
    assert CONF_NAME not in subentry.data


async def test_add_zone_form_defaults(hass: HomeAssistant) -> None:
    """The first-shown form carries the product-decision defaults.

    `rain_exposed=True` (a garden zone is outdoors by default) and
    `rain_factor=1.0` min/mm (deliberately conservative) are product decisions —
    a schema refactor cannot quietly change them.
    """
    entry = await _setup_controller(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    schema = result["data_schema"]
    assert schema is not None
    defaults = {
        key.schema: key.default()
        for key in schema.schema
        if key.default is not vol.UNDEFINED
    }
    assert defaults == {
        CONF_MORNING_DURATION: 10,
        CONF_EVENING_DURATION: 10,
        CONF_RAIN_EXPOSED: True,
        CONF_RAIN_FACTOR: 1.0,
    }


async def test_add_zone_normalizes_durations_to_int_minutes(
    hass: HomeAssistant,
) -> None:
    """Durations are stored as int minutes even from a float submission.

    The frontend number box sends floats ("10" arrives as 10.0) and a websocket
    submission can send any float in range — the stored contract is int minutes.
    """
    entry = await _setup_controller(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_MORNING_DURATION: 10.0,
            CONF_EVENING_DURATION: 15.0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    subentry = next(iter(entry.subentries.values()))
    assert subentry.data[CONF_MORNING_DURATION] == 10
    assert isinstance(subentry.data[CONF_MORNING_DURATION], int)
    assert subentry.data[CONF_EVENING_DURATION] == 15
    assert isinstance(subentry.data[CONF_EVENING_DURATION], int)


async def test_add_zone_rejects_pump_as_valve(hass: HomeAssistant) -> None:
    """A "valve" that is actually the pump cannot sequence — rejected (AC 1)."""
    entry = await _setup_controller(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_VALVE_SWITCH: CONTROLLER_OPTIONS["pump_switch"],
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VALVE_SWITCH: "valve_is_pump"}

    # Recovery: picking a real valve on the re-shown form creates the zone.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=dict(ZONE_INPUT),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 1


async def test_add_zone_rejects_duplicate_valve(hass: HomeAssistant) -> None:
    """Two zones driving one valve cannot sequence — rejected (AC 1)."""
    entry = await _setup_controller(hass)
    await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_NAME: "Zone 2",
            CONF_VALVE_SWITCH: "switch.zone_1_valve",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VALVE_SWITCH: "valve_already_configured"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_NAME: "Zone 2",
            CONF_VALVE_SWITCH: "switch.zone_2_valve",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 2


async def test_reconfigure_prefills_and_updates_only_that_zone(
    hass: HomeAssistant,
) -> None:
    """Reconfigure prefills from the subentry and touches nothing else (AC 2)."""
    entry = await _setup_controller(hass)
    zone1 = await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")
    zone2 = await add_zone(hass, entry, name="Zone 2", valve="switch.zone_2_valve")
    zone2_data_before = dict(zone2.data)

    result = await entry.start_subentry_reconfigure_flow(hass, zone1.subentry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    schema = result["data_schema"]
    assert schema is not None
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in schema.schema
        if key.description is not None and "suggested_value" in key.description
    }
    assert suggested == {
        **zone1.data,
        CONF_NAME: "Zone 1",
    }

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_NAME: "Zone 1 renamed",
            CONF_MORNING_DURATION: 20,
            CONF_EVENING_DURATION: 25,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    updated = entry.subentries[zone1.subentry_id]
    assert updated.subentry_id == zone1.subentry_id
    assert updated.title == "Zone 1 renamed"
    assert updated.data[CONF_MORNING_DURATION] == 20
    assert updated.data[CONF_EVENING_DURATION] == 25
    # The sibling is byte-identical: same data, same title, same id.
    sibling = entry.subentries[zone2.subentry_id]
    assert sibling.data == zone2_data_before
    assert sibling.title == "Zone 2"


async def test_reconfigure_keeping_own_valve_is_allowed(hass: HomeAssistant) -> None:
    """A zone keeping its own valve must not trip the duplicate check (AC 2)."""
    entry = await _setup_controller(hass)
    zone1 = await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")

    result = await entry.start_subentry_reconfigure_flow(hass, zone1.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_NAME: "Zone 1", CONF_MORNING_DURATION: 30},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.subentries[zone1.subentry_id].data[CONF_MORNING_DURATION] == 30


async def test_reconfigure_rejects_sibling_valve(hass: HomeAssistant) -> None:
    """Reconfiguring onto ANOTHER zone's valve is still rejected (AC 2)."""
    entry = await _setup_controller(hass)
    await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")
    zone2 = await add_zone(hass, entry, name="Zone 2", valve="switch.zone_2_valve")

    result = await entry.start_subentry_reconfigure_flow(hass, zone2.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_NAME: "Zone 2",
            CONF_VALVE_SWITCH: "switch.zone_1_valve",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VALVE_SWITCH: "valve_already_configured"}


async def test_reconfigure_rejects_pump_as_valve(hass: HomeAssistant) -> None:
    """The valve-is-pump guard applies on edits too, not just on creation."""
    entry = await _setup_controller(hass)
    zone1 = await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")

    result = await entry.start_subentry_reconfigure_flow(hass, zone1.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_VALVE_SWITCH: CONTROLLER_OPTIONS["pump_switch"],
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VALVE_SWITCH: "valve_is_pump"}


async def test_zone_device_links_via_device_to_controller(
    hass: HomeAssistant,
) -> None:
    """The zone device hangs off the controller; subentry id is the key (AC 3)."""
    entry = await _setup_controller(hass)
    zone = await add_zone(hass, entry, name="Front Lawn")

    registry = dr.async_get(hass)
    controller_device = registry.async_get_device(
        identifiers={(DOMAIN, entry.entry_id)},
    )
    assert controller_device is not None

    zone_device = registry.async_get_device(
        identifiers={(DOMAIN, zone.subentry_id)},
    )
    assert zone_device is not None
    assert zone_device.via_device_id == controller_device.id
    assert zone_device.name == "Front Lawn"
    assert zone_device.manufacturer == "ha-irrigation-controller"
    assert zone_device.config_entries_subentries[entry.entry_id] == {
        zone.subentry_id,
    }


async def test_add_zone_reloads_entry_without_restart(hass: HomeAssistant) -> None:
    """Adding a zone re-runs setup in place — no HA restart (AC 5)."""
    entry = await _setup_controller(hass)
    runtime_data_before = entry.runtime_data

    await add_zone(hass, entry)

    assert entry.runtime_data is not runtime_data_before


async def test_reconfigure_zone_reloads_entry_without_restart(
    hass: HomeAssistant,
) -> None:
    """Editing a zone re-runs setup in place — no HA restart (AC 5)."""
    entry = await _setup_controller(hass)
    zone = await add_zone(hass, entry)
    runtime_data_before = entry.runtime_data

    result = await entry.start_subentry_reconfigure_flow(hass, zone.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_EVENING_DURATION: 45},
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    assert entry.runtime_data is not runtime_data_before


async def test_remove_zone_reloads_and_cleans_up(hass: HomeAssistant) -> None:
    """Removing a zone drops its subentry and device, siblings intact (AC 2, 5).

    `async_remove_subentry` is exactly what the UI delete button calls over
    websocket — this covers the removal path that never touches our flow code.
    """
    entry = await _setup_controller(hass)
    zone1 = await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")
    zone2 = await add_zone(hass, entry, name="Zone 2", valve="switch.zone_2_valve")
    runtime_data_before = entry.runtime_data

    assert hass.config_entries.async_remove_subentry(entry, zone1.subentry_id)
    await hass.async_block_till_done()

    assert zone1.subentry_id not in entry.subentries
    assert zone2.subentry_id in entry.subentries

    registry = dr.async_get(hass)
    assert registry.async_get_device(identifiers={(DOMAIN, zone1.subentry_id)}) is None
    assert (
        registry.async_get_device(identifiers={(DOMAIN, zone2.subentry_id)}) is not None
    )
    # The removal reloaded the entry (update listener), no restart needed.
    assert entry.runtime_data is not runtime_data_before


async def test_zone_count_is_unbounded(hass: HomeAssistant) -> None:
    """Five zones work identically to one — no 3-zone ceiling anywhere (AC 4)."""
    entry = await _setup_controller(hass)
    zones = [
        await add_zone(hass, entry, name=f"Zone {n}", valve=f"switch.zone_{n}_valve")
        for n in range(1, 6)
    ]
    assert len(entry.subentries) == 5

    registry = dr.async_get(hass)
    controller_device = registry.async_get_device(
        identifiers={(DOMAIN, entry.entry_id)},
    )
    assert controller_device is not None
    for zone in zones:
        device = registry.async_get_device(identifiers={(DOMAIN, zone.subentry_id)})
        assert device is not None
        assert device.via_device_id == controller_device.id

    # Removing a middle zone leaves the other four fully intact.
    middle = zones[2]
    assert hass.config_entries.async_remove_subentry(entry, middle.subentry_id)
    await hass.async_block_till_done()

    remaining = [zone for zone in zones if zone is not middle]
    assert list(entry.subentries) == [zone.subentry_id for zone in remaining]
    for zone in remaining:
        assert entry.subentries[zone.subentry_id].data == zone.data
        assert (
            registry.async_get_device(identifiers={(DOMAIN, zone.subentry_id)})
            is not None
        )


async def test_zone_order_is_creation_order(hass: HomeAssistant) -> None:
    """`entry.subentries` iteration order IS the sequencer order (AC 2).

    Story 1.4's sequencer iterates `entry.subentries` as-is; this pins that the
    order is insertion order and that reconfiguring an earlier zone does not
    reorder anything.
    """
    entry = await _setup_controller(hass)
    zone1 = await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")
    zone2 = await add_zone(hass, entry, name="Zone 2", valve="switch.zone_2_valve")
    zone3 = await add_zone(hass, entry, name="Zone 3", valve="switch.zone_3_valve")

    expected_order = [zone1.subentry_id, zone2.subentry_id, zone3.subentry_id]
    assert list(entry.subentries) == expected_order

    # Reconfiguring the FIRST zone edits in place — no reorder.
    result = await entry.start_subentry_reconfigure_flow(hass, zone1.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_NAME: "Zone 1", CONF_MORNING_DURATION: 42},
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    assert list(entry.subentries) == expected_order


async def test_setup_creates_devices_for_preexisting_zones(
    hass: HomeAssistant,
) -> None:
    """Setup registers one device per stored zone — the restart/reload path (AC 3).

    The flow tests above exercise devices created after a flow-triggered reload;
    this proves a fresh setup from storage produces the same devices.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            zone_subentry_data("Zone 1", "switch.zone_1_valve"),
            zone_subentry_data("Zone 2", "switch.zone_2_valve"),
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    controller_device = registry.async_get_device(
        identifiers={(DOMAIN, entry.entry_id)},
    )
    assert controller_device is not None
    assert len(entry.subentries) == 2
    for subentry in entry.subentries.values():
        device = registry.async_get_device(
            identifiers={(DOMAIN, subentry.subentry_id)},
        )
        assert device is not None
        assert device.via_device_id == controller_device.id
        assert device.name == subentry.title
