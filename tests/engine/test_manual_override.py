"""Manual override and scheduler pause on a virtual clock (Story 3.4).

The engine half only: the adapter reports `(entity_id, is_on, manual)` and
decides nothing (AD-7), so every case here is a direct
`async_switch_observed` call — exactly the shape the switch adapter's standing
watch produces.

Zero Home Assistant, zero fixtures (AD-1). The pause is LIVE state: nothing
here ever reads it back from a journal snapshot, because it is never written
to one.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.runs import CycleStatus
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

# Morning 07:00 → 07:20, evening 20:00 → 20:30.
MORNING_END = aware(7, 20)


def two_zone_plan() -> ControllerPlan:
    """Two zones of 600 s morning / 900 s evening; morning 07:00, evening 20:00."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900),
        make_zone("zone-2", valve=VALVE_2, morning_s=600, evening_s=900),
    )


def make_sequencer(
    plan: ControllerPlan | None = None,
    **seeds: Any,
) -> tuple[Sequencer, FakeSwitchPort, FakeJournalPort, FakeAnomalyPort]:
    """Wire a sequencer to fresh recording fakes, with any journal seeds."""
    switches = FakeSwitchPort()
    journal = FakeJournalPort()
    anomalies = FakeAnomalyPort()
    sequencer = Sequencer(
        plan if plan is not None else two_zone_plan(),
        switches=switches,
        journal=journal,
        anomalies=anomalies,
        **seeds,
    )
    return sequencer, switches, journal, anomalies


async def run_to_idle(sequencer: Sequencer, clock: VirtualClock) -> None:
    """Drive the ACTIVE cycle with the exact wake-up loop the runner uses.

    Watches the run, not `next_wakeup`: an idle engine still names the
    watchdog's next window end (Story 3.3), so a loop watching that would
    walk the calendar for ever.
    """
    while sequencer.current_run is not None:
        moment = sequencer.next_wakeup(clock.now())
        if moment is None:
            break
        clock.advance_to(moment)
        await sequencer.advance(clock.now())


async def manual_on(
    sequencer: Sequencer,
    entity_id: str,
    now: Any,
) -> None:
    """Report a hand-opened switch — a foreign context, nothing in flight."""
    await sequencer.async_switch_observed(
        entity_id,
        is_on=True,
        manual=True,
        now=now,
    )


async def observed_off(
    sequencer: Sequencer,
    entity_id: str,
    now: Any,
    *,
    manual: bool = True,
) -> None:
    """Report a switch observed off — by hand or by one of our own commands."""
    await sequencer.async_switch_observed(
        entity_id,
        is_on=False,
        manual=manual,
        now=now,
    )


# --------------------------------------------------------------------------
# Entering the pause
# --------------------------------------------------------------------------


async def test_a_manual_open_while_idle_pauses_and_commands_nothing() -> None:
    """AC 1 / matrix row 1: the pause is entered, silently."""
    sequencer, switches, journal, anomalies = make_sequencer()

    await manual_on(sequencer, VALVE_1, aware(6, 30))

    assert sequencer.manual_override is True
    assert switches.commands == []
    assert anomalies.reports == []
    assert anomalies.clears == []
    # Nothing to write: the pause is live state, never journalled.
    assert journal.snapshots == []


async def test_our_own_actuation_never_pauses() -> None:
    """AC 6 / matrix row 2: a transition the adapter attributes to us is not manual."""
    sequencer, _, journal, _ = make_sequencer()

    await sequencer.async_switch_observed(
        VALVE_1,
        is_on=True,
        manual=False,
        now=aware(7, 1),
    )

    assert sequencer.manual_override is False
    assert journal.snapshots == []


async def test_a_manual_close_on_its_own_never_pauses() -> None:
    """Decision 1: nothing is held open, so there is nothing to step aside from."""
    sequencer, switches, journal, _ = make_sequencer()

    await observed_off(sequencer, VALVE_1, aware(6, 30))

    assert sequencer.manual_override is False
    assert switches.commands == []
    assert journal.snapshots == []


async def test_the_pause_is_a_set_not_a_flag() -> None:
    """Matrix row 6: two hands-open entities need two closes to resume."""
    sequencer, _, _, _ = make_sequencer()

    await manual_on(sequencer, VALVE_1, aware(6, 30))
    await manual_on(sequencer, VALVE_2, aware(6, 31))
    await observed_off(sequencer, VALVE_1, aware(6, 40))

    assert sequencer.manual_override is True

    await observed_off(sequencer, VALVE_2, aware(6, 41))

    assert sequencer.manual_override is False


async def test_any_observed_off_clears_the_entry_whoever_commanded_it() -> None:
    """The engine's own close of a hand-opened valve ends the pause too.

    That is the hook Story 3.5's safety timeout pulls: it closes the valve
    through the verified port, the watch reports a NON-manual off, and the
    pause ends by the same path a hand-close takes.
    """
    sequencer, _, _, _ = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await observed_off(sequencer, VALVE_1, aware(6, 40), manual=False)

    assert sequencer.manual_override is False


async def test_an_off_queued_behind_its_own_on_still_releases() -> None:
    """The pre-lock filter is a filter, not the decision: re-tested inside.

    Both observations of one quick flip arrive while something else holds the
    lock. The OFF reads an `_manual_on` its own ON has not reached yet, so a
    pre-lock test alone would drop it — and the entity would stay held for
    ever with the switch physically off.
    """
    sequencer, _, _, _ = make_sequencer()
    lock = sequencer._lock  # noqa: SLF001 — the race IS the contract under test

    await lock.acquire()
    opened = asyncio.ensure_future(manual_on(sequencer, VALVE_1, aware(6, 30)))
    closed = asyncio.ensure_future(observed_off(sequencer, VALVE_1, aware(6, 30, 1)))
    await asyncio.sleep(0)
    lock.release()
    await asyncio.gather(opened, closed)

    assert sequencer.manual_override is False


async def test_a_reopen_landing_before_the_resume_holds_the_queue() -> None:
    """The drain re-reads the pause, so a second hand-open keeps the queue put.

    The release is applied the instant it is observed, but the drain it asks
    for waits for the lock — and another valve opened by hand meanwhile must
    win: draining then would start a cycle on top of an operator.
    """
    sequencer, switches, _, _ = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    lock = sequencer._lock  # noqa: SLF001 — the race IS the contract under test

    await lock.acquire()
    closed = asyncio.ensure_future(observed_off(sequencer, VALVE_1, aware(7, 5)))
    reopened = asyncio.ensure_future(manual_on(sequencer, VALVE_2, aware(7, 5, 1)))
    await asyncio.sleep(0)
    lock.release()
    await asyncio.gather(closed, reopened)

    assert sequencer.manual_override is True
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)
    assert sequencer.current_run is None
    assert switches.commands == []


async def test_a_run_now_is_never_handed_back_to_the_queue() -> None:
    """Decision 2: the invariant names PENDING *scheduled* runs, not manual ones.

    `CycleRunner.async_run_now` takes the lock twice — once to install the
    run, once to advance it — so an observation really can land on a PENDING
    run-now. Queueing it would strip its `manual` marker, make it waivable on
    a day credit and let it consume one.
    """
    sequencer, switches, _, _ = make_sequencer()
    clock = VirtualClock(aware(6, 40))
    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.PENDING
    assert run.manual is True

    await manual_on(sequencer, VALVE_1, aware(6, 40, 1))

    assert sequencer.current_run is run
    assert sequencer.deferred_kinds == ()

    await sequencer.advance(clock.now())

    assert sequencer.current_run is run
    assert run.manual is True
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]


async def test_nothing_is_observed_before_reconciliation() -> None:
    """Matrix row 10: a valve found open at boot is the reconciler's business."""
    sequencer, _, _, _ = make_sequencer(run_unreadable={})
    assert sequencer.reconciled is False

    await manual_on(sequencer, VALVE_1, aware(6, 30))

    assert sequencer.manual_override is False


# --------------------------------------------------------------------------
# What the pause suppresses
# --------------------------------------------------------------------------


async def test_a_scheduled_start_while_paused_is_queued_not_dispatched() -> None:
    """AC 2 / matrix row 3: no run, the kind queued, the queue saved."""
    sequencer, switches, journal, anomalies = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await sequencer.request_cycle(CycleKind.MORNING, aware(7))

    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)
    assert switches.commands == []
    assert anomalies.reports == []
    assert journal.snapshots[-1]["deferred"] == [
        {"kind": "morning", "reference": "2026-07-31T05:00:00+00:00"},
    ]


async def test_the_season_gate_stays_first() -> None:
    """Season OFF queues nothing, paused or not — a permitted non-watering cause."""
    sequencer, _, _, _ = make_sequencer(season_enabled=False)
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await sequencer.request_cycle(CycleKind.MORNING, aware(7))

    assert sequencer.deferred_kinds == ()


async def test_a_pending_run_is_handed_back_to_the_queue() -> None:
    """Matrix row 4: no PENDING scheduled run survives the start of a pause."""
    sequencer, switches, journal, _ = make_sequencer()

    # Requested at 06:55 for a 07:00 start: PENDING, nothing commanded.
    await sequencer.request_cycle(CycleKind.MORNING, aware(6, 55))
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.PENDING

    await manual_on(sequencer, VALVE_1, aware(6, 56))

    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)
    assert switches.commands == []
    assert journal.snapshots[-1]["run"] is None
    assert journal.snapshots[-1]["deferred"] == [
        {"kind": "morning", "reference": "2026-07-31T05:00:00+00:00"},
    ]


async def test_the_watchdog_returns_at_once_while_paused() -> None:
    """Matrix row 9: no missed record, no anomaly, no re-run; re-checked on resume."""
    sequencer, switches, journal, anomalies = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    # The morning window end: a miss the watchdog would normally make up.
    await sequencer.advance(MORNING_END)

    assert sequencer.current_run is None
    assert anomalies.reports == []
    assert switches.commands == []
    assert journal.snapshots == []

    # ...and it IS still detectable once the pause lifts.
    await observed_off(sequencer, VALVE_1, aware(7, 25))

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.MORNING
    assert run.late_rerun is True


async def test_a_deferred_start_stays_pending_for_the_watchdog() -> None:
    """A start deferred by the pause is "late, not lost", not a miss."""
    sequencer, _, _, anomalies = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))

    await observed_off(sequencer, VALVE_1, MORNING_END)

    # Drained, not made up: one run, and no `missed_cycle` anywhere.
    assert anomalies.reports == []
    run = sequencer.current_run
    assert run is not None
    assert run.late_rerun is False
    assert run.cycle_id == "2026-07-31-morning"


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------


async def test_the_last_close_drains_the_queue_and_starts_it_at_once() -> None:
    """AC 3 / matrix row 5: dispatched and STARTED in the same call."""
    sequencer, switches, _, _ = make_sequencer()
    clock = VirtualClock(aware(6, 30))
    await manual_on(sequencer, VALVE_1, clock.now())
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))

    clock.advance_to(aware(8, 10))
    await observed_off(sequencer, VALVE_1, clock.now())

    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.cycle_id == "2026-07-31-morning"
    # Its configured start is the plan's 07:00, its windows start at 08:10.
    assert run.configured_start == aware(7)
    assert run.zone_runs[0].planned_end == aware(8, 20)
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]
    assert sequencer.deferred_kinds == ()

    await run_to_idle(sequencer, clock)
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED


async def test_resume_quotes_the_durations_fresh() -> None:
    """AC 3: the deferred cycle is built at resume time, on the CURRENT ledger.

    A deficit carried by an earlier cycle lengthens the zone that owes it —
    proof the run was quoted at the pop, not at the request.
    """
    sequencer, _, _, _ = make_sequencer(
        ledger={"deficits": {"zone-1": 120}, "rain_baselines": {}},
    )
    await manual_on(sequencer, VALVE_1, aware(6, 30))
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))

    await observed_off(sequencer, VALVE_1, aware(8, 10))

    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].duration_s == 720
    assert run.zone_runs[0].carried_s == 120
    assert run.zone_runs[1].duration_s == 600


async def test_a_stale_day_deferral_is_dropped_at_resume() -> None:
    """Yesterday's queued cycle belongs to the watchdog, not to the resume."""
    sequencer, switches, _, _ = make_sequencer(
        deferred=[(CycleKind.EVENING, aware(20, day=30))],
    )
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await observed_off(sequencer, VALVE_1, aware(6, 40))

    assert sequencer.deferred_kinds == ()
    assert sequencer.current_run is None
    assert switches.commands == []


async def test_a_stale_drop_behind_a_running_cycle_is_still_journalled() -> None:
    """The drain is held by the live run, so the drop needs its own save."""
    sequencer, _, journal, _ = make_sequencer(
        deferred=[(CycleKind.EVENING, aware(20, day=30))],
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await manual_on(sequencer, VALVE_2, aware(7, 2))

    await observed_off(sequencer, VALVE_2, aware(7, 3))

    assert sequencer.deferred_kinds == ()
    assert journal.snapshots[-1]["deferred"] == []
    assert sequencer.current_run is not None


async def test_a_partial_resume_drains_nothing() -> None:
    """Matrix row 7: one of two hand-opened valves closing changes nothing."""
    sequencer, switches, _, _ = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))
    await manual_on(sequencer, VALVE_2, aware(6, 31))
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))

    await observed_off(sequencer, VALVE_1, aware(7, 5))

    assert sequencer.manual_override is True
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)
    assert sequencer.current_run is None
    assert switches.commands == []


async def test_a_resume_with_an_empty_queue_writes_nothing() -> None:
    """Nothing changed, nothing to say: no command, no anomaly, no journal write."""
    sequencer, switches, journal, anomalies = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await observed_off(sequencer, VALVE_1, aware(6, 40))

    assert sequencer.manual_override is False
    assert switches.commands == []
    assert anomalies.reports == []
    assert journal.snapshots == []


# --------------------------------------------------------------------------
# A cycle in progress is never fought
# --------------------------------------------------------------------------


async def test_a_running_cycle_is_untouched_by_a_manual_flip() -> None:
    """AC 4 / matrix row 8: the cycle runs on, the flipped valve is never commanded."""
    sequencer, switches, _, _ = make_sequencer()
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING

    await manual_on(sequencer, VALVE_2, aware(7, 3))

    assert sequencer.current_run is run
    assert sequencer.manual_override is True
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]

    await run_to_idle(sequencer, clock)

    # Zone 2's slot commands it as usual — a live cycle is not the pause's
    # business; the pause only stops the SCHEDULER from starting things.
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("off", PUMP),
    ]


async def test_a_completion_drains_nothing_while_the_pause_holds() -> None:
    """AC 4: the completing cycle starts no further cycle while a hand holds a valve."""
    sequencer, switches, _, _ = make_sequencer()
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    # Queued behind the running cycle, then the operator opens a valve.
    await sequencer.request_cycle(CycleKind.EVENING, aware(7, 2))
    await manual_on(sequencer, VALVE_2, aware(7, 3))

    await run_to_idle(sequencer, clock)

    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)
    assert switches.commands[-1] == ("off", PUMP)

    # ...and the same queue drains the moment the valve closes.
    await observed_off(sequencer, VALVE_2, aware(7, 30))

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.EVENING


async def test_the_operator_closing_the_live_valve_never_re_opens_it() -> None:
    """Matrix row 9b: no re-open, no pause; the slot's own close confirms trivially."""
    sequencer, switches, _, _ = make_sequencer()
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())

    await observed_off(sequencer, VALVE_1, aware(7, 4))

    assert sequencer.manual_override is False
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]

    await run_to_idle(sequencer, clock)
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED


# --------------------------------------------------------------------------
# Neither run_now nor cancel is refused while paused (Decision 2)
# --------------------------------------------------------------------------


async def test_run_now_is_honoured_while_paused() -> None:
    """Decision 2: the operator asking is not the scheduler guessing."""
    sequencer, switches, _, _ = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    assert await sequencer.async_run_now(CycleKind.MORNING, aware(6, 40)) is True
    await sequencer.advance(aware(6, 40))

    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]
    assert sequencer.manual_override is True


async def test_cancel_is_honoured_while_paused_and_leaves_the_queue_clear() -> None:
    """A cancel while paused stops the cycle and empties the queue, as ever."""
    sequencer, _, _, _ = make_sequencer()
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await manual_on(sequencer, VALVE_2, aware(7, 3))

    assert await sequencer.async_cancel_cycle(aware(7, 5)) is True

    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == ()
    assert sequencer.manual_override is True
