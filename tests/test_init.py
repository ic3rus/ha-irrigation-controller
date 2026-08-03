"""Smoke tests: config entry sets up and unloads cleanly, version guard fails loudly."""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller import (
    HaIrrigationRuntimeData,
    _async_entry_updated,
)
from custom_components.ha_irrigation_controller.adapters.anomalies import AnomalyManager
from custom_components.ha_irrigation_controller.adapters.journal import JournalAdapter
from custom_components.ha_irrigation_controller.adapters.timing import CycleRunner
from custom_components.ha_irrigation_controller.const import (
    CONF_ACTUATION_TIMEOUT,
    DEFAULT_ACTUATION_TIMEOUT_S,
    DOMAIN,
    MAX_ACTUATION_TIMEOUT_S,
    MAX_RAIN_FACTOR,
    MAX_ZONE_DURATION_MINUTES,
    MIN_ACTUATION_TIMEOUT_S,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
    MIN_RAIN_FACTOR,
    MIN_ZONE_DURATION_MINUTES,
)
from custom_components.ha_irrigation_controller.engine.config import (
    PlanValidationError,
    build_plan,
    parse_actuation_timeout,
)
from custom_components.ha_irrigation_controller.engine.plan import ControllerPlan
from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer
from tests.common import CONTROLLER_OPTIONS, controller_entry, zone_subentry_data

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_MODULE = "custom_components.ha_irrigation_controller"


def _patch_ha_version(
    monkeypatch: pytest.MonkeyPatch,
    major: int,
    minor: int,
    version: str,
) -> None:
    """Pin the HA version the setup guard sees."""
    monkeypatch.setattr(f"{_MODULE}.HA_MAJOR_VERSION", major)
    monkeypatch.setattr(f"{_MODULE}.HA_MINOR_VERSION", minor)
    monkeypatch.setattr(f"{_MODULE}.HA_VERSION", version)


async def test_setup_and_unload_entry(hass: HomeAssistant) -> None:
    """A config entry sets up, exposes runtime_data, and unloads cleanly."""
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, HaIrrigationRuntimeData)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # mypy narrowed entry.state to LOADED above and cannot see that async_unload
    # mutates it, hence the ignore.
    assert entry.state is ConfigEntryState.NOT_LOADED  # type: ignore[comparison-overlap]


async def test_setup_wires_the_engine_and_its_adapters(hass: HomeAssistant) -> None:
    """runtime_data carries the engine and the four adapters (Story 1.5, AC 1-5).

    Everything here is rebuilt per load; none of it is a persistence authority
    (AD-2 — only the journal adapter writes the Store).
    """
    entry = _entry_with_zones(zone_subentry_data("Zone A", "switch.zone_a_valve"))
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    data = entry.runtime_data
    assert isinstance(data.sequencer, Sequencer)
    assert isinstance(data.runner, CycleRunner)
    assert isinstance(data.journal, JournalAdapter)
    assert isinstance(data.anomalies, AnomalyManager)
    # The engine runs on the plan the setup validated — one plan, one home.
    assert data.sequencer.plan is data.plan
    assert data.sequencer.current_run is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unload_removes_the_platform_entities_and_leaves_no_timer(
    hass: HomeAssistant,
) -> None:
    """Unload tears the platform down; PHCC's verify_cleanup covers the timers.

    A surviving daily `async_track_time_change` registration would leak on
    every reload, and the reload-per-config-change regime runs constantly.
    """
    entry = _entry_with_zones(zone_subentry_data("Zone A", "switch.zone_a_valve"))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_cycle_status",
    )
    live = hass.states.async_all("sensor")
    assert len(live) == 2  # controller status + the zone's duration
    assert all(state.state != STATE_UNAVAILABLE for state in live)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    # The registry keeps the entities (a reload restores them); their states
    # go unavailable, which is what "the platform was torn down" looks like.
    assert all(
        state.state == STATE_UNAVAILABLE for state in hass.states.async_all("sensor")
    )


async def test_setup_registers_the_reload_listener(hass: HomeAssistant) -> None:
    """Setup registers exactly one update listener: the reload-on-change seam.

    Subentry add/edit/remove (including the UI delete button) only fire update
    listeners — this listener is the ONLY path by which those changes take
    effect without a restart (AC 5). It is unregistered on unload.
    """
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.update_listeners == [_async_entry_updated]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.update_listeners == []


async def test_setup_creates_the_controller_device(hass: HomeAssistant) -> None:
    """Setup registers the controller device zones will hang off later (AC 3)."""
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, entry.entry_id)},
    )
    assert device is not None
    assert device.entry_type is DeviceEntryType.SERVICE
    assert device.name == "Irrigation Controller"
    assert device.manufacturer == "ha-irrigation-controller"
    assert entry.entry_id in device.config_entries


def _entry_with_zones(*zones: Any) -> MockConfigEntry:
    """Build a controller entry carrying stored zone subentries."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=list(zones),
    )


async def test_setup_builds_the_validated_plan_into_runtime_data(
    hass: HomeAssistant,
) -> None:
    """Setup turns the stored config into the typed engine plan (Story 1.4, AC 4).

    Built through the real const.py-keyed storage shapes, this also pins that
    the engine builder's literal key strings agree with const.py.
    """
    entry = _entry_with_zones(
        zone_subentry_data("Zone A", "switch.zone_a_valve"),
        zone_subentry_data("Zone B", "switch.zone_b_valve"),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    plan = entry.runtime_data.plan
    assert isinstance(plan, ControllerPlan)
    assert plan.pump_entity_id == "switch.pool_pump"
    assert plan.morning_enabled is True
    assert plan.morning_start == time(7, 0)
    assert plan.evening_start == time(20, 0)
    # Insertion order preserved; zone key is the subentry id; minutes → seconds.
    assert [zone.name for zone in plan.zones] == ["Zone A", "Zone B"]
    assert [zone.zone_id for zone in plan.zones] == [
        subentry.subentry_id for subentry in entry.subentries.values()
    ]
    zone = plan.zones[0]
    assert zone.valve_entity_id == "switch.zone_a_valve"
    assert zone.morning_duration_s == 600
    assert zone.evening_duration_s == 900
    assert zone.rain_exposed is True
    assert zone.rain_factor == 1.0


async def test_setup_fails_loudly_on_malformed_stored_zone(
    hass: HomeAssistant,
) -> None:
    """Malformed .storage data (a restored backup, a hand edit) fails the setup.

    Load-time validation was explicitly deferred from the 1.3 review to this
    story: a zone the engine cannot water correctly must fail loudly (AD-4),
    never be skipped silently.
    """
    entry = _entry_with_zones(
        zone_subentry_data("Zone A", "switch.zone_a_valve", morning_duration="ten"),
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    # The reason names the offending zone and key so the operator can fix it.
    assert "morning_duration" in entry.reason
    assert "Zone A" in entry.reason


@pytest.mark.parametrize(
    ("key", "inside", "outside"),
    [
        ("morning_duration", MIN_ZONE_DURATION_MINUTES, MIN_ZONE_DURATION_MINUTES - 1),
        ("morning_duration", MAX_ZONE_DURATION_MINUTES, MAX_ZONE_DURATION_MINUTES + 1),
        ("evening_duration", MIN_ZONE_DURATION_MINUTES, MIN_ZONE_DURATION_MINUTES - 1),
        ("evening_duration", MAX_ZONE_DURATION_MINUTES, MAX_ZONE_DURATION_MINUTES + 1),
        ("rain_factor", MIN_RAIN_FACTOR, MIN_RAIN_FACTOR - 0.1),
        ("rain_factor", MAX_RAIN_FACTOR, MAX_RAIN_FACTOR + 0.1),
    ],
)
def test_every_engine_bound_agrees_with_const(
    key: str,
    inside: float,
    outside: float,
) -> None:
    """Each const.py bound is exactly the engine's own bound, on both edges.

    The engine cannot import const.py (it must stay importable hass-free as a
    standalone package), so all four bounds are duplicated there. Pinning only
    one of them — as this suite did until the 1.4 review — leaves the other
    three free to drift apart from the UI selectors silently.
    """
    zone = dict(zone_subentry_data("Zone A", "switch.zone_a_valve")["data"])

    build_plan(CONTROLLER_OPTIONS, [("zone-1", "Zone A", {**zone, key: inside})])

    with pytest.raises(PlanValidationError, match=key):
        build_plan(CONTROLLER_OPTIONS, [("zone-1", "Zone A", {**zone, key: outside})])


@pytest.mark.parametrize(
    ("inside", "outside"),
    [
        (MIN_ACTUATION_TIMEOUT_S, MIN_ACTUATION_TIMEOUT_S - 1),
        (MAX_ACTUATION_TIMEOUT_S, MAX_ACTUATION_TIMEOUT_S + 1),
    ],
)
def test_actuation_timeout_bounds_agree_with_const(inside: int, outside: int) -> None:
    """The timeout bounds and default duplicated in the engine match const.py.

    Same drift defense as the four zone bounds above: the engine cannot import
    const.py, so its module-private twins are pinned here on both edges.
    """
    options = {**CONTROLLER_OPTIONS, CONF_ACTUATION_TIMEOUT: inside}
    assert parse_actuation_timeout(options) == inside

    with pytest.raises(PlanValidationError, match=CONF_ACTUATION_TIMEOUT):
        parse_actuation_timeout({**CONTROLLER_OPTIONS, CONF_ACTUATION_TIMEOUT: outside})


def test_actuation_timeout_default_agrees_with_const() -> None:
    """An absent key parses to exactly the const.py default (legacy entries)."""
    options = {
        key: value
        for key, value in CONTROLLER_OPTIONS.items()
        if key != CONF_ACTUATION_TIMEOUT
    }
    assert parse_actuation_timeout(options) == DEFAULT_ACTUATION_TIMEOUT_S


async def test_entry_without_actuation_timeout_still_loads(
    hass: HomeAssistant,
) -> None:
    """An entry created before Story 1.5 has no timeout key and must keep loading."""
    options = {
        key: value
        for key, value in CONTROLLER_OPTIONS.items()
        if key != CONF_ACTUATION_TIMEOUT
    }
    entry = controller_entry(options)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_engine_duration_bounds_agree_with_const(hass: HomeAssistant) -> None:
    """The const maximum loads through the real setup path, one past it fails.

    The bound values themselves are pinned above; this one proves the builder
    the entry actually goes through is the one carrying them.
    """
    at_max = _entry_with_zones(
        zone_subentry_data(
            "Zone A",
            "switch.zone_a_valve",
            morning_duration=MAX_ZONE_DURATION_MINUTES,
        ),
    )
    at_max.add_to_hass(hass)
    assert await hass.config_entries.async_setup(at_max.entry_id)
    await hass.async_block_till_done()
    assert at_max.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(at_max.entry_id)
    await hass.async_block_till_done()

    past_max = _entry_with_zones(
        zone_subentry_data(
            "Zone B",
            "switch.zone_b_valve",
            morning_duration=MAX_ZONE_DURATION_MINUTES + 1,
        ),
    )
    past_max.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(past_max.entry_id)
    await hass.async_block_till_done()
    assert past_max.state is ConfigEntryState.SETUP_ERROR


async def test_setup_fails_loudly_below_min_ha_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the minimum HA version, setup raises ConfigEntryError (AC 3)."""
    _patch_ha_version(monkeypatch, 2026, 6, "2026.6.4")
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    assert MIN_HA_VERSION in entry.reason
    assert "2026.6.4" in entry.reason


async def test_setup_succeeds_at_exact_min_ha_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At exactly the minimum HA version, setup succeeds (boundary check)."""
    _patch_ha_version(monkeypatch, MIN_HA_MAJOR, MIN_HA_MINOR, MIN_HA_VERSION)
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_succeeds_on_prerelease_of_min_ha_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A beta of the minimum month release is supported, not rejected.

    Regression guard: a full-string comparison sorts 2026.7.0b5 BELOW 2026.7.0
    and locked beta-channel users out of the exact minimum release.
    """
    _patch_ha_version(
        monkeypatch,
        MIN_HA_MAJOR,
        MIN_HA_MINOR,
        f"{MIN_HA_MAJOR}.{MIN_HA_MINOR}.0b5",
    )
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
