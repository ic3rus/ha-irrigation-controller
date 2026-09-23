"""Sensor platform — passive projections of the engine STATE VIEW (AC 5, AD-6).

Both entities READ the one state view (`state_view.current_view`, built by
`engine/view.py`, AD-14) and never mutate anything: they command nothing,
own no timer and hold no state of their own. Since Story 4.1 no entity reads
an engine object directly — the view is the only projection contract, and
the same document is what the WebSocket channel pushes. Updates arrive
through the ONE dispatcher signal the runner pushes after each engine step,
so nothing polls and nothing restores (`RestoreEntity` is never
authoritative — AD-2).

The attributes stay COARSE (AD-10): a handful of scalars per entity, never a
section of the document — the recorder silently drops attribute sets above
16 KB, and the timeline belongs on the WebSocket channel.

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
from ..engine.runs import CycleStatus, ZoneRunStatus  # noqa: TID252
from ..state_view import current_view  # noqa: TID252
from .entity import HaIrrigationControllerEntity, HaIrrigationZoneEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .. import HaIrrigationConfigEntry  # noqa: TID252
    from ..engine.view import RunView, StateView, ZoneRunView  # noqa: TID252

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
    async_add_entities([CycleStatusSensor(entry)])
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE):
        # `config_subentry_id` puts the entity on the zone's device AND makes
        # it removed with the subentry — no manual cleanup anywhere.
        async_add_entities(
            [ZoneLastWateringSensor(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


def _zone_in(run: RunView, zone_id: str) -> ZoneRunView | None:
    """Return `zone_id`'s slot in the run view, or None when it has none."""
    return next((zone for zone in run["zones"] if zone["zone_id"] == zone_id), None)


class CycleStatusSensor(HaIrrigationControllerEntity, SensorEntity):
    """What the controller is doing right now: idle, or the run's status.

    The dispatcher subscription comes from the shared base (`entities/entity.py`),
    which every platform inherits — our base is FIRST so its
    `_attr_should_poll = False` and `__init__` win, while `SensorEntity`'s own
    overrides still precede `Entity`'s.
    """

    # No state_class and no unit: HA raises on either for an ENUM sensor.
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, entry: HaIrrigationConfigEntry) -> None:
        """Bind the projection to the controller entry whose view it reads."""
        super().__init__(entry, "cycle_status")
        # Every state this sensor can report MUST be listed — HA raises on an
        # unlisted one — and each listed value needs a translation entry.
        #
        # The list is deliberately wider than what today's push points can
        # actually surface: the dispatcher fires after `advance()` returns, by
        # which point PENDING has already become RUNNING and COMPLETED has
        # already released `current_run` (so a finished cycle reads `idle`).
        # Declaring the full CycleStatus keeps the sensor safe for Epic 3's
        # mid-advance pushes instead of making them a breaking change.
        # CANCELLED (Story 1.6), WAIVED (Story 2.3) and INTERRUPTED (Story
        # 3.2) join CycleStatus and land here for free — none of the three
        # can be the state either (a waived cycle is never `current_run`, a
        # cancelled or interrupted one is released before the push), but each
        # needs its translation.
        self._attr_options = [STATE_IDLE, *(status.value for status in CycleStatus)]

    @property
    def native_value(self) -> str:
        """Return the active run's status, or idle when nothing is running."""
        run = current_view(self._entry)["runs"]["current"]
        return STATE_IDLE if run is None else run["status"]

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        """Return a COARSE summary — AD-10 forbids the timeline in attributes.

        `config_change_pending` is the runner's deferred-reload flag (Story
        1.7): true from the moment a config edit lands during a cycle until
        the cycle completes and the reload fires. A projection pushed by the
        same dispatcher signal as everything else — no new entity.

        `day_credit` (Story 2.3) is the irrigation day a completed run-now
        has already watered — the ISO date, or None — read from the ledger,
        never written (AD-6). It is the operator's only view of why the next
        scheduled cycle of that day will be waived; it clears the moment that
        decision is made. Same coarse-attribute precedent as Story 2.2.

        `manual_override` (Story 3.4) is true while the operator holds a
        governed switch open by hand: the scheduler starts nothing, and a
        start falling inside the pause is queued rather than skipped. It is
        the ONE operator-visible surface of the pause — no new entity, no
        anomaly, no notification, because a pause is normal operation. It
        goes false again the moment the last hand-opened switch closes.

        `current_zone` is the name of the view's `live_zone_id` — the
        engine's one reading of "which slot is open" (Story 4.1).
        """
        view = current_view(self._entry)
        run = view["runs"]["current"]
        live_zone_id = None if run is None else run["live_zone_id"]
        zone = (
            None if run is None or live_zone_id is None else _zone_in(run, live_zone_id)
        )
        return {
            "cycle_id": None if run is None else run["cycle_id"],
            "current_zone": None if zone is None else zone["name"],
            "config_change_pending": view["controller"]["config_change_pending"],
            "day_credit": view["ledger"]["day_credit"],
            "manual_override": view["controller"]["manual_override"],
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
    ) -> None:
        """Bind the projection to one zone subentry of the entry whose view it reads."""
        super().__init__(entry, subentry, "last_watering_duration")
        self._zone_id = subentry.subentry_id

    @property
    def native_value(self) -> int | None:
        """Return this zone's last effective watering seconds, None if never.

        The value is the view's `effective_s`, which the engine computes with
        `effective_seconds` — the ONE helper Epic 2's deficit also reads
        (AD-5) — for the slot `_shown_zone` selects.
        """
        zone = self._shown_zone(current_view(self._entry))
        return None if zone is None else zone["effective_s"]

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Return the zone's water debt and rain credit, COARSE (AD-10).

        `carried_deficit` is the deficit the ledger applied to the run this
        sensor is showing — the SAME slot `native_value` reads, so the two
        always describe one run — and `pending_deficit` is what the ledger
        still holds for this zone right now. `rain_credit` (Story 2.4) is
        the seconds of rain credit the ledger subtracted from that same run
        — the operator's view of why a zone watered less, or not at all
        (NFR8). All three read 0 when there is nothing to show: a zone that
        never watered, owes nothing and was credited nothing has no debt and
        no credit, not unknown ones. The ledger is READ here, never written
        (AD-6).
        """
        view = current_view(self._entry)
        zone = self._shown_zone(view)
        ledger_zone = view["ledger"]["zones"].get(self._zone_id)
        return {
            "carried_deficit": 0 if zone is None else zone["carried_s"],
            "pending_deficit": 0 if ledger_zone is None else ledger_zone["deficit_s"],
            "rain_credit": 0 if zone is None else zone["rain_credit_s"],
        }

    def _shown_zone(self, view: StateView) -> ZoneRunView | None:
        """Return the slot this sensor reports on, or None when it has none.

        The running cycle is consulted FIRST: once this zone's slot has closed
        it is fresher than `last_run`, and the operator watching a cycle
        should see the zone that just finished, not yesterday's figure.

        Three cases where a zone with no `actual_end` is still an ANSWER
        rather than missing data — all water zero seconds, which is what the
        README documents and what `effective_s` reads for them, and any
        would otherwise flap a MEASUREMENT sensor to `unknown` and pollute
        its long-term statistics:

        - a CANCELLED run: the cancel stopped the cycle before that zone's
          slot;
        - an INTERRUPTED run (Story 3.2): the startup reconciler closed the
          cycle before that zone's slot — its shortfall is in
          `pending_deficit`;
        - a SKIPPED zone (Story 2.4): rain covered its whole base, so the
          slot was never commanded — the `rain_credit` attribute says why.

        `last_run` survives a reload since Story 3.2 (the journal restores
        it), so a reload no longer resets this sensor to `unknown`.
        """
        runs = view["runs"]
        for run in (runs["current"], runs["last"]):
            if run is None:
                continue
            zone = _zone_in(run, self._zone_id)
            if zone is not None and (
                zone["actual_end"] is not None
                or run["status"] in (CycleStatus.CANCELLED, CycleStatus.INTERRUPTED)
                or zone["status"] == ZoneRunStatus.SKIPPED
            ):
                return zone
        return None
