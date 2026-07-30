"""Shared helpers: the representative controller entry used across test modules."""

from __future__ import annotations

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import (
    CONF_EVENING_START,
    CONF_HUMIDITY_SENSOR,
    CONF_MORNING_ENABLED,
    CONF_MORNING_START,
    CONF_PUMP_SWITCH,
    CONF_RAIN_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    DOMAIN,
)

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
}


def controller_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    """Build a controller entry configured the way the config flow creates it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(options if options is not None else CONTROLLER_OPTIONS),
    )
