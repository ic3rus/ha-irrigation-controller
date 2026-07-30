"""Tests for the minimal confirm-step config flow (real flow: Story 1.2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import DOMAIN

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant

_FLOW_MODULE = "custom_components.ha_irrigation_controller.config_flow"


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """The confirm step shows a form, then creates an entry with empty data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Irrigation Controller"
    assert result["data"] == {}


async def test_user_flow_aborts_when_already_configured(hass: HomeAssistant) -> None:
    """A second flow aborts — one controller entry only.

    Enforced by `single_config_entry` in manifest.json, which makes HA hide the
    entry point rather than offering "create" and aborting afterwards.
    """
    MockConfigEntry(domain=DOMAIN, title="Irrigation Controller", data={}).add_to_hass(
        hass,
    )

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
        "required": "2026.7.0",
        "running": "2026.6.4",
    }
