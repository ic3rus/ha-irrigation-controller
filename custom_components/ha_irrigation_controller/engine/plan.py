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
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
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
    # Rain credit inputs, both read by `Ledger.quote` (Story 2.4):
    # `rain_exposed` gates the credit (a sheltered zone is never reduced) and
    # `rain_factor` is the min/mm conversion (0 disables it for this zone).
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
    # How long a hand-opened governed switch may hold the scheduler paused
    # before the engine closes it itself (Story 3.5). SECONDS here, minutes
    # at the UI and storage surface — the zone-duration convention. No
    # default on purpose: every other field is explicit, and a plan built
    # without it would silently quote a timeout nobody configured.
    manual_timeout_s: int
    zones: tuple[ZoneSpec, ...]

    def start_time(self, kind: CycleKind) -> time:
        """Return the configured start time for `kind` (HA-local)."""
        if kind is CycleKind.MORNING:
            return self.morning_start
        return self.evening_start

    def total_duration_s(self, kind: CycleKind) -> int:
        """Return the summed zone durations for `kind`, in seconds."""
        return sum(zone.duration_s(kind) for zone in self.zones)

    @property
    def governed_entities(self) -> tuple[str, ...]:
        """Return every switch this controller commands: pump, then each valve.

        THE one answer to "which switches does this controller govern"
        (Story 3.4): the manual-override watch subscribes to exactly this
        set, and the watch has to be re-armed whenever the plan is swapped.

        Ordered — pump first, then the zones in plan order — and deduplicated,
        because nothing forbids two zones sharing a valve, or a zone valve
        that IS the pump entity: a set would reorder by PYTHONHASHSEED and a
        plain tuple would subscribe twice to one entity.
        """
        ordered = dict.fromkeys(
            (self.pump_entity_id, *(zone.valve_entity_id for zone in self.zones)),
        )
        return tuple(ordered)


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
    durations: Sequence[int] | None = None,
) -> tuple[ZoneWindow, ...]:
    """Derive back-to-back zone windows for one cycle starting at `start`.

    Each zone's start is the previous zone's end — the accumulation IS the
    "no start-time arithmetic for the operator" behavior (FR1). Zone order is
    the plan's order, exactly as given.

    `durations` are the per-zone seconds to accumulate, in plan order; they
    default to the plan's own durations for `kind`. The sequencer passes the
    ledger's QUOTED durations (Story 2.2) so a carried deficit shifts every
    following zone's window — and `next_wakeup()` with it. Schedule
    derivation and overlap validation keep the default: they describe the
    plan, not a particular run.

    Durations are ELAPSED seconds, so the accumulation runs in UTC and the
    windows are converted back to `start`'s timezone. Adding a `timedelta` to a
    `ZoneInfo`-aware datetime is wall-clock arithmetic: across a spring-forward
    a 3600 s zone would span 02:30+01:00 → 03:30+02:00 and receive no water at
    all, and across a fall-back it would water twice as long.
    """
    if durations is None:
        durations = [zone.duration_s(kind) for zone in plan.zones]
    windows: list[ZoneWindow] = []
    local_tz = start.tzinfo
    cursor = start.astimezone(UTC)
    for zone, duration_s in zip(plan.zones, durations, strict=True):
        end = cursor + timedelta(seconds=duration_s)
        windows.append(
            ZoneWindow(
                zone_id=zone.zone_id,
                start=cursor.astimezone(local_tz),
                end=end.astimezone(local_tz),
            ),
        )
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


def overlap_offender(plan: ControllerPlan) -> CycleKind | None:
    """Return the cycle that runs into the other one, or None when they fit.

    Pinned rule (the contract, consumed by the config flows and pinned by
    tests for both orderings): compute BOTH cycle windows on the SAME calendar
    day — no midnight wrap — and report overlap iff the two half-open
    intervals intersect. This handles an evening start earlier than the
    morning start symmetrically, which the flows accept today (they only
    reject equality). A plan without a morning cycle never overlaps.

    The offender is the cycle that starts EARLIER: its window is the one still
    watering when the other one is due. Naming it is what lets the flows
    attach the error to a field the operator can actually change — flagging
    the morning duration when the evening window is the offender produces a
    form no edit can clear.
    """
    if not plan.morning_enabled:
        return None
    morning_start = _seconds_since_midnight(plan.morning_start)
    evening_start = _seconds_since_midnight(plan.evening_start)
    morning_end = morning_start + plan.total_duration_s(CycleKind.MORNING)
    evening_end = evening_start + plan.total_duration_s(CycleKind.EVENING)
    if max(morning_start, evening_start) >= min(morning_end, evening_end):
        return None
    return CycleKind.MORNING if morning_start <= evening_start else CycleKind.EVENING


def cycles_overlap(plan: ControllerPlan) -> bool:
    """Report whether the two cycles' derived windows collide."""
    return overlap_offender(plan) is not None


def irrigation_day(scheduled_start: datetime) -> date:
    """Return the irrigation day: HA-local calendar date of a cycle's start.

    THE single day-attribution helper (conventions): FR12's waiver, FR18's
    same-day resume and FR21's re-run window all key off this exact date.
    `scheduled_start` is aware and HA-local by contract; no conversion — a
    late-evening cycle belongs to its own local day even when UTC disagrees.
    """
    return scheduled_start.date()
