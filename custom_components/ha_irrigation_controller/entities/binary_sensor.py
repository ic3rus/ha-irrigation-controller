"""Binary sensor platform — the controller's health (Story 3.1, FR23).

ONE entity, `device_class: problem`: `on` iff at least one anomaly is open.
Health IS the set of open Repairs issues — the anomaly manager keeps that set
and pushes the engine-state signal on every open, clear and dismissal, so
this projection refreshes the moment an issue appears or the operator
acknowledges one. Since Story 4.1 it reads the `health` section of the ONE
state view (`state_view.current_view`, AD-14) — the same document the
timeline card receives — and never mutates anything (AD-6): no polling, no
`RestoreEntity`, no second authority.

HA's loader imports `<package>.binary_sensor`, so the module it actually
loads is the root-level `binary_sensor.py`; this module is what that one
re-exports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from ..state_view import current_view  # noqa: TID252
from .entity import HaIrrigationControllerEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .. import HaIrrigationConfigEntry  # noqa: TID252

# Mirrors the root `binary_sensor.py` (the module HA's loader actually
# imports): the projection is dispatcher-pushed and never polls.
PARALLEL_UPDATES: Final = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 — platform signature
    entry: HaIrrigationConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the health entity to the controller device."""
    async_add_entities([HealthBinarySensor(entry)])


class HealthBinarySensor(HaIrrigationControllerEntity, BinarySensorEntity):
    """`on` while any anomaly is open; attributes say which (AD-10: coarse).

    `open_anomalies` lists `{anomaly, zone_id?, entity_id?, role?}` — the
    kind and the subject keys, the same shape as the `anomaly_cleared` bus
    payload — never the full contexts (the timeline card gets health from
    the pushed state, which carries exactly these two values as
    `health.open` and `health.last`). `last_anomaly` is the most recent
    report, kind plus context, kept after it clears.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, entry: HaIrrigationConfigEntry) -> None:
        """Bind the projection to the controller entry whose view it reads."""
        super().__init__(entry, "health")

    @property
    def is_on(self) -> bool:
        """Return True iff at least one anomaly is open."""
        return bool(current_view(self._entry)["health"]["open"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the open anomalies (kind + subject) and the last report."""
        health = current_view(self._entry)["health"]
        return {
            "open_anomalies": health["open"],
            "last_anomaly": health["last"],
        }
