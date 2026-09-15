"""Rain sensor adapter — the ONE implementation of `RainPort` (Stories 2.4, 2.5).

The engine reads the gauge through `engine.ports.RainPort` exactly once per
cycle, at quote time (`Sequencer._build_run`); this adapter is what answers.
It reads the configured precipitation entity's state synchronously and does
the two things the engine must never do (AD-1):

- VALIDATE the reading — a missing entity, `unknown`/`unavailable`, a
  non-numeric, non-finite or negative state and an unknown unit all read as
  `None`, which the engine takes as "no modulation for this cycle": full
  durations, no anomaly. Fail-wet (AD-4): a doubtful gauge never skips
  water. Everything downstream of the reading — a total that decreased
  (gauge reset), a total from another gauge, a total that jumped after a
  connectivity gap — is the ledger's arithmetic, not this adapter's: the
  clamp is the whole reset policy and there is no plausibility cap here.
- CONVERT units to millimetres — `mm` (or no unit attribute) as is, `cm`
  *10, `in` *25.4, per Home Assistant's `UnitOfPrecipitationDepth`. The
  engine speaks mm only.

The entity is whatever the operator picked in the generic precipitation
picker (`config_flow.py`, `_DEVICE_CLASS_PRECIPITATION`): any cumulative
precipitation sensor, nothing vendor-specific. Its entity id is also the
port's `source_id` (Story 2.5): the identity the ledger banks its baselines
under, so a total from a different entity never credits rain measured by the
previous one. Renames are followed by the registry tracker rewriting the
option, which reloads the entry — so this adapter is simply rebuilt with the
new id and holds no subscription of its own. The new id is a new source,
and the cost of a rename is therefore ONE banking cycle: the next cycle
waters in full and re-banks, the one after modulates. Accepted rather than
looked up in the registry: that would add a hass dependency for a case the
operator cannot notice. Every `None` is logged at DEBUG, never higher: a
transient sensor gap is not an operator-facing fault, and a gauge doubtful
for weeks is deliberately just as silent — the operator reads
`rain_total_mm: null` in history. The registry tracker's
CONFIGURED_ENTITY_MISSING on a removed or disabled sensor is the one fault
surfaced.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPrecipitationDepth,
)

from ..const import LOGGER  # noqa: TID252

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Millimetres per unit of the sensor's `unit_of_measurement`. A sensor with no
# unit attribute is taken as mm (the picker's help text asks for mm); any unit
# outside this table is unknown and reads as None rather than being guessed.
_MM_PER_UNIT: Final[dict[str, float]] = {
    UnitOfPrecipitationDepth.MILLIMETERS: 1.0,
    UnitOfPrecipitationDepth.CENTIMETERS: 10.0,
    UnitOfPrecipitationDepth.INCHES: 25.4,
}


class RainSensorAdapter:
    """Read one precipitation entity as a cumulative total in mm.

    Implements `engine.ports.RainPort`. Stateless apart from the ids it was
    built with: the state machine is the only source of the reading, and the
    engine snapshots what it is given.
    """

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        """Bind the adapter to the configured precipitation entity."""
        self._hass = hass
        self._entity_id = entity_id

    @property
    def source_id(self) -> str:
        """Return the configured entity id — the identity baselines are banked under."""
        return self._entity_id

    def total_mm(self) -> float | None:
        """Return the gauge's cumulative total in mm, or None when doubtful."""
        state = self._hass.states.get(self._entity_id)
        if state is None:
            LOGGER.debug("Rain sensor %s has no state; not modulating", self._entity_id)
            return None
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            LOGGER.debug(
                "Rain sensor %s is %s; not modulating",
                self._entity_id,
                state.state,
            )
            return None
        try:
            value = float(state.state)
        except ValueError:
            LOGGER.debug(
                "Rain sensor %s reports a non-numeric state %r; not modulating",
                self._entity_id,
                state.state,
            )
            return None
        if not math.isfinite(value) or value < 0:
            LOGGER.debug(
                "Rain sensor %s reports %r, which is not a finite non-negative "
                "total; not modulating",
                self._entity_id,
                state.state,
            )
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        factor = 1.0 if unit is None else _factor_for(unit)
        if factor is None:
            LOGGER.debug(
                "Rain sensor %s reports in unit %r, which is not a precipitation "
                "depth; not modulating",
                self._entity_id,
                unit,
            )
            return None
        return value * factor


def _factor_for(unit: object) -> float | None:
    """Return mm per `unit`, or None for a unit the adapter does not know."""
    if not isinstance(unit, str):
        return None
    return _MM_PER_UNIT.get(unit)
