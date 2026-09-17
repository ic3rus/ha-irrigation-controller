"""History section: outcome records, 7-day pruning, effective seconds (AC 4).

Hass-free like the rest of `tests/engine/` — the history lives in the engine
precisely so it stays virtual-clock testable in the bare venv, and so Epic 4's
state view reads it from the engine rather than from storage.
"""

from __future__ import annotations

from datetime import date

from custom_components.ha_irrigation_controller.engine.history import (
    HISTORY_RETENTION_DAYS,
    history_entry,
    prune_history,
)
from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleRun,
    CycleStatus,
    ZoneRun,
    ZoneRunStatus,
    effective_seconds,
)
from tests.engine.common import aware

VALVE_1 = "switch.zone_1_valve"


def zone_run(
    *,
    status: ZoneRunStatus = ZoneRunStatus.COMPLETED,
    start_minute: int = 0,
    end_minute: int | None = 10,
    zone_id: str = "zone-1",
) -> ZoneRun:
    """Build one finished zone run with representative instants."""
    return ZoneRun(
        zone_id=zone_id,
        name="Front Lawn",
        valve_entity_id=VALVE_1,
        duration_s=600,
        base_s=600,
        planned_start=aware(7),
        planned_end=aware(7, 10),
        status=status,
        actual_start=aware(7, start_minute),
        actual_end=None if end_minute is None else aware(7, end_minute),
    )


def cycle_run(*zones: ZoneRun) -> CycleRun:
    """Build one completed cycle run carrying `zones`."""
    return CycleRun(
        cycle_id="2026-07-31-morning",
        kind=CycleKind.MORNING,
        configured_start=aware(7),
        scheduled_start=aware(7),
        pump_entity_id="switch.pool_pump",
        zone_runs=zones,
        status=CycleStatus.COMPLETED,
    )


def test_effective_seconds_of_a_completed_zone() -> None:
    """A confirmed zone contributes its elapsed open-to-close seconds."""
    assert effective_seconds(zone_run()) == 600


def test_effective_seconds_of_a_failed_zone_is_zero() -> None:
    """A failed open never confirmed, so nothing was watered — Epic 2's input."""
    assert effective_seconds(zone_run(status=ZoneRunStatus.FAILED)) == 0


def test_effective_seconds_while_the_zone_is_still_running() -> None:
    """A zone with no end instant yet contributes nothing (not a negative)."""
    assert effective_seconds(zone_run(end_minute=None)) == 0


def test_effective_seconds_of_a_never_started_zone_is_zero() -> None:
    """A pending zone has neither instant."""
    zone = zone_run()
    zone.actual_start = None
    assert effective_seconds(zone) == 0


def test_effective_seconds_never_goes_negative() -> None:
    """A backwards clock step must not produce a negative DURATION.

    `now` comes from the wall clock, which is not monotonic: an NTP
    correction, a manual time change or a VM snapshot restore between a zone's
    open and its close would otherwise feed a negative value to the
    MEASUREMENT sensor and to Epic 2's deficit.
    """
    zone = zone_run(start_minute=10, end_minute=8)

    assert effective_seconds(zone) == 0


def test_effective_seconds_of_an_unconfirmed_close_still_counts() -> None:
    """An unconfirmed CLOSE watered its slot — only a failed OPEN waters nothing.

    The valve may even be stuck open; the slot's elapsed time is what the
    ledger reads, and bounded over-watering is the fail-wet trade (AD-4).
    """
    zone = zone_run()
    zone.close_confirmed = False
    assert effective_seconds(zone) == 600


def test_history_entry_is_a_compact_serializable_outcome() -> None:
    """The record is coarse by design: history, not a replay log."""
    run = cycle_run(
        zone_run(),
        zone_run(status=ZoneRunStatus.FAILED, zone_id="zone-2", end_minute=25),
    )

    entry = history_entry(run, aware(7, 26))

    assert entry == {
        "cycle_id": "2026-07-31-morning",
        "irrigation_day": "2026-07-31",
        "kind": "morning",
        "status": "completed",
        # Story 2.1's marker, written explicitly on every record: a reader must
        # not have to infer "scheduled" from a missing key.
        "manual": False,
        # Story 2.3: the id of the run-now that excused a waived cycle — on
        # every record, None for one that ran, so Epic 4 reads one shape.
        "waived_by": None,
        # Story 3.2's recovery marker, written on every record too: None for
        # a cycle that was never interrupted.
        "recovery": None,
        # Story 3.3's late-re-run marker, likewise on every record: True only
        # on the make-up cycle the watchdog dispatched for a missed one.
        "late_rerun": False,
        "configured_start": "2026-07-31T05:00:00+00:00",
        "scheduled_start": "2026-07-31T05:00:00+00:00",
        "ended_at": "2026-07-31T05:26:00+00:00",
        # Story 2.5: the quote-time gauge reading — None here (no gauge), a
        # number for a modulated cycle; how Epic 4 tells "doubtful" from
        # "no rain" without an anomaly.
        "rain_total_mm": None,
        # Story 2.2: the quoted duration and the carried deficit sit next to
        # what the zone actually watered, so Epic 4 can show all three.
        "zones": [
            {
                "zone_id": "zone-1",
                "status": "completed",
                "planned_s": 600,
                "carried_s": 0,
                "rain_credit_s": 0,
                "effective_s": 600,
            },
            {
                "zone_id": "zone-2",
                "status": "failed",
                "planned_s": 600,
                "carried_s": 0,
                "rain_credit_s": 0,
                "effective_s": 0,
            },
        ],
    }


def test_a_waived_record_names_the_run_now_that_excused_it() -> None:
    """Story 2.3: a waived cycle is a history record — the same shape, never run.

    Built from a run that never started: `status` is `waived`, `waived_by`
    is the crediting run-now's id, every zone is still PENDING with its
    quoted duration and 0 s watered, and `ended_at` is the decision instant.
    Epic 3's watchdog reads this as a permitted non-watering cause.
    """
    zone = zone_run()
    zone.status = ZoneRunStatus.PENDING
    zone.actual_start = None
    zone.actual_end = None
    run = cycle_run(zone)
    run.cycle_id = "2026-07-31-morning-2"
    run.status = CycleStatus.WAIVED

    entry = history_entry(run, aware(7), waived_by="2026-07-31-morning")

    assert entry["status"] == "waived"
    assert entry["waived_by"] == "2026-07-31-morning"
    assert entry["manual"] is False
    assert entry["cycle_id"] == "2026-07-31-morning-2"
    assert entry["ended_at"] == "2026-07-31T05:00:00+00:00"
    assert entry["zones"] == [
        {
            "zone_id": "zone-1",
            "status": "pending",
            "planned_s": 600,
            "carried_s": 0,
            "rain_credit_s": 0,
            "effective_s": 0,
        },
    ]


def test_a_rain_skipped_zone_records_why_nothing_flowed() -> None:
    """Story 2.4: `status: "skipped"`, 0 s planned and watered, the credit next to them.

    Epic 3's watchdog reads a skipped zone with `rain_credit_s > 0` as a
    PERMITTED non-watering cause — the record has to say so on its own.
    """
    skipped = ZoneRun(
        zone_id="zone-1",
        name="Front Lawn",
        valve_entity_id=VALVE_1,
        duration_s=0,
        base_s=600,
        rain_credit_s=720,
        planned_start=aware(7),
        planned_end=aware(7),
        status=ZoneRunStatus.SKIPPED,
    )
    watered = zone_run(zone_id="zone-2")
    watered.rain_credit_s = 210

    entry = history_entry(cycle_run(skipped, watered), aware(7, 10))

    assert entry["status"] == "completed"
    assert entry["zones"] == [
        {
            "zone_id": "zone-1",
            "status": "skipped",
            "planned_s": 0,
            "carried_s": 0,
            "rain_credit_s": 720,
            "effective_s": 0,
        },
        {
            "zone_id": "zone-2",
            "status": "completed",
            "planned_s": 600,
            "carried_s": 0,
            "rain_credit_s": 210,
            "effective_s": 600,
        },
    ]


def test_the_record_carries_the_quote_time_reading_of_a_modulated_cycle() -> None:
    """Story 2.5: `rain_total_mm` is the run's snapshot, verbatim — 15.0 here."""
    run = cycle_run(zone_run())
    run.rain_total_mm = 15.0
    run.rain_source = "sensor.rain_gauge"

    entry = history_entry(run, aware(7, 10))

    assert entry["rain_total_mm"] == 15.0
    # The source is ledger material, not history: the record says what the
    # gauge read, the zone records say what it was worth.
    assert "rain_source" not in entry


def test_the_record_of_a_doubtful_quote_says_null_not_zero() -> None:
    """Story 2.5: a doubtful gauge is `rain_total_mm: None` with 0 s credit per zone.

    Distinct from "no rain" (a number, with 0 s credit) — the one signal that
    tells Epic 4 modulation was OFF for this cycle, since no anomaly says so.
    """
    run = cycle_run(zone_run(), zone_run(zone_id="zone-2"))
    assert run.rain_total_mm is None

    entry = history_entry(run, aware(7, 10))

    assert entry["rain_total_mm"] is None
    zones = entry["zones"]
    assert isinstance(zones, list)
    assert [zone["rain_credit_s"] for zone in zones] == [0, 0]


def test_ended_at_is_the_completion_instant_not_the_last_zone_close() -> None:
    """The pump-off (FR3) happens AFTER the last zone closes — `ended_at` covers it.

    Keying the field off `zone_runs[-1].actual_end` would report a cycle
    duration that excludes its final actuation.
    """
    run = cycle_run(zone_run(end_minute=10))

    entry = history_entry(run, aware(7, 11))

    assert entry["ended_at"] == "2026-07-31T05:11:00+00:00"


def test_history_entry_of_a_zoneless_cycle() -> None:
    """A cycle with no zones still records when it ended, with no zone list."""
    entry = history_entry(cycle_run(), aware(7))

    assert entry["zones"] == []
    assert entry["ended_at"] == "2026-07-31T05:00:00+00:00"


def test_prune_history_keeps_today_and_the_six_preceding_days() -> None:
    """Retention is today plus the six days before it (7 calendar days)."""
    today = date(2026, 7, 31)
    entries: list[dict[str, object]] = [
        {"irrigation_day": f"2026-07-{day:02d}", "cycle_id": f"c-{day}"}
        for day in range(20, 32)
    ]

    kept = prune_history(entries, today)

    assert [entry["irrigation_day"] for entry in kept] == [
        "2026-07-25",
        "2026-07-26",
        "2026-07-27",
        "2026-07-28",
        "2026-07-29",
        "2026-07-30",
        "2026-07-31",
    ]
    assert HISTORY_RETENTION_DAYS == 7


def test_prune_history_boundary_on_both_sides() -> None:
    """Exactly six days back is kept; exactly seven days back is dropped."""
    today = date(2026, 7, 31)
    entries: list[dict[str, object]] = [
        {"irrigation_day": "2026-07-24"},
        {"irrigation_day": "2026-07-25"},
    ]

    kept = prune_history(entries, today)

    assert [entry["irrigation_day"] for entry in kept] == ["2026-07-25"]


def test_prune_history_preserves_order_and_drops_nothing_when_recent() -> None:
    """Pruning is a filter: order (oldest→newest) is never disturbed."""
    today = date(2026, 7, 31)
    entries: list[dict[str, object]] = [
        {"irrigation_day": "2026-07-30", "cycle_id": "a"},
        {"irrigation_day": "2026-07-31", "cycle_id": "b"},
        {"irrigation_day": "2026-07-31", "cycle_id": "c"},
    ]

    assert prune_history(entries, today) == entries


def test_prune_history_drops_a_future_dated_entry_never() -> None:
    """A clock that jumped backwards must not silently erase real history."""
    today = date(2026, 7, 31)
    entries: list[dict[str, object]] = [{"irrigation_day": "2026-08-02"}]

    assert prune_history(entries, today) == entries
