"""Schedule derivation, the pinned overlap rule and the irrigation-day helper.

AC 1: each zone's start time is derived from the one global start time by
accumulating durations, and any duration change recomputes the whole schedule
because the schedule is a pure projection of the plan (AD-6) — there is no
stored derived state to invalidate.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time, timedelta

from custom_components.ha_irrigation_controller.engine.plan import (
    CycleKind,
    cycles_overlap,
    derive_schedule,
    irrigation_day,
    zone_windows,
)
from tests.engine.common import TZ, aware, make_plan, make_zone


def test_zone_starts_accumulate_from_global_start() -> None:
    """Zones start back to back: global start, then each previous end (FR1)."""
    plan = make_plan(
        make_zone("zone-1", morning_s=600),
        make_zone("zone-2", valve="switch.zone_2_valve", morning_s=900),
    )
    schedule = derive_schedule(plan, CycleKind.MORNING, aware(6))

    assert schedule.start == aware(7)
    assert [(w.zone_id, w.start, w.end) for w in schedule.zones] == [
        ("zone-1", aware(7), aware(7, 10)),
        ("zone-2", aware(7, 10), aware(7, 25)),
    ]
    assert schedule.end == aware(7, 25)


def test_changing_a_duration_recomputes_the_whole_schedule() -> None:
    """A new plan with one changed duration shifts every later zone (AC 1)."""
    zone1 = make_zone("zone-1", morning_s=600)
    zone2 = make_zone("zone-2", valve="switch.zone_2_valve", morning_s=900)
    before = derive_schedule(make_plan(zone1, zone2), CycleKind.MORNING, aware(6))
    after = derive_schedule(
        make_plan(replace(zone1, morning_duration_s=1200), zone2),
        CycleKind.MORNING,
        aware(6),
    )

    assert before.zones[1].start == aware(7, 10)
    assert after.zones[1].start == aware(7, 20)
    assert after.zones[1].end == aware(7, 35)


def test_evening_schedule_uses_evening_start_and_durations() -> None:
    """The evening cycle derives from the evening start and evening durations."""
    plan = make_plan(make_zone("zone-1", morning_s=600, evening_s=1800))
    schedule = derive_schedule(plan, CycleKind.EVENING, aware(6))

    assert schedule.start == aware(20)
    assert schedule.zones[0].end == aware(20, 30)


def test_zone_order_is_plan_order_never_sorted() -> None:
    """Derivation preserves plan order (the subentry insertion order contract)."""
    plan = make_plan(
        make_zone("zebra", name="Zebra", valve="switch.z"),
        make_zone("alpha", name="Alpha", valve="switch.a"),
    )
    schedule = derive_schedule(plan, CycleKind.MORNING, aware(6))

    assert [w.zone_id for w in schedule.zones] == ["zebra", "alpha"]


def test_derive_schedule_uses_reference_day_and_tzinfo() -> None:
    """The reference datetime provides the calendar day AND the tzinfo."""
    plan = make_plan(make_zone())
    schedule = derive_schedule(plan, CycleKind.MORNING, aware(23, 59, day=30))

    assert schedule.start == aware(7, day=30)
    assert schedule.start.tzinfo == TZ


def test_zone_windows_accumulate_from_an_explicit_start() -> None:
    """The sequencer variant accumulates from the instant it is handed."""
    plan = make_plan(
        make_zone("zone-1", morning_s=600),
        make_zone("zone-2", valve="switch.zone_2_valve", morning_s=900),
    )
    windows = zone_windows(plan, CycleKind.MORNING, aware(7, 3))

    assert windows[0].start == aware(7, 3)
    assert windows[0].end == aware(7, 13)
    assert windows[1].end == aware(7, 28)


def test_empty_plan_schedule_is_empty() -> None:
    """A plan without zones derives an empty schedule ending at its start."""
    schedule = derive_schedule(make_plan(), CycleKind.MORNING, aware(6))

    assert schedule.zones == ()
    assert schedule.end == schedule.start


def test_no_overlap_when_the_gap_fits_the_morning_cycle() -> None:
    """Morning ending before the evening start does not overlap."""
    plan = make_plan(
        make_zone(morning_s=1200),
        morning_start=time(7, 0),
        evening_start=time(20, 0),
    )
    assert cycles_overlap(plan) is False


def test_overlap_when_morning_runs_past_evening_start() -> None:
    """A morning window crossing the evening start collides (pinned rule)."""
    plan = make_plan(
        make_zone("zone-1", morning_s=1200),
        make_zone("zone-2", valve="switch.zone_2_valve", morning_s=1200),
        morning_start=time(7, 0),
        evening_start=time(7, 30),
    )
    assert cycles_overlap(plan) is True


def test_overlap_when_evening_starts_before_morning() -> None:
    """The rule is symmetric: an earlier evening crossing morning collides.

    Today's flows accept evening_start < morning_start (only equality is
    rejected), so the same-calendar-day rule must catch this ordering too.
    """
    plan = make_plan(
        make_zone(morning_s=600, evening_s=5400),
        morning_start=time(7, 0),
        evening_start=time(6, 0),
    )
    assert cycles_overlap(plan) is True


def test_evening_before_morning_without_crossing_does_not_overlap() -> None:
    """An earlier evening whose window ends before morning start is fine."""
    plan = make_plan(
        make_zone(morning_s=600, evening_s=1800),
        morning_start=time(7, 0),
        evening_start=time(6, 0),
    )
    assert cycles_overlap(plan) is False


def test_touching_windows_do_not_overlap() -> None:
    """Half-open intervals: morning ending exactly at evening start is legal."""
    plan = make_plan(
        make_zone(morning_s=1800),
        morning_start=time(7, 0),
        evening_start=time(7, 30),
    )
    assert cycles_overlap(plan) is False


def test_disabled_morning_never_overlaps() -> None:
    """A plan without a morning cycle can never collide."""
    plan = make_plan(
        make_zone(morning_s=7200, evening_s=7200),
        morning_enabled=False,
        morning_start=time(7, 0),
        evening_start=time(7, 0),
    )
    assert cycles_overlap(plan) is False


def test_empty_plan_never_overlaps() -> None:
    """Zero zones means zero-length windows — no collision even at equal starts."""
    plan = make_plan(morning_start=time(7, 0), evening_start=time(7, 0))
    assert cycles_overlap(plan) is False


def test_irrigation_day_is_the_local_date_of_the_scheduled_start() -> None:
    """The helper reads the HA-local calendar date — no UTC conversion.

    00:30 at UTC+2 is still 22:30 the previous day in UTC; the irrigation day
    must be the LOCAL date (conventions), so a UTC conversion here would be a
    bug this test catches.
    """
    assert irrigation_day(aware(0, 30)) == date(2026, 7, 31)


def test_irrigation_day_does_no_arithmetic_of_its_own() -> None:
    """A late-evening start belongs to its own day, not the next."""
    assert irrigation_day(aware(23, 59) + timedelta(minutes=0)) == date(2026, 7, 31)
