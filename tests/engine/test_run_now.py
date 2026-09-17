"""Run-now on a virtual clock (Story 2.1, engine half).

`async_run_now` is `request_cycle`'s sibling, not a flag inside it, and this
suite is where that distinction is measured: the season gate does not apply,
the run is tagged manual, and everything after the decision — windows, pump
orchestration, verified actuation, journalling, history — is the scheduled
path unchanged.

Driven the way every other engine suite drives the machine:
`while (t := engine.next_wakeup()) is not None`. No sleeps, no wall clock, no
Home Assistant (AD-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.runs import (
    MANUAL_KEY,
    CycleStatus,
    ZoneRunStatus,
    is_manual,
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
    """Build two zones whose morning and evening durations differ.

    Ten morning minutes and fifteen evening ones per zone: the two kinds
    produce different boundaries, so a test that resolved the wrong duration
    map cannot accidentally pass.
    """
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900),
        make_zone("zone-2", valve=VALVE_2, morning_s=600, evening_s=900),
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
# Happy path: the same cycle a schedule would have produced
# --------------------------------------------------------------------------


async def test_run_now_waters_the_morning_durations_in_plan_order() -> None:
    """Pump first, zones in plan order, each on its MORNING duration.

    Started at 14:30 — nowhere near either configured start — so the windows
    can only come from the dispatch instant, and the durations only from the
    named kind.
    """
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(14, 30))

    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True
    await sequencer.advance(clock.now())
    run = sequencer.current_run
    assert run is not None
    assert [zone.duration_s for zone in run.zone_runs] == [600, 600]
    assert [zone.planned_end for zone in run.zone_runs] == [
        aware(14, 40),
        aware(14, 50),
    ]

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
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert all(zone.status is ZoneRunStatus.COMPLETED for zone in finished.zone_runs)


async def test_run_now_evening_waters_the_evening_durations() -> None:
    """The caller names the cycle, and the cycle names the duration column."""
    sequencer, _, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(14, 30))

    assert await sequencer.async_run_now(CycleKind.EVENING, clock.now()) is True

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.EVENING
    assert [zone.duration_s for zone in run.zone_runs] == [900, 900]


async def test_run_now_dispatches_at_once_rather_than_at_the_configured_start() -> None:
    """`dispatch_at=now`: the run starts immediately and keeps its identity.

    `configured_start` stays the plan's 07:00 — it is what the cycle id and
    the irrigation day key off — while `scheduled_start` is the instant the
    operator asked for.
    """
    sequencer, _, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(14, 30))

    await sequencer.async_run_now(CycleKind.MORNING, clock.now())

    run = sequencer.current_run
    assert run is not None
    assert run.configured_start == aware(7)
    assert run.scheduled_start == aware(14, 30)
    assert sequencer.next_wakeup(clock.now()) == aware(14, 30)


# --------------------------------------------------------------------------
# The season is not a gate (AC 2, AC "season toggled mid-run")
# --------------------------------------------------------------------------


async def test_run_now_runs_with_the_season_off() -> None:
    """AC 2: an explicit operator action is independent of scheduling.

    The same call through `request_cycle` waters nothing at all — that
    contrast is the whole reason this entry point exists.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(),
        season_enabled=False,
    )
    clock = VirtualClock(aware(14, 30))

    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True
    await run_to_idle(sequencer, clock)

    assert switches.commands[0] == ("on", PUMP)
    assert switches.commands[-1] == ("off", PUMP)
    assert sequencer.season_enabled is False
    assert anomalies.reports == []
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED


async def test_run_now_does_not_turn_the_season_back_on() -> None:
    """Running once is not resuming the season — the mode flag is untouched."""
    sequencer, _, _, _ = make_sequencer(two_zone_plan(), season_enabled=False)
    clock = VirtualClock(aware(14, 30))

    await sequencer.async_run_now(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    assert sequencer.season_enabled is False


async def test_the_season_switched_off_mid_run_does_not_abort_a_run_now() -> None:
    """`async_set_season` never touches `self._run`, whatever started it."""
    sequencer, switches, _, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(14, 30))

    await sequencer.async_run_now(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(14, 35))
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
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert anomalies.reports == []


# --------------------------------------------------------------------------
# Refusals — False, never a raise, never a defer
# --------------------------------------------------------------------------


async def test_run_now_is_refused_while_a_cycle_is_pending() -> None:
    """Busy is `current_run is not None` — PENDING counts, not just RUNNING.

    A pending run holds its own snapshot and a scheduled start; overwriting it
    would abandon a cycle nobody cancelled.
    """
    sequencer, switches, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6, 59))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    pending = sequencer.current_run
    assert pending is not None
    assert pending.status is CycleStatus.PENDING
    writes_before = len(journal.snapshots)

    assert await sequencer.async_run_now(CycleKind.EVENING, clock.now()) is False

    assert sequencer.current_run is pending
    assert pending.status is CycleStatus.PENDING
    assert sequencer.deferred_kinds == ()
    assert switches.commands == []
    assert len(journal.snapshots) == writes_before

    # The runner advances unconditionally after a run-now, refused or not.
    # A PENDING run is the one case where that could start a cycle early, so
    # the refusal has to leave it waiting for its own scheduled start.
    await sequencer.advance(clock.now())

    assert pending.status is CycleStatus.PENDING
    assert switches.commands == []
    assert sequencer.next_wakeup(clock.now()) == aware(7)


async def test_run_now_is_refused_while_a_cycle_is_running() -> None:
    """The live cycle is left exactly as it was: no close, no cancel, no write.

    `cancel_cycle` is the only thing that stops a cycle — a run-now must not
    become a second one by the back door.
    """
    sequencer, switches, journal, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    live = sequencer.current_run
    assert live is not None
    assert live.status is CycleStatus.RUNNING
    clock.advance_to(aware(7, 3))
    commands_before = list(switches.commands)
    writes_before = len(journal.snapshots)

    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is False

    assert sequencer.current_run is live
    assert live.status is CycleStatus.RUNNING
    assert live.manual is False
    assert switches.commands == commands_before
    assert len(journal.snapshots) == writes_before
    assert anomalies.reports == []


async def test_a_refused_run_now_is_not_queued_behind_the_running_cycle() -> None:
    """The deferral queue is for SCHEDULED work — a refused command is an error.

    Were it deferred, the operator's "not now" would turn into a surprise
    cycle minutes later, with no way to tell it was ever refused.
    """
    sequencer, _, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())

    assert await sequencer.async_run_now(CycleKind.EVENING, clock.now()) is False
    assert sequencer.deferred_kinds == ()

    await run_to_idle(sequencer, clock)

    assert sequencer.current_run is None


async def test_run_now_on_a_zoneless_plan_files_no_phantom_run() -> None:
    """A zero-length "manual run" in history is water Story 2.3 would credit.

    The scheduled path files such a cycle as completed (`_start_cycle`'s
    no-zones branch); run-now refuses instead, so nothing reaches history and
    nothing is commanded.
    """
    sequencer, switches, journal, anomalies = make_sequencer(make_plan())
    clock = VirtualClock(aware(14, 30))

    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is False
    await sequencer.advance(clock.now())

    assert sequencer.current_run is None
    assert sequencer.last_run is None
    assert switches.commands == []
    assert journal.snapshots == []
    assert anomalies.reports == []
    # A zoneless plan waters nothing by construction, so there is no cycle to
    # miss and the watchdog names no deadline either (Story 3.3).
    assert sequencer.next_wakeup(clock.now()) is None


async def test_run_now_morning_waters_a_plan_whose_morning_cycle_is_off() -> None:
    """`morning_enabled` suppresses the daily START, not the cycle itself.

    Exactly the season's rule one level down: run-now is independent of
    scheduling, and an evening-only controller is a scheduling choice. The
    operator named the morning cycle, so the morning durations water.
    """
    plan = make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900),
        morning_enabled=False,
    )
    sequencer, switches, _, _ = make_sequencer(plan)
    clock = VirtualClock(aware(14, 30))

    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.MORNING
    assert [zone.duration_s for zone in run.zone_runs] == [600]
    assert run.configured_start == aware(7)

    await run_to_idle(sequencer, clock)

    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("off", PUMP),
    ]
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED


# --------------------------------------------------------------------------
# The manual marker (AC 3)
# --------------------------------------------------------------------------


async def test_a_completed_run_now_is_marked_manual_in_history() -> None:
    """AC 3: the outcome record says it was a manual run, and `last_run` is it."""
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(14, 30))

    await sequencer.async_run_now(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    finished = sequencer.last_run
    assert finished is not None
    assert finished.manual is True
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert len(history) == 1
    assert is_manual(history[0]) is True
    assert history[0]["status"] == CycleStatus.COMPLETED.value
    assert history[0]["kind"] == CycleKind.MORNING.value


async def test_a_scheduled_cycle_is_not_marked_manual() -> None:
    """The marker is only meaningful if the default path never sets it.

    Both the run snapshot and the history record say so explicitly rather than
    omitting the key — a reader must not have to infer "scheduled" from a
    missing field in a freshly written document.
    """
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    finished = sequencer.last_run
    assert finished is not None
    assert finished.manual is False
    assert finished.as_dict()[MANUAL_KEY] is False
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert is_manual(history[0]) is False


async def test_the_marker_rides_the_run_snapshot_while_the_cycle_is_live() -> None:
    """Story 3.2 resumes from `run`, so the marker has to be in it too."""
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(14, 30))

    await sequencer.async_run_now(CycleKind.MORNING, clock.now())

    run = journal.snapshots[-1]["run"]
    assert isinstance(run, dict)
    assert is_manual(run) is True


async def test_is_manual_defaults_a_legacy_record_to_scheduled() -> None:
    """A document written before this story carries no marker — and must load.

    Only a real `True` is a manual run: an absent key, a `None`, a truthy
    string or a `1` from a hand-edited backup all read as scheduled, which is
    the direction that waters (AD-4).
    """
    assert is_manual({}) is False
    assert is_manual({"cycle_id": "2026-07-31-morning"}) is False
    assert is_manual({MANUAL_KEY: None}) is False
    assert is_manual({MANUAL_KEY: "true"}) is False
    assert is_manual({MANUAL_KEY: 1}) is False
    assert is_manual({MANUAL_KEY: True}) is True


# --------------------------------------------------------------------------
# Identity: a second run of a kind on one irrigation day
# --------------------------------------------------------------------------


async def test_a_run_now_after_the_scheduled_cycle_gets_an_occurrence_suffix() -> None:
    """The cycle id must stay UNIQUE — Epic 2 keys idempotent writes off it.

    The first run of a kind keeps the bare id, so the scheduled cycle every
    other feature reads never changes shape; the run-now that follows it the
    same day is `-2`.
    """
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    clock.advance_to(aware(14, 30))
    await sequencer.async_run_now(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)

    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert [record["cycle_id"] for record in history] == [
        "2026-07-31-morning",
        "2026-07-31-morning-2",
    ]
    # Same irrigation day, same kind, told apart ONLY by the marker and the id.
    assert [record["irrigation_day"] for record in history] == ["2026-07-31"] * 2
    assert [is_manual(record) for record in history] == [False, True]


async def test_a_scheduled_cycle_falling_due_during_a_run_now_still_defers() -> None:
    """Story 2.1 changes nothing about deferral — 2.3 owns the day credit.

    The evening start arriving while a run-now is watering queues the evening
    cycle exactly as it would behind any other run: it is never started on
    top of the running cycle and never dropped at request time. What happens
    to it when it is popped is the ledger's decision (Story 2.3): this
    run-now completes every zone, so the popped evening is WAIVED — filed to
    history naming the run-now, and `last_run` stays the run-now. The
    deferral itself is what this test pins; `test_day_credit.py` owns the
    waiver.
    """
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(19, 55))

    await sequencer.async_run_now(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(20))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())

    assert sequencer.deferred_kinds == (CycleKind.EVENING,)
    assert sequencer.current_run is not None
    assert sequencer.current_run.manual is True

    await run_to_idle(sequencer, clock)

    assert not sequencer.deferred_kinds
    finished = sequencer.last_run
    assert finished is not None
    assert finished.manual is True
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert [(record["kind"], record["status"]) for record in history] == [
        ("morning", "completed"),
        ("evening", "waived"),
    ]
