"""Shared base classes pinning the entity conventions for every platform.

Two devices, two bases: the controller (Story 1.5's cycle status, Story 1.6's
season switch) and one device per zone subentry (Story 1.5's per-zone
duration). Both restate nothing — every platform inherits the conventions,
including the ONE engine-state subscription, so no platform ever grows a
second copy of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from ..const import DOMAIN, engine_state_signal  # noqa: TID252

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry, ConfigSubentry

    from .. import HaIrrigationConfigEntry  # noqa: TID252


class _EngineStateSubscriber(Entity):
    """Mixin: subscribe to the engine-state signal, write state, nothing else.

    `entity-event-setup`: the subscription is registered in
    `async_added_to_hass` and released through `async_on_remove` — the switch's
    dispatcher handle IS entry-scoped, so PHCC's `verify_cleanup` fails on a
    missing release.

    A plain `Entity` subclass rather than a platform-specific one, so BOTH
    bases below can inherit it and every platform gets the subscription for
    free. The resulting MRO on a concrete entity is
    `<entity> → <our base> → _EngineStateSubscriber → <PlatformEntity> → Entity`:
    our base still precedes `Entity`, so its `_attr_should_poll = False` wins,
    while the `super().async_added_to_hass()` below still reaches the platform
    class normally (neither `SensorEntity` nor `SwitchEntity` overrides it).
    """

    _entry: ConfigEntry

    async def async_added_to_hass(self) -> None:
        """Connect the entity to the runner's engine-state signal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                engine_state_signal(self._entry.entry_id),
                self._async_engine_state_changed,
            ),
        )

    @callback
    def _async_engine_state_changed(self) -> None:
        """Re-read the engine — the entity owns no state of its own (AD-6)."""
        self.async_write_ha_state()


class HaIrrigationControllerEntity(_EngineStateSubscriber):
    """Base entity attached to the controller device.

    `has_entity_name` lets the frontend compose "<device name> <entity name>", and
    the name itself comes from `translation_key` — so no entity hardcodes a
    user-visible string. The unique_id is derived from the entry id and a stable
    key, never from a name the operator can change.
    """

    # Narrowed once here (the mixin's `ConfigEntry` is what the dispatcher
    # subscription needs): every platform reads `current_view(self._entry)`,
    # which wants the typed entry and its `runtime_data`.
    _entry: HaIrrigationConfigEntry

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: HaIrrigationConfigEntry, key: str) -> None:
        """Bind the entity to its config entry under a stable key."""
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )


class HaIrrigationZoneEntity(_EngineStateSubscriber):
    """Base entity attached to ONE zone's device (its config subentry, AD-8).

    Same conventions as the controller base; the unique_id additionally
    carries the subentry id — the zone key everywhere — so two zones never
    collide and a removed-and-re-added zone gets a genuinely new entity.
    """

    _entry: HaIrrigationConfigEntry

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: HaIrrigationConfigEntry,
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
