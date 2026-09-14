"""Deficit carry-forward on a virtual clock (Story 2.2, sequencer half).

The ledger's arithmetic is pinned in `test_ledger.py`; this suite is where
its two call sites are measured through the machine: a cycle is QUOTED in
`_create_run` (windows and `next_wakeup()` follow the quoted durations) and
SETTLED in `_complete_cycle` (before the snapshot is saved, before a deferred
cycle is popped). Driven the way every other engine suite drives the machine:
`while (t := engine.next_wakeup()) is not None`. No sleeps, no wall clock, no
Home Assistant (AD-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleRun,
    CycleStatus,
    ZoneRunStatus,
    effective_seconds,
)
from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer
from tests.engine.common import (
    FakeAnomalyPort,
    FakeJournalPort,
    FakeSwitchPort,
    VirtualClock,
    aware,
    make_plan,
    make_zone,
)

if TYPE_CHECKING:
    from custom_components.ha_irrigation_controller.engine.plan import ControllerPlan

PUMP = "switch.pool_pump"
VALVE_1 = "switch.zone_1_valve"
VALVE_2 = "switch.zone_2_valve"
VALVE_3 = "switch.zone_3_valve"


def two_zone_plan() -> ControllerPlan:
    """Two zones: 600 s morning / 900 s evening each, so the kinds differ."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900),
        make_zone("zone-2", valve=VALVE_2, morning_s=600, evening_s=900),
    )


def make_sequencer(
    plan: ControllerPlan,
    *,
    ledger: dict[str, object] | None = None,
) -> tuple[Sequencer, FakeSwitchPort, FakeJournalPort, FakeAnomalyPort]:
    """Wire a sequencer to fresh recording fakes, optionally with a ledger seed."""
    switches = FakeSwitchPort()
    journal = FakeJournalPort()
    anomalies = FakeAnomalyPort()
    sequencer = Sequencer(
        plan,
        switches=switches,
        journal=journal,
        anomalies=anomalies,
        ledger=ledger,
    )
    return sequencer, switches, journal, anomalies


async def run_to_idle(sequencer: Sequencer, clock: VirtualClock) -> None:
    """Drive the engine with the exact wake-up loop the runner uses."""
    while (moment := sequencer.next_wakeup()) is not None:
        clock.advance_to(moment)
        await sequencer.advance(clock.now())


def current(sequencer: Sequencer) -> CycleRun:
    """Return the active run (mypy strict: never the None branch)."""
    run = sequencer.current_run
    assert run is not None
    return run


async def fail_a_morning(
    sequencer: Sequencer,
    switches: FakeSwitchPort,
    clock: VirtualClock,
    *,
    day: int,
) -> None:
    """Run the morning cycle of `day` with zone 1's valve refusing to open."""
    switches.failing.add(("on", VALVE_1))
    clock.advance_to(aware(7, day=day))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)
    switches.failing.discard(("on", VALVE_1))


# --------------------------------------------------------------------------
# Acceptance 1: a failed open extends the next run and shifts the windows
# --------------------------------------------------------------------------


async def test_a_failed_open_extends_the_next_run_of_that_kind() -> None:
    """AC 1: `duration_s` = base + shortfall, and the following zone is shifted.

    Zone 1 fails on day 1 (0 s watered, 600 owed). On day 2 it is quoted
    1200 s, so zone 2 starts at 07:20 instead of 07:10 and the cycle ends at
    07:30 — `next_wakeup()` follows the extended run.
    """
    sequencer, switches, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await fail_a_morning(sequencer, switches, clock, day=31)
    assert sequencer.ledger.deficit_s("zone-1") == 600

    clock.advance_to(aware(7, day=1, month=8))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    zone1, zone2 = current(sequencer).zone_runs
    assert (zone1.duration_s, zone1.base_s, zone1.carried_s) == (1200, 600, 600)
    assert (zone2.duration_s, zone2.base_s, zone2.carried_s) == (600, 600, 0)
    assert (zone1.planned_start, zone1.planned_end) == (
        aware(7, day=1, month=8),
        aware(7, 20, day=1, month=8),
    )
    assert (zone2.planned_start, zone2.planned_end) == (
        aware(7, 20, day=1, month=8),
        aware(7, 30, day=1, month=8),
    )

    await sequencer.advance(clock.now())
    assert sequencer.next_wakeup() == aware(7, 20, day=1, month=8)

    await run_to_idle(sequencer, clock)

    finished = sequencer.last_run
    assert finished is not None
    assert [effective_seconds(zone) for zone in finished.zone_runs] == [1200, 600]
    # The debt is paid: the extended run settled to nothing owed.
    assert sequencer.ledger.as_dict() == {
        "settled_cycle_id": "2026-08-01-morning",
        "deficits": {},
        "day_credit": None,
        "rain_baselines": {},
    }


async def test_a_failed_open_raises_no_new_anomaly_for_the_deficit() -> None:
    """Matrix "failed open": Story 1.5's open-unconfirmed anomaly is the only one."""
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await fail_a_morning(sequencer, switches, clock, day=31)

    assert [kind.value for kind, _ in anomalies.reports] == ["valve_open_unconfirmed"]


async def test_the_deficit_extends_the_other_kind_too_capped_at_its_base() -> None:
    """A morning failure (600 owed) extends the evening: 900 + 600 = 1500."""
    sequencer, switches, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await fail_a_morning(sequencer, switches, clock, day=31)

    clock.advance_to(aware(20))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())

    zone1 = current(sequencer).zone_runs[0]
    assert (zone1.duration_s, zone1.base_s, zone1.carried_s) == (1500, 900, 600)


# --------------------------------------------------------------------------
# Partial cancel
# --------------------------------------------------------------------------


async def test_a_partial_cancel_books_each_zones_shortfall() -> None:
    """Matrix "partial cancel": zone 1 full, zone 2 cut at +120 s, zone 3 unreached."""
    plan = make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600),
        make_zone("zone-2", valve=VALVE_2, morning_s=600),
        make_zone("zone-3", valve=VALVE_3, morning_s=600),
    )
    sequencer, _, _, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 10))
    await sequencer.advance(clock.now())  # zone 1 closes, zone 2 opens
    clock.advance_to(aware(7, 12))

    assert await sequencer.async_cancel_cycle(clock.now()) is True

    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.CANCELLED
    assert sequencer.ledger.as_dict() == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {"zone-2": 480, "zone-3": 600},
        "day_credit": None,
        "rain_baselines": {},
    }


# --------------------------------------------------------------------------
# The cap holds across cycles
# --------------------------------------------------------------------------


async def test_three_consecutive_failures_never_exceed_twice_the_base() -> None:
    """Deficits never compound: a 1200 s quote that fails still owes 600, not 1200.

    Day 1 fails on base (600 quoted, 600 owed); days 2 and 3 fail on the
    extended quote (1200 quoted, 0 watered) and the settlement clamps the
    shortfall at one base — so day 4 is quoted 1200 again, never 1800.
    """
    sequencer, switches, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    quoted: list[int] = []

    for day in (28, 29, 30):
        await fail_a_morning(sequencer, switches, clock, day=day)
        finished = sequencer.last_run
        assert finished is not None
        quoted.append(finished.zone_runs[0].duration_s)
        assert sequencer.ledger.deficit_s("zone-1") == 600

    assert quoted == [600, 1200, 1200]
    await sequencer.request_cycle(CycleKind.MORNING, aware(7, day=31))
    assert current(sequencer).zone_runs[0].duration_s == 1200


# --------------------------------------------------------------------------
# Both terminal paths settle, exactly once, before the snapshot
# --------------------------------------------------------------------------


async def test_the_completion_snapshot_already_carries_the_settlement() -> None:
    """Settle happens BEFORE `_save`: the journal written at completion has it.

    Without this, a crash between the completion write and the next
    transition would lose the deficit the cycle just produced.
    """
    sequencer, switches, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    switches.failing.add(("on", VALVE_1))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    # Quoting wrote nothing: the ledger in every snapshot so far is untouched.
    assert all(
        snapshot["ledger"]
        == {
            "settled_cycle_id": None,
            "deficits": {},
            "day_credit": None,
            "rain_baselines": {},
        }
        for snapshot in journal.snapshots
    )

    await run_to_idle(sequencer, clock)

    # The completion write still has the cycle under `run` (it is released
    # into `last_run` only after the save), so that is where to look for it.
    completion = next(
        snapshot
        for snapshot in journal.snapshots
        if isinstance(run := snapshot["run"], dict) and run["status"] == "completed"
    )
    assert completion["ledger"] == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {"zone-1": 600},
        "day_credit": None,
        "rain_baselines": {},
    }
    assert journal.snapshots[-1]["ledger"] == completion["ledger"]


async def test_the_run_snapshot_carries_base_and_carried_seconds() -> None:
    """Story 3.2 resumes from `run`, so the settlement inputs have to be in it."""
    sequencer, _, journal, _ = make_sequencer(
        two_zone_plan(),
        ledger={"settled_cycle_id": "2026-07-30-evening", "deficits": {"zone-1": 300}},
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    run = journal.snapshots[-1]["run"]
    assert isinstance(run, dict)
    zone1, zone2 = run["zones"]
    assert (zone1["duration_s"], zone1["base_s"], zone1["carried_s"]) == (900, 600, 300)
    assert (zone2["duration_s"], zone2["base_s"], zone2["carried_s"]) == (600, 600, 0)


async def test_history_records_the_quoted_and_carried_seconds() -> None:
    """Epic 4 reads `planned_s` and `carried_s` next to `effective_s`."""
    sequencer, switches, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await fail_a_morning(sequencer, switches, clock, day=31)

    clock.advance_to(aware(7, day=1, month=8))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert history[-1]["zones"][0] == {
        "zone_id": "zone-1",
        "status": "completed",
        "planned_s": 1200,
        "carried_s": 600,
        "rain_credit_s": 0,
        "effective_s": 1200,
    }


async def test_a_suspend_settles_nothing_and_the_deficit_is_re_applied() -> None:
    """`async_suspend` is not a terminal path: the quote is forgotten, the debt is not.

    Quote is pure, so the 300 s owed are still in the ledger after the
    suspend — and a sequencer rebuilt from the journal quotes them again.
    Doubt waters (AD-4).
    """
    seed: dict[str, object] = {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300},
        "day_credit": None,
        "rain_baselines": {},
    }
    sequencer, _, journal, _ = make_sequencer(two_zone_plan(), ledger=seed)
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    assert current(sequencer).zone_runs[0].duration_s == 900

    assert await sequencer.async_suspend(clock.now()) is True

    assert sequencer.ledger.as_dict() == seed
    stored = journal.snapshots[-1]["ledger"]
    assert isinstance(stored, dict)
    rebuilt, _, _, _ = make_sequencer(two_zone_plan(), ledger=stored)
    await rebuilt.request_cycle(CycleKind.MORNING, clock.now())
    assert current(rebuilt).zone_runs[0].duration_s == 900


# --------------------------------------------------------------------------
# Every creation path is quoted: deferred pop, run-now, seeded first run
# --------------------------------------------------------------------------


async def test_a_deferred_cycle_is_quoted_with_the_deficit_just_settled() -> None:
    """The deferred pop inside `_complete_cycle` sees the fresh settlement.

    The evening start arrives while zone 1's morning slot is failing; the
    evening cycle created at the morning's completion is quoted 900 + 600.
    """
    sequencer, switches, _, _ = make_sequencer(two_zone_plan())
    switches.failing.add(("on", VALVE_1))
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 5))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)

    # Drain the morning only: the evening is created in the same `advance`
    # and starts at once, so stop at its first zone boundary.
    clock.advance_to(aware(7, 10))
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 20))
    await sequencer.advance(clock.now())

    evening = current(sequencer)
    assert evening.kind is CycleKind.EVENING
    assert evening.scheduled_start == aware(7, 20)
    zone1, zone2 = evening.zone_runs
    assert (zone1.duration_s, zone1.base_s, zone1.carried_s) == (1500, 900, 600)
    assert zone1.planned_end == aware(7, 45)
    assert (zone2.planned_start, zone2.planned_end) == (aware(7, 45), aware(8))


async def test_a_run_now_applies_and_settles_the_deficit() -> None:
    """FR11's "current durations" are the current EFFECTIVE durations.

    One arithmetic, no manual-run branch: the run-now after a failed morning
    waters 1200 s on zone 1 and pays the debt off — and, having completed
    with no shortfall, credits the day (Story 2.3) in the same settlement.
    """
    sequencer, switches, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await fail_a_morning(sequencer, switches, clock, day=31)

    clock.advance_to(aware(14, 30))
    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True
    run = current(sequencer)
    assert run.manual is True
    assert [zone.duration_s for zone in run.zone_runs] == [1200, 600]
    assert [zone.planned_end for zone in run.zone_runs] == [
        aware(14, 50),
        aware(15),
    ]

    await run_to_idle(sequencer, clock)

    assert sequencer.ledger.as_dict() == {
        "settled_cycle_id": "2026-07-31-morning-2",
        "deficits": {},
        "day_credit": {
            "irrigation_day": "2026-07-31",
            "cycle_id": "2026-07-31-morning-2",
        },
        "rain_baselines": {},
    }


async def test_a_seeded_ledger_extends_the_first_run() -> None:
    """AC 3: a deficit recorded before a restart is applied after setup."""
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        ledger={"settled_cycle_id": "2026-07-30-evening", "deficits": {"zone-2": 300}},
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    zone1, zone2 = current(sequencer).zone_runs
    assert (zone1.duration_s, zone1.carried_s) == (600, 0)
    assert (zone2.duration_s, zone2.carried_s) == (900, 300)
    assert zone2.planned_end == aware(7, 25)


async def test_without_a_seed_the_first_run_is_quoted_on_base() -> None:
    """AC 2: a journal written before this story seeds nothing — base durations."""
    sequencer, _, journal, _ = make_sequencer(two_zone_plan(), ledger=None)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert [zone.duration_s for zone in current(sequencer).zone_runs] == [600, 600]
    assert journal.snapshots[-1]["ledger"] == {
        "settled_cycle_id": None,
        "deficits": {},
        "day_credit": None,
        "rain_baselines": {},
    }


async def test_a_deficit_for_a_removed_zone_is_dropped_at_the_next_settlement() -> None:
    """Matrix "zone removed": never quoted, gone once a cycle settles."""
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        ledger={"settled_cycle_id": None, "deficits": {"zone-gone": 600}},
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    assert [zone.carried_s for zone in current(sequencer).zone_runs] == [0, 0]
    await run_to_idle(sequencer, clock)

    assert sequencer.ledger.as_dict()["deficits"] == {}


# --------------------------------------------------------------------------
# Zero-dwell catch-up
# --------------------------------------------------------------------------


async def test_a_zero_dwell_catch_up_carries_the_full_deficit() -> None:
    """Matrix "zero-dwell catch-up": one late `advance` drains every boundary at once.

    Each zone opens and closes at 09:00 (`actual_start == actual_end`) and is
    filed COMPLETED — but the ledger does not read that as watered. Both
    zones carry their whole 600 s.
    """
    sequencer, _, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    clock.advance_to(aware(9))
    await sequencer.advance(clock.now())

    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    for zone in finished.zone_runs:
        assert zone.status is ZoneRunStatus.COMPLETED
        assert zone.actual_start == zone.actual_end == aware(9)
    assert sequencer.ledger.as_dict()["deficits"] == {"zone-1": 600, "zone-2": 600}
