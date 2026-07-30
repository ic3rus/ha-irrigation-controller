"""Smoke tests: config entry sets up and unloads cleanly, version guard fails loudly."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType

from custom_components.ha_irrigation_controller import HaIrrigationRuntimeData
from custom_components.ha_irrigation_controller.const import (
    DOMAIN,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
)
from tests.common import controller_entry

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
