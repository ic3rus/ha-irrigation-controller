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
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import (
    CONF_EVENING_DURATION,
    CONF_EVENING_START,
    CONF_MORNING_DURATION,
    CONF_PUMP_SWITCH,
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


async def test_add_zone_rounds_half_minutes_up(hass: HomeAssistant) -> None:
    """Half minutes round UP, in the same direction every time.

    Bare `round()` is half-to-EVEN, so 10.5 would store 10 while 11.5 stores 12
    — an arbitrary result for a duration the operator can read back.
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
            CONF_MORNING_DURATION: 10.5,
            CONF_EVENING_DURATION: 11.5,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    subentry = next(iter(entry.subentries.values()))
    assert subentry.data[CONF_MORNING_DURATION] == 11
    assert subentry.data[CONF_EVENING_DURATION] == 12


async def test_add_zone_requires_a_name(hass: HomeAssistant) -> None:
    """A blank name is rejected — it is the zone's only operator-facing identity.

    `TextSelector` is `vol.Schema(str)`, so "" and "   " both pass the schema; a
    websocket submission reaches the step with them. Storing one would name the
    zone device after the config entry ("Irrigation Controller"), colliding with
    the controller's own device.
    """
    entry = await _setup_controller(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_NAME: "   "},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "name_required"}

    # Recovery: a real name creates the zone, stored stripped.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_NAME: "  Front Lawn  "},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert next(iter(entry.subentries.values())).title == "Front Lawn"


async def test_add_zone_rejects_duplicate_name(hass: HomeAssistant) -> None:
    """Two zones cannot share a name — case-insensitively (AC 2).

    The subentry id is the zone key in code, but the name is the ONLY thing
    distinguishing two zones for the operator (device list, dashboard card,
    notifications), so it has to be unique to be useful.
    """
    entry = await _setup_controller(hass)
    await add_zone(hass, entry, name="Front Lawn", valve="switch.zone_1_valve")

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_NAME: "front lawn",
            CONF_VALVE_SWITCH: "switch.zone_2_valve",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "name_already_configured"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_NAME: "Back Lawn",
            CONF_VALVE_SWITCH: "switch.zone_2_valve",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 2


async def test_reconfigure_keeping_own_name_is_allowed(hass: HomeAssistant) -> None:
    """A zone keeping its own name must not trip the duplicate-name check."""
    entry = await _setup_controller(hass)
    await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")
    zone2 = await add_zone(hass, entry, name="Zone 2", valve="switch.zone_2_valve")

    result = await entry.start_subentry_reconfigure_flow(hass, zone2.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_NAME: "Zone 2",
            CONF_VALVE_SWITCH: "switch.zone_2_valve",
            CONF_MORNING_DURATION: 30,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    # Renaming onto the SIBLING's name is still rejected.
    result = await entry.start_subentry_reconfigure_flow(hass, zone2.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_NAME: "Zone 1",
            CONF_VALVE_SWITCH: "switch.zone_2_valve",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "name_already_configured"}


async def test_add_zone_resolves_a_registry_id_to_its_entity_id(
    hass: HomeAssistant,
) -> None:
    """A registry id is resolved before the guards, not stored raw.

    `EntitySelector` runs `cv.entity_id_or_uuid` and returns a registry id
    BEFORE applying its `domain` filter, so a websocket submission can carry one.
    Left unresolved it would defeat every string comparison in the flow — and
    hand Story 1.4 an id `hass.states.get()` knows nothing about.
    """
    entry = await _setup_controller(hass)
    registry = er.async_get(hass)
    valve = registry.async_get_or_create(
        "switch", "test", "zone_1", suggested_object_id="zone_1_valve"
    )
    pump = registry.async_get_or_create(
        "switch", "test", "pump", suggested_object_id="pool_pump"
    )
    assert pump.entity_id == CONTROLLER_OPTIONS[CONF_PUMP_SWITCH]

    # The pump's registry id must trip valve_is_pump exactly like its entity_id.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_VALVE_SWITCH: pump.id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VALVE_SWITCH: "valve_is_pump"}

    # A real valve's registry id is accepted and STORED as its entity_id.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_VALVE_SWITCH: valve.id},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    subentry = next(iter(entry.subentries.values()))
    assert subentry.data[CONF_VALVE_SWITCH] == valve.entity_id


async def test_add_zone_rejects_an_unresolvable_registry_id(
    hass: HomeAssistant,
) -> None:
    """A registry id matching no entity is rejected, not stored dangling."""
    entry = await _setup_controller(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        # A well-formed registry id (32 hex chars) that is in no registry —
        # `cv.entity_id_or_uuid` accepts it, so it reaches the step.
        user_input={**ZONE_INPUT, CONF_VALVE_SWITCH: "0" * 32},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VALVE_SWITCH: "valve_not_found"}


async def test_reconfigure_aborts_when_the_zone_was_deleted(
    hass: HomeAssistant,
) -> None:
    """A zone deleted while its form is open aborts cleanly (no traceback).

    The UI delete button calls `async_remove_subentry` over websocket and
    aborts no in-progress flow, so this is reachable from a second browser tab.
    Unguarded, `_get_reconfigure_subentry()` raises `UnknownSubEntry` out of the
    step and the operator gets "Unknown error occurred" with a wedged dialog.
    """
    entry = await _setup_controller(hass)
    zone = await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")

    result = await entry.start_subentry_reconfigure_flow(hass, zone.subentry_id)
    assert result["type"] is FlowResultType.FORM

    assert hass.config_entries.async_remove_subentry(entry, zone.subentry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=dict(ZONE_INPUT),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "zone_not_found"


async def _setup_tight_gap_controller(hass: HomeAssistant) -> MockConfigEntry:
    """Set up a controller whose morning→evening gap is only 30 minutes.

    With the morning cycle enabled at 07:00 and the evening at 07:30, any
    morning-duration total above 30 minutes overlaps the evening window.
    """
    entry = controller_entry(
        {**CONTROLLER_OPTIONS, CONF_EVENING_START: "07:30:00"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_add_zone_rejects_cycle_overlap(hass: HomeAssistant) -> None:
    """A zone pushing the morning window past the evening start is rejected.

    Uses the engine's overlap rule (Story 1.4, resolves the 1.2 deferral) —
    the flow never duplicates the sum-of-durations math.
    """
    entry = await _setup_tight_gap_controller(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_MORNING_DURATION: 40},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MORNING_DURATION: "cycles_overlap"}
    assert not entry.subentries

    # Recovery: a duration fitting the gap creates the zone.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_MORNING_DURATION: 20},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 1


async def test_reconfigure_rejects_cycle_overlap(hass: HomeAssistant) -> None:
    """Growing an existing zone past the evening start is rejected on edit.

    The zone's own stored duration is excluded from the proposal — only the
    submitted value counts, so shrinking back always remains possible.
    """
    entry = await _setup_tight_gap_controller(hass)
    zone = await add_zone(hass, entry)

    result = await entry.start_subentry_reconfigure_flow(hass, zone.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_MORNING_DURATION: 40},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MORNING_DURATION: "cycles_overlap"}
    assert zone.data[CONF_MORNING_DURATION] == 10

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_MORNING_DURATION: 25},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert zone.data[CONF_MORNING_DURATION] == 25


async def test_add_zone_blames_the_evening_duration_when_it_is_the_offender(
    hass: HomeAssistant,
) -> None:
    """With the evening cycle first, its duration is the field to shrink.

    The morning duration is at the schema minimum here, so flagging it would
    hand the operator an error no edit of that field could ever clear.
    """
    entry = controller_entry(
        {**CONTROLLER_OPTIONS, CONF_EVENING_START: "06:00:00"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_MORNING_DURATION: 1,
            CONF_EVENING_DURATION: 90,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EVENING_DURATION: "evening_cycle_overlap"}
    assert not entry.subentries

    # Recovery: an evening window closing before 07:00 creates the zone.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            **ZONE_INPUT,
            CONF_MORNING_DURATION: 1,
            CONF_EVENING_DURATION: 55,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 1


async def test_add_zone_still_works_when_a_sibling_zone_is_malformed(
    hass: HomeAssistant,
) -> None:
    """One bad stored zone must not lock the operator out of the zone flows.

    The proposal merges the new zone over its siblings, so a sibling the engine
    cannot parse used to raise straight out of the step ("Unknown error
    occurred") — leaving deletion, which bypasses flow code, as the only way
    out.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            zone_subentry_data("Zone A", "switch.zone_a_valve", morning_duration="ten"),
        ],
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input=dict(ZONE_INPUT),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.subentries) == 2


async def test_options_flow_rejects_a_zone_valve_as_the_pump(
    hass: HomeAssistant,
) -> None:
    """The pump/valve exclusion holds from the CONTROLLER side too (AC 1).

    Guarding it only in the zone flow leaves the invariant defeatable through
    the adjacent options form — and wedges the zone: its own reconfigure
    resubmits its valve, which would then trip `valve_is_pump` with no way out.
    """
    entry = await _setup_controller(hass)
    await add_zone(hass, entry, name="Zone 1", valve="switch.zone_1_valve")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_PUMP_SWITCH: "switch.zone_1_valve"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PUMP_SWITCH: "pump_is_zone_valve"}
    assert entry.options[CONF_PUMP_SWITCH] == CONTROLLER_OPTIONS[CONF_PUMP_SWITCH]

    # Recovery: a switch no zone drives is accepted.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={**CONTROLLER_OPTIONS, CONF_PUMP_SWITCH: "switch.other_pump"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_PUMP_SWITCH] == "switch.other_pump"


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
    # `add_zone` returns the LIVE ConfigSubentry and async_update_subentry mutates
    # it in place, so every "unchanged?" assertion below must compare against a
    # snapshot taken now — comparing the object with itself always passes.
    zone1_id = zone1.subentry_id
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

    # Edited in place: same subentry id, so the zone key never changes (AC 3).
    assert list(entry.subentries) == [zone1_id, zone2.subentry_id]
    updated = entry.subentries[zone1_id]
    assert updated.title == "Zone 1 renamed"
    assert updated.data[CONF_MORNING_DURATION] == 20
    assert updated.data[CONF_EVENING_DURATION] == 25
    # The sibling is byte-identical: same data, same title, same id.
    sibling = entry.subentries[zone2.subentry_id]
    assert sibling.data == zone2_data_before
    assert sibling.title == "Zone 2"

    # Renaming the zone renames its device. This rests on async_get_or_create
    # forwarding `name=` into _async_update_device for an existing device —
    # non-obvious semantics a refactor to `if device is None:` would break
    # silently, leaving every renamed zone's device stuck on its old name.
    registry = dr.async_get(hass)
    zone1_device = registry.async_get_device(identifiers={(DOMAIN, zone1_id)})
    assert zone1_device is not None
    assert zone1_device.name == "Zone 1 renamed"
    zone2_device = registry.async_get_device(
        identifiers={(DOMAIN, zone2.subentry_id)},
    )
    assert zone2_device is not None
    assert zone2_device.name == "Zone 2"


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

    # Snapshot BEFORE the removal: `add_zone` returns the live ConfigSubentry,
    # so `entry.subentries[id].data == zone.data` would compare the surviving
    # object with itself and pass no matter what the removal did.
    before = {
        zone.subentry_id: (zone.title, dict(zone.data))
        for zone in zones
        if zone is not zones[2]
    }

    # Removing a middle zone leaves the other four fully intact.
    middle = zones[2]
    assert hass.config_entries.async_remove_subentry(entry, middle.subentry_id)
    await hass.async_block_till_done()

    remaining = [zone for zone in zones if zone is not middle]
    assert list(entry.subentries) == [zone.subentry_id for zone in remaining]
    for subentry_id, (title, data) in before.items():
        survivor = entry.subentries[subentry_id]
        assert survivor.title == title
        assert survivor.data == data
        assert (
            registry.async_get_device(identifiers={(DOMAIN, subentry_id)}) is not None
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
            zone_subentry_data("Zone 3", "switch.zone_3_valve"),
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
    assert len(entry.subentries) == 3
    for subentry in entry.subentries.values():
        device = registry.async_get_device(
            identifiers={(DOMAIN, subentry.subentry_id)},
        )
        assert device is not None
        assert device.via_device_id == controller_device.id
        assert device.name == subentry.title


async def test_zone_order_survives_the_storage_path(hass: HomeAssistant) -> None:
    """Zone order is preserved when the entry is built from stored data (AC 2).

    `test_zone_order_is_creation_order` pins the order within one live session;
    Story 1.4's sequencer needs it to hold after a restart too. This builds the
    entry from `subentries_data=` — the same shape the config-entry store
    reloads — and pins that both the raw mapping and the typed accessor the
    sequencer will call come back in the stored order.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            zone_subentry_data("Zone A", "switch.zone_a_valve"),
            zone_subentry_data("Zone B", "switch.zone_b_valve"),
            zone_subentry_data("Zone C", "switch.zone_c_valve"),
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    expected = ["Zone A", "Zone B", "Zone C"]
    assert [s.title for s in entry.subentries.values()] == expected
    assert [
        s.title for s in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)
    ] == expected


async def test_add_zone_rejects_our_own_season_switch_as_a_valve(
    hass: HomeAssistant,
) -> None:
    """The valve picker refuses the integration's own switch entity.

    Same rule as the pump guard in `test_config_flow.py`, from the other side:
    a zone whose "valve" is our own season switch would make the sequencer
    command itself mid-cycle, and the ONE lock turns that into a stalled
    actuation plus a season flip landing out of band.
    """
    entry = await _setup_controller(hass)
    season = er.async_get(hass).async_get_entity_id(
        "switch",
        DOMAIN,
        f"{entry.entry_id}_season",
    )
    assert season is not None

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_ZONE),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_VALVE_SWITCH: season},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_VALVE_SWITCH: "valve_is_own_entity"}

    # Recovery: a real valve saves through the same open form.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**ZONE_INPUT, CONF_VALVE_SWITCH: "switch.zone_1_valve"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    zones = entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)
    assert [s.data[CONF_VALVE_SWITCH] for s in zones] == ["switch.zone_1_valve"]
