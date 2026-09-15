"""Forced-unload suspend and the mid-run plan swap on a virtual clock (Story 1.7).

Two engine capabilities the reload deferral rests on, driven the way every
other engine suite drives the machine — no sleeps, no wall clock, no Home
Assistant:

- `async_suspend` makes the hardware safe when the entry is torn down under a
  running cycle, and mutates NOTHING: the journal keeps the intent Story 3.2
  resumes from.
- `plan` is replaceable mid-run: the running cycle never notices, the next
  cycle created (a deferred-queue pop inside `_complete_cycle` included) is
  built from the new plan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleStatus,
    ZoneRunStatus,
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


def two_zone_plan(*, zone_1_s: int = 600, zone_2_s: int = 600) -> ControllerPlan:
    """Build a two-zone plan from 07:00 with the given per-zone seconds."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=zone_1_s, evening_s=zone_1_s),
        make_zone("zone-2", valve=VALVE_2, morning_s=zone_2_s, evening_s=zone_2_s),
    )


def make_sequencer(
    plan: ControllerPlan,
) -> tuple[Sequencer, FakeSwitchPort, FakeJournalPort, FakeAnomalyPort]:
    """Wire a sequencer to fresh recording fakes."""
    switches = FakeSwitchPort()
    journal = FakeJournalPort()
    anomalies = FakeAnomalyPort()
    sequencer = Sequencer(plan, switches=switches, journal=journal, anomalies=anomalies)
    return sequencer, switches, journal, anomalies


async def start_morning_cycle(
    sequencer: Sequencer,
    clock: VirtualClock,
) -> None:
    """Request the morning cycle at 07:00 and start it: pump on, zone 1 open."""
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())


# --------------------------------------------------------------------------
# async_suspend
# --------------------------------------------------------------------------


async def test_suspend_with_nothing_active_is_a_no_op() -> None:
    """Idle: False, no command, no anomaly, no journal write."""
    sequencer, switches, journal, anomalies = make_sequencer(two_zone_plan())

    assert await sequencer.async_suspend(aware(7)) is False

    assert switches.commands == []
    assert anomalies.reports == []
    assert journal.snapshots == []


async def test_suspend_of_a_pending_run_reports_the_interruption_only() -> None:
    """PENDING commanded nothing, so nothing is un-commanded — but it is lost.

    AD-4: a scheduled cycle lost to a forced unload (pre-3.2) is never
    silent, so `CYCLE_INTERRUPTED` is raised even though no valve was open.
    """
    sequencer, switches, journal, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6, 59))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.PENDING
    saves = len(journal.snapshots)
    wakeup = sequencer.next_wakeup()

    assert await sequencer.async_suspend(clock.now()) is True

    assert switches.commands == []
    assert anomalies.reports == [
        (
            AnomalyKind.CYCLE_INTERRUPTED,
            {"cycle_id": run.cycle_id, "kind": "morning", "zone_id": None},
        ),
    ]
    # Nothing mutated, nothing saved: the intent is exactly as journalled.
    assert sequencer.current_run is run
    assert run.status is CycleStatus.PENDING
    assert len(journal.snapshots) == saves
    assert sequencer.next_wakeup() == wakeup


async def test_suspend_of_a_running_cycle_closes_the_valve_then_the_pump() -> None:
    """RUNNING: live valve off, then pump off, `CYCLE_INTERRUPTED` — and no mutation.

    Decision 1: the run keeps its RUNNING status, the zone keeps its open
    slot (no `actual_end`, no `close_confirmed`), the journal is not written,
    `last_run` stays empty and `next_wakeup()` still names the boundary. The
    journal therefore holds an honest RUNNING intent for Story 3.2.
    """
    sequencer, switches, journal, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await start_morning_cycle(sequencer, clock)
    run = sequencer.current_run
    assert run is not None
    saves = len(journal.snapshots)
    wakeup = sequencer.next_wakeup()
    clock.advance_to(aware(7, 4))

    assert await sequencer.async_suspend(clock.now()) is True

    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("off", PUMP),
    ]
    assert anomalies.reports == [
        (
            AnomalyKind.CYCLE_INTERRUPTED,
            {"cycle_id": run.cycle_id, "kind": "morning", "zone_id": "zone-1"},
        ),
    ]
    assert sequencer.current_run is run
    assert sequencer.last_run is None
    assert run.status is CycleStatus.RUNNING
    zone = run.zone_runs[0]
    assert zone.status is ZoneRunStatus.RUNNING
    assert zone.actual_end is None
    assert zone.close_confirmed is None
    assert run.pump_off_confirmed is None
    assert len(journal.snapshots) == saves
    assert sequencer.next_wakeup() == wakeup == aware(7, 10)


async def test_suspend_with_unconfirmed_closes_reports_both_and_still_succeeds() -> (
    None
):
    """A stuck valve or pump reports its own anomaly; the suspend still completes.

    Same fail-safe rule as a cancel: an unconfirmed close is reported, never
    what wedges the unload. The interruption is reported LAST, after the
    hardware outcome is known.
    """
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await start_morning_cycle(sequencer, clock)
    run = sequencer.current_run
    assert run is not None
    switches.failing = {("off", VALVE_1), ("off", PUMP)}

    assert await sequencer.async_suspend(clock.now()) is True

    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        AnomalyKind.PUMP_OFF_UNCONFIRMED,
        AnomalyKind.CYCLE_INTERRUPTED,
    ]
    assert anomalies.reports[0][1] == {
        "cycle_id": run.cycle_id,
        "zone_id": "zone-1",
        "entity_id": VALVE_1,
    }
    assert anomalies.reports[1][1] == {"cycle_id": run.cycle_id, "entity_id": PUMP}
    # Still no mutation: the failed close is not recorded on the zone either.
    assert run.zone_runs[0].close_confirmed is None
    assert run.pump_off_confirmed is None


async def test_suspend_of_a_raising_port_still_completes() -> None:
    """A port that leaks an exception is a not-confirmed outcome, not a crash."""
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await start_morning_cycle(sequencer, clock)
    switches.raising = {("off", VALVE_1)}

    assert await sequencer.async_suspend(clock.now()) is True

    kinds = [kind for kind, _ in anomalies.reports]
    assert kinds == [
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        AnomalyKind.CYCLE_INTERRUPTED,
    ]
    assert "error" in anomalies.reports[0][1]
    # The pump was still commanded off after the valve raised.
    assert switches.commands[-1] == ("off", PUMP)


# --------------------------------------------------------------------------
# plan swap during a deferral
# --------------------------------------------------------------------------


async def test_a_plan_swap_mid_run_leaves_the_running_cycle_untouched() -> None:
    """The running cycle reads only its AD-8 snapshot: new durations do not apply."""
    sequencer, switches, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await start_morning_cycle(sequencer, clock)
    run = sequencer.current_run
    assert run is not None

    # Zone 2 shortened to five minutes while zone 1 waters.
    sequencer.plan = two_zone_plan(zone_2_s=300)

    assert run.zone_runs[1].duration_s == 600
    assert sequencer.next_wakeup() == aware(7, 10)
    clock.advance_to(aware(7, 10))
    await sequencer.advance(clock.now())
    # Zone 2 still closes at its snapshotted 07:20, not at 07:15.
    assert sequencer.next_wakeup() == aware(7, 20)
    clock.advance_to(aware(7, 15))
    await sequencer.advance(clock.now())
    assert switches.commands[-1] == ("on", VALVE_2)
    clock.advance_to(aware(7, 20))
    await sequencer.advance(clock.now())

    assert sequencer.current_run is None
    assert sequencer.last_run is run
    assert run.status is CycleStatus.COMPLETED
    assert run.zone_runs[1].actual_end == aware(7, 20)


async def test_a_deferred_cycle_popped_after_the_swap_uses_the_new_plan() -> None:
    """AC 2 for the deferred queue: `_complete_cycle` reads `self.plan`, not the entry.

    The evening cycle is queued behind the morning one, the plan is swapped
    while the morning cycle runs, and the evening run created inside
    `_complete_cycle` must carry the NEW durations — and `next_wakeup()` must
    reflect them.
    """
    sequencer, _, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await start_morning_cycle(sequencer, clock)
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)

    sequencer.plan = two_zone_plan(zone_1_s=300, zone_2_s=120)

    clock.advance_to(aware(7, 10))
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 20))
    await sequencer.advance(clock.now())

    # The morning run completed; the evening run was created from the new
    # plan and dispatched at once (same `advance` call).
    morning = sequencer.last_run
    assert morning is not None
    assert morning.kind is CycleKind.MORNING
    assert [zone.duration_s for zone in morning.zone_runs] == [600, 600]
    evening = sequencer.current_run
    assert evening is not None
    assert evening.kind is CycleKind.EVENING
    assert evening.status is CycleStatus.RUNNING
    assert [zone.duration_s for zone in evening.zone_runs] == [300, 120]
    assert evening.zone_runs[0].planned_end == aware(7, 25)
    assert sequencer.next_wakeup() == aware(7, 25)


async def test_suspend_clears_what_it_confirms_before_reporting_the_interruption() -> (
    None
):
    """The confirmed close and pump-off are "healthy again" even on a suspend.

    A suspend commands two actuations through the same port as a cycle; when
    they confirm, the kinds they prove are cleared (the close, the pump-off),
    and ONLY then is `CYCLE_INTERRUPTED` reported — a suspend never clears
    that one.
    """
    sequencer, _, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await start_morning_cycle(sequencer, clock)

    assert await sequencer.async_suspend(clock.now())

    assert [kind for kind, _ in anomalies.reports] == [AnomalyKind.CYCLE_INTERRUPTED]
    assert [
        (kind, context.get("zone_id", context.get("entity_id")))
        for kind, context in anomalies.clears
        if kind is not AnomalyKind.JOURNAL_SAVE_FAILED
    ][-2:] == [
        (AnomalyKind.VALVE_CLOSE_UNCONFIRMED, "zone-1"),
        (AnomalyKind.PUMP_OFF_UNCONFIRMED, PUMP),
    ]
    assert AnomalyKind.CYCLE_INTERRUPTED not in {kind for kind, _ in anomalies.clears}
