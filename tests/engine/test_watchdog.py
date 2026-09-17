"""Missed-cycle watchdog on a virtual clock (Story 3.3, AC 1-6).

Two layers, bottom up: the pure `engine.watchdog` decisions — which cycles of
the current irrigation day were never performed, and when to look again — and
`Sequencer._check_missed` applying them through the ports (settle, record,
report, late re-run, one save).

Zero Home Assistant, zero fixtures (AD-1). The deadline cases run on a real
`ZoneInfo` so both DST transitions are exercised; everything else uses the
suite's fixed offset.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleStatus,
    ZoneRunStatus,
)
from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer
from custom_components.ha_irrigation_controller.engine.watchdog import (
    missed_cycles,
    next_deadline,
)
from tests.engine.common import (
    FakeAnomalyPort,
    FakeJournalPort,
    FakeSwitchPort,
    VirtualClock,
    aware,
    make_plan,
    make_zone,
    paris,
)

if TYPE_CHECKING:
    from datetime import datetime

    from custom_components.ha_irrigation_controller.engine.plan import ControllerPlan

PUMP = "switch.pool_pump"
VALVE_1 = "switch.zone_1_valve"
VALVE_2 = "switch.zone_2_valve"

# Morning 07:00 → 07:20, evening 20:00 → 20:30, two zones.
MORNING_END = aware(7, 20)
EVENING_END = aware(20, 30)


def two_zone_plan(**overrides: Any) -> ControllerPlan:
    """Two zones, 600 s morning / 900 s evening each; morning 07:00, evening 20:00."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900),
        make_zone("zone-2", valve=VALVE_2, morning_s=600, evening_s=900),
        **overrides,
    )


def record(
    kind: CycleKind,
    *,
    status: str = "completed",
    day: str = "2026-07-31",
) -> dict[str, object]:
    """Build the only three history-record keys the watchdog ever reads."""
    return {
        "irrigation_day": day,
        "kind": kind.value,
        "status": status,
        "cycle_id": f"{day}-{kind.value}",
    }


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


def detect(
    plan: ControllerPlan,
    now: datetime,
    **overrides: Any,
) -> tuple[tuple[CycleKind, bool], ...]:
    """Run the pure decision with permissive defaults; return (kind, rerunnable)."""
    inputs: dict[str, Any] = {
        "history": [],
        "season_enabled": True,
        "active": None,
        "deferred_kinds": (),
        "day_credit": None,
        "since": None,
        **overrides,
    }
    return tuple(
        (miss.kind, miss.rerunnable) for miss in missed_cycles(plan, now=now, **inputs)
    )


def history_of(journal: FakeJournalPort) -> list[dict[str, object]]:
    """Return the history section of the LAST snapshot the journal saw."""
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    return history


def missed_reports(anomalies: FakeAnomalyPort) -> list[dict[str, object]]:
    """Return the context of every `MISSED_CYCLE` report, in order."""
    return [
        context
        for kind, context in anomalies.reports
        if kind is AnomalyKind.MISSED_CYCLE
    ]


# --------------------------------------------------------------------------
# The pure decision: what the day expected vs what the journal records
# --------------------------------------------------------------------------


async def test_a_closed_window_with_no_record_is_a_rerunnable_miss() -> None:
    """Matrix "miss detected at window end": the window end itself already counts."""
    plan = two_zone_plan()

    # One second before the window closes there is nothing to see...
    assert detect(plan, aware(7, 19, 59)) == ()
    # ...and at the end instant the cycle is a miss, still worth watering.
    assert detect(plan, MORNING_END) == ((CycleKind.MORNING, True),)
    assert detect(plan, aware(9)) == ((CycleKind.MORNING, True),)


async def test_the_configured_start_is_the_plans_not_the_detection_instant() -> None:
    """The miss carries the operator's intent — what the record and re-run key off."""
    (miss,) = missed_cycles(
        two_zone_plan(),
        now=aware(9),
        history=[],
        season_enabled=True,
        active=None,
        deferred_kinds=(),
        day_credit=None,
        since=None,
    )

    assert miss.kind is CycleKind.MORNING
    assert miss.configured_start == aware(7)


@pytest.mark.parametrize(
    "status",
    ["completed", "waived", "cancelled", "interrupted", "missed"],
)
async def test_a_history_entry_in_any_status_means_the_cycle_was_handled(
    status: str,
) -> None:
    """Matrix "already handled" + "re-detection": existence, not status.

    `missed` is in the list on purpose: the record the watchdog files IS the
    at-most-once marker, so the same (day, kind) is never detected twice and
    never re-run twice.
    """
    handled = [record(CycleKind.MORNING, status=status)]

    assert detect(two_zone_plan(), aware(9), history=handled) == ()


async def test_a_zero_dwell_completed_record_counts_as_performed() -> None:
    """Decision 3: "was it handled", not "did enough water flow".

    A late catch-up that opened and closed every zone at the same instant is
    filed COMPLETED with zero effective seconds. The ledger books that
    under-watering; the watchdog must not also re-run it.
    """
    drained = {**record(CycleKind.MORNING), "zones": [{"effective_s": 0}]}

    assert detect(two_zone_plan(), aware(9), history=[drained]) == ()


async def test_another_days_record_never_covers_todays_cycle() -> None:
    """Records are matched on the irrigation day, never on the kind alone."""
    yesterday = record(CycleKind.MORNING, day="2026-07-30")

    assert detect(two_zone_plan(), aware(9), history=[yesterday]) == (
        (CycleKind.MORNING, True),
    )


async def test_only_the_current_irrigation_day_is_ever_scanned() -> None:
    """Decision 1: a multi-day outage produces misses for the day HA came back.

    Nothing at all is recorded for 2026-07-29 and 2026-07-30 — they simply
    have no entry in the 7-day view.
    """
    misses = missed_cycles(
        two_zone_plan(),
        now=aware(9),
        history=[],
        season_enabled=True,
        active=None,
        deferred_kinds=(),
        day_credit=None,
        since=None,
    )

    assert [miss.configured_start.date().isoformat() for miss in misses] == [
        "2026-07-31",
    ]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"season_enabled": False}, "season OFF"),
        ({"deferred_kinds": (CycleKind.MORNING,)}, "deferred, not lost"),
        ({"day_credit": "2026-07-31"}, "a run-now already watered the day"),
    ],
)
async def test_a_permitted_non_watering_cause_is_never_a_miss(
    overrides: dict[str, Any],
    reason: str,
) -> None:
    """Matrix "permitted cause": each one is silent — no record, no anomaly."""
    assert detect(two_zone_plan(), aware(9), **overrides) == (), reason


async def test_a_stale_day_credit_does_not_excuse_today() -> None:
    """The credit is keyed by irrigation day, like everything else."""
    assert detect(two_zone_plan(), aware(9), day_credit="2026-07-30") == (
        (CycleKind.MORNING, True),
    )


async def test_a_disabled_morning_cycle_is_never_expected() -> None:
    """`morning_enabled` suppresses the daily start, so there is nothing to miss."""
    plan = two_zone_plan(morning_enabled=False)

    assert detect(plan, aware(9)) == ()
    assert detect(plan, aware(23)) == ((CycleKind.EVENING, True),)


async def test_a_plan_with_no_zones_has_nothing_to_miss() -> None:
    """A zoneless plan waters nothing by construction; it is not a fault."""
    assert detect(make_plan(), aware(23)) == ()


async def test_the_live_run_of_that_day_and_kind_is_not_a_miss() -> None:
    """A cycle running late is late, never lost — and another kind is not it."""
    sequencer, _, _, _ = make_sequencer()
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    morning = sequencer.current_run
    assert morning is not None

    assert detect(two_zone_plan(), aware(9), active=morning) == ()
    # The same run does not account for the evening cycle it is not.
    assert detect(two_zone_plan(), aware(23), active=morning) == (
        (CycleKind.EVENING, True),
    )


async def test_only_the_latest_miss_of_a_day_is_ever_rerun() -> None:
    """Compensation is capped: the morning is recorded, the evening re-run.

    Two windows closed with nothing to show for them. Watering both, back to
    back at 22:00, is exactly the runaway compensation the story forbids — so
    every miss but the day's last is filed and booked, never re-run.
    """
    assert detect(two_zone_plan(), aware(22)) == (
        (CycleKind.MORNING, False),
        (CycleKind.EVENING, True),
    )


async def test_a_later_cycle_already_in_history_makes_a_miss_unrerunnable() -> None:
    """Matrix "late detection, next cycle already ran"."""
    assert detect(
        two_zone_plan(),
        aware(20, 40),
        history=[record(CycleKind.EVENING)],
    ) == ((CycleKind.MORNING, False),)


async def test_rerunnability_compares_starts_not_kinds() -> None:
    """A plan whose EVENING runs before its morning is handled symmetrically."""
    plan = two_zone_plan(morning_start=time(20), evening_start=time(7))

    # Evening 07:00→07:30, morning 20:00→20:20: the morning is the day's last.
    assert detect(plan, aware(22)) == (
        (CycleKind.MORNING, True),
        (CycleKind.EVENING, False),
    )


async def test_a_window_that_closed_before_we_existed_is_not_a_miss() -> None:
    """The floor: nobody skipped a cycle on a controller that did not exist.

    `since` is the instant this controller first became observable. A first
    install at 09:00 must not water the 07:00 morning it was never configured
    for — but the evening it WILL be there for stays fully watched.
    """
    plan = two_zone_plan()

    # Installed at 09:00: this morning is not ours, and nothing is due yet.
    assert detect(plan, aware(9), since=aware(9)) == ()
    # The very end instant counts as "before us": at-or-before, not before.
    assert detect(plan, aware(9), since=MORNING_END) == ()
    # ...and that same day's evening still is ours, once its window closes.
    assert detect(plan, EVENING_END, since=aware(9)) == ((CycleKind.EVENING, True),)


async def test_a_floor_earlier_than_the_window_leaves_the_miss_alone() -> None:
    """A controller that existed across the window owns the cycle it lost."""
    plan = two_zone_plan()

    assert detect(plan, aware(9), since=aware(6, 59, 59)) == (
        (CycleKind.MORNING, True),
    )


async def test_no_floor_at_all_watches_the_whole_day() -> None:
    """`None` is the absence of a fact, not a default the engine invents.

    Every virtual-clock suite drives `advance` without ever reconciling, so
    it never has a floor — and must keep seeing the day it describes.
    """
    assert detect(two_zone_plan(), aware(9), since=None) == ((CycleKind.MORNING, True),)


async def test_the_first_reconcile_stamps_the_floor_and_saves_once() -> None:
    """`async_reconcile` is the ONE place the floor is ever set (Story 3.3).

    A journal written before this story carries no stamp, so the first
    reconcile records the instant the controller became observable — and the
    windows it slept through before that are nobody's to make up. A second
    reconcile finds the stamp and changes nothing.
    """
    sequencer, switches, journal, anomalies = make_sequencer()

    await sequencer.async_reconcile(aware(9))

    assert journal.snapshots[-1]["watchdog_since"] == "2026-07-31T07:00:00+00:00"
    assert len(journal.snapshots) == 1
    # The morning window closed before the stamp, so nothing was made up.
    assert sequencer.current_run is None
    assert switches.commands == []
    assert anomalies.reports == []

    await sequencer.async_reconcile(aware(9, 30))

    assert len(journal.snapshots) == 1
    assert journal.snapshots[-1]["watchdog_since"] == "2026-07-31T07:00:00+00:00"


async def test_a_seeded_floor_is_never_restamped() -> None:
    """A controller that already existed keeps the instant it first did."""
    sequencer, _, journal, _ = make_sequencer(watchdog_since=aware(6))

    await sequencer.async_reconcile(aware(9))

    # Every write of this restart carries the floor it was seeded with...
    assert {snapshot["watchdog_since"] for snapshot in journal.snapshots} == {
        "2026-07-31T04:00:00+00:00",
    }
    # ...and the morning it really did sleep through is still made up.
    run = sequencer.current_run
    assert run is not None
    assert run.late_rerun is True


# --------------------------------------------------------------------------
# The deadline: one accessor, no timer of its own (AD-3)
# --------------------------------------------------------------------------


async def test_the_deadline_is_the_next_window_end_today_then_tomorrow() -> None:
    """Matrix "idle deadline" and "window still open"."""
    plan = make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=2700),
        make_zone("zone-2", valve=VALVE_2, morning_s=600, evening_s=2700),
    )

    # Inside the morning window: its own end.
    assert next_deadline(plan, aware(7, 5)) == aware(7, 20)
    # After it: the evening's, 90 minutes after the 20:00 start.
    assert next_deadline(plan, aware(12)) == aware(21, 30)
    # After that: tomorrow's earliest.
    assert next_deadline(plan, aware(21, 30)) == aware(7, 20, day=1, month=8)


async def test_the_deadline_is_strictly_after_now() -> None:
    """A deadline equal to `now` would fire, be re-armed and loop for ever."""
    plan = two_zone_plan()

    for moment in (MORNING_END, EVENING_END):
        later = next_deadline(plan, moment)
        assert later is not None
        assert later > moment


async def test_a_disabled_morning_cycle_is_not_a_deadline() -> None:
    """The deadline follows the same enabled-kinds rule the daily starts do."""
    plan = two_zone_plan(morning_enabled=False)

    assert next_deadline(plan, aware(6)) == aware(20, 30)


async def test_the_deadline_holds_its_wall_clock_across_the_spring_forward() -> None:
    """NFR3: Europe/Paris 2027-03-28 is 23 hours long; 07:20 is still 07:20.

    A stored "now + 24 h" would land at 08:20 on the short day — which is the
    whole reason the deadline is derived from the plan every time instead.
    """
    plan = two_zone_plan()

    deadline = next_deadline(plan, paris(2027, 3, 27, 20, 40))

    assert deadline == paris(2027, 3, 28, 7, 20)
    assert deadline.utcoffset() != paris(2027, 3, 27, 20, 40).utcoffset()
    # ...and the next one after it is that same day's evening.
    assert next_deadline(plan, deadline) == paris(2027, 3, 28, 20, 30)


async def test_the_deadline_holds_its_wall_clock_across_the_fall_back() -> None:
    """The 25-hour day: every deadline is still the plan's wall-clock window end."""
    plan = two_zone_plan()

    deadline = next_deadline(plan, paris(2027, 10, 30, 20, 40))

    assert deadline == paris(2027, 10, 31, 7, 20)
    assert deadline.utcoffset() != paris(2027, 10, 30, 20, 40).utcoffset()
    assert next_deadline(plan, deadline) == paris(2027, 10, 31, 20, 30)
    assert next_deadline(plan, paris(2027, 10, 31, 20, 30)) == paris(2027, 11, 1, 7, 20)


async def test_a_start_inside_the_spring_forward_gap_still_yields_a_deadline() -> None:
    """A 02:30 start on the day 02:30 does not exist: the window still closes.

    The daily tracker never fires that morning (the skip this story's watchdog
    exists to notice); the deadline must still name an instant after `now` so
    the check happens at all.
    """
    plan = two_zone_plan(morning_start=time(2, 30))

    deadline = next_deadline(plan, paris(2027, 3, 28, 0, 30))

    assert deadline is not None
    assert deadline > paris(2027, 3, 28, 0, 30)
    # 02:30+01:00 is 03:30 on the wall clock, and the two zones take 20 min.
    assert deadline == paris(2027, 3, 28, 3, 50)


async def test_next_wakeup_serves_the_deadline_only_while_idle() -> None:
    """AD-3: the run's own boundary wins; the watchdog owns the idle timer."""
    sequencer, _, _, _ = make_sequencer()
    clock = VirtualClock(aware(6, 50))

    assert sequencer.next_wakeup(clock.now()) == MORNING_END

    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    assert sequencer.next_wakeup(clock.now()) == aware(7)
    await sequencer.advance(aware(7))
    assert sequencer.next_wakeup(aware(7)) == aware(7, 10)


async def test_nothing_is_armed_and_nothing_checked_before_reconciliation() -> None:
    """Matrix "not reconciled": boot must not re-run a cycle before recovery.

    The gate is Story 3.2's, reused verbatim: with a restored run awaiting the
    reconciler there is no wake-up at all and `advance` performs nothing — so
    the watchdog cannot file, report or dispatch anything either.
    """
    sequencer, switches, journal, anomalies = make_sequencer()
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    restored = sequencer.current_run
    assert restored is not None

    gated, switches, journal, anomalies = make_sequencer(run=restored)
    assert gated.reconciled is False

    assert gated.next_wakeup(aware(22)) is None
    await gated.advance(aware(22))

    assert journal.snapshots == []
    assert anomalies.reports == []
    assert switches.commands == []


# --------------------------------------------------------------------------
# The effects: settle → record → report → dispatch, once, under the lock
# --------------------------------------------------------------------------


async def test_a_miss_at_the_window_end_is_recorded_and_re_run_at_once() -> None:
    """Matrix "miss detected at window end" + AC 1, end to end on the engine."""
    sequencer, switches, journal, anomalies = make_sequencer()

    await sequencer.advance(MORNING_END)

    filed, rerun = history_of(journal)[0], sequencer.current_run
    assert filed["cycle_id"] == "2026-07-31-morning"
    assert filed["status"] == "missed"
    assert filed["late_rerun"] is False
    zones = filed["zones"]
    assert isinstance(zones, list)
    # It never started, so every zone keeps its pre-run values.
    assert [(zone["status"], zone["effective_s"]) for zone in zones] == [
        ("pending", 0),
        ("pending", 0),
    ]
    assert missed_reports(anomalies) == [
        {
            "cycle_id": "2026-07-31-morning",
            "kind": "morning",
            "outcome": "rerun",
        },
    ]
    # The record consumed occurrence 1, so the make-up cycle is `-2` — and it
    # is watering already, dispatched at `now` on freshly quoted durations.
    assert rerun is not None
    assert rerun.cycle_id == "2026-07-31-morning-2"
    assert rerun.late_rerun is True
    assert rerun.status is CycleStatus.RUNNING
    assert rerun.configured_start == aware(7)
    assert rerun.scheduled_start == MORNING_END
    assert [zone.duration_s for zone in rerun.zone_runs] == [600, 600]
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]


async def test_the_re_run_leaves_the_ledger_to_its_own_completion() -> None:
    """Decision 2: settling first would put the shortfall in the re-run's quote too."""
    sequencer, _, _, _ = make_sequencer()

    await sequencer.advance(MORNING_END)

    assert sequencer.ledger.deficit_s("zone-1") == 0
    assert sequencer.ledger.settled_cycle_id is None

    # The re-run waters in full and ITS completion is what settles the day.
    clock = VirtualClock(MORNING_END)
    while sequencer.current_run is not None:
        moment = sequencer.next_wakeup(clock.now())
        assert moment is not None
        clock.advance_to(moment)
        await sequencer.advance(clock.now())

    assert sequencer.ledger.settled_cycle_id == "2026-07-31-morning-2"
    assert sequencer.ledger.as_dict()["deficits"] == {}


async def test_a_miss_that_is_not_re_run_books_one_base_duration_per_zone() -> None:
    """Matrix "late detection, next cycle already ran" + AC 5.

    Every zone is PENDING, so `effective_seconds` reads 0 and the clamp in
    `Ledger.settle` caps each deficit at the snapshotted `base_s` — the
    "deficits at one base duration" cap, never more.
    """
    sequencer, switches, journal, anomalies = make_sequencer(
        history=[record(CycleKind.EVENING)],
    )

    await sequencer.advance(aware(20, 40))

    assert sequencer.current_run is None
    assert switches.commands == []
    assert missed_reports(anomalies) == [
        {
            "cycle_id": "2026-07-31-morning",
            "kind": "morning",
            "outcome": "recorded",
        },
    ]
    assert history_of(journal)[-1]["status"] == "missed"
    assert sequencer.ledger.as_dict()["deficits"] == {"zone-1": 600, "zone-2": 600}
    assert sequencer.ledger.deficit_s("zone-1") == 600
    # One save for the whole decision, not one per step.
    assert len(journal.snapshots) == 1


async def test_a_recorded_miss_carries_its_debt_into_the_next_cycle_once() -> None:
    """The cap holds end to end: the next cycle is quoted base + one base."""
    sequencer, _, _, _ = make_sequencer(history=[record(CycleKind.EVENING)])
    await sequencer.advance(aware(20, 40))

    await sequencer.request_cycle(CycleKind.MORNING, aware(7, day=1, month=8))

    run = sequencer.current_run
    assert run is not None
    assert [
        (zone.base_s, zone.carried_s, zone.duration_s) for zone in run.zone_runs
    ] == [
        (600, 600, 1200),
        (600, 600, 1200),
    ]


async def test_a_miss_is_never_detected_twice() -> None:
    """AC 2: no second re-run and no second record, same day or after a restart."""
    sequencer, _, journal, anomalies = make_sequencer()
    await sequencer.advance(MORNING_END)
    stored = history_of(journal)

    # Same day, same machine: every later check finds the record.
    await sequencer.advance(aware(9))
    assert len(missed_reports(anomalies)) == 1

    # ...and after a restart that seeds exactly what the journal holds.
    restarted, switches, restarted_journal, restarted_anomalies = make_sequencer(
        history=stored,
        watchdog_since=aware(6),
    )
    await restarted.async_reconcile(aware(9))

    assert restarted.current_run is None
    assert restarted_anomalies.reports == []
    assert switches.commands == []
    assert restarted_journal.snapshots == []


async def test_an_unreadable_run_discarded_past_its_window_is_made_up() -> None:
    """A journal the reconciler could not trust leaves the cycle UNPERFORMED.

    `_discard_unreadable` commands every configured switch off, reports
    `CYCLE_RECOVERED{outcome: "discarded"}` and books nothing — deliberately,
    since nothing about that document can be believed. It files no history
    entry either, so when the discard happens AFTER the window has closed the
    watchdog finds a cycle with no record and makes it up in the same call.

    That is the intended outcome, not an accident of ordering: a discarded
    journal proves nothing about whether the water flowed, and doubt waters
    (AD-4). It stays bounded the usual way — the `missed` record filed here
    is the at-most-once marker, so a second restart re-runs nothing.

    A discard INSIDE the window (the reconciler suite's two cases, at 07:04)
    is untouched by this: the window has not closed, so there is no miss and
    the re-timed cycle is simply gone.
    """
    sequencer, switches, journal, anomalies = make_sequencer(
        run_unreadable={"cycle_id": "2026-07-31-morning", "kind": "morning"},
        watchdog_since=aware(6),
    )
    assert sequencer.reconciled is False

    await sequencer.async_reconcile(aware(9))

    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.CYCLE_RECOVERED,
        AnomalyKind.MISSED_CYCLE,
    ]
    assert missed_reports(anomalies) == [
        {
            "cycle_id": "2026-07-31-morning",
            "kind": "morning",
            "outcome": "rerun",
        },
    ]
    # Every configured switch was closed first, then the make-up cycle began.
    assert switches.commands == [
        ("off", VALVE_1),
        ("off", VALVE_2),
        ("off", PUMP),
        ("on", PUMP),
        ("on", VALVE_1),
    ]
    run = sequencer.current_run
    assert run is not None
    assert run.cycle_id == "2026-07-31-morning-2"
    assert run.late_rerun is True
    assert history_of(journal)[0]["status"] == "missed"


async def test_a_restart_after_a_slept_through_window_re_runs_the_cycle() -> None:
    """Matrix "miss detected at startup": inside `async_reconcile`, after the gate.

    `watchdog_since` is the journal's, from before the window: this
    controller existed across the outage, which is what makes the cycle its
    to make up (a FIRST install past that window is the opposite case).
    """
    sequencer, switches, journal, anomalies = make_sequencer(
        watchdog_since=aware(6),
    )

    await sequencer.async_reconcile(aware(9))

    run = sequencer.current_run
    assert run is not None
    assert run.cycle_id == "2026-07-31-morning-2"
    assert run.late_rerun is True
    assert run.scheduled_start == aware(9)
    assert [context["outcome"] for context in missed_reports(anomalies)] == ["rerun"]
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]
    assert history_of(journal)[0]["status"] == "missed"


async def test_a_nominal_day_produces_no_record_no_anomaly_and_no_write() -> None:
    """AC 3, the counter-metric: both cycles ran, so every window end is silent."""
    sequencer, switches, journal, anomalies = make_sequencer(
        history=[record(CycleKind.MORNING), record(CycleKind.EVENING)],
    )

    for moment in (MORNING_END, aware(12), EVENING_END, aware(23, 59)):
        await sequencer.advance(moment)

    assert journal.snapshots == []
    assert anomalies.reports == []
    assert switches.commands == []
    assert sequencer.current_run is None


@pytest.mark.parametrize(
    ("seeds", "reason"),
    [
        ({"season_enabled": False}, "season OFF"),
        (
            {
                "ledger": {
                    "day_credit": {"irrigation_day": "2026-07-31", "cycle_id": "x"}
                }
            },
            "day credit",
        ),
    ],
)
async def test_a_permitted_cause_writes_nothing_at_the_window_end(
    seeds: dict[str, Any],
    reason: str,
) -> None:
    """AC 4: nothing is detected, so nothing is written, reported or commanded."""
    sequencer, switches, journal, anomalies = make_sequencer(**seeds)

    await sequencer.advance(aware(9))

    assert (journal.snapshots, anomalies.reports, switches.commands) == ([], [], []), (
        reason
    )


async def test_a_disabled_morning_cycle_writes_nothing_at_the_window_end() -> None:
    """AC 4, the plan half: an opt-out cycle is not an expected one."""
    sequencer, switches, journal, anomalies = make_sequencer(
        two_zone_plan(morning_enabled=False),
    )

    await sequencer.advance(aware(9))

    assert (journal.snapshots, anomalies.reports, switches.commands) == ([], [], [])


async def test_both_misses_of_a_day_file_but_only_the_last_waters() -> None:
    """The whole-day outage: two records, two anomalies, ONE late re-run."""
    sequencer, _, journal, anomalies = make_sequencer()

    await sequencer.advance(aware(22))

    assert [
        (entry["cycle_id"], entry["status"]) for entry in history_of(journal)[:2]
    ] == [
        ("2026-07-31-morning", "missed"),
        ("2026-07-31-evening", "missed"),
    ]
    assert [context["outcome"] for context in missed_reports(anomalies)] == [
        "recorded",
        "rerun",
    ]
    run = sequencer.current_run
    assert run is not None
    assert (run.kind, run.cycle_id) == (CycleKind.EVENING, "2026-07-31-evening-2")


async def test_a_miss_of_the_other_kind_never_abandons_a_live_run() -> None:
    """A run in flight is never overwritten, and the miss books NOTHING.

    An operator's run-now of the morning cycle is still watering when the
    evening window closes with nothing recorded. Installing the make-up run
    there would drop the live cycle with its valve open, so the miss is only
    FILED — and deliberately not settled: `Ledger.settle` REPLACES the ledger
    with the settled run's zones, so the live cycle's own completion, minutes
    later in this very drive, would discard whatever the miss booked. A debt
    thrown away before any quote reads it is a lie in the journal; the record
    is the truth that is kept.

    The evening cycle is put before the morning one here so its window can
    close while the morning run-now is live — the only way to reach this
    branch on a plan whose two cycles do not overlap.
    """
    plan = two_zone_plan(evening_start=time(6, 0))
    sequencer, switches, journal, anomalies = make_sequencer(plan)
    await sequencer.async_run_now(CycleKind.MORNING, aware(7))
    await sequencer.advance(aware(7))
    live = sequencer.current_run
    assert live is not None

    # 07:10: the evening window (06:00 → 06:30) is long closed, and the
    # morning run-now is passing from its first zone to its second.
    await sequencer.advance(aware(7, 10))

    assert [context["outcome"] for context in missed_reports(anomalies)] == ["recorded"]
    assert history_of(journal)[-1]["cycle_id"] == "2026-07-31-evening"
    assert sequencer.current_run is live
    assert live.status is CycleStatus.RUNNING
    # Nothing booked: the ledger is exactly as the miss found it.
    assert sequencer.ledger.settled_cycle_id is None
    assert sequencer.ledger.as_dict()["deficits"] == {}

    # Drive the live cycle to completion — the settlement that would have
    # replaced anything the miss had written.
    await sequencer.advance(aware(7, 20))

    finished = sequencer.last_run
    assert finished is live
    assert finished.status is CycleStatus.COMPLETED
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("off", PUMP),
    ]
    # What is owed afterwards is the live cycle's own accounting and nothing
    # else: it watered both zones in full, so the day owes nothing at all.
    assert sequencer.ledger.settled_cycle_id == "2026-07-31-morning"
    assert sequencer.ledger.as_dict()["deficits"] == {}
    assert [(entry["cycle_id"], entry["status"]) for entry in history_of(journal)] == [
        ("2026-07-31-evening", "missed"),
        ("2026-07-31-morning", "completed"),
    ]


async def test_the_record_is_filed_before_the_re_run_is_dispatched() -> None:
    """The ordering the at-most-once marker depends on, observed at the port.

    The save the watchdog issues already carries the `missed` record AND the
    re-run — so a crash right after it re-detects nothing, and `-2` is the id
    the occurrence count really produced.
    """
    sequencer, _, journal, _ = make_sequencer()

    await sequencer.advance(MORNING_END)

    first = journal.snapshots[0]
    history = first["history"]
    run = first["run"]
    assert isinstance(history, list)
    assert isinstance(run, dict)
    assert [entry["status"] for entry in history] == ["missed"]
    assert run["cycle_id"] == "2026-07-31-morning-2"
    assert run["late_rerun"] is True
    assert run["status"] == "pending"


async def test_the_missed_record_never_becomes_last_run() -> None:
    """It is terminal at birth: built, filed and dropped, like a waived cycle."""
    sequencer, _, _, _ = make_sequencer(history=[record(CycleKind.EVENING)])

    await sequencer.advance(aware(20, 40))

    assert sequencer.last_run is None
    assert sequencer.current_run is None


async def test_the_late_re_run_rides_the_history_record_into_epic_4() -> None:
    """`late_rerun` is copied verbatim into the history entry the re-run files."""
    sequencer, _, journal, _ = make_sequencer()
    await sequencer.advance(MORNING_END)
    clock = VirtualClock(MORNING_END)
    while sequencer.current_run is not None:
        moment = sequencer.next_wakeup(clock.now())
        assert moment is not None
        clock.advance_to(moment)
        await sequencer.advance(clock.now())

    entries = history_of(journal)

    assert [(entry["status"], entry["late_rerun"]) for entry in entries] == [
        ("missed", False),
        ("completed", True),
    ]


async def test_a_late_re_run_is_still_waived_by_an_unconsumed_credit() -> None:
    """The re-run is an ORDINARY scheduled dispatch — the ledger still decides.

    The credit here is yesterday's, so it is stale and cleared; a credit for
    TODAY would have made the cycle a permitted cause and no miss at all.
    """
    sequencer, switches, _journal, _ = make_sequencer(
        ledger={"day_credit": {"irrigation_day": "2026-07-30", "cycle_id": "old"}},
    )

    await sequencer.advance(MORNING_END)

    assert sequencer.ledger.day_credit is None
    assert sequencer.current_run is not None
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]


async def test_a_deferred_cycle_is_not_a_miss_even_past_its_window() -> None:
    """A start deferred behind a live cycle is due, not lost (AD-4)."""
    sequencer, _, journal, anomalies = make_sequencer()
    await sequencer.async_run_now(CycleKind.EVENING, aware(6, 50))
    await sequencer.advance(aware(6, 50))
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)

    await sequencer.advance(aware(7, 10))

    assert missed_reports(anomalies) == []
    assert not any(entry["status"] == "missed" for entry in history_of(journal))
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)


async def test_a_raising_port_during_the_late_re_run_keeps_the_record() -> None:
    """Matrix error column: the usual `*_UNCONFIRMED`; the miss stays recorded."""
    sequencer, switches, journal, anomalies = make_sequencer()
    switches.raising.add(("on", VALVE_1))

    await sequencer.advance(MORNING_END)

    assert history_of(journal)[0]["status"] == "missed"
    assert AnomalyKind.VALVE_OPEN_UNCONFIRMED in {kind for kind, _ in anomalies.reports}
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].status is ZoneRunStatus.FAILED


async def test_the_engine_never_clears_a_missed_cycle_anomaly() -> None:
    """Acknowledge-only, like `CYCLE_RECOVERED`: a miss is news, not a fault to heal."""
    sequencer, _, _, anomalies = make_sequencer()
    await sequencer.advance(MORNING_END)
    clock = VirtualClock(MORNING_END)
    while sequencer.current_run is not None:
        moment = sequencer.next_wakeup(clock.now())
        assert moment is not None
        clock.advance_to(moment)
        await sequencer.advance(clock.now())

    assert AnomalyKind.MISSED_CYCLE not in {kind for kind, _ in anomalies.clears}
