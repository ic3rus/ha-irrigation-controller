"""Entity-registry tracker — configured entities follow renames, never vanish.

The entry stores ENTITY IDS (`config_flow._resolve_entity_id` pins that
contract) for the pump, the sensors and every zone's valve. An entity id is
what the operator can rename, and a renamed valve would otherwise silently
break watering: the command targets a dead id, the watch times out, and the
zone files FAILED cycle after cycle. This adapter makes the convention hold:

- **renames are tracked** and the stored id is rewritten in place through the
  config-entries API (the ONE write path, AD-8), so the update listener sees
  the change exactly like an operator edit;
- **disappearance raises an anomaly** — a configured entity removed from or
  disabled in the registry is reported as `CONFIGURED_ENTITY_MISSING` with
  its role, and NOTHING else changes: the stored id and the schedule stay,
  and the next cycle's commands raise the usual `*_UNCONFIRMED` kinds
  (fail-wet, AD-4). Never a silent skip (NFR1), never a removal of the entry
  (unlike `switch_as_x`).

Why not core's `helpers.helper_integration.async_handle_source_entity_changes`:
it tracks ONE source entity per call with hardcoded semantics (removing the
entry when the source is removed, reloading on every change, moving a device
link), and a controller has a pump, up to three sensors and N valves, none of
which may take the entry down. The keyed `async_track_entity_registry_updated_event`
underneath it is the right primitive, used directly.

Roles are derived from the LIVE entry at event time, never cached: a zone
deleted while a reload is deferred leaves a stale subscription behind, and its
rename must be ignored rather than raise `UnknownSubEntry` out of a bus
callback. The subscription set is recomputed after every rewrite because the
tracker is indexed on the OLD id (renames dispatch on `old_entity_id`) and
Home Assistant does not re-index it.

Entities with no registry entry (no `unique_id`) fire no registry events and
cannot be tracked; the README says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_entity_registry_updated_event

from ..const import (  # noqa: TID252
    CONF_HUMIDITY_SENSOR,
    CONF_PUMP_SWITCH,
    CONF_RAIN_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VALVE_SWITCH,
    LOGGER,
    SUBENTRY_TYPE_ZONE,
)
from ..engine.ports import AnomalyKind  # noqa: TID252

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant
    from homeassistant.helpers.entity_registry import EventEntityRegistryUpdatedData

    from .anomalies import AnomalyManager
    from .switches import VerifiedSwitchAdapter

# The controller-level roles, in `entry.options`. Every one is an entity id
# when present; the two weather sensors are optional and simply absent when
# the operator left them empty.
_OPTION_ROLES: tuple[str, ...] = (
    CONF_PUMP_SWITCH,
    CONF_RAIN_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_HUMIDITY_SENSOR,
)

# One configured occurrence of an entity: which stored key holds it and, for a
# zone's valve, which zone (the subentry id — the zone key everywhere, AD-8).
type ConfiguredRole = tuple[str, str | None]


class ConfiguredEntityTracker:
    """Follow the entry's configured entities through the entity registry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        anomalies: AnomalyManager,
        switches: VerifiedSwitchAdapter,
    ) -> None:
        """Bind the tracker to its entry and the two adapters it drives."""
        self._hass = hass
        self._entry = entry
        self._anomalies = anomalies
        self._switches = switches
        self._unsubscribe: CALLBACK_TYPE | None = None

    def configured_entities(self) -> dict[str, list[ConfiguredRole]]:
        """Map every stored entity id to the roles it fills, from the LIVE entry.

        Computed on every call rather than cached — see the module docstring.
        Only string values count: `build_plan` validates the pump, but the
        sensor keys are not validated at load time and storage drift there
        must not turn into a bus subscription on a non-string.
        """
        roles: dict[str, list[ConfiguredRole]] = {}
        for key in _OPTION_ROLES:
            value = self._entry.options.get(key)
            if isinstance(value, str):
                roles.setdefault(value, []).append((key, None))
        for subentry in self._entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE):
            value = subentry.data.get(CONF_VALVE_SWITCH)
            if isinstance(value, str):
                roles.setdefault(value, []).append(
                    (CONF_VALVE_SWITCH, subentry.subentry_id),
                )
        return roles

    @callback
    def async_start(self) -> None:
        """Subscribe to registry updates for every configured entity id."""
        self.async_stop()
        self._unsubscribe = async_track_entity_registry_updated_event(
            self._hass,
            list(self.configured_entities()),
            self._async_registry_updated,
        )

    @callback
    def async_stop(self) -> None:
        """Drop the registry subscription, idempotently (the unload path)."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    @callback
    def _async_registry_updated(
        self,
        event: Event[EventEntityRegistryUpdatedData],
    ) -> None:
        """Dispatch one registry event: rename → rewrite, gone → anomaly."""
        data = event.data
        if data["action"] == "remove":
            self._report_missing(data["entity_id"])
        elif data["action"] == "update":
            # Independent checks: one `async_update_entity` call can rename
            # AND disable in the same event, and the disabled check runs on
            # the NEW id — the rewrite above has just made it the stored one.
            if (old_entity_id := data.get("old_entity_id")) is not None:
                self._follow_rename(old_entity_id, data["entity_id"])
            if "disabled_by" in data["changes"]:
                # `changes` carries the OLD values, so the registry itself is
                # what says whether the entity is disabled NOW. Re-enabling
                # arrives through the same key and is deliberately ignored.
                current = er.async_get(self._hass).async_get(data["entity_id"])
                if current is not None and current.disabled_by is not None:
                    self._report_missing(data["entity_id"])

    def _report_missing(self, entity_id: str) -> None:
        """Raise `CONFIGURED_ENTITY_MISSING` once per role the entity fills."""
        for role, zone_id in self.configured_entities().get(entity_id, []):
            context: dict[str, object] = {"entity_id": entity_id, "role": role}
            if zone_id is not None:
                context["zone_id"] = zone_id
            self._anomalies.report(AnomalyKind.CONFIGURED_ENTITY_MISSING, context)

    def _follow_rename(self, old_entity_id: str, new_entity_id: str) -> None:
        """Rewrite every stored occurrence of `old_entity_id`, then re-subscribe.

        The adapter alias lands FIRST so a cycle commanding the old id from
        its snapshot is already redirected when the rewrite fires the update
        listener. One `async_update_entry` for the options and one
        `async_update_subentry` per zone: each write fires the listener, and
        the listener's fingerprint check is what keeps that cheap.
        """
        roles = self.configured_entities().get(old_entity_id)
        if not roles:
            return
        self._switches.async_rename(old_entity_id, new_entity_id)
        option_keys = [key for key, zone_id in roles if zone_id is None]
        if option_keys:
            self._hass.config_entries.async_update_entry(
                self._entry,
                options={
                    **self._entry.options,
                    **dict.fromkeys(option_keys, new_entity_id),
                },
            )
        zone_ids = {zone_id for _, zone_id in roles if zone_id is not None}
        for subentry in self._entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE):
            if subentry.subentry_id in zone_ids:
                self._hass.config_entries.async_update_subentry(
                    self._entry,
                    subentry,
                    data={**subentry.data, CONF_VALVE_SWITCH: new_entity_id},
                )
        LOGGER.info(
            "Configured entity %s was renamed to %s; stored configuration updated (%s)",
            old_entity_id,
            new_entity_id,
            ", ".join(
                key if zone_id is None else f"{key} of zone {zone_id}"
                for key, zone_id in roles
            ),
        )
        self.async_start()
