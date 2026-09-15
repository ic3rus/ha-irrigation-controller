"""Binary sensor platform — the controller's health (Story 3.1, FR23).

ONE entity, `device_class: problem`: `on` iff at least one anomaly is open.
Health IS the set of open Repairs issues — the anomaly manager keeps that set
and pushes the engine-state signal on every open, clear and dismissal, so
this projection refreshes the moment an issue appears or the operator
acknowledges one. It reads the manager and never mutates it (AD-6): no
polling, no `RestoreEntity`, no second authority.

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

from ..adapters.anomalies import subject_keys  # noqa: TID252
from .entity import HaIrrigationControllerEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .. import HaIrrigationConfigEntry  # noqa: TID252
    from ..adapters.anomalies import AnomalyManager  # noqa: TID252

# Mirrors the root `binary_sensor.py` (the module HA's loader actually
# imports): the projection is dispatcher-pushed and never polls.
PARALLEL_UPDATES: Final = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 — platform signature
    entry: HaIrrigationConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the health entity to the controller device."""
    async_add_entities([HealthBinarySensor(entry, entry.runtime_data.anomalies)])


class HealthBinarySensor(HaIrrigationControllerEntity, BinarySensorEntity):
    """`on` while any anomaly is open; attributes say which (AD-10: coarse).

    `open_anomalies` lists `{anomaly, zone_id?, entity_id?, role?}` — the
    kind and the subject keys, the same shape as the `anomaly_cleared` bus
    payload — never the full contexts (the timeline card gets health from
    the pushed state, not from attributes). `last_anomaly` is the most
    recent report, kind plus context, kept after it clears.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        entry: HaIrrigationConfigEntry,
        anomalies: AnomalyManager,
    ) -> None:
        """Bind the projection to the controller entry and its anomaly manager."""
        super().__init__(entry, "health")
        self._anomalies = anomalies

    @property
    def is_on(self) -> bool:
        """Return True iff at least one anomaly is open."""
        return bool(self._anomalies.open_anomalies)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the open anomalies (kind + subject) and the last report."""
        last = self._anomalies.last_anomaly
        return {
            "open_anomalies": [
                {
                    "anomaly": record.kind.value,
                    **subject_keys(record.kind, record.context),
                }
                for record in self._anomalies.open_anomalies
            ],
            "last_anomaly": None
            if last is None
            else {**dict(last.context), "anomaly": last.kind.value},
        }
