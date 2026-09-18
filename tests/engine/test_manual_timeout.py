"""Manual-valve safety timeout on a virtual clock (Story 3.5).

The engine half only. A hand-opened switch gets a deadline the moment it is
observed; the deadline is folded into `next_wakeup` like every other time
intent (AD-3) and served by `advance`. Nothing here arms a timer, sleeps or
reads a wall clock.

The plan, the time map and the observation helpers all come from
`test_manual_override` rather than being redeclared: two copies of one time
map drift, and every case here builds on the pause that story owns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleStatus,
    ZoneRunStatus,
)
from tests.engine.common import (
    MANUAL_TIMEOUT_S,
    FakeRainPort,
    VirtualClock,
    aware,
    make_plan,
    paris,
)
from tests.engine.test_manual_override import (
    MORNING_END,
    PUMP,
    VALVE_1,
    VALVE_2,
    make_sequencer,
    manual_on,
    observed_off,
    run_to_idle,
    two_zone_plan,
)

if TYPE_CHECKING:
    import pytest

    from custom_components.ha_irrigation_controller.engine.plan import (
        ControllerPlan,
    )

# The ledger section that makes zone 1 fully rain-covered and zone 2 not:
# 12 mm against a zero baseline at 1 min/mm credits 720 s over a 600 s zone.
_ZONE_1_COVERED: dict[str, object] = {
    "settled_cycle_id": None,
    "deficits": {},
    "rain_source": "gauge",
    "rain_baselines": {"zone-1": 0.0, "zone-2": 12.0},
}


def _other_pump() -> ControllerPlan:
    """Return the shared plan with the pump re-pointed at another switch."""
    return make_plan(
        *two_zone_plan().zones,
        pump="switch.other_pump",
        manual_timeout_s=180,
    )


def _zone_1_only() -> ControllerPlan:
    """Return the shared plan with zone 2 deleted (a UI delete-zone)."""
    return make_plan(
        *two_zone_plan().zones[:1],
        pump=PUMP,
        manual_timeout_s=MANUAL_TIMEOUT_S,
    )


# --------------------------------------------------------------------------
# The deadline as a time intent (AC 3)
# --------------------------------------------------------------------------


async def test_a_pending_deadline_is_news_to_nobody() -> None:
    """A pause is still normal operation: no command, no anomaly, no journal write.

    The counter-metric: nothing is reported while a deadline is merely
    pending, and the deadline itself is live state that is never journalled.
    """
    sequencer, switches, journal, anomalies = make_sequencer()

    await manual_on(sequencer, VALVE_1, aware(6, 30))

    assert sequencer.manual_override is True
    assert sequencer.next_wakeup(aware(6, 30)) == aware(7)
    assert switches.commands == []
    assert anomalies.reports == []
    assert anomalies.clears == []
    assert journal.snapshots == []


async def test_next_wakeup_is_a_minimum_over_every_branch() -> None:
    """AC 3: the deadline survives each branch the old priority chain returned from.

    `next_wakeup` used to be a chain whose first matching branch won. A
    per-entity deadline has to be able to fall between two zone boundaries,
    so the branches became a candidate and the answer a minimum — and the
    four early returns below are exactly the ones that used to swallow it.
    """
    # Idle, season ON: the deadline beats the watchdog's next window end...
    earlier, _, _, _ = make_sequencer()
    await manual_on(earlier, VALVE_1, aware(6, 30))
    assert earlier.next_wakeup(aware(6, 30)) == aware(7)

    # ...and loses to it when it falls later. A minimum, not a takeover.
    later, _, _, _ = make_sequencer(two_zone_plan(manual_timeout_s=14400))
    await manual_on(later, VALVE_1, aware(6, 30))
    assert later.next_wakeup(aware(6, 30)) == MORNING_END

    # Season OFF: the idle branch used to short-circuit to None.
    off_season, _, _, _ = make_sequencer(season_enabled=False)
    await manual_on(off_season, VALVE_1, aware(6, 30))
    assert off_season.next_wakeup(aware(6, 30)) == aware(7)

    # A plan with no zones: the other idle short-circuit.
    zoneless, _, _, _ = make_sequencer(make_plan(manual_timeout_s=MANUAL_TIMEOUT_S))
    await manual_on(zoneless, VALVE_1, aware(6, 30))
    assert zoneless.next_wakeup(aware(6, 30)) == aware(7)

    # Before reconciliation: still None, and provably so — nothing can be
    # held, because `async_switch_observed` is a no-op until then.
    unreconciled, _, _, _ = make_sequencer(run_unreadable={})
    await manual_on(unreconciled, VALVE_1, aware(6, 30))
    assert unreconciled.manual_override is False
    assert unreconciled.next_wakeup(aware(6, 30)) is None


async def test_a_pending_runs_start_is_a_candidate_not_a_takeover() -> None:
    """AC 3, the PENDING branch: it used to return `scheduled_start` outright.

    A run-now is the one PENDING run that coexists with a pause (Story 3.4
    honours the operator's own command while paused), so it is what reaches
    this branch. Both directions of the `min` are pinned: the start when it
    comes first, the deadline when it does.
    """
    later, _, _, _ = make_sequencer(two_zone_plan(manual_timeout_s=MANUAL_TIMEOUT_S))
    await manual_on(later, VALVE_1, aware(6, 30))
    assert await later.async_run_now(CycleKind.MORNING, aware(6, 40)) is True
    assert later.next_wakeup(aware(6, 39)) == aware(6, 40)

    sooner, _, _, _ = make_sequencer(two_zone_plan(manual_timeout_s=60))
    await manual_on(sooner, VALVE_1, aware(6, 30))
    assert await sooner.async_run_now(CycleKind.MORNING, aware(6, 35)) is True
    assert sooner.next_wakeup(aware(6, 32)) == aware(6, 31)


async def test_a_pending_deadline_survives_the_transient_slot_window() -> None:
    """AC 3, the branch that answers None: `_zone_index` past the last zone.

    `_advance_slot` bumps the index and SAVES before the cycle completes, and
    the runner re-arms inside that save — so the accessor really is called
    with the index past the end. The old chain answered None there; with a
    deadline pending that would hand the runner "no intent at all" and drop
    the bound until the next window end.
    """
    sequencer, _, journal, _ = make_sequencer(two_zone_plan(manual_timeout_s=7200))
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await manual_on(sequencer, VALVE_1, aware(7, 2))
    sampled: list[datetime | None] = []
    journal.observer = lambda: sampled.append(sequencer.next_wakeup(clock.now()))

    await run_to_idle(sequencer, clock)

    assert sequencer.current_run is None
    assert None not in sampled
    # The last saves are taken with the index past the last zone: the
    # deadline is all that is left, and it is what the runner re-arms on.
    assert sampled[-1] == aware(9, 2)


async def test_a_repeat_open_never_extends_a_pending_deadline() -> None:
    """The deadline is minted ONCE per manual open, at the observation."""
    sequencer, _, _, _ = make_sequencer()

    await manual_on(sequencer, VALVE_1, aware(6, 30))
    await manual_on(sequencer, VALVE_1, aware(6, 50))

    assert sequencer.next_wakeup(aware(6, 50)) == aware(7)


async def test_a_plan_swapped_mid_pause_never_re_quotes_a_pending_deadline() -> None:
    """An options edit reaching the live plan does not move what is counting down.

    The plan is replaceable in place (a zone added mid-cycle, the update
    listener's deferred reload), and the deadline was minted from the plan in
    force when the switch was observed.
    """
    sequencer, switches, _, _ = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    sequencer.plan = two_zone_plan(manual_timeout_s=14400)

    assert sequencer.next_wakeup(aware(6, 30)) == aware(7)

    await sequencer.advance(aware(7))

    assert switches.commands == [("off", VALVE_1)]


async def test_a_deadline_is_elapsed_seconds_across_a_fall_back() -> None:
    """The timeout is ELAPSED seconds, so it is accumulated in UTC.

    Europe/Paris lives 2027-10-31 02:30 twice. One hour after the FIRST of
    them is the second one — not 03:30, which wall-clock arithmetic
    (`now + timedelta(...)`) would produce and which is two elapsed hours
    away. Getting this wrong leaves a valve open a full hour past its
    configured maximum, twice a year. Asserted on the UTC instant and the
    offset, because Python compares two same-zone datetimes on their naive
    fields and would call the two 02:30s equal.
    """
    plan = two_zone_plan(manual_timeout_s=3600)
    sequencer, switches, _, anomalies = make_sequencer(plan)
    opened = paris(2027, 10, 31, 2, 30)

    await manual_on(sequencer, VALVE_1, opened)

    due = sequencer.next_wakeup(opened)
    assert due is not None
    assert due.astimezone(UTC) == datetime(2027, 10, 31, 1, 30, tzinfo=UTC)
    assert due.utcoffset() == timedelta(hours=1)

    await sequencer.advance(due)

    assert switches.commands == [("off", VALVE_1)]
    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
    ]


# --------------------------------------------------------------------------
# Expiry while nothing is watering (AC 1, AC 2, AC 4)
# --------------------------------------------------------------------------


async def test_a_hand_opened_valve_is_closed_once_and_reported_at_its_deadline() -> (
    None
):
    """AC 1 / matrix row 1: one close through the verified port, one anomaly.

    The close is confirmed, so `VALVE_CLOSE_UNCONFIRMED` is CLEARED for that
    zone exactly as any other confirmed command clears it — and the timeout
    anomaly is reported on top, because the valve was left open whatever the
    close reported.
    """
    sequencer, switches, journal, anomalies = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await sequencer.advance(aware(7))

    assert switches.commands == [("off", VALVE_1)]
    assert anomalies.reports == [
        (
            AnomalyKind.MANUAL_VALVE_TIMEOUT,
            {"zone_id": "zone-1", "entity_id": VALVE_1},
        ),
    ]
    assert anomalies.clears == [
        (
            AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
            {"zone_id": "zone-1", "entity_id": VALVE_1},
        ),
    ]
    assert sequencer.manual_override is False
    assert sequencer.current_run is None
    # Nothing about a run changed, so nothing is journalled: the pause and
    # its deadline are live state from first to last.
    assert journal.snapshots == []


async def test_a_hand_close_before_the_deadline_cancels_it_silently() -> None:
    """AC 2 / matrix row 2: nothing commanded, nothing reported, no intent.

    The operator closing the switch themselves: the release path Story 3.4
    built is the whole cancellation mechanism. The non-manual release is the
    NEXT test — a different call, and matrix row 10's actual input.
    """
    sequencer, switches, journal, anomalies = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await observed_off(sequencer, VALVE_1, aware(6, 40))

    assert sequencer.manual_override is False
    assert sequencer.next_wakeup(aware(6, 40)) == MORNING_END

    await sequencer.advance(aware(7))

    assert switches.commands == []
    assert anomalies.reports == []
    assert journal.snapshots == []


async def test_a_non_manual_release_takes_the_deadline_with_it() -> None:
    """Matrix row 10: a held switch that goes `unavailable`, or is removed.

    The adapter cannot attribute either of those to the operator, so it
    reports the OFF with `manual=False` — a DIFFERENT call from a hand-close,
    and the one Story 3.4's release path answers so a pause never outlives a
    switch that dropped off. The deadline goes with the entry: nothing is
    ever commanded and no timeout anomaly fires, because no valve was left
    open by hand past its deadline — there is no valve to speak of any more.
    """
    sequencer, switches, journal, anomalies = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await observed_off(sequencer, VALVE_1, aware(6, 40), manual=False)

    assert sequencer.manual_override is False
    # The only intent left is the watchdog's next window end.
    assert sequencer.next_wakeup(aware(6, 40)) == MORNING_END

    await sequencer.advance(aware(7))

    assert switches.commands == []
    assert anomalies.reports == []
    assert journal.snapshots == []


async def test_two_held_switches_each_hold_the_pause_until_their_own_deadline() -> None:
    """Matrix row 3: the first close releases nothing but itself.

    Two hand-opened switches are two deadlines; the scheduler resumes on the
    LAST one, and the queued cycle waters in that very call.
    """
    sequencer, switches, _, _ = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6))
    await manual_on(sequencer, VALVE_2, aware(6, 20))
    await sequencer.request_cycle(CycleKind.MORNING, aware(6, 25))

    await sequencer.advance(aware(6, 30))

    assert switches.commands == [("off", VALVE_1)]
    assert sequencer.manual_override is True
    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)

    await sequencer.advance(aware(6, 50))

    assert sequencer.manual_override is False
    assert switches.commands == [
        ("off", VALVE_1),
        ("off", VALVE_2),
        ("on", PUMP),
        ("on", VALVE_1),
    ]
    assert sequencer.deferred_kinds == ()


async def test_two_deadlines_due_at_once_are_served_in_plan_order() -> None:
    """`plan.governed_entities` order — pump first, then plan order.

    Two closes in one `advance` need ONE defined sequence, and it is the same
    ordering the watch subscribes with.
    """
    sequencer, switches, _, _ = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6))
    await manual_on(sequencer, PUMP, aware(6, 1))

    await sequencer.advance(aware(6, 35))

    assert switches.commands == [("off", PUMP), ("off", VALVE_1)]
    assert sequencer.manual_override is False


async def test_a_hand_close_landing_mid_loop_is_never_reported_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-entity re-test inside the loop, and why it is not defensive.

    Three deadlines fall due in one `advance`. The first close awaits its
    verification, and the operator closes ANOTHER of the held switches by
    hand inside that window — the release applies before the lock, so it is
    already visible when the loop reaches that entity. Reporting a timeout
    for a switch the operator has just closed themselves would be a lie, and
    would command a valve that is already off.

    The interleaved close is `VALVE_1`, not the last switch held: this fake
    reports the observation INLINE inside the port call, where the real
    adapter hops to a background task, so a release that emptied the
    collection would sit waiting for the lock expiry is holding.
    """
    sequencer, switches, _, anomalies = make_sequencer()
    await manual_on(sequencer, PUMP, aware(6))
    await manual_on(sequencer, VALVE_1, aware(6))
    await manual_on(sequencer, VALVE_2, aware(6))
    closing = switches.async_turn_off

    async def _close_and_interleave(entity_id: str) -> bool:
        if entity_id == PUMP:
            await observed_off(sequencer, VALVE_1, aware(6, 35))
        return await closing(entity_id)

    monkeypatch.setattr(switches, "async_turn_off", _close_and_interleave)

    await sequencer.advance(aware(6, 35))

    assert switches.commands == [("off", PUMP), ("off", VALVE_2)]
    assert [(kind, context["entity_id"]) for kind, context in anomalies.reports] == [
        (AnomalyKind.MANUAL_VALVE_TIMEOUT, PUMP),
        (AnomalyKind.MANUAL_VALVE_TIMEOUT, VALVE_2),
    ]
    assert sequencer.manual_override is False


async def test_a_deferred_start_is_drained_and_watered_in_the_same_call() -> None:
    """AC 4: the timeout ends the pause and the deferred cycle starts at once."""
    sequencer, switches, _, anomalies = make_sequencer()
    await manual_on(sequencer, VALVE_1, aware(6, 45))
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))

    await sequencer.advance(aware(7, 15))

    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.cycle_id == "2026-07-31-morning"
    assert run.late_rerun is False
    assert switches.commands == [("off", VALVE_1), ("on", PUMP), ("on", VALVE_1)]
    assert sequencer.deferred_kinds == ()
    # The timeout is the only news: the cycle is late, not missed.
    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
    ]


async def test_the_watchdog_is_asked_before_the_queue_is_drained() -> None:
    """Story 3.4's triage #4 on the expiry path: expire, CHECK, then drain.

    The pause outlives the morning window end, so the release owes the
    watchdog a look — and it must get that look while `self._run` is still
    empty. Were the queue drained first, the popped evening cycle would read
    as "another cycle is watering" and the morning would be filed `recorded`
    instead of re-run, burning that `(day, kind)` for ever.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=5400),
    )
    await manual_on(sequencer, VALVE_1, aware(6, 30))
    await sequencer.request_cycle(CycleKind.EVENING, aware(6, 45))

    await sequencer.advance(aware(8))

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.MORNING
    assert run.late_rerun is True
    assert run.status is CycleStatus.RUNNING
    # The evening keeps its place in the queue behind the re-run.
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)
    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
        AnomalyKind.MISSED_CYCLE,
    ]
    assert anomalies.reports[1][1]["outcome"] == "rerun"
    assert switches.commands == [("off", VALVE_1), ("on", PUMP), ("on", VALVE_1)]


async def test_a_hand_close_also_asks_the_watchdog_before_draining() -> None:
    """The resume path orders it the way the timeout path does — one rule.

    Story 3.4's triage #4 on the OTHER release path: a popped run is
    `self._run` the instant it is installed, and `missed_cycles` reads a live
    run as "another cycle is watering", so the morning would be filed
    `recorded` for good instead of re-run.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=14400),
    )
    await manual_on(sequencer, VALVE_1, aware(6, 30))
    await sequencer.request_cycle(CycleKind.EVENING, aware(6, 45))

    await observed_off(sequencer, VALVE_1, aware(8))

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.MORNING
    assert run.late_rerun is True
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)
    assert [kind for kind, _ in anomalies.reports] == [AnomalyKind.MISSED_CYCLE]
    assert anomalies.reports[0][1]["outcome"] == "rerun"
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]


async def test_the_deadline_is_still_served_with_the_season_off() -> None:
    """Matrix row 12: the valve closes, the anomaly fires, nothing is watered."""
    sequencer, switches, _, anomalies = make_sequencer(season_enabled=False)
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await sequencer.advance(aware(7))

    assert switches.commands == [("off", VALVE_1)]
    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
    ]
    assert sequencer.manual_override is False
    assert sequencer.current_run is None


# --------------------------------------------------------------------------
# A close that does not confirm never holds the pause (AC 6)
# --------------------------------------------------------------------------


async def test_an_unconfirmed_close_reports_both_and_releases_anyway() -> None:
    """Matrix row 7: two anomalies, the entity released, the scheduler resumed.

    Holding the pause open on a failed close would recreate exactly the
    permanent, watering-free pause this story exists to remove.
    """
    sequencer, switches, _, anomalies = make_sequencer()
    switches.failing = {("off", VALVE_1)}
    await manual_on(sequencer, VALVE_1, aware(6, 45))
    await sequencer.request_cycle(CycleKind.MORNING, aware(7))

    await sequencer.advance(aware(7, 15))

    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
    ]
    assert sequencer.manual_override is False
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert switches.commands == [("off", VALVE_1), ("on", PUMP), ("on", VALVE_1)]


async def test_a_port_that_raises_is_the_same_outcome_with_an_error_key() -> None:
    """Matrix row 8: `_command` converts the raise; nothing aborts."""
    sequencer, switches, _, anomalies = make_sequencer()
    switches.raising = {("off", VALVE_1)}
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await sequencer.advance(aware(7))

    kinds = [kind for kind, _ in anomalies.reports]
    assert kinds == [
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
    ]
    assert "error" in anomalies.reports[0][1]
    assert "error" not in anomalies.reports[1][1]
    assert sequencer.manual_override is False


async def test_expiry_never_retries_the_close_it_could_not_confirm() -> None:
    """One attempt per manual open: no re-arm, no retry, nothing re-commanded."""
    sequencer, switches, _, _ = make_sequencer()
    switches.failing = {("off", VALVE_1)}
    await manual_on(sequencer, VALVE_1, aware(6, 30))

    await sequencer.advance(aware(7))
    assert switches.commands == [("off", VALVE_1)]
    assert sequencer.next_wakeup(aware(7)) == MORNING_END

    await sequencer.advance(aware(7, 5))

    assert switches.commands == [("off", VALVE_1)]


async def test_a_hand_opened_pump_closes_with_a_zone_less_anomaly() -> None:
    """Matrix row 9 / Decision 3: the pump holds the pause like any valve.

    Its anomaly carries `zone_id: None` — the manager keys the issue by the
    entity id then — and its confirmed close clears the PUMP kind, not the
    valve one.
    """
    sequencer, switches, _, anomalies = make_sequencer()
    await manual_on(sequencer, PUMP, aware(6, 30))

    await sequencer.advance(aware(7))

    assert switches.commands == [("off", PUMP)]
    assert anomalies.reports == [
        (AnomalyKind.MANUAL_VALVE_TIMEOUT, {"zone_id": None, "entity_id": PUMP}),
    ]
    assert anomalies.clears == [
        (AnomalyKind.PUMP_OFF_UNCONFIRMED, {"zone_id": None, "entity_id": PUMP}),
    ]
    assert sequencer.manual_override is False


# --------------------------------------------------------------------------
# A held entity the current plan no longer governs
# --------------------------------------------------------------------------


async def test_a_held_entity_the_plan_no_longer_governs_is_still_closed() -> None:
    """A zone deleted mid-pause must not leave its deadline elapsed for ever.

    A plan swap plus the UI's delete-zone button is an ordinary combination.
    Were the held valve left out of the due pass, `next_wakeup` would keep
    naming its past deadline — the runner re-arms on it, fires at once, and
    spins for ever with `manual_override` stuck True.
    """
    sequencer, switches, _, anomalies = make_sequencer()
    await manual_on(sequencer, VALVE_2, aware(6, 30))

    # Zone 2 deleted; the update listener swaps the plan in place.
    sequencer.plan = _zone_1_only()

    await sequencer.advance(aware(7))

    assert switches.commands == [("off", VALVE_2)]
    assert anomalies.reports == [
        (AnomalyKind.MANUAL_VALVE_TIMEOUT, {"zone_id": None, "entity_id": VALVE_2}),
    ]
    assert sequencer.manual_override is False
    # The value is GONE, not stuck: the only intent left is the watchdog's
    # next window end, which on the one-zone plan is 07:10.
    assert sequencer.next_wakeup(aware(7)) == aware(7, 10)


async def test_a_zoneless_valve_is_not_reported_with_the_pumps_kind() -> None:
    """The close's own kind comes from the plan's pump id, not from `zone_id`.

    A valve whose zone was deleted while it was held resolves to no zone.
    Choosing the kind on `zone_id is not None` would report "the pump did not
    confirm off" about a zone valve.
    """
    sequencer, switches, _, anomalies = make_sequencer()
    switches.failing = {("off", VALVE_2)}
    await manual_on(sequencer, VALVE_2, aware(6, 30))
    sequencer.plan = _zone_1_only()

    await sequencer.advance(aware(7))

    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
    ]


# --------------------------------------------------------------------------
# A RUNNING cycle's own switches are off limits (Decision 2, AC 5)
# --------------------------------------------------------------------------


async def test_a_timeout_during_a_running_cycle_leaves_the_cycle_alone() -> None:
    """Matrix row 4: the deadline is served without pre-empting a zone boundary.

    Zone 2's valve is hand-opened while zone 1 waters and its deadline falls
    INSIDE zone 1's slot — so the run does not hold it yet and it times out
    normally, with the boundary re-armed straight after.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=180),
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await manual_on(sequencer, VALVE_2, aware(7, 2))

    assert sequencer.next_wakeup(aware(7, 2)) == aware(7, 5)

    clock.advance_to(aware(7, 5))
    await sequencer.advance(clock.now())

    assert switches.commands == [("on", PUMP), ("on", VALVE_1), ("off", VALVE_2)]
    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
    ]
    assert sequencer.manual_override is False
    assert sequencer.next_wakeup(clock.now()) == aware(7, 10)

    await run_to_idle(sequencer, clock)

    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_2),
        ("off", VALVE_1),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("off", PUMP),
    ]
    assert finished.zone_runs[1].duration_s == 600
    assert sequencer.ledger.as_dict()["deficits"] == {}


async def test_a_deadline_inside_the_held_valves_own_slot_is_skipped() -> None:
    """AC 5 / matrix row 5 (Decision 2): the cycle's claim wins.

    Zone 2's valve is hand-opened at 07:05 with an eight-minute timeout, so
    its deadline (07:13) falls inside its OWN slot (07:10-07:20). Closing it
    there would end the zone after three minutes while the run still booked
    its full ten and carried no deficit — three minutes of water recorded as
    ten, which is the one failure class Epic 3 forbids outright. So expiry
    skips it, the deadline is merely spent, and the boundary's own close is
    what releases the pause.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=480),
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await manual_on(sequencer, VALVE_2, aware(7, 5))

    # The boundary comes first, then the deadline inside the new slot.
    assert sequencer.next_wakeup(aware(7, 5)) == aware(7, 10)
    clock.advance_to(aware(7, 10))
    await sequencer.advance(clock.now())
    assert sequencer.next_wakeup(clock.now()) == aware(7, 13)

    clock.advance_to(aware(7, 13))
    await sequencer.advance(clock.now())

    # Nothing commanded, nothing reported — and the spent deadline is no
    # longer an intent, so the boundary is what the timer points at.
    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", VALVE_2),
    ]
    assert anomalies.reports == []
    assert sequencer.manual_override is True
    assert sequencer.next_wakeup(clock.now()) == MORNING_END

    await run_to_idle(sequencer, clock)

    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    zone2 = finished.zone_runs[1]
    assert zone2.duration_s == 600
    assert zone2.actual_end == MORNING_END
    assert zone2.close_confirmed is True
    assert sequencer.ledger.as_dict()["deficits"] == {}
    # ONE close of that valve, at its own boundary — never a duplicate.
    assert switches.commands.count(("off", VALVE_2)) == 1

    # And THAT close is what ends the pause, by Story 3.4's release path.
    await observed_off(sequencer, VALVE_2, MORNING_END, manual=False)

    assert sequencer.manual_override is False


async def test_a_late_advance_never_flaps_the_valve_it_is_about_to_open() -> None:
    """`_run_holds` tests the slot live AT `now`, not the one the index names.

    Expiry is step 1 of `_advance_locked`, so on a single late call that
    drains several boundaries at once the index still names an EARLIER slot
    when expiry runs. Reading it there would close the valve the loop opens
    three lines later — a flap on real hardware, plus a `manual_valve_timeout`
    push about a valve that is about to water.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=480),
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await manual_on(sequencer, VALVE_2, aware(7, 5))

    # ONE call covering both 07:10's boundary and 07:13's deadline.
    await sequencer.advance(aware(7, 13))

    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", VALVE_2),
    ]
    assert anomalies.reports == []
    assert sequencer.manual_override is True


async def test_a_deadline_on_the_pump_mid_cycle_is_skipped() -> None:
    """Matrix row 6: the remaining zones keep their pressure.

    A mid-cycle pump close would run every zone after it dry — and the run
    would still book them in full. The pump's own off at completion is what
    releases it.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=180),
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await manual_on(sequencer, PUMP, aware(7, 2))

    clock.advance_to(aware(7, 5))
    await sequencer.advance(clock.now())

    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]
    assert anomalies.reports == []
    assert sequencer.manual_override is True
    assert sequencer.next_wakeup(clock.now()) == aware(7, 10)

    await run_to_idle(sequencer, clock)

    assert switches.commands == [
        ("on", PUMP),
        ("on", VALVE_1),
        ("off", VALVE_1),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("off", PUMP),
    ]
    assert sequencer.ledger.as_dict()["deficits"] == {}

    await observed_off(sequencer, PUMP, MORNING_END, manual=False)

    assert sequencer.manual_override is False


async def test_a_deadline_due_while_its_own_zone_is_skipped_still_times_out() -> None:
    """A rain-SKIPPED slot is never commanded, so it shelters nothing.

    Zone 1 is fully rain-covered and quoted at zero, so the cycle never opens
    its valve — the run is not holding it, and a hand-open on it must time
    out like any other. `_run_holds` counts the live slot only while THAT
    zone's status is RUNNING, which a skipped one never reaches.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=300),
        rain=FakeRainPort(total=12.0),
        ledger=_ZONE_1_COVERED,
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].status is ZoneRunStatus.SKIPPED
    assert run.zone_runs[1].status is ZoneRunStatus.RUNNING

    await manual_on(sequencer, VALVE_1, aware(7, 2))
    clock.advance_to(aware(7, 7))
    await sequencer.advance(clock.now())

    assert switches.commands == [("on", PUMP), ("on", VALVE_2), ("off", VALVE_1)]
    assert anomalies.reports == [
        (
            AnomalyKind.MANUAL_VALVE_TIMEOUT,
            {"zone_id": "zone-1", "entity_id": VALVE_1},
        ),
    ]
    assert sequencer.manual_override is False


async def test_a_completion_still_drains_nothing_while_a_hand_open_remains() -> None:
    """Decision 2's cost, bounded: the pause outlives the cycle that absorbed it.

    Zone 2's valve is held through its own slot (its deadline spent), so the
    completing cycle starts nothing — until the boundary's close is observed.
    """
    sequencer, switches, _, _ = make_sequencer(
        two_zone_plan(manual_timeout_s=480),
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, aware(7, 2))
    await manual_on(sequencer, VALVE_2, aware(7, 5))

    # Step the boundary, then serve the deadline inside zone 2's own slot:
    # spent, so what is left is the boundary. Asserted here rather than left
    # to `run_to_idle`, which a deadline stuck in the past would spin on for
    # ever instead of failing.
    for moment in (aware(7, 10), aware(7, 13)):
        clock.advance_to(moment)
        await sequencer.advance(clock.now())
    assert sequencer.next_wakeup(clock.now()) == MORNING_END

    await run_to_idle(sequencer, clock)

    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)
    assert switches.commands[-1] == ("off", PUMP)

    await observed_off(sequencer, VALVE_2, MORNING_END, manual=False)

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.EVENING


# --------------------------------------------------------------------------
# A reload or a restart (matrix row 11)
# --------------------------------------------------------------------------


async def test_a_reload_loses_the_pause_and_its_deadline_but_not_the_start() -> None:
    """Matrix row 11: live state dies with the process; the queue does not.

    `_manual_on` is never journalled and never seeded, so a pending deadline
    cannot survive a reload — which is exactly why nothing closes a switch
    still physically open afterwards, and why the README says so rather than
    promising an unconditional close. The start the pause deferred IS
    journalled, and `async_reconcile` drains it (Story 3.4, Decision 5).
    """
    before, _, journal, _ = make_sequencer()
    await manual_on(before, VALVE_1, aware(6, 45))
    await before.request_cycle(CycleKind.MORNING, aware(7))

    # The deadline is a live time intent before the reload...
    assert before.manual_override is True
    assert before.next_wakeup(aware(7)) == aware(7, 15)
    # ...and the ONE thing the journal carries is the deferred start. The
    # key set is pinned, not just `deferred`: a new field here would be the
    # pause (or its deadlines) leaking into storage, which is the whole
    # premise of this row.
    snapshot = journal.snapshots[-1]
    assert snapshot["deferred"] == [
        {"kind": "morning", "reference": "2026-07-31T05:00:00+00:00"},
    ]
    assert set(snapshot) == {
        "schema_version",
        "run",
        "zone_index",
        "last_run",
        "deferred",
        "history",
        "season_enabled",
        "ledger",
        "watchdog_since",
    }

    # The reload: a fresh machine seeded from that snapshot and nothing else.
    after, switches, _, anomalies = make_sequencer(
        deferred=[(CycleKind.MORNING, aware(7))],
    )

    assert after.manual_override is False
    assert after.next_wakeup(aware(7, 5)) == MORNING_END

    await after.async_reconcile(aware(7, 5))

    run = after.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.cycle_id == "2026-07-31-morning"
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]
    assert anomalies.reports == []


async def test_a_deadline_due_while_its_own_zone_failed_to_open_still_times_out() -> (
    None
):
    """A FAILED slot never confirmed its open, so the cycle is not holding it.

    The SKIPPED sibling's shape, on the other excluded status. The valve was
    commanded on and never reported ON, so whatever holds it open it is not
    this cycle — and sheltering it would leave a hand-opened valve past its
    maximum with no issue, no push and nothing left to close it.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=300),
    )
    switches.failing = {("on", VALVE_1)}
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].status is ZoneRunStatus.FAILED

    await manual_on(sequencer, VALVE_1, aware(7, 2))
    clock.advance_to(aware(7, 7))
    await sequencer.advance(clock.now())

    assert switches.commands == [("on", PUMP), ("on", VALVE_1), ("off", VALVE_1)]
    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.VALVE_OPEN_UNCONFIRMED,
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
    ]
    assert sequencer.manual_override is False


async def test_the_run_keeps_its_own_pump_when_the_plan_re_points_it() -> None:
    """`_run_holds` reads the RUN's snapshot, never the live plan (AD-8).

    A zone edit mid-cycle swaps `sequencer.plan` in place, and a re-pointed
    pump would make the live cycle's own pump look like a switch the plan no
    longer governs: expiry would close it at its deadline, and every zone
    after that would be booked fully watered with no pressure behind it.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=180),
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await manual_on(sequencer, PUMP, aware(7, 2))

    sequencer.plan = _other_pump()

    clock.advance_to(aware(7, 5))
    await sequencer.advance(clock.now())

    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]
    assert anomalies.reports == []
    assert sequencer.manual_override is True


async def test_a_spent_deadline_is_re_armed_once_the_run_lets_go() -> None:
    """The bound comes back when the cycle's claim ends without a close.

    Decision 2 hands the bound to the cycle: the slot's own close releases
    the switch. Here that close is never confirmed, so no OFF is ever
    observed and the spent `None` would sit in the collection for ever —
    `manual_override` stuck True, the scheduler stopped, the watchdog
    returning at once, and nothing left to shut the valve until a reload.
    That is the permanent pause this story exists to remove, so it must not
    come back through the Decision 2 door.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(manual_timeout_s=480),
    )
    switches.failing = {("off", VALVE_2)}
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await manual_on(sequencer, VALVE_2, aware(7, 5))

    # 07:10 opens zone 2's slot, 07:13 spends the deadline inside it, and
    # 07:20 is the boundary whose close does not confirm.
    for moment in (aware(7, 10), aware(7, 13), MORNING_END):
        clock.advance_to(moment)
        await sequencer.advance(clock.now())

    assert sequencer.current_run is None
    assert sequencer.manual_override is True
    # Re-armed from the instant the run let go — a full timeout, not the
    # elapsed one, and a real intent the runner can arm on.
    assert sequencer.next_wakeup(MORNING_END) == aware(7, 28)

    clock.advance_to(aware(7, 28))
    await sequencer.advance(clock.now())

    assert switches.commands[-1] == ("off", VALVE_2)
    assert [kind for kind, _ in anomalies.reports] == [
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
    ]
    assert sequencer.manual_override is False
