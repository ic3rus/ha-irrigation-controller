"""Sensor platform — passive projections of engine state (AC 5, AD-6).

Both entities READ the sequencer and never mutate it: they command nothing,
own no timer and hold no state of their own. Updates arrive through the ONE
dispatcher signal the runner pushes after each engine step, so nothing polls
and nothing restores (`RestoreEntity` is never authoritative — AD-2).

HA's loader imports `<package>.sensor`, so the module it actually loads is the
root-level `sensor.py`; this module is what that one re-exports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTime

from ..const import SUBENTRY_TYPE_ZONE  # noqa: TID252
from ..engine.runs import CycleStatus, effective_seconds  # noqa: TID252
from .entity import HaIrrigationControllerEntity, HaIrrigationZoneEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .. import HaIrrigationConfigEntry  # noqa: TID252
    from ..engine.runs import CycleRun, ZoneRun  # noqa: TID252
    from ..engine.sequencer import Sequencer  # noqa: TID252

# The engine has no cycle at all most of the time, which is not a CycleStatus:
# an ENUM sensor must declare every state it can ever report.
STATE_IDLE = "idle"

# Mirrors the root `sensor.py` (the module HA's loader actually imports): the
# projections are dispatcher-pushed and never poll.
PARALLEL_UPDATES: Final = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 — platform signature
    entry: HaIrrigationConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the controller projection plus one projection per zone."""
    sequencer = entry.runtime_data.sequencer
    async_add_entities([CycleStatusSensor(entry, sequencer)])
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE):
        # `config_subentry_id` puts the entity on the zone's device AND makes
        # it removed with the subentry — no manual cleanup anywhere.
        async_add_entities(
            [ZoneLastWateringSensor(entry, subentry, sequencer)],
            config_subentry_id=subentry.subentry_id,
        )


class CycleStatusSensor(HaIrrigationControllerEntity, SensorEntity):
    """What the controller is doing right now: idle, or the run's status.

    The dispatcher subscription comes from the shared base (`entities/entity.py`),
    which every platform inherits — our base is FIRST so its
    `_attr_should_poll = False` and `__init__` win, while `SensorEntity`'s own
    overrides still precede `Entity`'s.
    """

    # No state_class and no unit: HA raises on either for an ENUM sensor.
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(
        self,
        entry: HaIrrigationConfigEntry,
        sequencer: Sequencer,
    ) -> None:
        """Bind the projection to the controller entry and its sequencer."""
        super().__init__(entry, "cycle_status")
        self._sequencer = sequencer
        # Every state this sensor can report MUST be listed — HA raises on an
        # unlisted one — and each listed value needs a translation entry.
        #
        # The list is deliberately wider than what today's push points can
        # actually surface: the dispatcher fires after `advance()` returns, by
        # which point PENDING has already become RUNNING and COMPLETED has
        # already released `current_run` (so a finished cycle reads `idle`).
        # Declaring the full CycleStatus keeps the sensor safe for Epic 3's
        # mid-advance pushes instead of making them a breaking change.
        # CANCELLED joins CycleStatus in Story 1.6 and lands here for free.
        self._attr_options = [STATE_IDLE, *(status.value for status in CycleStatus)]

    @property
    def native_value(self) -> str:
        """Return the active run's status, or idle when nothing is running."""
        run = self._sequencer.current_run
        return STATE_IDLE if run is None else run.status.value

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return a COARSE summary — AD-10 forbids the timeline in attributes."""
        run = self._sequencer.current_run
        zone = None if run is None else _live_zone(run)
        return {
            "cycle_id": None if run is None else run.cycle_id,
            "current_zone": None if zone is None else zone.name,
        }


class ZoneLastWateringSensor(HaIrrigationZoneEntity, SensorEntity):
    """How long this zone last actually watered, in seconds."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: HaIrrigationConfigEntry,
        subentry: ConfigSubentry,
        sequencer: Sequencer,
    ) -> None:
        """Bind the projection to one zone subentry and the sequencer."""
        super().__init__(entry, subentry, "last_watering_duration")
        self._sequencer = sequencer
        self._zone_id = subentry.subentry_id

    @property
    def native_value(self) -> int | None:
        """Return this zone's last effective watering seconds, None if never.

        The running cycle is consulted FIRST: once this zone's slot has closed
        it is fresher than `last_run`, and the operator watching a cycle
        should see the zone that just finished, not yesterday's figure.

        The value itself always comes from `effective_seconds` — the ONE
        helper Epic 2's deficit also reads (AD-5).
        """
        for run in (self._sequencer.current_run, self._sequencer.last_run):
            zone = _zone_run(run, self._zone_id)
            if zone is not None and zone.actual_end is not None:
                return effective_seconds(zone)
        return None


def _zone_run(run: CycleRun | None, zone_id: str) -> ZoneRun | None:
    """Return `zone_id`'s slot in `run`, or None when it has none."""
    if run is None:
        return None
    return next((zone for zone in run.zone_runs if zone.zone_id == zone_id), None)


def _live_zone(run: CycleRun) -> ZoneRun | None:
    """Return the zone whose slot is currently open, or None.

    Identified by "started but not finished" rather than by status: a FAILED
    zone is indistinguishable by status from a finished one, and it is still
    the live slot until its planned end (fail-wet consumes the slot, AD-4).
    """
    return next(
        (
            zone
            for zone in run.zone_runs
            if zone.actual_start is not None and zone.actual_end is None
        ),
        None,
    )
