"""The season switch: the operator's one-action end of season (FR10, AC 1).

The FIRST entity that commands. It still owns no state: `is_on` reads
`sequencer.season_enabled`, the turn_on/turn_off handlers call the runner, and
the runner is what re-arms the ONE timer and pushes the dispatcher signal the
entity re-reads on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import DOMAIN
from tests.common import (
    CONTROLLER_OPTIONS,
    PUMP,
    VALVE_1,
    fire_at,
    register_switch_domain,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant, State


def season_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Resolve the season switch from its unique_id, never from its name."""
    entity_id = er.async_get(hass).async_get_entity_id(
        Platform.SWITCH,
        DOMAIN,
        f"{entry.entry_id}_season",
    )
    assert entity_id is not None
    return entity_id


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    """Return an existing entity's state (mypy strict: never the None branch)."""
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def flip(hass: HomeAssistant, entity_id: str, *, on: bool) -> None:
    """Drive the switch through the REAL `switch.turn_on`/`turn_off` service."""
    await hass.services.async_call(
        Platform.SWITCH,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> MockConfigEntry:
    """Set up a one-zone controller just before its morning start.

    The fake hardware switches are registered by the tests that need them,
    AFTER this fixture: forwarding `Platform.SWITCH` registers the real switch
    services, and `async_register` is last-wins (see `register_switch_domain`).
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", VALVE_1)],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


async def test_the_season_switch_reports_on_by_default(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """Fail-wet: a fresh install has no stored season and therefore waters."""
    season = season_entity_id(hass, entry)

    state = state_of(hass, season)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_DEVICE_CLASS] == SwitchDeviceClass.SWITCH

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_flipping_the_switch_drives_the_one_engine_flag(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """AC 1: the switch is a command surface over the engine, not a second state."""
    season = season_entity_id(hass, entry)
    sequencer = entry.runtime_data.sequencer

    await flip(hass, season, on=False)
    assert sequencer.season_enabled is False
    assert state_of(hass, season).state == STATE_OFF

    await flip(hass, season, on=True)
    assert sequencer.season_enabled is True
    assert state_of(hass, season).state == STATE_ON

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_season_off_suspends_the_daily_start_without_an_anomaly(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """AC 2 through the real switch: no cycle, nothing queued, NO anomaly."""
    calls = register_switch_domain(hass)
    season = season_entity_id(hass, entry)

    await flip(hass, season, on=False)
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    assert not sequencer.deferred_kinds
    assert calls == []
    assert entry.runtime_data.anomalies.open_anomalies == frozenset()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_season_off_lets_a_running_cycle_finish(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """AC 1: only an explicit cancel stops a cycle — never the season switch."""
    calls = register_switch_domain(hass)
    season = season_entity_id(hass, entry)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await flip(hass, season, on=False)
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
    ]
    assert entry.runtime_data.sequencer.current_run is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_switch_follows_the_engine_conventions(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """has_entity_name, a stable unique_id, and the CONTROLLER device."""
    season = season_entity_id(hass, entry)
    registry_entry = er.async_get(hass).async_get(season)
    assert registry_entry is not None

    assert registry_entry.has_entity_name is True
    assert registry_entry.translation_key == "season"
    assert registry_entry.unique_id == f"{entry.entry_id}_season"
    # On the controller device, beside the cycle-status sensor — NOT on a zone.
    assert registry_entry.config_subentry_id is None
    assert registry_entry.device_id is not None
    device = dr.async_get(hass).async_get(registry_entry.device_id)
    # HA 2026.9 returns `DeviceEntry | ChildDeviceEntry`; ours are top-level.
    assert isinstance(device, dr.DeviceEntry)
    assert device.identifiers == {(DOMAIN, entry.entry_id)}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_switch_state_survives_a_reload(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """AC 2: the journal is the authority, so the mode outlives the entry.

    No `RestoreEntity` anywhere — the value comes back through the journal
    seed, which is why the switch can be a pure projection.
    """
    season = season_entity_id(hass, entry)
    await flip(hass, season, on=False)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.sequencer.season_enabled is False
    assert state_of(hass, season).state == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unloading_leaves_no_lingering_subscription(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """The mixin's `async_on_remove` is load-bearing (`entity-event-setup`).

    PHCC's autouse `verify_cleanup` is what fails on a leak; asserted here too
    so a regression names the switch rather than whatever test runs next.
    """
    season = season_entity_id(hass, entry)
    assert hass.states.get(season) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # The registry keeps the entity (a reload restores it); its state going
    # unavailable is what "the platform was torn down" looks like. Asserted as
    # the exact state, not merely "not on and not off": the loose form passes
    # for any string at all, including a corrupted one.
    state = state_of(hass, season)
    assert state.state == STATE_UNAVAILABLE
