"""Smoke tests: config entry sets up and unloads cleanly, version guard fails loudly."""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller import (
    HaIrrigationRuntimeData,
    _async_entry_updated,
)
from custom_components.ha_irrigation_controller.const import (
    DOMAIN,
    MAX_ZONE_DURATION_MINUTES,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
)
from custom_components.ha_irrigation_controller.engine.plan import ControllerPlan
from tests.common import CONTROLLER_OPTIONS, controller_entry, zone_subentry_data

if TYPE_CHECKING:
    import pytest
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


async def test_engine_duration_bounds_agree_with_const(hass: HomeAssistant) -> None:
    """The engine's own duration bounds match const.py's UI bounds.

    The engine cannot import const.py (it must stay importable hass-free as a
    standalone package), so the bounds are duplicated — this pins them equal:
    the const maximum loads, one past it fails.
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
