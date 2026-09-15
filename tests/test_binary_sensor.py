"""The health binary sensor: `on` iff an anomaly is open (Story 3.1, FR23).

A passive projection of the anomaly manager, refreshed on the ONE
engine-state signal the manager pushes on every open, clear and dismissal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import DOMAIN
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from tests.common import CONTROLLER_OPTIONS, zone_subentry_data

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

VALVE_CONTEXT: dict[str, object] = {
    "cycle_id": "2026-07-31-morning",
    "zone_id": "zone-a",
    "entity_id": "switch.zone_a_valve",
}


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    """Return an existing entity's state (mypy strict: never the None branch)."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state


@pytest.fixture
async def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up a one-zone controller whose zone id is readable below."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {
                **zone_subentry_data("Zone A", "switch.zone_a_valve"),
                "subentry_id": "zone-a",
            },
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


def health_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Resolve the health entity's id from its unique_id (never from its name)."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor",
        DOMAIN,
        f"{entry.entry_id}_health",
    )
    assert entity_id is not None
    return entity_id


async def test_health_is_off_with_empty_attributes_when_nothing_is_open(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """A fresh controller is healthy: `off`, `problem`, empty lists, named Health."""
    state = state_of(hass, health_entity_id(hass, entry))

    assert state.state == STATE_OFF
    assert state.attributes[ATTR_DEVICE_CLASS] == BinarySensorDeviceClass.PROBLEM
    assert state.attributes["open_anomalies"] == []
    assert state.attributes["last_anomaly"] is None
    assert state.attributes[ATTR_FRIENDLY_NAME] == "Irrigation Controller Health"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_health_mirrors_open_issues_through_report_and_clear(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """Report → `on` with the kind and subject; clear → `off`, last kept."""
    anomalies = entry.runtime_data.anomalies
    health = health_entity_id(hass, entry)

    anomalies.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    anomalies.report(
        AnomalyKind.CONFIGURED_ENTITY_MISSING,
        {"entity_id": "switch.pool_pump", "role": "pump_switch"},
    )
    await hass.async_block_till_done()

    state = state_of(hass, health)
    assert state.state == STATE_ON
    assert state.attributes["open_anomalies"] == [
        {"anomaly": "valve_open_unconfirmed", "zone_id": "zone-a"},
        {"anomaly": "configured_entity_missing", "role": "pump_switch"},
    ]
    assert state.attributes["last_anomaly"] == {
        "anomaly": "configured_entity_missing",
        "entity_id": "switch.pool_pump",
        "role": "pump_switch",
    }

    anomalies.clear(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    anomalies.clear(
        AnomalyKind.CONFIGURED_ENTITY_MISSING,
        {"entity_id": "switch.pool_pump", "role": "pump_switch"},
    )
    await hass.async_block_till_done()

    state = state_of(hass, health)
    assert state.state == STATE_OFF
    assert state.attributes["open_anomalies"] == []
    assert state.attributes["last_anomaly"] is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_health_turns_off_when_the_operator_acknowledges_the_issue(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """Dismissal through Repairs reaches the entity on the same signal."""
    anomalies = entry.runtime_data.anomalies
    health = health_entity_id(hass, entry)
    anomalies.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()
    assert state_of(hass, health).state == STATE_ON

    ir.async_delete_issue(hass, DOMAIN, "valve_open_unconfirmed:zone-a")
    await hass.async_block_till_done()

    assert state_of(hass, health).state == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_health_lives_on_the_controller_device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """Same device as the cycle-status sensor and the season switch."""
    registry_entry = er.async_get(hass).async_get(health_entity_id(hass, entry))
    assert registry_entry is not None
    assert registry_entry.device_id is not None
    device = dr.async_get(hass).async_get(registry_entry.device_id)
    assert device is not None
    assert (DOMAIN, entry.entry_id) in device.identifiers

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
