"""Tests for the controller config flow and its options flow (Story 1.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import (
    CONF_ACTUATION_TIMEOUT,
    CONF_EVENING_START,
    CONF_HUMIDITY_SENSOR,
    CONF_MANUAL_TIMEOUT,
    CONF_MORNING_ENABLED,
    CONF_MORNING_START,
    CONF_NOTIFY_TARGET,
    CONF_PUMP_SWITCH,
    CONF_RAIN_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    DOMAIN,
    MIN_HA_VERSION,
)
from tests.common import (
    CONTROLLER_OPTIONS,
    controller_entry,
    fire_at,
    register_switch_domain,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

_FLOW_MODULE = "custom_components.ha_irrigation_controller.config_flow"

_MINIMAL_INPUT: dict[str, Any] = {
    CONF_PUMP_SWITCH: "switch.pool_pump",
    CONF_RAIN_SENSOR: "sensor.rain_gauge",
    CONF_MORNING_ENABLED: False,
    CONF_MORNING_START: "07:00:00",
    CONF_EVENING_START: "20:00:00",
    CONF_ACTUATION_TIMEOUT: 10,
    CONF_MANUAL_TIMEOUT: 30,
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
        CONF_ACTUATION_TIMEOUT: 10,
        CONF_MANUAL_TIMEOUT: 30,
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
    # The notify target (Story 3.1) is optional the same way.
    assert CONF_NOTIFY_TARGET not in result["options"]


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


async def test_actuation_timeout_is_stored_as_whole_seconds(
    hass: HomeAssistant,
) -> None:
    """A frontend float submission lands as the documented int seconds.

    `NumberSelector` coerces to float, so the frontend sends `10.0` — the
    stored contract (and the engine parser) wants int seconds.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={**_MINIMAL_INPUT, CONF_ACTUATION_TIMEOUT: 15.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_ACTUATION_TIMEOUT] == 15
    assert isinstance(result["options"][CONF_ACTUATION_TIMEOUT], int)


@pytest.mark.parametrize("out_of_range", [0, 121])
async def test_actuation_timeout_out_of_range_is_rejected_by_the_selector(
    hass: HomeAssistant,
    out_of_range: int,
) -> None:
    """The selector's own bounds refuse a timeout outside 1..120 seconds."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    with pytest.raises(InvalidData, match=CONF_ACTUATION_TIMEOUT):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={**_MINIMAL_INPUT, CONF_ACTUATION_TIMEOUT: out_of_range},
        )


async def test_manual_timeout_is_stored_as_whole_minutes(
    hass: HomeAssistant,
) -> None:
    """A frontend float submission lands as the documented int minutes (3.5).

    Same coercion as the actuation timeout above, in the other unit: the
    safety timeout is MINUTES at the UI and storage surface, and the engine
    parser wants whole ones.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={**_MINIMAL_INPUT, CONF_MANUAL_TIMEOUT: 45.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_MANUAL_TIMEOUT] == 45
    assert isinstance(result["options"][CONF_MANUAL_TIMEOUT], int)


@pytest.mark.parametrize("out_of_range", [0, 241])
async def test_manual_timeout_out_of_range_is_rejected_by_the_selector(
    hass: HomeAssistant,
    out_of_range: int,
) -> None:
    """The selector's own bounds refuse a timeout outside 1..240 minutes.

    There is no "off" value on purpose (Story 3.5, Decision 1): a pause
    nothing ever closes is the failure this option exists to remove, so `0`
    is out of range rather than a way to disable it.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    with pytest.raises(InvalidData, match=CONF_MANUAL_TIMEOUT):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={**_MINIMAL_INPUT, CONF_MANUAL_TIMEOUT: out_of_range},
        )


async def test_options_flow_edits_the_manual_timeout(hass: HomeAssistant) -> None:
    """The safety timeout is editable without a restart, like every option."""
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_MANUAL_TIMEOUT: 90},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options[CONF_MANUAL_TIMEOUT] == 90
    assert entry.runtime_data.sequencer.plan.manual_timeout_s == 5400


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
        "required": MIN_HA_VERSION,
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


async def test_options_flow_edit_mid_cycle_applies_after_the_cycle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The options flow is unchanged; the listener defers the reload (Story 1.7).

    Same flow as the idle variant above, submitted while a cycle runs: the
    options are written at once, the entry is not reloaded until the cycle
    completes, then exactly once.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    runtime_data_before = entry.runtime_data

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_EVENING_START: "21:30:00"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EVENING_START] == "21:30:00"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is runtime_data_before
    assert entry.runtime_data.runner.reload_pending is True

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not runtime_data_before
    assert entry.runtime_data.plan.evening_start.hour == 21

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


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


async def test_options_flow_rejects_overlapping_cycles(hass: HomeAssistant) -> None:
    """Times letting the morning schedule cross the evening start are rejected.

    Uses the engine's overlap rule (Story 1.4, resolves the 1.2 deferral) —
    the flow never duplicates the sum-of-durations math.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            # Two zones at 10 morning minutes each: morning runs 07:00-07:20.
            zone_subentry_data("Zone A", "switch.zone_a_valve"),
            zone_subentry_data("Zone B", "switch.zone_b_valve"),
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_EVENING_START: "07:15:00"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MORNING_START: "cycles_overlap"}

    # Recovery: an evening start clear of the morning window saves.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_EVENING_START: "07:20:00"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EVENING_START] == "07:20:00"


async def test_options_flow_blames_the_evening_cycle_when_it_is_the_offender(
    hass: HomeAssistant,
) -> None:
    """The overlap rule is symmetric, so the error must follow the guilty cycle.

    With the evening cycle starting first, its window is the one still watering
    when the morning cycle is due. Flagging the morning start here would give
    the operator a field no edit could clear.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            # 15 evening minutes: an evening cycle at 06:50 runs to 07:05.
            zone_subentry_data("Zone A", "switch.zone_a_valve"),
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_EVENING_START: "06:50:00"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EVENING_START: "evening_cycle_overlap"}

    # Recovery: an evening window that closes before the morning start saves.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_EVENING_START: "06:45:00"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EVENING_START] == "06:45:00"


async def test_options_flow_still_works_when_a_stored_zone_is_malformed(
    hass: HomeAssistant,
) -> None:
    """The repair path must survive the data that broke the setup.

    Malformed `.storage` data fails the setup loudly and the message tells the
    operator to fix the reported field — through this very form. Letting the
    engine's validation error escape the validator turned that into "Unknown
    error occurred", making a recoverable bad zone unrecoverable.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            zone_subentry_data("Zone A", "switch.zone_a_valve", rain_factor="wet"),
        ],
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_MORNING_START: "07:30:00"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_MORNING_START] == "07:30:00"


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


async def test_options_flow_rejects_our_own_season_switch_as_the_pump(
    hass: HomeAssistant,
) -> None:
    """The integration's own `switch.*` entity is not usable as hardware.

    Story 1.6 made this integration publish a switch entity, so its own plain
    `domain="switch"` pump picker offers it back. Commanding it through the
    verified adapter would re-enter the sequencer while `advance()` holds its
    lock — broken only by the actuation timeout, at the cost of a stall and a
    spurious anomaly per command. Checked against the entity REGISTRY, so a
    rename cannot defeat it, and enforced in validation rather than in the
    selector because the WebSocket API bypasses pickers.
    """
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    season = er.async_get(hass).async_get_entity_id(
        "switch",
        DOMAIN,
        f"{entry.entry_id}_season",
    )
    assert season is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_PUMP_SWITCH: season},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PUMP_SWITCH: "pump_is_own_entity"}
    assert entry.options[CONF_PUMP_SWITCH] == CONTROLLER_OPTIONS[CONF_PUMP_SWITCH]

    # Recovery: a real switch still saves through the same open form.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_PUMP_SWITCH: "switch.other_pump"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PUMP_SWITCH] == "switch.other_pump"


# --------------------------------------------------------------------------
# Story 3.1 — the notify target option
# --------------------------------------------------------------------------


async def test_options_flow_clears_the_notify_target(hass: HomeAssistant) -> None:
    """The target, once set, can be cleared by emptying its picker (no `default`)."""
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            key: value
            for key, value in CONTROLLER_OPTIONS.items()
            if key != CONF_NOTIFY_TARGET
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_NOTIFY_TARGET not in entry.options
    assert entry.state is ConfigEntryState.LOADED


async def test_a_notify_target_given_as_a_registry_id_is_stored_as_an_entity_id(
    hass: HomeAssistant,
) -> None:
    """Pin "entity ids everywhere": the tracker and the parser rely on it."""
    phone = er.async_get(hass).async_get_or_create(
        "notify",
        "mobile_app",
        "phone_uid",
        suggested_object_id="mobile_app_phone",
    )
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_NOTIFY_TARGET: phone.id},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_NOTIFY_TARGET] == "notify.mobile_app_phone"


async def test_options_flow_rejects_our_own_entity_as_the_notify_target(
    hass: HomeAssistant,
) -> None:
    """Same guard as the pump: an entity of ours is never the push target.

    The integration publishes no notify entity today, so the guard is
    exercised through a registry entry filed under our platform.
    """
    own = er.async_get(hass).async_get_or_create(
        "notify",
        DOMAIN,
        "own_notify_uid",
        suggested_object_id="irrigation_own",
    )
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_NOTIFY_TARGET: own.entity_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NOTIFY_TARGET: "notify_target_is_own_entity"}
    assert entry.options[CONF_NOTIFY_TARGET] == CONTROLLER_OPTIONS[CONF_NOTIFY_TARGET]

    # Recovery: a real target saves through the same open form.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            **CONTROLLER_OPTIONS,
            CONF_NOTIFY_TARGET: "notify.mobile_app_tablet",
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_NOTIFY_TARGET] == "notify.mobile_app_tablet"
