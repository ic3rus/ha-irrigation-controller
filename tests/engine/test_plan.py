"""Schedule derivation, the pinned overlap rule and the irrigation-day helper.

AC 1: each zone's start time is derived from the one global start time by
accumulating durations, and any duration change recomputes the whole schedule
because the schedule is a pure projection of the plan (AD-6) — there is no
stored derived state to invalidate.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.ha_irrigation_controller.engine.plan import (
    CycleKind,
    cycles_overlap,
    derive_schedule,
    irrigation_day,
    overlap_offender,
    zone_windows,
)
from tests.engine.common import TZ, aware, make_plan, make_zone

# A real tz database zone, used ONLY by the DST tests below: the rest of the
# suite runs on a fixed offset, which by construction cannot expose wall-clock
# vs elapsed-time bugs.
PARIS = ZoneInfo("Europe/Paris")


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


def test_overlap_offender_is_the_cycle_that_starts_first() -> None:
    """The offender is the window still watering when the other cycle is due.

    The flows attach the error to the offender's field; naming the wrong one
    produces a form the operator cannot clear by editing the flagged value.
    """
    morning_first = make_plan(
        make_zone("zone-1", morning_s=1200),
        make_zone("zone-2", valve="switch.zone_2_valve", morning_s=1200),
        morning_start=time(7, 0),
        evening_start=time(7, 30),
    )
    assert overlap_offender(morning_first) is CycleKind.MORNING

    evening_first = make_plan(
        make_zone(morning_s=600, evening_s=5400),
        morning_start=time(7, 0),
        evening_start=time(6, 0),
    )
    assert overlap_offender(evening_first) is CycleKind.EVENING

    assert overlap_offender(make_plan(make_zone(morning_s=600))) is None


def elapsed(start: datetime, end: datetime) -> timedelta:
    """Return the REAL time between two aware datetimes.

    Subtracting two datetimes that share one `tzinfo` object is wall-clock
    arithmetic — Python ignores the common tzinfo — which is precisely the
    trap these tests exist to catch, so they compare UTC instants instead.
    """
    return end.astimezone(UTC) - start.astimezone(UTC)


def test_zone_windows_water_for_elapsed_time_across_spring_forward() -> None:
    """A DST gap must not shorten watering — durations are elapsed seconds.

    Wall-clock accumulation would make this 3600 s zone span 02:30+01:00 →
    03:30+02:00, an elapsed time of ZERO: the zone would receive no water at
    all on that one morning of the year.
    """
    plan = make_plan(make_zone(morning_s=3600))
    start = datetime(2026, 3, 29, 2, 30, tzinfo=PARIS)

    window = zone_windows(plan, CycleKind.MORNING, start)[0]

    assert elapsed(window.start, window.end) == timedelta(seconds=3600)
    # The wall clock legitimately shows a two-hour jump — that is the DST gap,
    # not extra watering.
    assert window.end.hour == 4  # 04:30 CEST — one real hour after 02:30 CET


def test_zone_windows_water_for_elapsed_time_across_fall_back() -> None:
    """The mirror case: a repeated hour must not double a zone's watering."""
    plan = make_plan(make_zone(morning_s=3600))
    start = datetime(2026, 10, 25, 2, 30, tzinfo=PARIS)

    window = zone_windows(plan, CycleKind.MORNING, start)[0]

    assert elapsed(window.start, window.end) == timedelta(seconds=3600)


def test_zone_windows_stay_back_to_back_across_a_dst_boundary() -> None:
    """Accumulation is unbroken: every zone still starts at the previous end."""
    plan = make_plan(
        make_zone("zone-1", morning_s=1800),
        make_zone("zone-2", valve="switch.zone_2_valve", morning_s=1800),
        make_zone("zone-3", valve="switch.zone_3_valve", morning_s=1800),
    )
    start = datetime(2026, 3, 29, 1, 45, tzinfo=PARIS)

    windows = zone_windows(plan, CycleKind.MORNING, start)

    assert windows[0].start == start
    assert windows[1].start == windows[0].end
    assert windows[2].start == windows[1].end
    assert elapsed(start, windows[-1].end) == timedelta(seconds=5400)


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
