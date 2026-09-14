"""Sequencer state machine on a virtual clock (AC 2, 3, 4, 5).

Every test drives the engine exactly the way Story 1.5's adapter will:
`while (t := engine.next_wakeup()) is not None: clock.advance_to(t);
await engine.advance(clock.now())` — no sleeps, no wall clock, no HA.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import time

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
    history: list[dict[str, object]] | None = None,
) -> tuple[Sequencer, FakeSwitchPort, FakeJournalPort, FakeAnomalyPort]:
    """Wire a sequencer to fresh recording fakes, optionally seeded."""
    switches = FakeSwitchPort()
    journal = FakeJournalPort()
    anomalies = FakeAnomalyPort()
    sequencer = Sequencer(
        plan,
        switches=switches,
        journal=journal,
        anomalies=anomalies,
        history=history,
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
    """A single advance far past the end drains the whole cycle (missed wakeups).

    KNOWN LIMITATION, pinned here deliberately rather than left implicit: every
    elapsed boundary fires at the same instant, so each zone is opened and
    closed with `actual_start == actual_end` and the cycle is recorded
    COMPLETED having watered nothing. On real hardware that is a burst of
    back-to-back relay commands, and Epic 2's ledger reads these run objects as
    a completed cycle. Reviewed 2026-07-31 and kept as-is; see deferred-work.md.
    """
    sequencer, switches, _, anomalies = make_sequencer(three_zone_plan())

    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    await sequencer.advance(aware(9))

    assert switches.commands[0] == ("on", PUMP)
    assert switches.commands[-1] == ("off", PUMP)
    assert len(switches.commands) == 8
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    assert sequencer.next_wakeup() is None
    # The limitation itself: zero elapsed watering, recorded as success.
    for zone in run.zone_runs:
        assert zone.status is ZoneRunStatus.COMPLETED
        assert zone.actual_start == aware(9)
        assert zone.actual_end == aware(9)
    assert anomalies.reports == []


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


async def test_run_is_anchored_on_the_plans_configured_start() -> None:
    """The plan's start time drives the schedule, not the caller's instant (FR1).

    Story 1.5 arms `async_track_time_change` on the configured start, so `now`
    normally coincides with it — but the plan is the single source of the
    schedule (AD-6), and a timer that fires a little late must not shift every
    zone window with it.
    """
    sequencer, _, _, _ = make_sequencer(three_zone_plan())

    await sequencer.request_cycle(CycleKind.MORNING, aware(7, 0, 12))

    run = sequencer.current_run
    assert run is not None
    assert run.configured_start == aware(7)
    assert run.scheduled_start == aware(7)
    assert run.zone_runs[0].planned_start == aware(7)
    assert run.zone_runs[-1].planned_end == aware(7, 30)


async def test_next_wakeup_is_safe_at_every_instant_the_engine_yields() -> None:
    """The re-arm contract holds even mid-transition (no IndexError window).

    A 1.5 adapter that persists then re-arms calls `next_wakeup()` from inside
    the journal save — including the save issued between the last zone closing
    and the cycle completing, when the zone index points past the last zone.
    """
    sequencer, _, journal, _ = make_sequencer(three_zone_plan())
    clock = VirtualClock(aware(7))
    seen: list[object] = []
    journal.observer = lambda: seen.append(sequencer.next_wakeup())

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    assert seen  # the observer ran; no call raised
    assert seen[-1] is None


async def test_next_wakeup_is_safe_for_a_running_empty_cycle() -> None:
    """A zero-zone run reaches RUNNING with no zone to point at."""
    sequencer, _, journal, _ = make_sequencer(make_plan())
    seen: list[object] = []
    journal.observer = lambda: seen.append(sequencer.next_wakeup())

    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    await sequencer.advance(aware(7))

    assert seen[-1] is None


async def test_concurrent_advance_calls_do_not_interleave() -> None:
    """Overlapping advances are serialized — no duplicate hardware commands.

    Story 1.5's timer can fire while a previous advance is still awaiting a
    state-verified service call; without serialization the two calls interleave
    their mutations and command the same valve twice.
    """
    sequencer, switches, _, _ = make_sequencer(three_zone_plan())

    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    await asyncio.gather(sequencer.advance(aware(7)), sequencer.advance(aware(7)))

    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]
    assert sequencer.next_wakeup() == aware(7, 10)


async def test_a_switch_port_that_raises_is_treated_as_unconfirmed() -> None:
    """A leaked adapter exception never aborts the cycle (AD-4).

    Letting it propagate would abandon the run mid-flight with a valve open and
    no re-arm — the one branch fail-wet forbids.
    """
    sequencer, switches, _, anomalies = make_sequencer(three_zone_plan())
    switches.raising.add(("on", VALVE_1))
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    zone1, zone2, zone3 = run.zone_runs
    assert zone1.status is ZoneRunStatus.FAILED
    assert zone1.open_confirmed is False
    assert zone2.status is ZoneRunStatus.COMPLETED
    assert zone3.status is ZoneRunStatus.COMPLETED
    kind, context = anomalies.reports[0]
    assert kind is AnomalyKind.VALVE_OPEN_UNCONFIRMED
    assert "PortError" in str(context["error"])


async def test_a_journal_port_that_raises_reports_and_keeps_watering() -> None:
    """A failed save is an anomaly, not a stopped cycle (NFR2 is best-effort)."""
    sequencer, switches, journal, anomalies = make_sequencer(three_zone_plan())
    journal.raising = True
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    assert switches.commands[0] == ("on", PUMP)
    assert switches.commands[-1] == ("off", PUMP)
    kinds = {kind for kind, _ in anomalies.reports}
    assert kinds == {AnomalyKind.JOURNAL_SAVE_FAILED}


async def test_repeated_requests_get_distinct_cycle_ids() -> None:
    """Same-day runs of one kind stay distinguishable (AD-5 keys off the id).

    The queue accepts repeats on purpose (Epic 2's run-now), so the id carries
    an occurrence suffix rather than colliding.
    """
    plan = make_plan(make_zone("zone-1", valve=VALVE_1, morning_s=600))
    sequencer, _, _, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.MORNING, CycleKind.MORNING)

    ids: list[str] = []
    while (moment := sequencer.next_wakeup()) is not None:
        clock.advance_to(moment)
        await sequencer.advance(clock.now())
        run = sequencer.last_run
        if run is not None and run.cycle_id not in ids:
            ids.append(run.cycle_id)

    assert ids == [
        "2026-07-31-morning",
        "2026-07-31-morning-2",
        "2026-07-31-morning-3",
    ]


async def test_a_cycle_deferred_across_midnight_keeps_its_irrigation_day() -> None:
    """Day attribution follows the configured start, never the dispatch instant.

    `irrigation_day` is THE helper FR12's waiver, FR18's resume and FR21's
    re-run window all key off; a deferred evening cycle that happens to start
    after midnight still belongs to the day it was scheduled for.
    """
    plan = make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=600),
        morning_start=time(23, 55),
        evening_start=time(20, 0),
    )
    sequencer, _, _, _ = make_sequencer(plan)
    clock = VirtualClock(aware(23, 55))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())

    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    assert run.kind is CycleKind.EVENING
    # Dispatched after midnight, still filed under the day it was requested for.
    assert run.scheduled_start == aware(0, 5, day=1, month=8)
    assert run.configured_start == aware(20)
    assert run.cycle_id == "2026-07-31-evening"


async def test_history_gets_one_entry_per_completed_cycle_oldest_first() -> None:
    """Each completed cycle appends its outcome record, in completion order."""
    plan = make_plan(make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900))
    sequencer, _, journal, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    await run_to_idle(sequencer, clock)

    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert [entry["cycle_id"] for entry in history] == [
        "2026-07-31-morning",
        "2026-07-31-evening",
    ]
    assert history[0]["zones"] == [
        {
            "zone_id": "zone-1",
            "status": "completed",
            "planned_s": 600,
            "carried_s": 0,
            "effective_s": 600,
        },
    ]
    json.dumps(history)  # serializable, or this raises


async def test_seeded_history_is_kept_and_appended_to() -> None:
    """A rebuilt engine keeps the outcomes it was handed (the reload path).

    Without the seed the first `_save` after a reload would overwrite the
    stored 7-day section with an empty list — and the reload regime runs on
    every config change.
    """
    plan = make_plan(make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900))
    prior: list[dict[str, object]] = [
        {"irrigation_day": "2026-07-30", "cycle_id": "2026-07-30-evening"},
    ]
    sequencer, _, journal, _ = make_sequencer(plan, prior)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert [entry["cycle_id"] for entry in history] == [
        "2026-07-30-evening",
        "2026-07-31-morning",
    ]


async def test_the_seed_is_copied_not_aliased() -> None:
    """The caller's list must not grow behind its back (AD-8: owned copies)."""
    prior: list[dict[str, object]] = []
    plan = make_plan(make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900))
    sequencer, _, _, _ = make_sequencer(plan, prior)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    assert prior == []


async def test_history_records_zero_effective_seconds_for_a_failed_zone() -> None:
    """A zone whose open never confirmed watered nothing (Epic 2's input)."""
    sequencer, switches, journal, _ = make_sequencer(three_zone_plan())
    switches.failing.add(("on", VALVE_1))
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert history[-1]["zones"][0] == {
        "zone_id": "zone-1",
        "status": "failed",
        "planned_s": 600,
        "carried_s": 0,
        "effective_s": 0,
    }
    assert history[-1]["zones"][1]["effective_s"] == 900


async def test_history_is_pruned_to_the_retention_window() -> None:
    """Cycles older than the 7-day window fall out as new ones complete."""
    plan = make_plan(make_zone("zone-1", valve=VALVE_1, morning_s=600))
    sequencer, _, journal, _ = make_sequencer(plan)

    for day in (25, 31):
        clock = VirtualClock(aware(7, day=day, month=7))
        await sequencer.request_cycle(CycleKind.MORNING, clock.now())
        await run_to_idle(sequencer, clock)

    # A 3 August cycle is 9 days past the 25 July one (dropped) and 3 days
    # past the 31 July one (kept).
    clock = VirtualClock(aware(7, day=3, month=8))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert [entry["irrigation_day"] for entry in history] == [
        "2026-07-31",
        "2026-08-03",
    ]


async def test_history_survives_a_deferred_cycle_created_in_the_same_call() -> None:
    """The append happens before the deferral branch, never after it."""
    plan = make_plan(make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=600))
    sequencer, _, journal, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())

    clock.advance_to(aware(7, 10))
    await sequencer.advance(clock.now())

    # The morning cycle completed and the evening one was created in the SAME
    # advance: the snapshot taken right after must already carry the record.
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert [entry["cycle_id"] for entry in history] == ["2026-07-31-morning"]


async def test_journal_snapshot_carries_what_a_restore_needs() -> None:
    """The snapshot must be restorable, not merely displayable.

    `zone_index` is the only thing that says which zone is currently open (a
    FAILED zone looks identical whether it is the live slot or a finished one),
    and the completed run must survive the creation of a deferred one.
    """
    plan = make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=600),
        make_zone("zone-2", valve=VALVE_2, morning_s=600, evening_s=600),
    )
    sequencer, _, journal, _ = make_sequencer(plan)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())

    pending = journal.snapshots[-1]
    assert pending["zone_index"] == 0
    assert pending["deferred"] == [
        {"kind": "evening", "reference": "2026-07-31T05:00:00+00:00"},
    ]

    clock.advance_to(aware(7, 10))
    await sequencer.advance(clock.now())
    assert journal.snapshots[-1]["zone_index"] == 1

    await run_to_idle(sequencer, clock)
    final = journal.snapshots[-1]
    json.dumps(final)  # still serializable with the added fields
    last_run = final["last_run"]
    assert isinstance(last_run, dict)
    assert last_run["cycle_id"] == "2026-07-31-morning"
    active = final["run"]
    assert isinstance(active, dict)
    assert active["cycle_id"] == "2026-07-31-evening"
