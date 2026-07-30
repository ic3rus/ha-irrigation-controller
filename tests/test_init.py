"""Smoke tests: config entry sets up and unloads cleanly, version guard fails loudly."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import DOMAIN, MIN_HA_VERSION

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant


async def test_setup_and_unload_entry(hass: HomeAssistant) -> None:
    """A config entry sets up, exposes runtime_data, and unloads cleanly."""
    entry = MockConfigEntry(domain=DOMAIN, title="Irrigation Controller", data={})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_fails_loudly_below_min_ha_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the minimum HA version, setup raises ConfigEntryError (AC 3)."""
    monkeypatch.setattr(
        "custom_components.ha_irrigation_controller.HA_VERSION",
        "2026.6.4",
    )
    entry = MockConfigEntry(domain=DOMAIN, title="Irrigation Controller", data={})
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
    monkeypatch.setattr(
        "custom_components.ha_irrigation_controller.HA_VERSION",
        MIN_HA_VERSION,
    )
    entry = MockConfigEntry(domain=DOMAIN, title="Irrigation Controller", data={})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
