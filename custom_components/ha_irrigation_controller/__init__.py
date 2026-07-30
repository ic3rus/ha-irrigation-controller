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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType

from .const import DOMAIN, MIN_HA_MAJOR, MIN_HA_MINOR, MIN_HA_VERSION

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

type HaIrrigationConfigEntry = ConfigEntry[HaIrrigationRuntimeData]


@dataclass
class HaIrrigationRuntimeData:
    """Runtime data placeholder; real engine wiring arrives in Story 1.4."""


async def async_setup_entry(
    hass: HomeAssistant,
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

    # The ONE update listener of this integration: every config change —
    # options edit, zone subentry add/edit/remove (including UI deletion, which
    # never touches flow code) — fires it, and it schedules a reload so the
    # change applies without restarting HA (FR8). No flow performs its own
    # reload; Story 1.7 will teach this seam to defer while a cycle runs.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    device_registry = dr.async_get(hass)
    # The controller is a virtual service device; zone devices link to it below.
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer="ha-irrigation-controller",
        name="Irrigation Controller",
    )

    # One device per zone subentry, keyed by the subentry id (the zone key
    # everywhere, AD-8). Removal needs no manual cleanup: async_remove_subentry
    # clears the subentry's devices and entities from both registries itself.
    for subentry in entry.subentries.values():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry.subentry_id,
            identifiers={(DOMAIN, subentry.subentry_id)},
            via_device=(DOMAIN, entry.entry_id),
            manufacturer="ha-irrigation-controller",
            name=subentry.title,
        )

    entry.runtime_data = HaIrrigationRuntimeData()
    return True


async def _async_entry_updated(
    hass: HomeAssistant,
    entry: HaIrrigationConfigEntry,
) -> None:
    """Reload the entry on any config change — the restart-free half of FR8."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: HaIrrigationConfigEntry,  # noqa: ARG001
) -> bool:
    """Unload a config entry."""
    return True
