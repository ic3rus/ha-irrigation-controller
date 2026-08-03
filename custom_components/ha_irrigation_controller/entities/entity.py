"""Shared base classes pinning the entity conventions for every platform.

Two devices, two bases: the controller (Story 1.5's cycle status, Story 1.6's
season switch) and one device per zone subentry (Story 1.5's per-zone
duration). Both restate nothing — every platform inherits the conventions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from ..const import DOMAIN  # noqa: TID252

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry, ConfigSubentry


class HaIrrigationControllerEntity(Entity):
    """Base entity attached to the controller device.

    `has_entity_name` lets the frontend compose "<device name> <entity name>", and
    the name itself comes from `translation_key` — so no entity hardcodes a
    user-visible string. The unique_id is derived from the entry id and a stable
    key, never from a name the operator can change.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        """Bind the entity to its config entry under a stable key."""
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )


class HaIrrigationZoneEntity(Entity):
    """Base entity attached to ONE zone's device (its config subentry, AD-8).

    Same conventions as the controller base; the unique_id additionally
    carries the subentry id — the zone key everywhere — so two zones never
    collide and a removed-and-re-added zone gets a genuinely new entity.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        key: str,
    ) -> None:
        """Bind the entity to its zone subentry under a stable key."""
        self._entry = entry
        self._subentry = subentry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{subentry.subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
        )
