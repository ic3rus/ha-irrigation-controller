"""Shared test doubles and builders for the hass-free engine suite (AC 5).

Everything here is plain stdlib plus the engine's own types: no Home
Assistant, no PHCC. The fakes record what the engine commands so tests can
assert ordering and payloads.
"""

from __future__ import annotations

import copy
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

from custom_components.ha_irrigation_controller.engine.plan import (
    ControllerPlan,
    ZoneSpec,
)

if TYPE_CHECKING:
    from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind

# The engine contract says "aware, HA-local". A fixed offset keeps the suite
# independent of the host tz database while still catching naive/aware bugs.
TZ = timezone(timedelta(hours=2))


def aware(
    hour: int,
    minute: int = 0,
    second: int = 0,
    *,
    day: int = 31,
) -> datetime:
    """Return an aware HA-local datetime on 2026-07-`day`."""
    return datetime(2026, 7, day, hour, minute, second, tzinfo=TZ)


class VirtualClock:
    """Satisfies the Clock port; time moves only when a test advances it."""

    def __init__(self, start: datetime) -> None:
        """Start the clock at `start`."""
        self._now = start

    def now(self) -> datetime:
        """Return the current virtual time."""
        return self._now

    def advance_to(self, moment: datetime) -> None:
        """Jump the virtual clock to `moment`."""
        self._now = moment


class FakeSwitchPort:
    """Records every command; commands listed in `failing` never confirm."""

    def __init__(self) -> None:
        """Start with an empty command log and nothing failing."""
        self.commands: list[tuple[str, str]] = []
        self.failing: set[tuple[str, str]] = set()

    async def async_turn_on(self, entity_id: str) -> bool:
        """Record the on command and report its confirmation outcome."""
        self.commands.append(("on", entity_id))
        return ("on", entity_id) not in self.failing

    async def async_turn_off(self, entity_id: str) -> bool:
        """Record the off command and report its confirmation outcome."""
        self.commands.append(("off", entity_id))
        return ("off", entity_id) not in self.failing


class FakeJournalPort:
    """Records a deep copy of every saved snapshot (the engine mutates state)."""

    def __init__(self) -> None:
        """Start with an empty snapshot log."""
        self.snapshots: list[dict[str, object]] = []

    async def async_save(self, snapshot: dict[str, object]) -> None:
        """Record the snapshot as it was at save time."""
        self.snapshots.append(copy.deepcopy(snapshot))


class FakeAnomalyPort:
    """Records every reported anomaly."""

    def __init__(self) -> None:
        """Start with an empty report log."""
        self.reports: list[tuple[AnomalyKind, dict[str, object]]] = []

    def report(self, kind: AnomalyKind, context: dict[str, object]) -> None:
        """Record the anomaly kind and its context payload."""
        self.reports.append((kind, dict(context)))


def make_zone(  # noqa: PLR0913 — one keyword per spec field is the readable shape
    zone_id: str = "zone-1",
    *,
    name: str = "Front Lawn",
    valve: str = "switch.zone_1_valve",
    morning_s: int = 600,
    evening_s: int = 900,
    rain_exposed: bool = True,
    rain_factor: float = 1.0,
) -> ZoneSpec:
    """Build a zone spec with representative defaults."""
    return ZoneSpec(
        zone_id=zone_id,
        name=name,
        valve_entity_id=valve,
        morning_duration_s=morning_s,
        evening_duration_s=evening_s,
        rain_exposed=rain_exposed,
        rain_factor=rain_factor,
    )


def make_plan(
    *zones: ZoneSpec,
    pump: str = "switch.pool_pump",
    morning_enabled: bool = True,
    morning_start: time = time(7, 0),
    evening_start: time = time(20, 0),
) -> ControllerPlan:
    """Build a controller plan with representative defaults."""
    return ControllerPlan(
        pump_entity_id=pump,
        morning_enabled=morning_enabled,
        morning_start=morning_start,
        evening_start=evening_start,
        zones=zones,
    )
