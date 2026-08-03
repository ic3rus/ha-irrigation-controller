"""Shared helpers: the representative controller entry used across test modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import (
    CONF_ACTUATION_TIMEOUT,
    CONF_EVENING_DURATION,
    CONF_EVENING_START,
    CONF_HUMIDITY_SENSOR,
    CONF_MORNING_DURATION,
    CONF_MORNING_ENABLED,
    CONF_MORNING_START,
    CONF_PUMP_SWITCH,
    CONF_RAIN_EXPOSED,
    CONF_RAIN_FACTOR,
    CONF_RAIN_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VALVE_SWITCH,
    DOMAIN,
    SUBENTRY_TYPE_ZONE,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant

# Representative controller options — every entry carries its configuration in
# options (entry.data is empty by design). Story 1.3+ schema changes update this
# single definition, not one copy per test module.
CONTROLLER_OPTIONS: dict[str, Any] = {
    CONF_PUMP_SWITCH: "switch.pool_pump",
    CONF_RAIN_SENSOR: "sensor.rain_gauge",
    CONF_TEMPERATURE_SENSOR: "sensor.outdoor_temp",
    CONF_HUMIDITY_SENSOR: "sensor.outdoor_hum",
    CONF_MORNING_ENABLED: True,
    CONF_MORNING_START: "07:00:00",
    CONF_EVENING_START: "20:00:00",
    CONF_ACTUATION_TIMEOUT: 10,
}


def controller_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    """Build a controller entry configured the way the config flow creates it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(options if options is not None else CONTROLLER_OPTIONS),
    )


# Representative zone form submission. The zone name becomes the subentry TITLE
# (not data), so ZONE_DATA below is what actually lands in subentry.data.
ZONE_INPUT: dict[str, Any] = {
    CONF_NAME: "Front Lawn",
    CONF_VALVE_SWITCH: "switch.zone_1_valve",
    CONF_MORNING_DURATION: 10,
    CONF_EVENING_DURATION: 15,
    CONF_RAIN_EXPOSED: True,
    CONF_RAIN_FACTOR: 1.0,
}

ZONE_DATA: dict[str, Any] = {
    key: value for key, value in ZONE_INPUT.items() if key != CONF_NAME
}


def zone_subentry_data(
    title: str,
    valve: str,
    **overrides: Any,
) -> ConfigSubentryData:
    """Build setup-time subentry data the way the zone flow stores it.

    `overrides` land in the data mapping verbatim — including deliberately
    malformed values for the load-time validation tests (Story 1.4).
    """
    return ConfigSubentryData(
        data={**ZONE_DATA, CONF_VALVE_SWITCH: valve, **overrides},
        subentry_type=SUBENTRY_TYPE_ZONE,
        title=title,
        unique_id=None,
    )


async def add_zone(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    name: str = "Front Lawn",
    valve: str = "switch.zone_1_valve",
) -> ConfigSubentry:
    """Add a zone through the real subentry flow and return the created subentry.

    The returned `ConfigSubentry` is the LIVE object out of `entry.subentries`,
    and `async_update_subentry` mutates it in place — so any later "unchanged?"
    assertion must compare against a snapshot (`dict(zone.data)`) taken before
    the mutation, never against the returned object itself.
    """
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_NAME: name, CONF_VALVE_SWITCH: valve},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    # MockConfigEntry is untyped (PHCC ships no py.typed); pin the real type.
    subentries: dict[str, ConfigSubentry] = entry.subentries
    return next(
        subentry
        for subentry in subentries.values()
        if subentry.data[CONF_VALVE_SWITCH] == valve
    )
