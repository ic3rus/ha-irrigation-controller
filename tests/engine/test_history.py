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

    entry = history_entry(run)

    assert entry == {
        "cycle_id": "2026-07-31-morning",
        "irrigation_day": "2026-07-31",
        "kind": "morning",
        "status": "completed",
        "configured_start": "2026-07-31T05:00:00+00:00",
        "scheduled_start": "2026-07-31T05:00:00+00:00",
        "ended_at": "2026-07-31T05:25:00+00:00",
        "zones": [
            {"zone_id": "zone-1", "status": "completed", "effective_s": 600},
            {"zone_id": "zone-2", "status": "failed", "effective_s": 0},
        ],
    }


def test_history_entry_of_a_zoneless_cycle() -> None:
    """A cycle with no zones has no end instant and an empty zone list."""
    entry = history_entry(cycle_run())

    assert entry["zones"] == []
    assert entry["ended_at"] is None


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
