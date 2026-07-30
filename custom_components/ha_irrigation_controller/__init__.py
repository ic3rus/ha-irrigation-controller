"""HA Irrigation Controller — deterministic irrigation scheduling for Home Assistant.

For more details about this integration, please refer to
https://github.com/ic3rus/ha-irrigation-controller
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from awesomeversion import AwesomeVersion
from homeassistant.const import __version__ as HA_VERSION  # noqa: N812
from homeassistant.exceptions import ConfigEntryError

from .const import MIN_HA_VERSION

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

type HaIrrigationConfigEntry = ConfigEntry[HaIrrigationRuntimeData]


@dataclass
class HaIrrigationRuntimeData:
    """Runtime data placeholder; real engine wiring arrives in Story 1.4."""


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: HaIrrigationConfigEntry,
) -> bool:
    """Set up HA Irrigation Controller from a config entry."""
    if AwesomeVersion(HA_VERSION) < AwesomeVersion(MIN_HA_VERSION):
        msg = (
            f"HA Irrigation Controller requires Home Assistant {MIN_HA_VERSION} "
            f"or newer; this instance runs {HA_VERSION}. "
            "Please update Home Assistant before setting up this integration."
        )
        raise ConfigEntryError(msg)

    entry.runtime_data = HaIrrigationRuntimeData()
    return True


async def async_unload_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: HaIrrigationConfigEntry,  # noqa: ARG001
) -> bool:
    """Unload a config entry."""
    return True
