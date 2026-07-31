"""Sequencer state machine on a virtual clock (AC 2, 3, 4, 5).

Every test drives the engine exactly the way Story 1.5's adapter will:
`while (t := engine.next_wakeup()) is not None: clock.advance_to(t);
await engine.advance(clock.now())` — no sleeps, no wall clock, no HA.
"""

from __future__ import annotations

import json
from dataclasses import replace

from custom_components.ha_irrigation_controller.engine.plan import (
    ControllerPlan,
    CycleKind,
)
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleStatus,
    ZoneRunStatus,
)
from custom_components.ha_irrigation_controller.engine.sequencer import (
    JOURNAL_SCHEMA_VERSION,
    Sequencer,
)
from tests.engine.common import (
    FakeAnomalyPort,
    FakeJournalPort,
    FakeSwitchPort,
    VirtualClock,
    aware,
    make_plan,
    make_zone,
)

PUMP = "switch.pool_pump"
VALVE_1 = "switch.zone_1_valve"
VALVE_2 = "switch.zone_2_valve"
VALVE_3 = "switch.zone_3_valve"


def three_zone_plan() -> ControllerPlan:
    """Build a representative three-zone plan with distinct durations."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600),
        make_zone("zone-2", valve=VALVE_2, morning_s=900),
        make_zone("zone-3", valve=VALVE_3, morning_s=300),
    )


def make_sequencer(
    plan: ControllerPlan,
) -> tuple[Sequencer, FakeSwitchPort, FakeJournalPort, FakeAnomalyPort]:
    """Wire a sequencer to fresh recording fakes."""
    switches = FakeSwitchPort()
    journal = FakeJournalPort()
    anomalies = FakeAnomalyPort()
    sequencer = Sequencer(
        plan,
        switches=switches,
        journal=journal,
        anomalies=anomalies,
    )
    return sequencer, switches, journal, anomalies


async def run_to_idle(sequencer: Sequencer, clock: VirtualClock) -> None:
    """Drive the engine with the exact wake-up loop the 1.5 adapter will use."""
    while (moment := sequencer.next_wakeup()) is not None:
        clock.advance_to(moment)
        await sequencer.advance(clock.now())


async def test_full_cycle_command_ordering() -> None:
    """Pump on → zones strictly sequential → pump off (FR2, FR3)."""
    sequencer, switches, _, anomalies = make_sequencer(three_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("on", VALVE_3),
        ("off", VALVE_3),
        ("off", PUMP),
    ]
    assert anomalies.reports == []
    assert sequencer.next_wakeup() is None


async def test_close_is_always_commanded_before_the_next_open() -> None:
    """No two valve-open commands without the intervening close (FR2)."""
    sequencer, switches, _, _ = make_sequencer(three_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    open_valve: str | None = None
    for action, entity_id in switches.commands:
        if entity_id == PUMP:
            continue
        if action == "on":
            assert open_valve is None, (
                f"opened {entity_id} while {open_valve} was still open"
            )
            open_valve = entity_id
        else:
            assert open_valve == entity_id
            open_valve = None
    assert open_valve is None


async def test_completed_run_statuses_and_timestamps() -> None:
    """The run records planned and actual boundaries and confirmations."""
    sequencer, _, _, _ = make_sequencer(three_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    assert run.cycle_id == "2026-07-31-morning"
    assert run.kind is CycleKind.MORNING
    assert run.status is CycleStatus.COMPLETED
    assert run.pump_on_confirmed is True
    assert run.pump_off_confirmed is True
    zone1, zone2, zone3 = run.zone_runs
    assert zone1.planned_start == aware(7)
    assert zone1.planned_end == aware(7, 10)
    assert zone2.planned_start == aware(7, 10)
    assert zone2.planned_end == aware(7, 25)
    assert zone3.planned_end == aware(7, 30)
    for zone in run.zone_runs:
        assert zone.status is ZoneRunStatus.COMPLETED
        assert zone.actual_start == zone.planned_start
        assert zone.actual_end == zone.planned_end
        assert zone.open_confirmed is True
        assert zone.close_confirmed is True
    assert sequencer.current_run is None


async def test_next_wakeup_contract() -> None:
    """Idle → None; pending → scheduled start; running → next zone boundary."""
    sequencer, _, _, _ = make_sequencer(three_zone_plan())
    clock = VirtualClock(aware(7))
    assert sequencer.next_wakeup() is None

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    assert sequencer.next_wakeup() == aware(7)

    await sequencer.advance(clock.now())
    assert sequencer.next_wakeup() == aware(7, 10)


async def test_advance_is_idempotent_for_a_given_now() -> None:
    """Advancing twice at the same instant performs no duplicate transition."""
    sequencer, switches, journal, _ = make_sequencer(three_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    commands = list(switches.commands)
    saves = len(journal.snapshots)

    await sequencer.advance(clock.now())
    assert switches.commands == commands
    assert len(journal.snapshots) == saves


async def test_advance_before_the_scheduled_start_is_a_noop() -> None:
    """A stale advance (earlier than the pending start) performs nothing.

    Story 1.5's adapter may fire a timer armed before a new cycle was
    requested; the pending run must wait for its own scheduled start.
    """
    sequencer, switches, _, _ = make_sequencer(three_zone_plan())

    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    await sequencer.advance(aware(6, 59))

    assert switches.commands == []
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.PENDING
    assert sequencer.next_wakeup() == aware(7)


async def test_advance_mid_zone_is_a_noop() -> None:
    """An advance between boundaries finds no due transition."""
    sequencer, switches, _, _ = make_sequencer(three_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 5))
    commands = list(switches.commands)

    await sequencer.advance(clock.now())
    assert switches.commands == commands
    assert sequencer.next_wakeup() == aware(7, 10)


async def test_one_late_advance_performs_every_due_transition() -> None:
    """A single advance far past the end drains the whole cycle (missed wakeups)."""
    sequencer, switches, _, _ = make_sequencer(three_zone_plan())

    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    await sequencer.advance(aware(9))

    assert switches.commands[0] == ("on", PUMP)
    assert switches.commands[-1] == ("off", PUMP)
    assert len(switches.commands) == 8
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    assert sequencer.next_wakeup() is None


async def test_failed_pump_on_continues_fail_wet() -> None:
    """An unconfirmed pump-on reports an anomaly and the zones still run (FR4)."""
    sequencer, switches, _, anomalies = make_sequencer(three_zone_plan())
    switches.failing.add(("on", PUMP))
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    assert run.pump_on_confirmed is False
    assert run.status is CycleStatus.COMPLETED
    assert [entity for action, entity in switches.commands if action == "on"] == [
        PUMP,
        VALVE_1,
        VALVE_2,
        VALVE_3,
    ]
    kinds = [kind for kind, _ in anomalies.reports]
    assert kinds == [AnomalyKind.PUMP_ON_UNCONFIRMED]
    assert anomalies.reports[0][1]["cycle_id"] == "2026-07-31-morning"


async def test_failed_zone_open_still_consumes_its_slot() -> None:
    """A failed open marks the zone FAILED but its slot still elapses (AD-4)."""
    sequencer, switches, _, anomalies = make_sequencer(three_zone_plan())
    switches.failing.add(("on", VALVE_1))
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    zone1, zone2, _ = run.zone_runs
    assert zone1.status is ZoneRunStatus.FAILED
    assert zone1.open_confirmed is False
    assert zone1.actual_end == zone1.planned_end
    assert zone2.status is ZoneRunStatus.COMPLETED
    assert zone2.actual_start == aware(7, 10)
    assert run.status is CycleStatus.COMPLETED
    kinds = [kind for kind, _ in anomalies.reports]
    assert kinds == [AnomalyKind.VALVE_OPEN_UNCONFIRMED]
    assert anomalies.reports[0][1]["zone_id"] == "zone-1"


async def test_failed_zone_close_raises_anomaly_and_proceeds() -> None:
    """A failed close never stalls: anomaly, then the next zone opens (AD-4)."""
    sequencer, switches, _, anomalies = make_sequencer(three_zone_plan())
    switches.failing.add(("off", VALVE_1))
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    zone1 = run.zone_runs[0]
    assert zone1.status is ZoneRunStatus.COMPLETED
    assert zone1.close_confirmed is False
    assert run.status is CycleStatus.COMPLETED
    # The close was still COMMANDED before the next open (FR2 is about
    # commands), and the sequence proceeded.
    index_close = switches.commands.index(("off", VALVE_1))
    index_next_open = switches.commands.index(("on", VALVE_2))
    assert index_close < index_next_open
    kinds = [kind for kind, _ in anomalies.reports]
    assert kinds == [AnomalyKind.VALVE_CLOSE_UNCONFIRMED]


async def test_failed_pump_off_completes_the_cycle() -> None:
    """An unconfirmed pump-off is an anomaly, not an unfinished cycle."""
    sequencer, switches, _, anomalies = make_sequencer(three_zone_plan())
    switches.failing.add(("off", PUMP))
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    assert run.pump_off_confirmed is False
    kinds = [kind for kind, _ in anomalies.reports]
    assert kinds == [AnomalyKind.PUMP_OFF_UNCONFIRMED]


async def test_journal_saved_on_every_transition_with_versioned_payload() -> None:
    """Requested, started, each zone start/end, completed — one save each (NFR2)."""
    plan = make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600),
        make_zone("zone-2", valve=VALVE_2, morning_s=900),
    )
    sequencer, _, journal, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    # requested, cycle started, zone-1 started, zone-1 done, zone-2 started,
    # zone-2 done, cycle completed.
    assert len(journal.snapshots) == 7
    for snapshot in journal.snapshots:
        assert snapshot["schema_version"] == JOURNAL_SCHEMA_VERSION
        json.dumps(snapshot)  # serializable, or this raises

    def run_status(snapshot: dict[str, object]) -> str:
        run = snapshot["run"]
        assert isinstance(run, dict)
        status = run["status"]
        assert isinstance(status, str)
        return status

    assert run_status(journal.snapshots[0]) == "pending"
    assert run_status(journal.snapshots[1]) == "running"
    assert run_status(journal.snapshots[-1]) == "completed"


async def test_journal_datetimes_are_utc_iso_strings() -> None:
    """Datetimes serialize as UTC ISO-8601 (conventions): 07:00+02:00 → 05:00Z."""
    sequencer, _, journal, _ = make_sequencer(make_plan(make_zone()))
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    run = journal.snapshots[-1]["run"]
    assert isinstance(run, dict)
    assert run["scheduled_start"] == "2026-07-31T05:00:00+00:00"
    zone = run["zones"][0]
    assert zone["planned_end"] == "2026-07-31T05:10:00+00:00"


async def test_running_cycle_is_isolated_from_plan_changes() -> None:
    """AD-8: a mid-cycle plan change is invisible to the running cycle."""
    plan = three_zone_plan()
    sequencer, switches, _, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())

    # Swap the plan mid-cycle: different valves, durations and zone set.
    sequencer.plan = make_plan(
        make_zone("zone-9", valve="switch.other_valve", morning_s=60),
    )

    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    assert [zone.zone_id for zone in run.zone_runs] == ["zone-1", "zone-2", "zone-3"]
    assert run.zone_runs[2].planned_end == aware(7, 30)
    assert ("on", "switch.other_valve") not in switches.commands

    # The NEXT cycle uses the new plan.
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)
    next_run = sequencer.last_run
    assert next_run is not None
    assert [zone.zone_id for zone in next_run.zone_runs] == ["zone-9"]


async def test_cycle_requested_while_running_is_deferred_then_runs() -> None:
    """The runtime overlap defense: defer, then run immediately after (AD-4)."""
    plan = make_plan(make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=600))
    sequencer, switches, _, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    assert sequencer.current_run is not None

    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)

    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    assert run.kind is CycleKind.EVENING
    assert run.status is CycleStatus.COMPLETED
    # The deferred cycle started exactly when the first one completed.
    assert run.scheduled_start == aware(7, 10)
    assert not sequencer.deferred_kinds
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("off", PUMP),
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("off", PUMP),
    ]


async def test_cycle_requested_while_pending_is_deferred() -> None:
    """A not-yet-started run also defers new requests — never two active runs."""
    plan = make_plan(make_zone(morning_s=600, evening_s=600))
    sequencer, _, _, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)

    await run_to_idle(sequencer, clock)
    run = sequencer.last_run
    assert run is not None
    assert run.kind is CycleKind.EVENING
    assert sequencer.next_wakeup() is None


async def test_empty_plan_cycle_completes_without_commands() -> None:
    """No zones → nothing to water: no pump against a closed manifold."""
    sequencer, switches, _, anomalies = make_sequencer(make_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    assert switches.commands == []
    assert anomalies.reports == []
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    assert sequencer.next_wakeup() is None


async def test_snapshot_survives_zone_spec_replacement() -> None:
    """Snapshot means owned copies — replacing specs cannot leak into the run."""
    zone = make_zone("zone-1", valve=VALVE_1, morning_s=600)
    plan = make_plan(zone)
    sequencer, _, _, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    sequencer.plan = make_plan(replace(zone, morning_duration_s=60))

    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].duration_s == 600
    assert sequencer.next_wakeup() == aware(7)
