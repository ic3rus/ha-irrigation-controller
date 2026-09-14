"""A completed run-now credits the day (Story 2.3, sequencer half).

The credit rule itself is pinned in `test_ledger.py`; this suite is where the
DECISION is measured through the machine: the two scheduled call sites
(`request_cycle` creating a run, the deferred pop in `_complete_cycle`) ask
the ledger, a run-now never does, and a waived cycle is a history record —
never `current_run`, never `last_run`, never settled, never commanded, never
an anomaly. Driven the way every other engine suite drives the machine:
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

# The credit a completed run-now of 2026-07-31's morning cycle leaves behind,
# in the shape the journal adapter seeds it.
CREDIT_31: dict[str, object] = {
    "irrigation_day": "2026-07-31",
    "cycle_id": "2026-07-31-morning",
}


def two_zone_plan() -> ControllerPlan:
    """Two zones: 600 s morning / 900 s evening each, morning 07:00, evening 20:00."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900),
        make_zone("zone-2", valve=VALVE_2, morning_s=600, evening_s=900),
    )


def make_sequencer(
    plan: ControllerPlan,
    *,
    ledger: dict[str, object] | None = None,
    history: list[dict[str, object]] | None = None,
    season_enabled: bool = True,
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
        season_enabled=season_enabled,
        ledger=ledger,
    )
    return sequencer, switches, journal, anomalies


async def run_to_idle(sequencer: Sequencer, clock: VirtualClock) -> None:
    """Drive the engine with the exact wake-up loop the runner uses."""
    while (moment := sequencer.next_wakeup()) is not None:
        clock.advance_to(moment)
        await sequencer.advance(clock.now())


async def complete_a_run_now(
    sequencer: Sequencer,
    clock: VirtualClock,
    kind: CycleKind = CycleKind.MORNING,
) -> CycleRun:
    """Fire a `kind` run-now at the clock's instant and drive it to completion."""
    assert await sequencer.async_run_now(kind, clock.now()) is True
    await run_to_idle(sequencer, clock)
    finished = sequencer.last_run
    assert finished is not None
    return finished


def current(sequencer: Sequencer) -> CycleRun:
    """Return the active run (mypy strict: never the None branch)."""
    run = sequencer.current_run
    assert run is not None
    return run


def history_of(journal: FakeJournalPort) -> list[dict[str, object]]:
    """Return the history section of the last snapshot, typed."""
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    return history


def zones_of(record: dict[str, object]) -> list[dict[str, object]]:
    """Return a history record's zone list, typed."""
    zones = record["zones"]
    assert isinstance(zones, list)
    return zones


def outcomes(journal: FakeJournalPort) -> list[tuple[object, object, object]]:
    """Return (cycle_id, status, waived_by) per history record, oldest first."""
    return [
        (record["cycle_id"], record["status"], record["waived_by"])
        for record in history_of(journal)
    ]


# --------------------------------------------------------------------------
# AC 1 — happy path: the day's next scheduled cycle is waived, the one after runs
# --------------------------------------------------------------------------


async def test_a_completed_run_now_waives_the_next_scheduled_cycle_of_its_day() -> None:
    """Matrix "happy path": 06:00 run-now, 07:00 morning waived, 20:00 evening runs.

    At 07:00 nothing is created and nothing is commanded; history gains a
    `waived` record naming the run-now; the credit is gone; no anomaly. The
    run-now is still `last_run` — the zone sensors keep showing the real last
    watering. The evening asks too, finds no credit, and waters normally.
    """
    sequencer, switches, journal, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6))
    run_now = await complete_a_run_now(sequencer, clock)
    assert run_now.cycle_id == "2026-07-31-morning"
    assert sequencer.ledger.day_credit == "2026-07-31"
    commands_before = list(switches.commands)
    writes_before = len(journal.snapshots)

    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())

    assert sequencer.current_run is None
    assert sequencer.next_wakeup() is None
    assert sequencer.last_run is run_now
    assert sequencer.deferred_kinds == ()
    assert switches.commands == commands_before
    assert anomalies.reports == []
    assert sequencer.ledger.day_credit is None
    # The decision is journalled: exactly one save, carrying the record.
    assert len(journal.snapshots) == writes_before + 1
    assert outcomes(journal) == [
        ("2026-07-31-morning", "completed", None),
        ("2026-07-31-morning-2", "waived", "2026-07-31-morning"),
    ]

    clock.advance_to(aware(20))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    evening = current(sequencer)
    assert evening.status is CycleStatus.PENDING
    await run_to_idle(sequencer, clock)

    finished = sequencer.last_run
    assert finished is evening
    assert finished.status is CycleStatus.COMPLETED
    assert finished.manual is False
    assert outcomes(journal)[-1] == ("2026-07-31-evening", "completed", None)
    assert anomalies.reports == []


async def test_the_waived_record_is_the_snapshot_a_real_run_would_have_had() -> None:
    """Built by the same code as a real run: quoted durations, next occurrence id.

    Everything a real record carries is there — filed under the operator's
    configured start, zones PENDING at 0 s watered — plus `waived_by`.
    """
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6))
    await complete_a_run_now(sequencer, clock)

    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert history_of(journal)[-1] == {
        "cycle_id": "2026-07-31-morning-2",
        "irrigation_day": "2026-07-31",
        "kind": "morning",
        "status": "waived",
        "manual": False,
        "waived_by": "2026-07-31-morning",
        "configured_start": "2026-07-31T05:00:00+00:00",
        "scheduled_start": "2026-07-31T05:00:00+00:00",
        "ended_at": "2026-07-31T05:00:00+00:00",
        "zones": [
            {
                "zone_id": "zone-1",
                "status": "pending",
                "planned_s": 600,
                "carried_s": 0,
                "effective_s": 0,
            },
            {
                "zone_id": "zone-2",
                "status": "pending",
                "planned_s": 600,
                "carried_s": 0,
                "effective_s": 0,
            },
        ],
    }
    # Dropped, not installed: the snapshot's machine state is idle.
    assert journal.snapshots[-1]["run"] is None
    assert journal.snapshots[-1]["deferred"] == []


async def test_a_waived_cycle_still_consumes_an_occurrence_number() -> None:
    """Ids stay unique: the evening run-now after a waived morning is `-3`, not `-2`."""
    sequencer, _, journal, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6))
    await complete_a_run_now(sequencer, clock)
    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    clock.advance_to(aware(9))
    await complete_a_run_now(sequencer, clock)

    assert [record[0] for record in outcomes(journal)] == [
        "2026-07-31-morning",
        "2026-07-31-morning-2",
        "2026-07-31-morning-3",
    ]


async def test_a_seeded_waived_record_still_counts_toward_the_occurrence() -> None:
    """After a reload, the waived record in the seeded history keeps its id taken.

    `_next_occurrence` counts history by day and kind regardless of status:
    with the run-now and its waived morning both seeded, the next morning
    run-now on that day is `-3` — never `-2` again.
    """
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        history=[
            {
                "cycle_id": "2026-07-31-morning",
                "irrigation_day": "2026-07-31",
                "kind": "morning",
                "status": "completed",
                "manual": True,
                "waived_by": None,
            },
            {
                "cycle_id": "2026-07-31-morning-2",
                "irrigation_day": "2026-07-31",
                "kind": "morning",
                "status": "waived",
                "manual": False,
                "waived_by": "2026-07-31-morning",
            },
        ],
    )
    clock = VirtualClock(aware(9))

    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True

    assert current(sequencer).cycle_id == "2026-07-31-morning-3"


# --------------------------------------------------------------------------
# Deferred behind the run-now: the pop is the decision
# --------------------------------------------------------------------------


async def test_a_cycle_deferred_behind_the_run_now_is_waived_when_popped() -> None:
    """Matrix "deferred behind run-now": one `_complete_cycle` call does it all.

    The morning falls due while the run-now waters and is deferred, as ever.
    When the run-now completes, its settlement records the credit and the
    deferred pop — in the same call — consumes it: the machine is idle
    (`next_wakeup()` is None), the morning is filed waived, no anomaly.
    """
    sequencer, switches, journal, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6, 55))
    await sequencer.async_run_now(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)

    await run_to_idle(sequencer, clock)

    assert clock.now() == aware(7, 15)
    assert sequencer.current_run is None
    assert sequencer.next_wakeup() is None
    assert not sequencer.deferred_kinds
    assert switches.commands[-1] == ("off", PUMP)
    assert len(switches.commands) == 6
    assert anomalies.reports == []
    assert sequencer.ledger.day_credit is None
    finished = sequencer.last_run
    assert finished is not None
    assert finished.manual is True
    assert outcomes(journal) == [
        ("2026-07-31-morning", "completed", None),
        ("2026-07-31-morning-2", "waived", "2026-07-31-morning"),
    ]
    # A deferred pop is dispatched at completion, and the record says so.
    assert history_of(journal)[-1]["scheduled_start"] == "2026-07-31T05:15:00+00:00"


async def test_a_waived_pop_lets_the_next_deferred_entry_through() -> None:
    """Two entries queued behind the run-now: the first is waived, the second runs.

    `_complete_cycle` loops while the queue is non-empty and nothing was
    installed — otherwise the evening would be stranded behind a morning
    that never existed until the next completion, which never comes.
    """
    sequencer, _, journal, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6, 55))
    await sequencer.async_run_now(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    clock.advance_to(aware(7, 5))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.MORNING, CycleKind.EVENING)

    # Zone 1 of the run-now closes and zone 2 opens at 07:05; the run-now
    # completes at 07:15, where the evening is created in the same `advance`
    # and starts at once — so stop there, at its first zone boundary.
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 15))
    await sequencer.advance(clock.now())

    evening = current(sequencer)
    assert evening.kind is CycleKind.EVENING
    assert evening.status is CycleStatus.RUNNING
    assert evening.scheduled_start == aware(7, 15)
    assert not sequencer.deferred_kinds
    assert outcomes(journal) == [
        ("2026-07-31-morning", "completed", None),
        ("2026-07-31-morning-2", "waived", "2026-07-31-morning"),
    ]

    await run_to_idle(sequencer, clock)

    assert outcomes(journal)[-1] == ("2026-07-31-evening", "completed", None)
    assert anomalies.reports == []


# --------------------------------------------------------------------------
# AC 2 — doubt waters: a cancelled or partial run-now credits nothing
# --------------------------------------------------------------------------


async def test_a_cancelled_run_now_credits_nothing_and_the_morning_runs_extended() -> (
    None
):
    """Matrix "cancelled run-now": cancelled during zone 2 → no credit, 480 s owed.

    The morning at 07:00 is created, not waived, and zone 2 is quoted
    600 + 480. The only records are the cancel and the completed morning.
    """
    sequencer, _, journal, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6))
    await sequencer.async_run_now(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(6, 10))
    await sequencer.advance(clock.now())  # zone 1 closes, zone 2 opens
    clock.advance_to(aware(6, 12))
    assert await sequencer.async_cancel_cycle(clock.now()) is True
    assert sequencer.ledger.day_credit is None
    assert sequencer.ledger.deficit_s("zone-2") == 480

    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    morning = current(sequencer)
    assert morning.status is CycleStatus.PENDING
    zone1, zone2 = morning.zone_runs
    assert (zone1.duration_s, zone1.carried_s) == (600, 0)
    assert (zone2.duration_s, zone2.carried_s) == (1080, 480)
    await run_to_idle(sequencer, clock)

    assert outcomes(journal) == [
        ("2026-07-31-morning", "cancelled", None),
        ("2026-07-31-morning-2", "completed", None),
    ]
    assert sequencer.ledger.as_dict()["deficits"] == {}
    assert anomalies.reports == []


async def test_a_run_now_with_a_failed_zone_credits_nothing() -> None:
    """Matrix "partial run-now": COMPLETED with a FAILED open → no credit.

    Story 1.5's open-unconfirmed anomaly is the only one raised; the morning
    runs with zone 2's 600 s deficit applied.
    """
    sequencer, switches, journal, anomalies = make_sequencer(two_zone_plan())
    switches.failing.add(("on", VALVE_2))
    clock = VirtualClock(aware(6))
    run_now = await complete_a_run_now(sequencer, clock)
    switches.failing.discard(("on", VALVE_2))
    assert run_now.status is CycleStatus.COMPLETED
    assert run_now.zone_runs[1].status is ZoneRunStatus.FAILED
    assert sequencer.ledger.day_credit is None
    assert [kind.value for kind, _ in anomalies.reports] == ["valve_open_unconfirmed"]

    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    morning = current(sequencer)
    assert [zone.duration_s for zone in morning.zone_runs] == [600, 1200]
    await run_to_idle(sequencer, clock)

    assert outcomes(journal) == [
        ("2026-07-31-morning", "completed", None),
        ("2026-07-31-morning-2", "completed", None),
    ]
    assert len(anomalies.reports) == 1


async def test_a_zero_dwell_run_now_credits_nothing() -> None:
    """A run-now drained by one late `advance` watered nothing and credits nothing."""
    sequencer, _, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(6))
    await sequencer.async_run_now(CycleKind.MORNING, clock.now())

    clock.advance_to(aware(6, 45))
    await sequencer.advance(clock.now())

    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert sequencer.ledger.day_credit is None
    assert sequencer.ledger.as_dict()["deficits"] == {"zone-1": 600, "zone-2": 600}

    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert [zone.duration_s for zone in current(sequencer).zone_runs] == [1200, 1200]


# --------------------------------------------------------------------------
# One credit, one day: stale credits are discarded at the first decision
# --------------------------------------------------------------------------


async def test_a_credit_with_no_later_cycle_that_day_is_cleared_next_day() -> None:
    """Matrix "stale credit": a run-now after the evening credits day D; D+1 runs.

    The first scheduled decision is another day's, so the credit is cleared
    and the cycle waters — the operator loses a waiver they could not have
    used, never water.
    """
    sequencer, _, journal, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(21))
    await complete_a_run_now(sequencer, clock, CycleKind.EVENING)
    assert sequencer.ledger.day_credit == "2026-07-31"

    clock.advance_to(aware(7, day=1, month=8))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    morning = current(sequencer)
    assert morning.cycle_id == "2026-08-01-morning"
    assert sequencer.ledger.day_credit is None
    await run_to_idle(sequencer, clock)

    assert outcomes(journal) == [
        ("2026-07-31-evening", "completed", None),
        ("2026-08-01-morning", "completed", None),
    ]
    assert anomalies.reports == []


async def test_a_run_now_across_midnight_credits_the_day_it_was_configured_for() -> (
    None
):
    """Matrix "past midnight": started 23:50 on D, done 00:20 on D+1 → credit D.

    `configured_start` is the plan's evening start on the fire day, so the
    credit is 2026-07-31's and D+1's morning runs normally.
    """
    sequencer, _, _, _ = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(23, 50))
    run_now = await complete_a_run_now(sequencer, clock, CycleKind.EVENING)
    assert run_now.configured_start == aware(20)
    assert clock.now() == aware(0, 20, day=1, month=8)
    assert sequencer.ledger.day_credit == "2026-07-31"

    clock.advance_to(aware(7, day=1, month=8))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert current(sequencer).cycle_id == "2026-08-01-morning"
    assert sequencer.ledger.day_credit is None


async def test_two_completed_run_nows_leave_one_credit_naming_the_second() -> None:
    """Matrix "two run-nows same day": the morning is waived ONCE, by the second id."""
    sequencer, _, journal, anomalies = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(5))
    await complete_a_run_now(sequencer, clock)
    clock.advance_to(aware(6))
    second = await complete_a_run_now(sequencer, clock)
    assert second.cycle_id == "2026-07-31-morning-2"
    assert sequencer.ledger.as_dict()["day_credit"] == {
        "irrigation_day": "2026-07-31",
        "cycle_id": "2026-07-31-morning-2",
    }

    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    assert sequencer.current_run is None
    clock.advance_to(aware(20))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert current(sequencer).kind is CycleKind.EVENING
    await run_to_idle(sequencer, clock)

    assert outcomes(journal) == [
        ("2026-07-31-morning", "completed", None),
        ("2026-07-31-morning-2", "completed", None),
        ("2026-07-31-morning-3", "waived", "2026-07-31-morning-2"),
        ("2026-07-31-evening", "completed", None),
    ]
    assert anomalies.reports == []


# --------------------------------------------------------------------------
# A waived cycle is never settled: the quote was pure, the debt stays
# --------------------------------------------------------------------------


async def test_an_outstanding_deficit_survives_a_waived_cycle() -> None:
    """Matrix "outstanding deficit + waiver": 300 s owed before, 300 s owed after.

    The waived record shows the quote that WOULD have run (900 s on zone 1),
    but nothing settles, so the evening is quoted with the same 300 s.
    """
    sequencer, _, journal, _ = make_sequencer(
        two_zone_plan(),
        ledger={
            "settled_cycle_id": "2026-07-30-evening",
            "deficits": {"zone-1": 300},
            "day_credit": CREDIT_31,
        },
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert sequencer.current_run is None
    waived = history_of(journal)[-1]
    assert waived["status"] == "waived"
    assert zones_of(waived)[0] == {
        "zone_id": "zone-1",
        "status": "pending",
        "planned_s": 900,
        "carried_s": 300,
        "effective_s": 0,
    }
    assert sequencer.ledger.as_dict() == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300},
        "day_credit": None,
    }

    clock.advance_to(aware(20))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())

    zone1 = current(sequencer).zone_runs[0]
    assert (zone1.duration_s, zone1.base_s, zone1.carried_s) == (1200, 900, 300)


# --------------------------------------------------------------------------
# AC 3 — restart: a seeded credit waives the same day's cycle
# --------------------------------------------------------------------------


async def test_a_seeded_credit_waives_the_first_scheduled_cycle_of_its_day() -> None:
    """Matrix "restart between credit and cycle": nothing has run in this life.

    `last_run` is None — the waived cycle must not become it — and the
    record names a run-now this sequencer never saw.
    """
    sequencer, switches, journal, anomalies = make_sequencer(
        two_zone_plan(),
        ledger={"settled_cycle_id": None, "deficits": {}, "day_credit": CREDIT_31},
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())

    assert sequencer.current_run is None
    assert sequencer.last_run is None
    assert sequencer.next_wakeup() is None
    assert switches.commands == []
    assert anomalies.reports == []
    assert sequencer.ledger.day_credit is None
    # The seeded history is empty, so the waived cycle takes the bare id.
    assert outcomes(journal) == [("2026-07-31-morning", "waived", "2026-07-31-morning")]


async def test_a_seeded_credit_for_another_day_is_cleared_and_the_cycle_runs() -> None:
    """The seed carries the same stale-credit rule as a live one."""
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        ledger={
            "settled_cycle_id": None,
            "deficits": {},
            "day_credit": {"irrigation_day": "2026-07-30", "cycle_id": "old"},
        },
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert current(sequencer).cycle_id == "2026-07-31-morning"
    assert sequencer.ledger.day_credit is None


# --------------------------------------------------------------------------
# Season OFF short-circuits before any decision
# --------------------------------------------------------------------------


async def test_the_season_gate_comes_before_the_decision_and_keeps_the_credit() -> None:
    """Matrix "season OFF": a request with the season off never asks the ledger.

    The run-now credits the day even with the season off (it settles like
    any cycle). The 07:00 request returns before any decision — no record,
    no save, credit intact. The season comes back on the next day: that
    first decision is another day's, so the credit is cleared and the cycle
    runs.
    """
    sequencer, _, journal, anomalies = make_sequencer(
        two_zone_plan(),
        season_enabled=False,
    )
    clock = VirtualClock(aware(6))
    await complete_a_run_now(sequencer, clock)
    assert sequencer.ledger.day_credit == "2026-07-31"
    writes_before = len(journal.snapshots)

    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert sequencer.current_run is None
    assert sequencer.ledger.day_credit == "2026-07-31"
    assert len(journal.snapshots) == writes_before
    assert len(history_of(journal)) == 1

    clock.advance_to(aware(6, day=1, month=8))
    await sequencer.async_set_season(enabled=True, now=clock.now())
    clock.advance_to(aware(7, day=1, month=8))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert current(sequencer).cycle_id == "2026-08-01-morning"
    assert sequencer.ledger.day_credit is None
    assert anomalies.reports == []


# --------------------------------------------------------------------------
# A run-now never asks
# --------------------------------------------------------------------------


async def test_a_run_now_is_never_waived_and_never_consumes_the_credit() -> None:
    """The manual path installs unconditionally — the operator asked.

    With today's credit seeded, a run-now runs; its cancel leaves the credit
    exactly as seeded (a non-crediting settlement is not a reader); and the
    07:00 morning is then waived by the ORIGINAL credit.
    """
    sequencer, _, journal, anomalies = make_sequencer(
        two_zone_plan(),
        ledger={"settled_cycle_id": None, "deficits": {}, "day_credit": CREDIT_31},
    )
    clock = VirtualClock(aware(6))

    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True
    run_now = current(sequencer)
    assert run_now.manual is True
    assert sequencer.ledger.as_dict()["day_credit"] == CREDIT_31
    await sequencer.advance(clock.now())
    clock.advance_to(aware(6, 5))
    assert await sequencer.async_cancel_cycle(clock.now()) is True
    assert sequencer.ledger.as_dict()["day_credit"] == CREDIT_31

    clock.advance_to(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    assert sequencer.current_run is None
    assert sequencer.last_run is run_now
    assert outcomes(journal) == [
        ("2026-07-31-morning", "cancelled", None),
        ("2026-07-31-morning-2", "waived", "2026-07-31-morning"),
    ]
    assert anomalies.reports == []
