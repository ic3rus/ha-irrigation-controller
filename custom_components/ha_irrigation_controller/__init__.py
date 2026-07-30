"""HA Irrigation Controller — deterministic irrigation scheduling for Home Assistant.

For more details about this integration, please refer to
https://github.com/ic3rus/ha-irrigation-controller
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import (
    MAJOR_VERSION as HA_MAJOR_VERSION,
    MINOR_VERSION as HA_MINOR_VERSION,
    __version__ as HA_VERSION,  # noqa: N812
)
from homeassistant.exceptions import ConfigEntryError

from .const import DOMAIN, MIN_HA_MAJOR, MIN_HA_MINOR, MIN_HA_VERSION

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
    # Compared on (major, minor) so prereleases of the minimum month release are
    # accepted; passing no message lets HA render the English text from
    # translations/en.json, so entry.reason stays informative AND translatable.
    if (HA_MAJOR_VERSION, HA_MINOR_VERSION) < (MIN_HA_MAJOR, MIN_HA_MINOR):
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="unsupported_ha_version",
            translation_placeholders={
                "required": MIN_HA_VERSION,
                "running": HA_VERSION,
            },
        )

    entry.runtime_data = HaIrrigationRuntimeData()
    return True


async def async_unload_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: HaIrrigationConfigEntry,  # noqa: ARG001
) -> bool:
    """Unload a config entry."""
    return True
