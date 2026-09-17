"""Season gate and explicit cancel on a virtual clock (Story 1.6, AC 1, 2, 4).

The two engine capabilities this story adds, driven the way every other engine
suite drives the machine: `while (t := engine.next_wakeup()) is not None` — no
sleeps, no wall clock, no Home Assistant.

Both are deliberately quiet: season OFF is a PERMITTED non-watering cause and
an operator cancel is an intent, so neither raises an anomaly (AD-4). The
anomaly log is asserted empty in almost every test below for exactly that
reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.runs import (
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


def two_zone_plan() -> ControllerPlan:
    """Build a two-zone plan: ten morning minutes each, from 07:00."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=600),
        make_zone("zone-2", valve=VALVE_2, morning_s=600, evening_s=600),
    )


def make_sequencer(
    plan: ControllerPlan,
    *,
    season_enabled: bool = True,
) -> tuple[Sequencer, FakeSwitchPort, FakeJournalPort, FakeAnomalyPort]:
    """Wire a sequencer to fresh recording fakes at a given season state."""
    switches = FakeSwitchPort()
    journal = FakeJournalPort()
    anomalies = FakeAnomalyPort()
    sequencer = Sequencer(
        plan,
        switches=switches,
        journal=journal,
        anomalies=anomalies,
        season_enabled=season_enabled,
    )
    return sequencer, switches, journal, anomalies


async def run_to_idle(sequencer: Sequencer, clock: VirtualClock) -> None:
    """Drive the ACTIVE cycle with the exact wake-up loop the runner uses.

    Stops the moment the machine goes idle. Since Story 3.3 `next_wakeup`
    answers the watchdog's next window-end deadline while idle — the runner's
    loop is the same one and simply never stops — so a test loop that only
    watched it would walk the calendar for ever.
    """
    while sequencer.current_run is not None:
        moment = sequencer.next_wakeup(clock.now())
        if moment is None:
            break
        clock.advance_to(moment)
        await sequencer.advance(clock.now())


# --------------------------------------------------------------------------
# Season (AC 1, AC 2)
# --------------------------------------------------------------------------


async def test_season_defaults_to_on() -> None:
    """Fail-wet: an unspecified season waters (AD-4)."""
    sequencer, _, _, _ = make_sequencer(two_zone_plan())

    assert sequencer.season_enabled is True


async def test_season_off_creates_no_run_queues_nothing_and_saves_nothing() -> None:
    """AC 2: a daily start under season OFF is a permitted non-watering cause.

    No run, no defer, no journal write and — the part a watchdog would get
    wrong — NO anomaly. The daily tracker itself stays armed; the gate is here.
    """
    sequencer, switches, journal, anomalies = make_sequencer(
        two_zone_plan(),
        season_enabled=False,
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())

    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == ()
    assert switches.commands == []
    assert journal.snapshots == []
    assert anomalies.reports == []
    # Nothing armed either: with the season off the watchdog can only ever
    # find nothing, so it names no deadline (Story 3.3).
    assert sequencer.next_wakeup(clock.now()) is None


async def test_season_on_behaves_exactly_as_before() -> None:
    """The gate is the ONLY change to `request_cycle` — season ON is untouched."""
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("off", PUMP),
    ]
    assert anomalies.reports == []


async def test_turning_the_season_off_clears_a_queued_deferred_cycle() -> None:
    """AC 1: "suspends all scheduling in one action" includes queued work."""
    sequencer, _, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)

    assert await sequencer.async_set_season(enabled=False, now=clock.now()) is True

    assert not sequencer.deferred_kinds
    # The RUNNING cycle is untouched: only an explicit cancel stops one.
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING


async def test_setting_the_season_to_its_current_value_changes_nothing() -> None:
    """Idempotent: no change means no write — the journal is not churned."""
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    assert await sequencer.async_set_season(enabled=True, now=clock.now()) is False

    assert journal.snapshots == []
    assert sequencer.season_enabled is True


async def test_the_snapshot_carries_the_season_in_both_states() -> None:
    """One more key in the ONE document — never a second store (AD-2)."""
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    assert await sequencer.async_set_season(enabled=False, now=clock.now()) is True
    assert journal.snapshots[-1]["season_enabled"] is False

    assert await sequencer.async_set_season(enabled=True, now=clock.now()) is True
    assert journal.snapshots[-1]["season_enabled"] is True


async def test_a_running_cycle_completes_when_the_season_goes_off() -> None:
    """AC 1: a cycle already RUNNING finishes normally, pump-off included."""
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.async_set_season(enabled=False, now=clock.now())

    await run_to_idle(sequencer, clock)

    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("off", PUMP),
    ]
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    assert anomalies.reports == []


async def test_season_off_then_on_honours_the_next_daily_start() -> None:
    """Nothing is replayed; the next start simply waters again."""
    sequencer, switches, _, _ = make_sequencer(two_zone_plan(), season_enabled=False)
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    assert switches.commands == []

    await sequencer.async_set_season(enabled=True, now=clock.now())
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED


# --------------------------------------------------------------------------
# Cancel (AC 4)
# --------------------------------------------------------------------------


async def test_cancelling_while_idle_reports_false_and_saves_nothing() -> None:
    """The engine speaks no user-facing errors — the SERVICE raises, not this."""
    sequencer, switches, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    assert await sequencer.async_cancel_cycle(clock.now()) is False

    assert switches.commands == []
    assert journal.snapshots == []


async def test_cancelling_a_pending_run_commands_nothing_at_all() -> None:
    """Nothing was ever commanded, so nothing must be un-commanded."""
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6, 50))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.PENDING

    assert await sequencer.async_cancel_cycle(clock.now()) is True

    assert switches.commands == []
    assert anomalies.reports == []
    assert sequencer.current_run is None
    filed = sequencer.last_run
    assert filed is not None
    assert filed.status is CycleStatus.CANCELLED
    assert filed.pump_off_confirmed is None
    # Nothing of the cancelled run is left to serve; the watchdog's deadline
    # on the morning window end is (the cancelled record makes it a no-op).
    assert sequencer.next_wakeup(clock.now()) == aware(7, 20)


async def test_cancelling_mid_zone_closes_the_live_valve_then_the_pump() -> None:
    """AC 4: valve before pump — the pump on a closed manifold is the safe order."""
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 4))

    assert await sequencer.async_cancel_cycle(clock.now()) is True

    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("off", PUMP),
    ]
    assert anomalies.reports == []
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.CANCELLED
    assert run.pump_off_confirmed is True
    # The live zone really closed at the cancel instant...
    first, second = run.zone_runs
    assert first.status is ZoneRunStatus.COMPLETED
    assert first.actual_end == aware(7, 4)
    assert effective_seconds(first) == 240
    # ...and the zone never reached stays PENDING, so Epic 2's deficit reads
    # the shortfall from the ONE helper for free (AD-5).
    assert second.status is ZoneRunStatus.PENDING
    assert effective_seconds(second) == 0
    assert sequencer.next_wakeup(clock.now()) == aware(7, 20)


async def test_a_cancelled_cycle_is_filed_in_history() -> None:
    """`history_entry` writes `run.status.value`, so cancel records itself."""
    sequencer, _, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 4))
    await sequencer.async_cancel_cycle(clock.now())

    run = sequencer.last_run
    assert run is not None
    entry = run.as_dict()
    assert entry["status"] == "cancelled"


async def test_cancel_drops_a_deferred_cycle_instead_of_starting_it() -> None:
    """Cancelling "the cycle" must not immediately start the next one (AC 4)."""
    sequencer, switches, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)

    clock.advance_to(aware(7, 4))
    await sequencer.async_cancel_cycle(clock.now())

    assert not sequencer.deferred_kinds
    assert sequencer.current_run is None
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("off", PUMP),
    ]
    # The half of AC 4 a test must assert, not assume: no cycle boundary left
    # to re-arm — only the watchdog's standing deadline.
    assert sequencer.current_run is None
    assert sequencer.next_wakeup(clock.now()) == aware(7, 20)

    await run_to_idle(sequencer, clock)
    assert switches.commands[-1] == ("off", PUMP)


async def test_an_unconfirmed_close_during_a_cancel_still_finishes_the_cancel() -> None:
    """A failing valve must not wedge the cancel — it reports and proceeds."""
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    switches.failing.add(("off", VALVE_1))
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 4))

    assert await sequencer.async_cancel_cycle(clock.now()) is True

    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
    ]
    assert switches.commands[-1] == ("off", PUMP)
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.CANCELLED
    assert run.zone_runs[0].close_confirmed is False


async def test_cancelling_a_failed_zone_keeps_it_failed() -> None:
    """A FAILED zone stays FAILED: the cancel does not launder a bad open."""
    sequencer, switches, _, _ = make_sequencer(two_zone_plan())
    switches.failing.add(("on", VALVE_1))
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 4))
    await sequencer.async_cancel_cycle(clock.now())

    run = sequencer.last_run
    assert run is not None
    zone = run.zone_runs[0]
    # Found by "started but not finished", never by status — a FAILED zone is
    # indistinguishable by status from a finished one but still owns the slot.
    assert zone.actual_end == aware(7, 4)
    assert zone.status is ZoneRunStatus.FAILED
    assert effective_seconds(zone) == 0


async def test_cancelling_an_empty_plan_run_needs_no_live_zone() -> None:
    """A zero-zone run has nothing open; the cancel still files and releases."""
    sequencer, switches, _, _ = make_sequencer(make_plan())
    clock = VirtualClock(aware(6, 50))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    assert await sequencer.async_cancel_cycle(clock.now()) is True

    assert switches.commands == []
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.CANCELLED


async def test_closing_the_live_zone_of_a_run_with_none_open_commands_nothing() -> None:
    """The no-live-zone guard, reached directly — it has no public path.

    A RUNNING run always has an open slot by the time `advance` yields: a
    zero-zone run completes inside the same call that set it RUNNING, and a
    zone's close is immediately followed by the next open or by completion.
    The guard exists so a cancel arriving in some future mid-advance window
    (Epic 3 adds push points there) cannot command a valve that is not open —
    and it is exercised here rather than left to a coverage exemption, because
    the failure mode would be an `AttributeError` inside a cancel.
    """
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    run = sequencer.current_run
    assert run is not None
    # Every slot finished: nothing satisfies "started but not finished".
    for zone in run.zone_runs:
        zone.actual_start = aware(7)
        zone.actual_end = aware(7, 10)
    commanded = len(switches.commands)

    await sequencer._close_live_zone(run, clock.now())  # noqa: SLF001 — no public path

    assert len(switches.commands) == commanded
    assert anomalies.reports == []


async def test_the_snapshot_after_a_cancel_records_the_terminal_status() -> None:
    """The journal is the authority; a cancel must be visible in it (AD-2).

    The snapshot is taken inside `_complete_cycle` — before the slot is
    released — exactly as it is for a normally completed cycle, so `run` still
    carries the finished object. What the cancel changes is its STATUS, and
    the outcome record filed alongside it.
    """
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 4))
    await sequencer.async_cancel_cycle(clock.now())

    snapshot = journal.snapshots[-1]
    history = snapshot["history"]
    assert isinstance(history, list)
    assert [record["status"] for record in history] == ["cancelled"]
    run = snapshot["run"]
    assert isinstance(run, dict)
    assert run["status"] == "cancelled"
    assert snapshot["deferred"] == []
    assert snapshot["season_enabled"] is True
