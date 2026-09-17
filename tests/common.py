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

from custom_components.ha_irrigation_controller.adapters.journal import STORAGE_KEY
from custom_components.ha_irrigation_controller.const import (
    CONF_ACTUATION_TIMEOUT,
    CONF_EVENING_DURATION,
    CONF_EVENING_START,
    CONF_HUMIDITY_SENSOR,
    CONF_MORNING_DURATION,
    CONF_MORNING_ENABLED,
    CONF_MORNING_START,
    CONF_NOTIFY_TARGET,
    CONF_PUMP_SWITCH,
    CONF_RAIN_EXPOSED,
    CONF_RAIN_FACTOR,
    CONF_RAIN_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VALVE_SWITCH,
    DOMAIN,
    SUBENTRY_TYPE_ZONE,
)
from custom_components.ha_irrigation_controller.engine.runs import MANUAL_KEY
from custom_components.ha_irrigation_controller.engine.sequencer import (
    JOURNAL_SCHEMA_VERSION,
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
    CONF_NOTIFY_TARGET: "notify.mobile_app_phone",
}


# The switch entities the engine-level suites drive. Kept here rather than in
# one test module so the modules that share them do not import each other.
PUMP = "switch.pool_pump"
VALVE_1 = "switch.zone_1_valve"
VALVE_2 = "switch.zone_2_valve"


def register_switch_domain(
    hass: HomeAssistant,
    *,
    extra_hardware: tuple[str, ...] = (),
    seed_states: bool = True,
) -> list[ServiceCall]:
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

    REGISTRY-MANAGED HARDWARE (Story 1.7's rename tests) has a second order
    contract. `EntityRegistry.async_generate_entity_id` refuses an id already
    present in the state machine, so a registry entry must exist BEFORE the
    state does — and a renamed id must be owned by the fake hardware or its
    close is unconfirmed. The sequence is therefore: create the registry
    entries → set up the entry → `register_switch_domain(hass,
    extra_hardware=(<new ids>,), seed_states=False)` → `hass.states.async_set`
    each stored id → rename in the registry → `hass.states.async_set(new_id)`.
    `extra_hardware` names the ids the fake will own in addition to the three
    representative switches; `seed_states=False` skips the initial OFF states
    so the caller can set them after the registry entries exist.
    """
    calls: list[ServiceCall] = []
    # A tuple, not a set: the seeding loop below sets initial states, and a set
    # would order them by PYTHONHASHSEED.
    fake_hardware = (PUMP, VALVE_1, VALVE_2, *extra_hardware)
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
    if seed_states:
        for entity_id in fake_hardware:
            hass.states.async_set(entity_id, STATE_OFF)
    return calls


def register_notify_domain(hass: HomeAssistant) -> list[ServiceCall]:
    """Register a recording `notify.send_message`, the anomaly push target.

    The integration declares no dependency on the notify component (it only
    calls the service), so nothing registers it in tests; without this
    helper every push ends in `ServiceNotFound`, which the manager logs and
    swallows — fine for a test that does not look at pushes, useless for one
    that does.
    """
    calls: list[ServiceCall] = []

    async def _handle(call: ServiceCall) -> None:
        calls.append(call)

    hass.services.async_register("notify", "send_message", _handle)
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


# --------------------------------------------------------------------------
# Journal documents (Story 3.2): what a crashed engine leaves in `.storage`
# --------------------------------------------------------------------------

# 07:00 Europe/Paris on the representative day, as `utc_iso` writes it — the
# morning start of `CONTROLLER_OPTIONS`; `zone_subentry_data` waters 10 min.
MORNING_START_UTC = "2026-07-31T05:00:00+00:00"
ZONE_A_END_UTC = "2026-07-31T05:10:00+00:00"
ZONE_B_END_UTC = "2026-07-31T05:20:00+00:00"


def zone_document(
    zone_id: str,
    valve: str,
    start: str,
    end: str,
    **overrides: object,
) -> dict[str, object]:
    """Build one zone record exactly as `ZoneRun.as_dict` writes it (10 min slot)."""
    return {
        "zone_id": zone_id,
        "name": zone_id.replace("-", " ").title(),
        "valve_entity_id": valve,
        "duration_s": 600,
        "base_s": 600,
        "carried_s": 0,
        "rain_credit_s": 0,
        "planned_start": start,
        "planned_end": end,
        "status": "pending",
        "actual_start": None,
        "actual_end": None,
        "open_confirmed": None,
        "close_confirmed": None,
        **overrides,
    }


def run_document(
    *zones: dict[str, object],
    status: str = "running",
    **overrides: object,
) -> dict[str, object]:
    """Build a cycle record exactly as `CycleRun.as_dict` writes it (07:00 morning)."""
    return {
        "cycle_id": "2026-07-31-morning",
        "kind": "morning",
        "configured_start": MORNING_START_UTC,
        "scheduled_start": MORNING_START_UTC,
        "pump_entity_id": PUMP,
        "status": status,
        "pump_on_confirmed": None if status == "pending" else True,
        "pump_off_confirmed": None,
        "manual": False,
        "rain_total_mm": None,
        "rain_source": None,
        "recovery": None,
        "late_rerun": False,
        "zones": list(zones),
        **overrides,
    }


def zone_a_document(**overrides: object) -> dict[str, object]:
    """Zone A's slot (`zone-a`, VALVE_1, 07:00-07:10)."""
    return zone_document(
        "zone-a", VALVE_1, MORNING_START_UTC, ZONE_A_END_UTC, **overrides
    )


def zone_b_document(**overrides: object) -> dict[str, object]:
    """Zone B's slot (`zone-b`, VALVE_2, 07:10-07:20)."""
    return zone_document("zone-b", VALVE_2, ZONE_A_END_UTC, ZONE_B_END_UTC, **overrides)


def crashed_during_zone_a() -> dict[str, object]:
    """Build the run a crash leaves at 07:0x: zone A open since 07:00, B pending."""
    return run_document(
        zone_a_document(
            status="running",
            actual_start=MORNING_START_UTC,
            open_confirmed=True,
        ),
        zone_b_document(),
    )


def history_record(
    kind: str = "morning",
    *,
    day: str = "2026-07-31",
    status: str = "completed",
    **overrides: object,
) -> dict[str, object]:
    """Build one stored history entry as `history_entry` writes it, zones elided.

    Enough for every reader that matters at the storage boundary: the day
    parsing `prune_history` does, the occurrence count, and Story 3.3's
    watchdog — which asks only whether a record for `(irrigation_day, kind)`
    exists at all, whatever its status.
    """
    return {
        "cycle_id": f"{day}-{kind}",
        "irrigation_day": day,
        "kind": kind,
        "status": status,
        MANUAL_KEY: False,
        "waived_by": None,
        "recovery": None,
        "late_rerun": False,
        "zones": [],
        **overrides,
    }


def journal_document(**sections: object) -> dict[str, Any]:
    """Build the stored `.storage` document around the given journal sections."""
    return {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [],
            "season_enabled": True,
            **sections,
        },
    }
