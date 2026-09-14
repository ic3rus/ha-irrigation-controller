"""Configured entities follow registry renames and never vanish silently (Story 1.7).

Every scenario drives the REAL entity registry — `async_update_entity` with
`new_entity_id`, `async_remove`, `disabled_by` — through a set-up entry, so
what is pinned is the whole chain: registry event → tracker → config-entries
write → the ONE update listener → reload (or deferral), and registry event →
anomaly with nothing else touched.

Registry-managed test hardware has an ORDER CONTRACT of its own, spelled out
in `register_switch_domain`: registry entries first (an id already in the
state machine is refused), then the entry, then the fake services, then the
states.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import (
    CONF_HUMIDITY_SENSOR,
    CONF_PUMP_SWITCH,
    CONF_RAIN_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VALVE_SWITCH,
    DOMAIN,
    EVENT_HA_IRRIGATION_CONTROLLER,
    SUBENTRY_TYPE_ZONE,
)
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.runs import CycleStatus
from tests.common import (
    CONTROLLER_OPTIONS,
    PUMP,
    VALVE_1,
    VALVE_2,
    fire_at,
    register_switch_domain,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant

NEW_PUMP = "switch.main_pump"
NEW_VALVE = "switch.front_valve"

# Every configured entity of the representative controller, keyed by the id
# the options/subentries store, with a registry unique_id of its own.
_REGISTRY_ENTITIES: dict[str, tuple[str, str]] = {
    PUMP: ("switch", "pump_uid"),
    VALVE_1: ("switch", "valve_1_uid"),
    VALVE_2: ("switch", "valve_2_uid"),
    CONTROLLER_OPTIONS[CONF_RAIN_SENSOR]: ("sensor", "rain_uid"),
    CONTROLLER_OPTIONS[CONF_TEMPERATURE_SENSOR]: ("sensor", "temp_uid"),
    CONTROLLER_OPTIONS[CONF_HUMIDITY_SENSOR]: ("sensor", "hum_uid"),
}


def register_configured_entities(hass: HomeAssistant) -> er.EntityRegistry:
    """Give every configured entity a registry entry under its stored id.

    BEFORE any state exists: `async_generate_entity_id` refuses an id present
    in the state machine and would silently hand out `switch.pool_pump_2`.
    """
    registry = er.async_get(hass)
    for entity_id, (domain, unique_id) in _REGISTRY_ENTITIES.items():
        created = registry.async_get_or_create(
            domain,
            "test",
            unique_id,
            suggested_object_id=entity_id.split(".", 1)[1],
        )
        assert created.entity_id == entity_id
    return registry


def rename(hass: HomeAssistant, old_entity_id: str, new_entity_id: str) -> None:
    """Rename a registry entity and move its fake state under the new id.

    The registry does not touch the state machine (an entity platform would):
    the fake hardware has to, or the renamed id has no state to confirm on.
    """
    er.async_get(hass).async_update_entity(old_entity_id, new_entity_id=new_entity_id)
    old_state = hass.states.get(old_entity_id)
    if old_state is not None:
        hass.states.async_remove(old_entity_id)
        hass.states.async_set(new_entity_id, old_state.state)


def zone_of(entry: MockConfigEntry, title: str) -> ConfigSubentry:
    """Return the live subentry carrying `title`."""
    subentries: dict[str, ConfigSubentry] = entry.subentries
    return next(subentry for subentry in subentries.values() if subentry.title == title)


def record_anomalies(hass: HomeAssistant) -> list[Event]:
    """Collect every anomaly event fired for the rest of the test."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_HA_IRRIGATION_CONTROLLER, events.append)
    return events


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> MockConfigEntry:
    """Set up a two-zone controller whose entities are all registry-managed."""
    freezer.move_to("2026-07-31 06:59:00+02:00")
    register_configured_entities(hass)
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            zone_subentry_data("Zone A", VALVE_1),
            zone_subentry_data("Zone B", VALVE_2),
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


# --------------------------------------------------------------------------
# Renames, idle
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [CONF_PUMP_SWITCH, CONF_RAIN_SENSOR, CONF_TEMPERATURE_SENSOR, CONF_HUMIDITY_SENSOR],
)
async def test_renaming_a_controller_entity_rewrites_that_option_only(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    """Each option role follows a rename through ONE `async_update_entry`.

    The other options are untouched, no anomaly is raised, and the entry
    reloads (idle) so the engine runs on the new id.
    """
    events = record_anomalies(hass)
    old_entity_id: str = entry.options[role]
    new_entity_id = f"{old_entity_id}_renamed"
    writes: list[dict[str, object]] = []
    real_update = hass.config_entries.async_update_entry

    def _counting(config_entry: MockConfigEntry, **kwargs: Any) -> bool:
        writes.append(kwargs)
        return real_update(config_entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _counting)
    runtime_data_before = entry.runtime_data

    rename(hass, old_entity_id, new_entity_id)
    await hass.async_block_till_done()

    assert len(writes) == 1
    assert entry.options[role] == new_entity_id
    assert {key: value for key, value in entry.options.items() if key != role} == {
        key: value for key, value in CONTROLLER_OPTIONS.items() if key != role
    }
    assert events == []
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not runtime_data_before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_renaming_a_valve_rewrites_its_zone_only(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A valve rename lands in ITS subentry through `async_update_subentry`."""
    events = record_anomalies(hass)
    zone_b_before = dict(zone_of(entry, "Zone B").data)

    rename(hass, VALVE_1, NEW_VALVE)
    await hass.async_block_till_done()

    assert zone_of(entry, "Zone A").data[CONF_VALVE_SWITCH] == NEW_VALVE
    assert dict(zone_of(entry, "Zone B").data) == zone_b_before
    assert dict(entry.options) == CONTROLLER_OPTIONS
    assert events == []
    assert VALVE_1 in caplog.text
    assert NEW_VALVE in caplog.text
    # The reload rebuilt the plan on the new id.
    plan = entry.runtime_data.plan
    assert [zone.valve_entity_id for zone in plan.zones] == [NEW_VALVE, VALVE_2]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_tracker_follows_the_new_id_after_a_rename(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """A second rename of the SAME entity is followed too — re-subscription.

    The keyed tracker dispatches a rename on the OLD id and is never
    re-indexed, so without a re-subscription after the rewrite the second
    rename would go unnoticed.
    """
    rename(hass, PUMP, NEW_PUMP)
    await hass.async_block_till_done()
    assert entry.options[CONF_PUMP_SWITCH] == NEW_PUMP

    rename(hass, NEW_PUMP, "switch.garden_pump")
    await hass.async_block_till_done()

    assert entry.options[CONF_PUMP_SWITCH] == "switch.garden_pump"
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Renames, mid-cycle
# --------------------------------------------------------------------------


async def test_a_live_valve_renamed_mid_cycle_is_closed_under_its_new_id(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """The adapter alias: the snapshot commands the old id, the service call the new.

    The stored id is rewritten, the reload is deferred behind the cycle, and
    the close of the renamed valve is confirmed on the new id — a real cycle,
    real services, real registry.
    """
    calls = register_switch_domain(hass, extra_hardware=(NEW_VALVE,), seed_states=False)
    for entity_id in (PUMP, VALVE_1, VALVE_2):
        hass.states.async_set(entity_id, STATE_OFF)
    runtime_data_before = entry.runtime_data
    sequencer = entry.runtime_data.sequencer

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    rename(hass, VALVE_1, NEW_VALVE)
    await hass.async_block_till_done()

    # Rewritten, deferred, and the running cycle still holds its snapshot.
    assert zone_of(entry, "Zone A").data[CONF_VALVE_SWITCH] == NEW_VALVE
    assert entry.runtime_data is runtime_data_before
    assert entry.runtime_data.runner.reload_pending is True
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].valve_entity_id == VALVE_1

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", NEW_VALVE),
        ("turn_on", VALVE_2),
    ]
    assert run.zone_runs[0].close_confirmed is True
    assert hass.states.get(NEW_VALVE) is not None
    assert hass.states.get(NEW_VALVE).state == STATE_OFF  # type: ignore[union-attr]

    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    # The cycle completed, the ONE reload fired, and the new engine runs on
    # the renamed valve.
    assert run.status is CycleStatus.COMPLETED
    assert entry.runtime_data is not runtime_data_before
    assert entry.runtime_data.plan.zones[0].valve_entity_id == NEW_VALVE
    assert entry.runtime_data.anomalies.open_anomalies == frozenset()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_two_renames_during_one_deferral_are_both_followed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """Renamed twice before the deferred reload lands: both rewrites, chained alias.

    The second rename dispatches on the FIRST new id, which the tracker only
    knows because it re-subscribed after the first rewrite; the pump-off at
    the end of the cycle must reach the LAST id through the chained alias.
    """
    calls = register_switch_domain(
        hass,
        extra_hardware=(NEW_PUMP, "switch.garden_pump"),
        seed_states=False,
    )
    for entity_id in (PUMP, VALVE_1, VALVE_2):
        hass.states.async_set(entity_id, STATE_OFF)
    runtime_data_before = entry.runtime_data

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    rename(hass, PUMP, NEW_PUMP)
    await hass.async_block_till_done()
    rename(hass, NEW_PUMP, "switch.garden_pump")
    await hass.async_block_till_done()

    assert entry.options[CONF_PUMP_SWITCH] == "switch.garden_pump"
    assert entry.runtime_data is runtime_data_before

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    assert (calls[-1].service, calls[-1].data[ATTR_ENTITY_ID]) == (
        "turn_off",
        "switch.garden_pump",
    )
    assert hass.states.get("switch.garden_pump") is not None
    assert hass.states.get("switch.garden_pump").state == STATE_OFF  # type: ignore[union-attr]
    assert entry.runtime_data is not runtime_data_before
    assert entry.runtime_data.plan.pump_entity_id == "switch.garden_pump"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_rename_of_a_zone_deleted_mid_cycle_is_ignored(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """A stale subscription must not raise out of a bus callback.

    Deleting a zone while a reload is deferred leaves its valve subscribed
    (the tracker only re-subscribes after a rewrite). Renaming that valve then
    fires the handler for an id the LIVE entry no longer configures — it must
    write nothing and raise nothing (an `UnknownSubEntry` here would be
    logged as an unhandled callback error).
    """
    register_switch_domain(hass, seed_states=False)
    for entity_id in (PUMP, VALVE_1, VALVE_2):
        hass.states.async_set(entity_id, STATE_OFF)
    events = record_anomalies(hass)
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    zone_b = zone_of(entry, "Zone B")
    assert hass.config_entries.async_remove_subentry(entry, zone_b.subentry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.runner.reload_pending is True

    rename(hass, VALVE_2, "switch.orphan_valve")
    await hass.async_block_till_done()

    assert events == []
    assert [zone.title for zone in entry.subentries.values()] == ["Zone A"]
    assert dict(entry.options) == CONTROLLER_OPTIONS

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Removal and disabling
# --------------------------------------------------------------------------


async def test_removing_a_configured_valve_raises_an_anomaly_and_changes_nothing(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """`configured_entity_missing` names the entity, its role and its zone.

    Never a skip: the stored id and the schedule stay exactly as they were,
    and the entry is not reloaded, disabled or removed.
    """
    events = record_anomalies(hass)
    runtime_data_before = entry.runtime_data
    zone_a = zone_of(entry, "Zone A")

    er.async_get(hass).async_remove(VALVE_1)
    await hass.async_block_till_done()

    assert [event.data["anomaly"] for event in events] == ["configured_entity_missing"]
    assert events[0].data["entity_id"] == VALVE_1
    assert events[0].data["role"] == CONF_VALVE_SWITCH
    assert events[0].data["zone_id"] == zone_a.subentry_id
    anomalies = entry.runtime_data.anomalies
    assert AnomalyKind.CONFIGURED_ENTITY_MISSING in anomalies.open_anomalies
    assert zone_of(entry, "Zone A").data[CONF_VALVE_SWITCH] == VALVE_1
    assert entry.runtime_data is runtime_data_before
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_removing_a_configured_sensor_names_its_role(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """A controller-level entity carries its option key and no zone."""
    events = record_anomalies(hass)

    er.async_get(hass).async_remove(CONTROLLER_OPTIONS[CONF_RAIN_SENSOR])
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["role"] == CONF_RAIN_SENSOR
    assert "zone_id" not in events[0].data
    assert dict(entry.options) == CONTROLLER_OPTIONS

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_zone_added_mid_cycle_is_tracked_before_the_reload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """The busy branch re-subscribes the tracker: a new zone's valve is followed.

    Without the re-subscription an entity configured during a deferral would
    be untracked until the reload, and a rename of it inside the deferral
    would be missed — leaving a dead id stored.
    """
    register_switch_domain(hass, seed_states=False)
    for entity_id in (PUMP, VALVE_1, VALVE_2):
        hass.states.async_set(entity_id, STATE_OFF)
    registry = er.async_get(hass)
    valve_c = registry.async_get_or_create(
        "switch",
        "test",
        "valve_c_uid",
        suggested_object_id="zone_c_valve",
    ).entity_id
    runtime_data_before = entry.runtime_data
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    assert hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(dict(zone_subentry_data("Zone C", valve_c)["data"])),
            subentry_type=SUBENTRY_TYPE_ZONE,
            title="Zone C",
            unique_id=None,
        ),
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is runtime_data_before
    assert entry.runtime_data.runner.reload_pending is True

    rename(hass, valve_c, "switch.side_valve")
    await hass.async_block_till_done()

    assert zone_of(entry, "Zone C").data[CONF_VALVE_SWITCH] == "switch.side_valve"
    assert entry.runtime_data is runtime_data_before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_rename_and_a_disable_in_one_update_do_both(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """One `async_update_entity` can rename AND disable: rewrite, then anomaly."""
    events = record_anomalies(hass)

    er.async_get(hass).async_update_entity(
        PUMP,
        new_entity_id=NEW_PUMP,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    await hass.async_block_till_done()

    assert entry.options[CONF_PUMP_SWITCH] == NEW_PUMP
    assert [event.data["anomaly"] for event in events] == ["configured_entity_missing"]
    assert events[0].data["entity_id"] == NEW_PUMP
    assert events[0].data["role"] == CONF_PUMP_SWITCH

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_entity_filling_two_roles_is_rewritten_in_one_write(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One sensor stored as both temperature and humidity: one write, both keys."""
    freezer.move_to("2026-07-31 06:59:00+02:00")
    registry = er.async_get(hass)
    shared = registry.async_get_or_create(
        "sensor",
        "test",
        "weather_uid",
        suggested_object_id="outdoor_weather",
    ).entity_id
    registry.async_get_or_create(
        "switch",
        "test",
        "pump_uid",
        suggested_object_id="pool_pump",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options={
            **CONTROLLER_OPTIONS,
            CONF_TEMPERATURE_SENSOR: shared,
            CONF_HUMIDITY_SENSOR: shared,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    writes: list[dict[str, Any]] = []
    real_update = hass.config_entries.async_update_entry

    def _counting(config_entry: MockConfigEntry, **kwargs: Any) -> bool:
        writes.append(kwargs)
        return real_update(config_entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _counting)

    rename(hass, shared, "sensor.garden_weather")
    await hass.async_block_till_done()

    assert len(writes) == 1
    assert entry.options[CONF_TEMPERATURE_SENSOR] == "sensor.garden_weather"
    assert entry.options[CONF_HUMIDITY_SENSOR] == "sensor.garden_weather"
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_disabling_a_configured_entity_raises_and_re_enabling_does_not(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """`changes` carries the OLD value; the registry decides: disabled NOW → anomaly."""
    events = record_anomalies(hass)
    registry = er.async_get(hass)

    registry.async_update_entity(PUMP, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()

    assert [event.data["anomaly"] for event in events] == ["configured_entity_missing"]
    assert events[0].data["entity_id"] == PUMP
    assert events[0].data["role"] == CONF_PUMP_SWITCH

    registry.async_update_entity(PUMP, disabled_by=None)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert dict(entry.options) == CONTROLLER_OPTIONS

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_other_registry_updates_are_ignored(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """An icon or name change is neither a rename nor a disappearance."""
    events = record_anomalies(hass)
    runtime_data_before = entry.runtime_data

    er.async_get(hass).async_update_entity(PUMP, name="Main pump", icon="mdi:pump")
    await hass.async_block_till_done()

    assert events == []
    assert dict(entry.options) == CONTROLLER_OPTIONS
    assert entry.runtime_data is runtime_data_before

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


async def test_the_tracker_is_unsubscribed_after_unload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """No bus listener survives the entry, and a post-unload rename writes nothing.

    PHCC's `verify_cleanup` cannot see bus listeners, so the leak is asserted
    explicitly: the `EVENT_ENTITY_REGISTRY_UPDATED` listener count returns to
    its pre-setup baseline (the keyed tracker holds one shared listener and
    drops it with its last key).
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    register_configured_entities(hass)
    baseline = hass.bus.async_listeners().get(EVENT_ENTITY_REGISTRY_UPDATED, 0)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", VALVE_1)],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.bus.async_listeners().get(EVENT_ENTITY_REGISTRY_UPDATED, 0) > baseline

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.bus.async_listeners().get(EVENT_ENTITY_REGISTRY_UPDATED, 0) == baseline
    rename(hass, PUMP, NEW_PUMP)
    await hass.async_block_till_done()
    assert entry.options[CONF_PUMP_SWITCH] == PUMP
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_stopping_the_tracker_twice_is_harmless(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """The unload path may reach the stop more than once."""
    tracker = entry.runtime_data.tracker

    tracker.async_stop()
    tracker.async_stop()

    rename(hass, PUMP, NEW_PUMP)
    await hass.async_block_till_done()
    assert entry.options[CONF_PUMP_SWITCH] == PUMP

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_configured_entities_are_read_from_the_live_entry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """Roles come from the entry at call time: every stored id, its key and zone."""
    tracker = entry.runtime_data.tracker

    assert tracker.configured_entities() == {
        PUMP: [(CONF_PUMP_SWITCH, None)],
        CONTROLLER_OPTIONS[CONF_RAIN_SENSOR]: [(CONF_RAIN_SENSOR, None)],
        CONTROLLER_OPTIONS[CONF_TEMPERATURE_SENSOR]: [(CONF_TEMPERATURE_SENSOR, None)],
        CONTROLLER_OPTIONS[CONF_HUMIDITY_SENSOR]: [(CONF_HUMIDITY_SENSOR, None)],
        VALVE_1: [(CONF_VALVE_SWITCH, zone_of(entry, "Zone A").subentry_id)],
        VALVE_2: [(CONF_VALVE_SWITCH, zone_of(entry, "Zone B").subentry_id)],
    }

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_entity_without_a_registry_entry_is_not_tracked(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """No `unique_id`, no registry entry, no events: documented, not a fault.

    A state-only entity fires no registry event when it disappears, so the
    tracker cannot see it — and its removal from the state machine must raise
    nothing (the next cycle's commands are what report it, fail-wet).
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", VALVE_1)],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    events = record_anomalies(hass)

    hass.states.async_remove(VALVE_1)
    await hass.async_block_till_done()

    assert events == []
    assert entry.runtime_data.anomalies.open_anomalies == frozenset()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
