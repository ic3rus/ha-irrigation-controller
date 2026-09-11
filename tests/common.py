"""Shared helpers: the representative controller entry used across test modules."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import ATTR_ENTITY_ID, CONF_NAME, STATE_OFF, STATE_ON
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

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
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant, ServiceCall

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


# The switch entities the engine-level suites drive. Kept here rather than in
# one test module so the modules that share them do not import each other.
PUMP = "switch.pool_pump"
VALVE_1 = "switch.zone_1_valve"
VALVE_2 = "switch.zone_2_valve"


def register_switch_domain(hass: HomeAssistant) -> list[ServiceCall]:
    """Register switch services that actually flip state, and record calls.

    A recorder alone would never confirm: the verified adapter watches for the
    state change, so the fake has to behave like a real switch.

    ORDER CONTRACT — call this AFTER the entry is set up. Since Story 1.6 the
    integration forwards `Platform.SWITCH`, so HA's switch component registers
    the real `switch.turn_on`/`turn_off` during `async_forward_entry_setups`,
    and `async_register` is last-wins: a fake registered first is silently
    replaced, the fake hardware then never flips, and every cycle stalls
    waiting for a confirmation that cannot arrive.

    Entity ids the fake does NOT own are delegated back to the handler it
    replaced, so the fake hardware and the integration's own season switch can
    both be driven from one test. `Service.job.target` is the function HA
    registered; calling it is exactly what `async_call` does.
    """
    calls: list[ServiceCall] = []
    # A tuple, not a set: the seeding loop below sets initial states, and a set
    # would order them by PYTHONHASHSEED.
    fake_hardware = (PUMP, VALVE_1, VALVE_2)
    replaced = {
        name: service.job.target
        for name, service in hass.services.async_services_for_domain("switch").items()
    }
    # The ORDER CONTRACT above, enforced rather than merely documented. An entry
    # for this domain existing means `async_forward_entry_setups` has run (or is
    # about to), and forwarding `Platform.SWITCH` is what loads HA's switch
    # component — so an empty `replaced` at that point means this helper is
    # about to be overwritten and every cycle will stall on a confirmation that
    # cannot arrive. A hang is the hardest failure to trace back to here; this
    # turns it into a named one. Runner-level tests build no entry at all and
    # legitimately have nothing to delegate to.
    assert replaced or not hass.config_entries.async_entries(DOMAIN), (
        "register_switch_domain must be called AFTER the entry is set up — "
        "see the ORDER CONTRACT in its docstring."
    )

    async def _handle(call: ServiceCall) -> None:
        # `.get`, not `[]`: a call targeting an area, a device or a label
        # carries no `entity_id` at all, and the list form is equally legal.
        # Neither is fake hardware, so both belong on the delegate path rather
        # than raising KeyError inside a service handler.
        entity_id = call.data.get(ATTR_ENTITY_ID)
        if entity_id not in fake_hardware:
            delegate = replaced.get(call.service)
            if delegate is not None:
                result = delegate(call)
                if inspect.isawaitable(result):
                    await result
            return
        calls.append(call)
        state = STATE_ON if call.service == "turn_on" else STATE_OFF
        hass.states.async_set(entity_id, state, context=call.context)

    hass.services.async_register("switch", "turn_on", _handle)
    hass.services.async_register("switch", "turn_off", _handle)
    for entity_id in fake_hardware:
        hass.states.async_set(entity_id, STATE_OFF)
    return calls


async def fire_at(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    moment: str,
) -> None:
    """Move the freezer to `moment` (local ISO) and fire the due timers."""
    freezer.move_to(moment)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


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
