"""Declarative plan model, schedule derivation and the irrigation-day helper.

The plan is the single source of truth (AD-6): the schedule is never stored,
it is recomputed on demand from one global start time per cycle and the
ordered per-zone durations (FR1). Zone order is the plan's order — the
subentry insertion order upstream — and is never sorted here.

Time discipline (AD-1): every datetime is handed in aware and HA-local; this
module does arithmetic on those values only — no wall-clock reads, no
timezone-database lookups.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, time


class CycleKind(StrEnum):
    """The two daily cycles a plan can schedule."""

    MORNING = "morning"
    EVENING = "evening"


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    """One zone as declared by the operator (zone key = subentry id, AD-8)."""

    zone_id: str
    name: str
    valve_entity_id: str
    morning_duration_s: int
    evening_duration_s: int
    # Rain fields ride through untouched until Epic 2 (rain credit/ledger).
    rain_exposed: bool
    rain_factor: float

    def duration_s(self, kind: CycleKind) -> int:
        """Return this zone's watering duration for `kind`, in seconds."""
        if kind is CycleKind.MORNING:
            return self.morning_duration_s
        return self.evening_duration_s


@dataclass(frozen=True, slots=True)
class ControllerPlan:
    """The declarative controller plan: start times plus ordered zones."""

    pump_entity_id: str
    morning_enabled: bool
    morning_start: time
    evening_start: time
    zones: tuple[ZoneSpec, ...]

    def start_time(self, kind: CycleKind) -> time:
        """Return the configured start time for `kind` (HA-local)."""
        if kind is CycleKind.MORNING:
            return self.morning_start
        return self.evening_start

    def total_duration_s(self, kind: CycleKind) -> int:
        """Return the summed zone durations for `kind`, in seconds."""
        return sum(zone.duration_s(kind) for zone in self.zones)


@dataclass(frozen=True, slots=True)
class ZoneWindow:
    """A zone's derived watering window within one cycle."""

    zone_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class CycleSchedule:
    """One cycle's derived schedule — a projection of the plan, never stored."""

    kind: CycleKind
    start: datetime
    zones: tuple[ZoneWindow, ...]

    @property
    def end(self) -> datetime:
        """Instant the last zone closes (== start when the plan has no zones)."""
        return self.zones[-1].end if self.zones else self.start


def zone_windows(
    plan: ControllerPlan,
    kind: CycleKind,
    start: datetime,
) -> tuple[ZoneWindow, ...]:
    """Derive back-to-back zone windows for one cycle starting at `start`.

    Each zone's start is the previous zone's end — the accumulation IS the
    "no start-time arithmetic for the operator" behavior (FR1). Zone order is
    the plan's order, exactly as given.
    """
    windows: list[ZoneWindow] = []
    cursor = start
    for zone in plan.zones:
        end = cursor + timedelta(seconds=zone.duration_s(kind))
        windows.append(ZoneWindow(zone_id=zone.zone_id, start=cursor, end=end))
        cursor = end
    return tuple(windows)


def derive_schedule(
    plan: ControllerPlan,
    kind: CycleKind,
    reference: datetime,
) -> CycleSchedule:
    """Derive the `kind` schedule on `reference`'s calendar day.

    `reference` provides the day AND the tzinfo: the configured start time is
    attached to the injected tzinfo verbatim — no timezone lookup of this
    module's own. Recompute-on-demand is the "recomputes automatically"
    behavior: there is no cache to invalidate (AD-6).
    """
    start = datetime.combine(
        reference.date(),
        plan.start_time(kind),
        tzinfo=reference.tzinfo,
    )
    return CycleSchedule(kind=kind, start=start, zones=zone_windows(plan, kind, start))


def _seconds_since_midnight(moment: time) -> int:
    """Return `moment` as whole seconds since midnight."""
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def cycles_overlap(plan: ControllerPlan) -> bool:
    """Report whether the two cycles' derived windows collide.

    Pinned rule (the contract, consumed by the config flows and pinned by
    tests for both orderings): compute BOTH cycle windows on the SAME calendar
    day — no midnight wrap — and report overlap iff the two half-open
    intervals intersect. This handles an evening start earlier than the
    morning start symmetrically, which the flows accept today (they only
    reject equality). A plan without a morning cycle never overlaps.
    """
    if not plan.morning_enabled:
        return False
    morning_start = _seconds_since_midnight(plan.morning_start)
    evening_start = _seconds_since_midnight(plan.evening_start)
    morning_end = morning_start + plan.total_duration_s(CycleKind.MORNING)
    evening_end = evening_start + plan.total_duration_s(CycleKind.EVENING)
    return max(morning_start, evening_start) < min(morning_end, evening_end)


def irrigation_day(scheduled_start: datetime) -> date:
    """Return the irrigation day: HA-local calendar date of a cycle's start.

    THE single day-attribution helper (conventions): FR12's waiver, FR18's
    same-day resume and FR21's re-run window all key off this exact date.
    `scheduled_start` is aware and HA-local by contract; no conversion — a
    late-evening cycle belongs to its own local day even when UTC disagrees.
    """
    return scheduled_start.date()
