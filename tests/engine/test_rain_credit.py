"""Rain credit on exposed zones, on a virtual clock (Story 2.4, sequencer half).

The mm → seconds rule and the baseline bookkeeping are pinned in
`test_ledger.py`; this suite is where the machine around them is measured:
the gauge is read ONCE in `_build_run` and snapshotted on the run, a zone
quoted at zero is SKIPPED (no valve command, no anomaly, no deficit), a run
whose zones are all zero never starts the pump, and every terminal path
(complete, cancel, waive, run-now, deferred pop, restart) treats the baselines
the way the matrix says. Driven the way every other engine suite drives the
machine: `while (t := engine.next_wakeup()) is not None`. No sleeps, no wall
clock, no Home Assistant (AD-1).
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
    FakeRainPort,
    FakeSwitchPort,
    VirtualClock,
    aware,
    make_plan,
    make_zone,
)

if TYPE_CHECKING:
    from datetime import datetime

    from custom_components.ha_irrigation_controller.engine.plan import ControllerPlan

PUMP = "switch.pool_pump"
VALVE_1 = "switch.zone_1_valve"
VALVE_2 = "switch.zone_2_valve"

# The identity `FakeRainPort` answers by default (Story 2.5): every baseline
# this suite seeds is banked under it, so the fake gauge's readings compare.
GAUGE = "gauge"

# The ledger section with nothing owed and no credit — the baselines vary.
NO_DEBT: dict[str, object] = {
    "settled_cycle_id": None,
    "deficits": {},
    "rain_source": GAUGE,
}


def two_zone_plan() -> ControllerPlan:
    """Two exposed zones at 1 min/mm: 600 s morning / 900 s evening each."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900),
        make_zone("zone-2", valve=VALVE_2, morning_s=600, evening_s=900),
    )


def make_sequencer(
    plan: ControllerPlan,
    *,
    rain: FakeRainPort | None,
    ledger: dict[str, object] | None = None,
) -> tuple[Sequencer, FakeSwitchPort, FakeJournalPort, FakeAnomalyPort]:
    """Wire a sequencer to fresh recording fakes and the given gauge."""
    switches = FakeSwitchPort()
    journal = FakeJournalPort()
    anomalies = FakeAnomalyPort()
    sequencer = Sequencer(
        plan,
        switches=switches,
        journal=journal,
        anomalies=anomalies,
        rain=rain,
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


def finished(sequencer: Sequencer) -> CycleRun:
    """Return the last completed run (mypy strict: never the None branch)."""
    run = sequencer.last_run
    assert run is not None
    return run


def baselines_of(sequencer: Sequencer) -> dict[str, float]:
    """Return the ledger's rain baselines as the journal section carries them."""
    baselines = sequencer.ledger.as_dict()["rain_baselines"]
    assert isinstance(baselines, dict)
    return baselines


def zone_records(journal: FakeJournalPort) -> list[dict[str, object]]:
    """Return the zone records of the LAST history entry the journal saw."""
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    zones = history[-1]["zones"]
    assert isinstance(zones, list)
    return zones


async def water_a_morning(
    sequencer: Sequencer,
    clock: VirtualClock,
    *,
    day: int,
    month: int = 7,
) -> None:
    """Request and drain the morning cycle of `day`."""
    clock.advance_to(aware(7, day=day, month=month))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)


# --------------------------------------------------------------------------
# The first cycle banks, the second spends
# --------------------------------------------------------------------------


async def test_the_first_cycle_ever_runs_in_full_and_banks_the_reading() -> None:
    """Matrix "first cycle ever": no baseline → credit 0, 600 s; settle sets 12.0."""
    rain = FakeRainPort(total=12.0)
    sequencer, _, _, anomalies = make_sequencer(two_zone_plan(), rain=rain)
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert run.rain_total_mm == 12.0
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (0, 600),
        (0, 600),
    ]
    assert [effective_seconds(z) for z in run.zone_runs] == [600, 600]
    assert baselines_of(sequencer) == {"zone-1": 12.0, "zone-2": 12.0}
    assert anomalies.reports == []


async def test_rain_since_the_previous_cycle_reduces_the_next_one() -> None:
    """Matrix "rain since last cycle": 3.5 mm → 210 s off each zone, windows shift.

    Day 1 banks 12.0. Day 2 reads 15.5: both zones are quoted 390 s, so zone
    2 starts at 07:06:30 and the cycle ends at 07:13 — `next_wakeup()`
    follows the reduced run. The credit is snapshotted on the run, on the
    journal and in history.
    """
    rain = FakeRainPort(total=12.0)
    sequencer, switches, journal, anomalies = make_sequencer(two_zone_plan(), rain=rain)
    clock = VirtualClock(aware(7))
    await water_a_morning(sequencer, clock, day=31)
    switches.commands.clear()

    rain.total = 15.5
    clock.advance_to(aware(7, day=1, month=8))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    run = current(sequencer)
    assert run.rain_total_mm == 15.5
    zone1, zone2 = run.zone_runs
    assert (zone1.duration_s, zone1.base_s, zone1.rain_credit_s) == (390, 600, 210)
    assert (zone2.duration_s, zone2.base_s, zone2.rain_credit_s) == (390, 600, 210)
    assert (zone1.planned_end, zone2.planned_end) == (
        aware(7, 6, 30, day=1, month=8),
        aware(7, 13, day=1, month=8),
    )
    snapshot_run = journal.snapshots[-1]["run"]
    assert isinstance(snapshot_run, dict)
    assert snapshot_run["rain_total_mm"] == 15.5
    assert snapshot_run["rain_source"] == GAUGE
    assert snapshot_run["zones"][0]["rain_credit_s"] == 210

    await sequencer.advance(clock.now())
    assert sequencer.next_wakeup() == aware(7, 6, 30, day=1, month=8)
    await run_to_idle(sequencer, clock)

    assert [effective_seconds(z) for z in finished(sequencer).zone_runs] == [390, 390]
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("off", PUMP),
    ]
    assert zone_records(journal)[0] == {
        "zone_id": "zone-1",
        "status": "completed",
        "planned_s": 390,
        "carried_s": 0,
        "rain_credit_s": 210,
        "effective_s": 390,
    }
    assert baselines_of(sequencer) == {"zone-1": 15.5, "zone-2": 15.5}
    assert sequencer.ledger.as_dict()["deficits"] == {}
    assert anomalies.reports == []


async def test_the_gauge_is_read_once_per_cycle_at_quote_time() -> None:
    """AD-8: one read in `_build_run`; nothing re-reads while the run progresses."""
    rain = FakeRainPort(total=12.0)
    sequencer, _, _, _ = make_sequencer(two_zone_plan(), rain=rain)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    assert rain.reads == 1
    rain.total = 99.0
    await run_to_idle(sequencer, clock)

    assert rain.reads == 1
    assert finished(sequencer).rain_total_mm == 12.0
    assert baselines_of(sequencer) == {"zone-1": 12.0, "zone-2": 12.0}


# --------------------------------------------------------------------------
# Zero means skip
# --------------------------------------------------------------------------


async def test_a_fully_covered_zone_is_skipped_without_a_valve_command() -> None:
    """Matrix "fully covered": credit 720 → quoted 0, SKIPPED, no open, no close.

    Zone 1 is covered, zone 2 is not: the pump runs for zone 2 alone, zone 1
    has no instants (0 s effective), no anomaly is raised and no deficit is
    booked — its quote was 0.
    """
    rain = FakeRainPort(total=12.0)
    sequencer, switches, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0, "zone-2": 12.0}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert run.status is CycleStatus.COMPLETED
    zone1, zone2 = run.zone_runs
    assert zone1.status is ZoneRunStatus.SKIPPED
    assert (zone1.duration_s, zone1.rain_credit_s) == (0, 720)
    assert (zone1.actual_start, zone1.actual_end) == (None, None)
    assert (zone1.open_confirmed, zone1.close_confirmed) == (None, None)
    assert effective_seconds(zone1) == 0
    assert zone2.status is ZoneRunStatus.COMPLETED
    assert effective_seconds(zone2) == 600
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("off", PUMP),
    ]
    assert anomalies.reports == []
    assert sequencer.ledger.as_dict()["deficits"] == {}
    assert zone_records(journal)[0] == {
        "zone_id": "zone-1",
        "status": "skipped",
        "planned_s": 0,
        "carried_s": 0,
        "rain_credit_s": 720,
        "effective_s": 0,
    }


async def test_a_skipped_last_zone_still_completes_the_cycle_with_the_pump_off() -> (
    None
):
    """The skip in the LAST slot goes through `_advance_slot` to the pump-off."""
    rain = FakeRainPort(total=12.0)
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-2": 0.0}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert run.status is CycleStatus.COMPLETED
    assert [z.status for z in run.zone_runs] == [
        ZoneRunStatus.COMPLETED,
        ZoneRunStatus.SKIPPED,
    ]
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("off", PUMP),
    ]
    assert run.pump_off_confirmed is True
    assert anomalies.reports == []


async def test_a_skipped_middle_zone_hands_over_from_zone_one_to_zone_three() -> None:
    """Zone 1 waters, zone 2 is skipped, zone 3 waters: one pump run, two valves."""
    rain = FakeRainPort(total=12.0)
    sequencer, switches, _, anomalies = make_sequencer(
        make_plan(
            make_zone("zone-1", valve=VALVE_1, morning_s=600),
            make_zone("zone-2", valve=VALVE_2, morning_s=600),
            make_zone("zone-3", valve="switch.zone_3_valve", morning_s=600),
        ),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-2": 0.0}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert run.status is CycleStatus.COMPLETED
    assert [z.status for z in run.zone_runs] == [
        ZoneRunStatus.COMPLETED,
        ZoneRunStatus.SKIPPED,
        ZoneRunStatus.COMPLETED,
    ]
    assert [effective_seconds(z) for z in run.zone_runs] == [600, 0, 600]
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", "switch.zone_3_valve"),
        ("off", "switch.zone_3_valve"),
        ("off", PUMP),
    ]
    assert anomalies.reports == []
    assert sequencer.ledger.as_dict()["deficits"] == {}


async def test_a_cancel_after_a_skipped_zone_closes_the_live_zone_only() -> None:
    """Zone 1 skipped, cancelled during zone 2: the cancel closes zone 2, not the skip.

    The skipped zone books no deficit (its quote was 0); zone 2 owes its
    shortfall; both baselines advance to the run's reading.
    """
    rain = FakeRainPort(total=12.0)
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0}},
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    assert switches.commands == [("on", PUMP), ("on", VALVE_2)]
    clock.advance_to(aware(7, 4))

    assert await sequencer.async_cancel_cycle(clock.now()) is True

    run = finished(sequencer)
    assert run.status is CycleStatus.CANCELLED
    zone1, zone2 = run.zone_runs
    assert zone1.status is ZoneRunStatus.SKIPPED
    assert (zone1.actual_start, zone1.actual_end) == (None, None)
    assert zone2.status is ZoneRunStatus.COMPLETED
    assert effective_seconds(zone2) == 240
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("off", PUMP),
    ]
    assert anomalies.reports == []
    assert sequencer.ledger.as_dict() == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {"zone-2": 360},
        "day_credit": None,
        "rain_baselines": {"zone-1": 12.0, "zone-2": 12.0},
        "rain_source": GAUGE,
    }


async def test_an_all_covered_cycle_never_starts_the_pump() -> None:
    """Matrix "all zones covered": no command at all, COMPLETED, every zone skipped.

    History is filed and the baselines advance exactly as for a watered run.
    """
    rain = FakeRainPort(total=12.0)
    sequencer, switches, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0, "zone-2": 0.0}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert run.status is CycleStatus.COMPLETED
    assert (run.pump_on_confirmed, run.pump_off_confirmed) == (None, None)
    assert all(z.status is ZoneRunStatus.SKIPPED for z in run.zone_runs)
    assert switches.commands == []
    assert anomalies.reports == []
    assert sequencer.current_run is None
    assert sequencer.next_wakeup() is None
    assert [record["status"] for record in zone_records(journal)] == [
        "skipped",
        "skipped",
    ]
    assert baselines_of(sequencer) == {"zone-1": 12.0, "zone-2": 12.0}
    assert sequencer.ledger.as_dict()["deficits"] == {}


async def test_a_covered_zone_in_debt_still_waters_its_carried_seconds() -> None:
    """Matrix "covered but in debt": quoted 300, the valve runs 300 s."""
    rain = FakeRainPort(total=12.0)
    sequencer, switches, _, _ = make_sequencer(
        make_plan(make_zone("zone-1", valve=VALVE_1, morning_s=600)),
        rain=rain,
        ledger={
            "settled_cycle_id": "2026-07-30-evening",
            "deficits": {"zone-1": 300},
            "rain_baselines": {"zone-1": 0.0},
            "rain_source": GAUGE,
        },
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    zone = current(sequencer).zone_runs[0]
    assert (zone.duration_s, zone.rain_credit_s, zone.carried_s) == (300, 720, 300)
    assert zone.planned_end == aware(7, 5)

    await run_to_idle(sequencer, clock)

    assert effective_seconds(finished(sequencer).zone_runs[0]) == 300
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("off", PUMP),
    ]
    assert sequencer.ledger.as_dict()["deficits"] == {}


async def test_next_wakeup_never_points_at_a_skipped_slot_across_an_await() -> None:
    """The adapter's re-arm contract holds through a skip.

    At every journal save — the moment the runner re-arms — `next_wakeup()`
    is either None or an instant no earlier than now, and the standard
    drive loop converges in a bounded number of steps.
    """
    rain = FakeRainPort(total=12.0)
    sequencer, _, journal, _ = make_sequencer(
        make_plan(
            make_zone("zone-1", valve=VALVE_1, morning_s=600),
            make_zone("zone-2", valve=VALVE_2, morning_s=600),
            make_zone("zone-3", valve="switch.zone_3_valve", morning_s=600),
        ),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0, "zone-2": 0.0}},
    )
    clock = VirtualClock(aware(7))
    # (what the runner would re-arm at, the engine time it was asked) pairs.
    observed: list[tuple[datetime | None, datetime]] = []
    journal.observer = lambda: observed.append((sequencer.next_wakeup(), clock.now()))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    steps = 0
    while (moment := sequencer.next_wakeup()) is not None:
        steps += 1
        assert steps <= 4, "the drive loop did not converge"
        clock.advance_to(moment)
        await sequencer.advance(clock.now())

    assert steps == 2  # the 07:00 start (two skips + one open) and the 07:10 close
    assert all(moment is None or moment >= asked for moment, asked in observed)
    # Once a skipped slot has been passed, no later save ever points back at it.
    assert [moment for moment, _ in observed if moment is not None] == [
        aware(7),  # PENDING: the scheduled start
        aware(7),  # RUNNING, index 0 (about to be skipped in this same call)
        aware(7),  # index 1 (about to be skipped in this same call)
        aware(7, 10),  # index 2: the one real slot
        aware(7, 10),  # zone 3 open
    ]
    run = finished(sequencer)
    assert [z.status for z in run.zone_runs] == [
        ZoneRunStatus.SKIPPED,
        ZoneRunStatus.SKIPPED,
        ZoneRunStatus.COMPLETED,
    ]
    assert effective_seconds(run.zone_runs[2]) == 600


# --------------------------------------------------------------------------
# Sheltered zones and a zero factor are untouched
# --------------------------------------------------------------------------


async def test_a_sheltered_zone_and_a_zero_factor_zone_water_in_full() -> None:
    """Matrix "sheltered zone" / "factor 0": credit 0 whatever the rainfall."""
    rain = FakeRainPort(total=50.0)
    sequencer, _, _, _ = make_sequencer(
        make_plan(
            make_zone("zone-1", valve=VALVE_1, morning_s=600, rain_exposed=False),
            make_zone("zone-2", valve=VALVE_2, morning_s=600, rain_factor=0.0),
        ),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0, "zone-2": 0.0}},
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert [(z.rain_credit_s, z.duration_s) for z in current(sequencer).zone_runs] == [
        (0, 600),
        (0, 600),
    ]


# --------------------------------------------------------------------------
# What the baseline is set to
# --------------------------------------------------------------------------


async def test_rain_during_the_cycle_credits_the_next_one() -> None:
    """Matrix "rain during the cycle": quote 10.0, gauge 14.0 at settle → baseline 10.0.

    The next quote (still 14.0) credits the 4 mm that fell during the cycle.
    """
    rain = FakeRainPort(total=10.0)
    sequencer, _, _, _ = make_sequencer(two_zone_plan(), rain=rain)
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    rain.total = 14.0
    await run_to_idle(sequencer, clock)
    assert baselines_of(sequencer) == {"zone-1": 10.0, "zone-2": 10.0}

    clock.advance_to(aware(20))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())

    assert [z.rain_credit_s for z in current(sequencer).zone_runs] == [240, 240]
    assert [z.duration_s for z in current(sequencer).zone_runs] == [660, 660]


async def test_an_unavailable_gauge_quotes_full_durations_and_spends_nothing() -> None:
    """Matrix "gauge unavailable at quote": None → credit 0, snapshot None, no anomaly.

    Zone 1 keeps its 5.0 baseline through the settlement; zone 2 never had
    one and still has none.
    """
    rain = FakeRainPort(total=None)
    sequencer, _, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 5.0}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert run.rain_total_mm is None
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (0, 600),
        (0, 600),
    ]
    # The completion write still files the run under `run` (it is released
    # into `last_run` only after that save), so that is where to look.
    snapshot_run = journal.snapshots[-1]["run"]
    assert isinstance(snapshot_run, dict)
    assert snapshot_run["rain_total_mm"] is None
    assert baselines_of(sequencer) == {"zone-1": 5.0}
    assert anomalies.reports == []


async def test_a_raising_gauge_is_read_as_unavailable() -> None:
    """A port that leaks an exception quotes full durations, no anomaly, no crash."""
    rain = FakeRainPort(total=12.0)
    rain.raising = True
    sequencer, _, _, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0, "zone-2": 0.0}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert run.rain_total_mm is None
    assert [z.duration_s for z in run.zone_runs] == [600, 600]
    assert baselines_of(sequencer) == {"zone-1": 0.0, "zone-2": 0.0}
    assert anomalies.reports == []


async def test_without_a_gauge_every_quote_is_unmodulated() -> None:
    """`rain=None` (no gauge): full durations, None snapshotted, baselines untouched."""
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        rain=None,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert run.rain_total_mm is None
    assert [z.duration_s for z in run.zone_runs] == [600, 600]
    assert baselines_of(sequencer) == {"zone-1": 0.0}


async def test_a_decreasing_total_credits_nothing_and_resets_the_baseline() -> None:
    """Matrix "decreasing total": baseline 40.0, total 0.5 → credit 0; settle → 0.5."""
    rain = FakeRainPort(total=0.5)
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 40.0, "zone-2": 40.0}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (0, 600),
        (0, 600),
    ]
    assert baselines_of(sequencer) == {"zone-1": 0.5, "zone-2": 0.5}


# --------------------------------------------------------------------------
# Terminal paths: cancel, waive, zone removed, deferred pop
# --------------------------------------------------------------------------


async def test_a_cancelled_run_advances_the_baselines_and_carries_the_shortfall() -> (
    None
):
    """Matrix "cancelled run": rain is never counted twice.

    3 mm credited 180 s off each 600 s zone (quoted 420). Cancelled at
    +02:00: zone 1 watered 120 and owes 300, zone 2 owes its whole 420 —
    and BOTH baselines move to 3.0, so the next quote credits only rain
    beyond it.
    """
    rain = FakeRainPort(total=3.0)
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0, "zone-2": 0.0}},
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    assert [z.duration_s for z in current(sequencer).zone_runs] == [420, 420]
    clock.advance_to(aware(7, 2))

    assert await sequencer.async_cancel_cycle(clock.now()) is True

    assert finished(sequencer).status is CycleStatus.CANCELLED
    assert sequencer.ledger.as_dict() == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {"zone-1": 300, "zone-2": 420},
        "day_credit": None,
        "rain_baselines": {"zone-1": 3.0, "zone-2": 3.0},
        "rain_source": GAUGE,
    }
    clock.advance_to(aware(20))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert [z.rain_credit_s for z in current(sequencer).zone_runs] == [0, 0]


async def test_a_waived_cycle_spends_no_rain() -> None:
    """Matrix "waived cycle": the credit is computed in the dropped run, never settled.

    The 07:00 morning is waived by a seeded day credit. Its history record
    carries the 720 s credit it WOULD have applied, but the baselines are
    unchanged — the rain credits the next real cycle.
    """
    rain = FakeRainPort(total=12.0)
    sequencer, switches, journal, _ = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={
            **NO_DEBT,
            "day_credit": {
                "irrigation_day": "2026-07-31",
                "cycle_id": "2026-07-31-morning",
            },
            "rain_baselines": {"zone-1": 0.0},
        },
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert sequencer.current_run is None
    assert switches.commands == []
    records = zone_records(journal)
    assert (records[0]["status"], records[0]["rain_credit_s"]) == ("pending", 720)
    assert baselines_of(sequencer) == {"zone-1": 0.0}

    clock.advance_to(aware(20))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    zone1, zone2 = current(sequencer).zone_runs
    assert (zone1.rain_credit_s, zone1.duration_s) == (720, 180)
    assert (zone2.rain_credit_s, zone2.duration_s) == (0, 900)


async def test_a_baseline_for_a_removed_zone_is_dropped_at_the_next_settlement() -> (
    None
):
    """Matrix "zone removed": the ledger is replaced by the settled run's zones."""
    rain = FakeRainPort(total=7.0)
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-gone": 3.0, "zone-1": 1.0}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    assert baselines_of(sequencer) == {"zone-1": 7.0, "zone-2": 7.0}


async def test_a_deferred_cycle_is_quoted_against_the_baseline_just_settled() -> None:
    """The deferred pop inside `_complete_cycle` sees the fresh baselines.

    Morning quoted at 5.0 (300 s credit each, quoted 300). The evening start
    arrives mid-morning and the gauge reaches 6.0 before the morning
    completes; the evening created at that completion credits ONLY the 1 mm
    since 5.0 — 60 s off each 900 s zone.
    """
    rain = FakeRainPort(total=5.0)
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0, "zone-2": 0.0}},
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    assert [z.duration_s for z in current(sequencer).zone_runs] == [300, 300]
    clock.advance_to(aware(7, 1))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)
    rain.total = 6.0

    # Drain the morning only: the evening is created in the same `advance`.
    clock.advance_to(aware(7, 5))
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 10))
    await sequencer.advance(clock.now())

    evening = current(sequencer)
    assert evening.kind is CycleKind.EVENING
    assert evening.rain_total_mm == 6.0
    assert [(z.rain_credit_s, z.duration_s) for z in evening.zone_runs] == [
        (60, 840),
        (60, 840),
    ]


# --------------------------------------------------------------------------
# Run-now and restart
# --------------------------------------------------------------------------


async def test_a_run_now_is_rain_reduced_like_any_cycle() -> None:
    """Decision: one quote for every cycle — a run-now after 3.5 mm waters 390 s."""
    rain = FakeRainPort(total=15.5)
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 12.0, "zone-2": 12.0}},
    )
    clock = VirtualClock(aware(14, 30))

    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True

    run = current(sequencer)
    assert run.manual is True
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (210, 390),
        (210, 390),
    ]


async def test_a_run_now_after_rain_covering_every_zone_credits_the_day() -> None:
    """Matrix "run-now after rain": completes at once, all skipped, nothing commanded.

    Having booked no deficit, the completed manual run credits the day
    (Story 2.3) — so the scheduled cycle of that day is waived.
    """
    rain = FakeRainPort(total=12.0)
    sequencer, switches, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={**NO_DEBT, "rain_baselines": {"zone-1": 0.0, "zone-2": 0.0}},
    )
    clock = VirtualClock(aware(6))

    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True
    await sequencer.advance(clock.now())

    assert sequencer.current_run is None
    run = finished(sequencer)
    assert (run.manual, run.status) == (True, CycleStatus.COMPLETED)
    assert all(z.status is ZoneRunStatus.SKIPPED for z in run.zone_runs)
    assert switches.commands == []
    assert anomalies.reports == []
    assert sequencer.ledger.as_dict() == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {},
        "day_credit": {
            "irrigation_day": "2026-07-31",
            "cycle_id": "2026-07-31-morning",
        },
        "rain_baselines": {"zone-1": 12.0, "zone-2": 12.0},
        "rain_source": GAUGE,
    }
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert (history[-1]["manual"], history[-1]["status"]) == (True, "completed")

    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    assert sequencer.current_run is None
    assert zone_records(journal)[0]["status"] == "pending"
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert history[-1]["waived_by"] == "2026-07-31-morning"


async def test_baselines_seeded_from_the_journal_credit_the_first_cycle() -> None:
    """Matrix "restart": the section the journal wrote re-seeds a fresh sequencer."""
    rain = FakeRainPort(total=12.0)
    first, _, journal, _ = make_sequencer(two_zone_plan(), rain=rain)
    clock = VirtualClock(aware(7))
    await water_a_morning(first, clock, day=31)
    stored = journal.snapshots[-1]["ledger"]
    assert isinstance(stored, dict)
    assert stored["rain_baselines"] == {"zone-1": 12.0, "zone-2": 12.0}

    rain.total = 15.5
    rebuilt, _, _, _ = make_sequencer(two_zone_plan(), rain=rain, ledger=stored)
    clock.advance_to(aware(7, day=1, month=8))
    await rebuilt.request_cycle(CycleKind.MORNING, clock.now())

    assert [(z.rain_credit_s, z.duration_s) for z in current(rebuilt).zone_runs] == [
        (210, 390),
        (210, 390),
    ]


async def test_a_pre_2_4_ledger_section_quotes_full_durations() -> None:
    """A section with no `rain_baselines` key: no credit, and the first cycle banks."""
    rain = FakeRainPort(total=12.0)
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={"settled_cycle_id": "2026-07-30-evening", "deficits": {}},
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (0, 600),
        (0, 600),
    ]
    assert baselines_of(sequencer) == {"zone-1": 12.0, "zone-2": 12.0}
