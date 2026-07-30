"""Tests for the controller config flow and its options flow (Story 1.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType

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
from tests.common import CONTROLLER_OPTIONS, controller_entry

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant

_FLOW_MODULE = "custom_components.ha_irrigation_controller.config_flow"

_MINIMAL_INPUT: dict[str, Any] = {
    CONF_PUMP_SWITCH: "switch.pool_pump",
    CONF_RAIN_SENSOR: "sensor.rain_gauge",
    CONF_MORNING_ENABLED: False,
    CONF_MORNING_START: "07:00:00",
    CONF_EVENING_START: "20:00:00",
}


async def test_user_flow_creates_entry_with_all_fields(hass: HomeAssistant) -> None:
    """The user step collects every field and stores them all in options (AC 1, 2)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=dict(CONTROLLER_OPTIONS),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Irrigation Controller"
    # Everything editable lives in options — data stays empty so the options flow
    # is the single source of truth (no data/options fallback duplication).
    assert result["data"] == {}
    assert result["options"] == CONTROLLER_OPTIONS


async def test_user_form_defaults_are_evening_only(hass: HomeAssistant) -> None:
    """The first-shown form defaults to the evening-only spring configuration.

    Pins the product decision: morning cycle off by default, 07:00:00/20:00:00
    start times — a schema refactor cannot quietly change them.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
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
        CONF_MORNING_ENABLED: False,
        CONF_MORNING_START: "07:00:00",
        CONF_EVENING_START: "20:00:00",
    }


async def test_user_flow_creates_entry_without_optional_sensors(
    hass: HomeAssistant,
) -> None:
    """Temperature and humidity are optional; when omitted they stay absent (AC 1)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=dict(_MINIMAL_INPUT),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == _MINIMAL_INPUT
    assert CONF_TEMPERATURE_SENSOR not in result["options"]
    assert CONF_HUMIDITY_SENSOR not in result["options"]


async def test_user_flow_rejects_identical_start_times(hass: HomeAssistant) -> None:
    """Two enabled cycles at the same instant would double-fire — rejected (AC 2)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_MORNING_START: "20:00:00"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_MORNING_START: "start_times_conflict"}

    # Recovery: correcting the time on the re-shown form creates the entry.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=dict(CONTROLLER_OPTIONS),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == CONTROLLER_OPTIONS


async def test_user_flow_rejects_equivalent_start_time_representations(
    hass: HomeAssistant,
) -> None:
    """Morning "20:00" and evening "20:00:00" are the same instant — rejected.

    The frontend widget always sends HH:MM:SS, but a websocket-driven flow can
    submit any string `cv.time` accepts — normalization keeps the conflict
    check honest for those too.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            **CONTROLLER_OPTIONS,
            CONF_MORNING_START: "20:00",
            CONF_EVENING_START: "20:00:00",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MORNING_START: "start_times_conflict"}


async def test_start_times_are_stored_normalized(hass: HomeAssistant) -> None:
    """Non-canonical time strings are stored canonicalized to HH:MM:SS.

    The options contract (and Story 1.4's parser) documents HH:MM:SS strings;
    `cv.time` alone would let "7:05" through verbatim.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            **_MINIMAL_INPUT,
            CONF_MORNING_START: "7:05",
            CONF_EVENING_START: "20:00",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_MORNING_START] == "07:05:00"
    assert result["options"][CONF_EVENING_START] == "20:00:00"


async def test_user_flow_allows_identical_times_when_morning_disabled(
    hass: HomeAssistant,
) -> None:
    """With the morning cycle off, its start time cannot collide with anything."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={**_MINIMAL_INPUT, CONF_MORNING_START: "20:00:00"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_aborts_when_already_configured(hass: HomeAssistant) -> None:
    """A second flow aborts — one controller entry only.

    Enforced by `single_config_entry` in manifest.json, which makes HA hide the
    entry point rather than offering "create" and aborting afterwards.
    """
    controller_entry().add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_user_flow_aborts_below_min_ha_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On an unsupported HA, the flow aborts instead of creating a doomed entry."""
    monkeypatch.setattr(f"{_FLOW_MODULE}.HA_MAJOR_VERSION", 2026)
    monkeypatch.setattr(f"{_FLOW_MODULE}.HA_MINOR_VERSION", 6)
    monkeypatch.setattr(f"{_FLOW_MODULE}.HA_VERSION", "2026.6.4")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_ha_version"
    assert result["description_placeholders"] == {
        "required": "2026.7.0",
        "running": "2026.6.4",
    }


async def test_options_flow_shows_prefilled_form(hass: HomeAssistant) -> None:
    """The options form opens prefilled from the current options (AC 4)."""
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    schema = result["data_schema"]
    assert schema is not None
    suggested = {
        key.schema: key.description["suggested_value"]
        for key in schema.schema
        if key.description is not None and "suggested_value" in key.description
    }
    assert suggested == CONTROLLER_OPTIONS


async def test_options_flow_edit_applies_without_restart(hass: HomeAssistant) -> None:
    """Editing options updates the entry and reloads it in place (AC 4, FR8)."""
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime_data_before = entry.runtime_data

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_EVENING_START: "21:30:00"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EVENING_START] == "21:30:00"
    # The update listener reloaded the entry in place: it is LOADED again and
    # async_setup_entry really ran again (fresh runtime_data), no HA restart.
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not runtime_data_before


async def test_options_flow_clears_optional_sensor(hass: HomeAssistant) -> None:
    """An optional sensor, once set, can be cleared by emptying its picker."""
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    cleared = {
        key: value
        for key, value in CONTROLLER_OPTIONS.items()
        if key != CONF_TEMPERATURE_SENSOR
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=cleared,
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_TEMPERATURE_SENSOR not in entry.options
    assert entry.options[CONF_HUMIDITY_SENSOR] == "sensor.outdoor_hum"


async def test_options_flow_rejects_identical_start_times(hass: HomeAssistant) -> None:
    """The start-time rule is enforced on edits too, not just on creation."""
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_MORNING_START: "20:00:00"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {CONF_MORNING_START: "start_times_conflict"}
    assert entry.options[CONF_MORNING_START] == "07:00:00"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_MORNING_START: "06:15:00"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MORNING_START] == "06:15:00"
