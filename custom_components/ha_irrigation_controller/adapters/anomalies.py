"""Anomaly-manager seed — the ONE implementation of `AnomalyPort` (AD-9 seed).

Explicitly OUT of Story 1.5 (Story 3.1 upgrades this class IN PLACE, keeping
the seam): Repairs issues, the notify service, the anomaly dismissal and
supersession lifecycle, and the WS health push. This seed does exactly three
things per report — log a warning, fire the single bus event, and keep the
in-memory record the health projection reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from ..const import EVENT_HA_IRRIGATION_CONTROLLER, LOGGER  # noqa: TID252

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

    from ..engine.ports import AnomalyKind  # noqa: TID252


@dataclass(frozen=True, slots=True)
class AnomalyRecord:
    """One reported anomaly: its kind and the context it was raised with.

    `frozen=True` protects the field binding, not what it points at — so the
    context is a read-only view, or a caller reading the health projection
    could mutate the record it is only supposed to observe.
    """

    kind: AnomalyKind
    context: Mapping[str, object]


class AnomalyManager:
    """Fan one anomaly report out to log, bus and the in-memory health record.

    `report` is synchronous (the port is), so everything here must stay
    event-loop-safe callback work — no awaits, no I/O.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the manager to hass for the bus fan-out."""
        self._hass = hass
        self._open: set[AnomalyKind] = set()
        self._last: AnomalyRecord | None = None

    @property
    def open_anomalies(self) -> frozenset[AnomalyKind]:
        """Return the kinds reported so far, read-only (health projection)."""
        return frozenset(self._open)

    @property
    def last_anomaly(self) -> AnomalyRecord | None:
        """Return the most recent report, read-only (health projection)."""
        return self._last

    def report(self, kind: AnomalyKind, context: dict[str, object]) -> None:
        """Report one anomaly: warn, fire the single bus event, record it."""
        LOGGER.warning("Anomaly %s: %s", kind.value, context)
        # The caller's context is spread FIRST: `event_type` is the single
        # discriminator every consumer dispatches on (Story 3.1 fans it out to
        # Repairs and notify), so a context key of the same name must lose to
        # it, never silently replace it.
        self._hass.bus.async_fire(
            EVENT_HA_IRRIGATION_CONTROLLER,
            {**context, "event_type": "anomaly", "anomaly": kind.value},
        )
        self._open.add(kind)
        self._last = AnomalyRecord(
            kind=kind,
            context=MappingProxyType(dict(context)),
        )
