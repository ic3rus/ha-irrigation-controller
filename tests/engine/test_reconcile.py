"""Startup reconciliation on a virtual clock (Story 3.2, AC 1-4, AD-11).

Every scenario is a real crash: a fresh machine is driven to the instant of
the crash and the run is taken FROM THE JOURNAL as the engine wrote it, then
rebuilt through `CycleRun.from_dict` into a NEW machine with the actual switch
states a restart would find — no hand-built run objects, so the snapshot the
reconciler acts on is exactly the one the engine journals.

Three layers, bottom up: the strict `from_dict` trust boundary, the pure
`plan_recovery` decision, and `Sequencer.async_reconcile` applying it through
the ports (orphan pass, resume commands, close through `_complete_cycle`).
"""

from __future__ import annotations

from datetime import UTC
from itertools import pairwise
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.ha_irrigation_controller.engine.plan import (
    ControllerPlan,
    CycleKind,
)
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.reconcile import (
    RecoveryOutcome,
    plan_recovery,
)
from custom_components.ha_irrigation_controller.engine.runs import (
    MAX_JOURNALED_SECONDS,
    CycleRun,
    CycleStatus,
    ZoneRunStatus,
    live_zone,
)
from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer
from tests.engine.common import (
    TZ,
    FakeAnomalyPort,
    FakeJournalPort,
    FakeSwitchPort,
    VirtualClock,
    aware,
    make_plan,
    make_zone,
)

if TYPE_CHECKING:
    from datetime import datetime

PUMP = "switch.pool_pump"
VALVE_1 = "switch.zone_1_valve"
VALVE_2 = "switch.zone_2_valve"
VALVE_3 = "switch.zone_3_valve"

# Everything OFF — what a clean restart finds.
ALL_OFF: dict[str, bool | None] = {
    PUMP: False,
    VALVE_1: False,
    VALVE_2: False,
    VALVE_3: False,
}


def three_zone_plan() -> ControllerPlan:
    """Build the crash-test plan: 07:00 → zone 1 (10 min), zone 2 (15), zone 3 (5)."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=600),
        make_zone("zone-2", valve=VALVE_2, morning_s=900, evening_s=900),
        make_zone("zone-3", valve=VALVE_3, morning_s=300, evening_s=300),
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
        plan if plan is not None else three_zone_plan(),
        switches=switches,
        journal=journal,
        anomalies=anomalies,
        **seeds,
    )
    return sequencer, switches, journal, anomalies


async def run_to_idle(sequencer: Sequencer, clock: VirtualClock) -> None:
    """Drive the ACTIVE cycle with the exact wake-up loop the adapter uses.

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


async def crash_snapshot(at: str) -> dict[str, object]:
    """Drive a fresh machine to the crash instant `at`; return the journaled run.

    The journal's LAST write before the crash is what a restart reads back:

    - `pending`: requested at 06:59 for 07:00, nothing commanded yet;
    - `zone-1`: 07:00, pump on and zone 1 open;
    - `between`: 07:10, zone 1 closed, zone 2 NOT yet opened — the
      `_advance_slot` save that precedes the open;
    - `zone-2`: 07:10, zone 2 open;
    - `done`: 07:30, every zone closed, the run still RUNNING — the save
      that precedes the completion's pump-off.
    """
    sequencer, _, journal, _ = make_sequencer()
    clock = VirtualClock(aware(6, 59))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    if at == "pending":
        return journal.snapshots[-1]
    clock.advance_to(aware(7))
    await sequencer.advance(clock.now())
    if at == "zone-1":
        return journal.snapshots[-1]
    clock.advance_to(aware(7, 10))
    await sequencer.advance(clock.now())
    if at == "between":
        return journal.snapshots[-2]
    if at == "zone-2":
        return journal.snapshots[-1]
    clock.advance_to(aware(7, 25))
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 30))
    await sequencer.advance(clock.now())
    assert at == "done"
    return journal.snapshots[-2]


def run_of(snapshot: dict[str, object]) -> CycleRun:
    """Rebuild the journaled run through the trust boundary, HA-local."""
    document = snapshot["run"]
    assert isinstance(document, dict)
    return CycleRun.from_dict(document, tz=TZ)


async def restart(
    at: str,
    *,
    states: dict[str, bool | None],
    **seeds: Any,
) -> tuple[Sequencer, FakeSwitchPort, FakeJournalPort, FakeAnomalyPort]:
    """Crash at `at`, then rebuild the machine on the journaled run and `states`."""
    run = run_of(await crash_snapshot(at))
    sequencer, switches, journal, anomalies = make_sequencer(run=run, **seeds)
    switches.states = dict(states)
    return sequencer, switches, journal, anomalies


def recovered(anomalies: FakeAnomalyPort) -> list[dict[str, object]]:
    """Return the contexts of every `CYCLE_RECOVERED` report."""
    return [
        context
        for kind, context in anomalies.reports
        if kind is AnomalyKind.CYCLE_RECOVERED
    ]


def other_reports(anomalies: FakeAnomalyPort) -> list[AnomalyKind]:
    """Return the kinds of every report that is not the recovery itself."""
    return [
        kind for kind, _ in anomalies.reports if kind is not AnomalyKind.CYCLE_RECOVERED
    ]


def cleared(anomalies: FakeAnomalyPort) -> set[AnomalyKind]:
    """Return the kinds ever cleared."""
    return {kind for kind, _ in anomalies.clears}


def windows(run: CycleRun) -> list[tuple[datetime, datetime]]:
    """Return every zone's planned window."""
    return [(zone.planned_start, zone.planned_end) for zone in run.zone_runs]


# --------------------------------------------------------------------------
# The trust boundary: `CycleRun.from_dict` is the strict inverse of `as_dict`
# --------------------------------------------------------------------------


@pytest.mark.parametrize("at", ["pending", "zone-1", "between", "zone-2", "done"])
async def test_from_dict_round_trips_every_journaled_shape(at: str) -> None:
    """What the engine wrote is exactly what comes back — instants HA-local."""
    snapshot = await crash_snapshot(at)
    document = snapshot["run"]
    assert isinstance(document, dict)

    run = CycleRun.from_dict(document, tz=TZ)

    assert run.as_dict() == document
    assert run.configured_start.tzinfo is TZ
    assert run.zone_runs[0].planned_start.tzinfo is TZ
    # A second trip through the boundary is the identity.
    assert CycleRun.from_dict(run.as_dict(), tz=TZ) == run


async def test_from_dict_reads_the_optional_markers_conservatively() -> None:
    """`manual` and `late_rerun` go through the "only a real True" rule.

    `recovery` (pre-3.2) and `late_rerun` (pre-3.3) are both optional on
    read, and a restart must not silently strip a marker it CAN read: a late
    re-run interrupted by a restart is still a late re-run, and Epic 4 shows
    it as one.
    """
    sequencer, _, journal, _ = make_sequencer()
    clock = VirtualClock(aware(9))
    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now())
    document = dict(journal.snapshots[-1]["run"])  # type: ignore[call-overload]
    del document["recovery"]
    del document["late_rerun"]

    run = CycleRun.from_dict(document, tz=TZ)

    assert run.manual is True
    assert run.recovery is None
    assert run.late_rerun is False
    assert CycleRun.from_dict({**document, "manual": "true"}, tz=TZ).manual is False
    # The marker really does survive the boundary, both ways round.
    marked = CycleRun.from_dict({**document, "late_rerun": True}, tz=TZ)
    assert marked.late_rerun is True
    assert marked.as_dict()["late_rerun"] is True
    hand_edited = CycleRun.from_dict({**document, "late_rerun": "true"}, tz=TZ)
    assert hand_edited.late_rerun is False


@pytest.mark.parametrize(
    "mutation",
    [
        {"cycle_id": None},
        {"kind": "noon"},
        {"status": "paused"},
        {"configured_start": "yesterday"},
        {"configured_start": "2026-07-31T05:00:00"},  # naive
        {"configured_start": 1785474000},
        {"scheduled_start": None},
        {"pump_entity_id": ["switch.pool_pump"]},
        {"pump_on_confirmed": "yes"},
        {"rain_total_mm": "12"},
        {"rain_source": 7},
        {"recovery": 1},
        {"zones": "none"},
        {"zones": [None]},
        {"zones": [{"zone_id": "zone-1"}]},
    ],
    ids=[
        "id-none",
        "kind-unknown",
        "status-unknown",
        "start-unparsable",
        "start-naive",
        "start-int",
        "scheduled-none",
        "pump-list",
        "confirmed-str",
        "rain-str",
        "source-int",
        "recovery-int",
        "zones-str",
        "zone-none",
        "zone-missing-keys",
    ],
)
async def test_from_dict_refuses_a_run_that_is_not_exactly_the_written_shape(
    mutation: dict[str, object],
) -> None:
    """Fail-wet at the boundary: a drifted run is a `ValueError`, never a guess."""
    document = (await crash_snapshot("zone-1"))["run"]
    assert isinstance(document, dict)

    with pytest.raises(ValueError, match=next(iter(mutation))[:5]):
        CycleRun.from_dict({**document, **mutation}, tz=TZ)


@pytest.mark.parametrize(
    "mutation",
    [
        {"duration_s": -1},
        {"duration_s": True},
        {"base_s": "600"},
        {"planned_end": None},
        {"actual_start": "07:00"},
        {"status": "open"},
        {"open_confirmed": 1},
        {"valve_entity_id": None},
    ],
)
async def test_from_dict_refuses_a_drifted_zone(mutation: dict[str, object]) -> None:
    """Zones are validated with the same strictness — the reconciler re-times them."""
    document = (await crash_snapshot("zone-1"))["run"]
    assert isinstance(document, dict)
    zones = document["zones"]
    assert isinstance(zones, list)
    first = zones[0]
    assert isinstance(first, dict)

    with pytest.raises(ValueError, match=next(iter(mutation))):
        CycleRun.from_dict(
            {**document, "zones": [{**first, **mutation}, *zones[1:]]},
            tz=TZ,
        )


async def test_from_dict_names_the_key_once_for_a_non_string_enum() -> None:
    """The type check runs outside the enum wrap: no "status: status: …" doubling."""
    document = (await crash_snapshot("zone-1"))["run"]
    assert isinstance(document, dict)

    with pytest.raises(ValueError, match=r"^status: expected a str, got int$"):
        CycleRun.from_dict({**document, "status": 3}, tz=TZ)


async def test_from_dict_refuses_a_duration_the_clock_arithmetic_cannot_carry() -> None:
    """A hand-edited `duration_s` of 10**20 would overflow `_retime`: refused."""
    document = (await crash_snapshot("zone-1"))["run"]
    assert isinstance(document, dict)
    zones = document["zones"]
    assert isinstance(zones, list)
    first = zones[0]
    assert isinstance(first, dict)

    with pytest.raises(ValueError, match=r"zones\[0\]\.duration_s"):
        CycleRun.from_dict(
            {**document, "zones": [{**first, "duration_s": 10**20}, *zones[1:]]},
            tz=TZ,
        )
    with pytest.raises(ValueError, match="base_s"):
        CycleRun.from_dict(
            {
                **document,
                "zones": [{**first, "base_s": MAX_JOURNALED_SECONDS + 1}, *zones[1:]],
            },
            tz=TZ,
        )
    # The ceiling itself is legal.
    CycleRun.from_dict(
        {**document, "zones": [{**first, "base_s": MAX_JOURNALED_SECONDS}, *zones[1:]]},
        tz=TZ,
    )


@pytest.mark.parametrize("total", [float("nan"), float("inf"), float("-inf")])
async def test_from_dict_refuses_a_non_finite_rain_total(total: float) -> None:
    """`rain_total_mm` must be finite — the ledger guards the same on its section."""
    document = (await crash_snapshot("zone-1"))["run"]
    assert isinstance(document, dict)

    with pytest.raises(ValueError, match="rain_total_mm"):
        CycleRun.from_dict({**document, "rain_total_mm": total}, tz=TZ)


async def test_from_dict_refuses_an_instant_it_cannot_convert() -> None:
    """An instant at `datetime`'s edge overflows `astimezone`: refused, not raised."""
    document = (await crash_snapshot("zone-1"))["run"]
    assert isinstance(document, dict)

    with pytest.raises(ValueError, match=r"scheduled_start.*out of range"):
        CycleRun.from_dict(
            {**document, "scheduled_start": "9999-12-31T23:00:00-02:00"},
            tz=TZ,
        )


async def failed_open_snapshot() -> dict[str, object]:
    """Drive a machine whose zone 1 open never confirms to 07:00; return the run.

    The slot is consumed FAILED (fail-wet) with its `actual_start` set — the
    live zone by `live_zone`'s rule, indistinguishable from a healthy one
    except by status.
    """
    sequencer, switches, journal, _ = make_sequencer()
    switches.failing = {("on", VALVE_1)}
    clock = VirtualClock(aware(6, 59))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    clock.advance_to(aware(7))
    await sequencer.advance(clock.now())
    return journal.snapshots[-1]


# --------------------------------------------------------------------------
# The pure decision: `plan_recovery`
# --------------------------------------------------------------------------


async def test_pending_same_day_season_on_resumes_from_now() -> None:
    """Crash before start: re-timed from `now`, `scheduled_start = now`, slot 0."""
    run = run_of(await crash_snapshot("pending"))
    now = aware(7, 3)

    recovery = plan_recovery(run, now=now, states=ALL_OFF, season_enabled=True)

    assert recovery.outcome is RecoveryOutcome.RESUMED
    assert recovery.slot == 0
    assert recovery.live_zone_id is None
    assert run.scheduled_start == now
    assert run.configured_start == aware(7)  # identity never moves
    assert windows(run) == [
        (aware(7, 3), aware(7, 13)),
        (aware(7, 13), aware(7, 28)),
        (aware(7, 28), aware(7, 33)),
    ]
    assert run.status is CycleStatus.PENDING


@pytest.mark.parametrize(
    ("now", "season_enabled"),
    [
        (aware(7, 3), False),  # season OFF: nothing was commanded, 1.6 covers RUNNING
        (aware(7, 3, day=1, month=8), True),  # a later irrigation day
    ],
    ids=["season-off", "later-day"],
)
async def test_pending_closes_when_it_cannot_resume(
    now: datetime,
    season_enabled: bool,  # noqa: FBT001 — a parametrized input, not a flag
) -> None:
    """A PENDING run under season OFF or on a later day is closed, zones untouched."""
    run = run_of(await crash_snapshot("pending"))
    before = windows(run)

    recovery = plan_recovery(
        run, now=now, states=ALL_OFF, season_enabled=season_enabled
    )

    assert recovery.outcome is RecoveryOutcome.CLOSED
    assert windows(run) == before
    assert all(zone.status is ZoneRunStatus.PENDING for zone in run.zone_runs)


async def test_live_valve_on_and_slot_not_elapsed_keeps_the_zone_running() -> None:
    """Proven watering since `actual_start`: `planned_end = actual_start + duration`."""
    run = run_of(await crash_snapshot("zone-1"))

    recovery = plan_recovery(
        run,
        now=aware(7, 4),
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
        season_enabled=True,
    )

    assert recovery.outcome is RecoveryOutcome.RESUMED
    assert recovery.slot == 0
    assert recovery.live_zone_id == "zone-1"
    zone = run.zone_runs[0]
    assert zone.status is ZoneRunStatus.RUNNING
    assert zone.actual_start == aware(7)
    assert zone.actual_end is None
    assert windows(run) == [
        (aware(7), aware(7, 10)),
        (aware(7, 10), aware(7, 25)),
        (aware(7, 25), aware(7, 30)),
    ]


async def test_live_valve_on_and_slot_elapsed_completes_it_at_now() -> None:
    """Over-watering is booked honestly: COMPLETED at `now`, resume at the next slot."""
    run = run_of(await crash_snapshot("zone-1"))

    recovery = plan_recovery(
        run,
        now=aware(7, 12),
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
        season_enabled=True,
    )

    assert recovery.outcome is RecoveryOutcome.RESUMED
    assert recovery.slot == 1
    zone = run.zone_runs[0]
    assert zone.status is ZoneRunStatus.COMPLETED
    assert zone.actual_end == aware(7, 12)
    assert windows(run)[1:] == [
        (aware(7, 12), aware(7, 27)),
        (aware(7, 27), aware(7, 32)),
    ]


@pytest.mark.parametrize("valve_state", [False, None], ids=["off", "unknown"])
async def test_live_valve_off_or_unknown_re_runs_the_zone_in_full(
    valve_state: bool | None,  # noqa: FBT001 — a parametrized switch state
) -> None:
    """Unproven watering: the zone is reset to PENDING and re-timed from `now`."""
    run = run_of(await crash_snapshot("zone-1"))

    recovery = plan_recovery(
        run,
        now=aware(7, 4),
        states={**ALL_OFF, PUMP: True, VALVE_1: valve_state},
        season_enabled=True,
    )

    assert recovery.outcome is RecoveryOutcome.RESUMED
    assert recovery.slot == 0
    assert recovery.live_zone_id == "zone-1"
    zone = run.zone_runs[0]
    assert zone.status is ZoneRunStatus.PENDING
    assert zone.actual_start is None
    assert zone.open_confirmed is None
    assert windows(run) == [
        (aware(7, 4), aware(7, 14)),
        (aware(7, 14), aware(7, 29)),
        (aware(7, 29), aware(7, 34)),
    ]
    assert live_zone(run) is None


async def test_a_failed_open_is_never_credited_even_with_the_valve_on_same_day() -> (
    None
):
    """A FAILED slot stays FAILED at a boundary; recovery credits it no more.

    The valve reading ON says nothing the unconfirmed open did not: the zone
    is unproven and re-runs in full from `now`, exactly like an OFF valve.
    """
    run = run_of(await failed_open_snapshot())
    assert run.zone_runs[0].status is ZoneRunStatus.FAILED

    recovery = plan_recovery(
        run,
        now=aware(7, 4),
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
        season_enabled=True,
    )

    assert recovery.outcome is RecoveryOutcome.RESUMED
    assert recovery.slot == 0
    zone = run.zone_runs[0]
    assert zone.status is ZoneRunStatus.PENDING
    assert zone.actual_start is None
    assert windows(run)[0] == (aware(7, 4), aware(7, 14))


async def test_a_failed_open_stays_failed_on_a_later_day() -> None:
    """Later day, valve ON, open unconfirmed: FAILED, 0 s, the whole slot owed."""
    run = run_of(await failed_open_snapshot())

    recovery = plan_recovery(
        run,
        now=aware(7, 4, day=1, month=8),
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
        season_enabled=True,
    )

    assert recovery.outcome is RecoveryOutcome.CLOSED
    zone = run.zone_runs[0]
    assert zone.status is ZoneRunStatus.FAILED
    assert zone.actual_end is None


async def test_a_crash_between_zones_resumes_at_the_next_pending_slot() -> None:
    """Zone 1 closed, zone 2 never opened: resume at zone 2, re-timed from `now`."""
    run = run_of(await crash_snapshot("between"))
    assert run.zone_runs[0].status is ZoneRunStatus.COMPLETED
    assert run.zone_runs[1].actual_start is None

    recovery = plan_recovery(
        run,
        now=aware(7, 12),
        states={**ALL_OFF, PUMP: True},
        season_enabled=True,
    )

    assert recovery.outcome is RecoveryOutcome.RESUMED
    assert recovery.slot == 1
    assert recovery.live_zone_id is None
    assert windows(run)[0] == (aware(7), aware(7, 10))  # finished: untouched
    assert windows(run)[1:] == [
        (aware(7, 12), aware(7, 27)),
        (aware(7, 27), aware(7, 32)),
    ]


async def test_every_slot_spent_resumes_past_the_last_one() -> None:
    """All zones closed, run still RUNNING: nothing left but the completion."""
    run = run_of(await crash_snapshot("done"))

    recovery = plan_recovery(
        run,
        now=aware(7, 31),
        states={**ALL_OFF, PUMP: True},
        season_enabled=True,
    )

    assert recovery.outcome is RecoveryOutcome.RESUMED
    assert recovery.slot == 3


async def test_running_same_day_under_season_off_still_resumes() -> None:
    """Story 1.6 AC 1: a cycle in progress completes — season OFF does not stop it."""
    run = run_of(await crash_snapshot("zone-2"))

    recovery = plan_recovery(
        run,
        now=aware(7, 12),
        states={**ALL_OFF, PUMP: True, VALVE_2: True},
        season_enabled=False,
    )

    assert recovery.outcome is RecoveryOutcome.RESUMED
    assert recovery.slot == 1


@pytest.mark.parametrize(
    ("valve_state", "expected_status", "expected_end"),
    [
        (True, ZoneRunStatus.COMPLETED, aware(7, 4, day=1, month=8)),
        (False, ZoneRunStatus.FAILED, None),
        (None, ZoneRunStatus.FAILED, None),
    ],
    ids=["on", "off", "unknown"],
)
async def test_a_later_day_closes_and_stamps_the_live_zone(
    valve_state: bool | None,  # noqa: FBT001 — a parametrized switch state
    expected_status: ZoneRunStatus,
    expected_end: datetime | None,
) -> None:
    """Later day: the live zone is COMPLETED at `now` when proven, FAILED otherwise."""
    run = run_of(await crash_snapshot("zone-1"))
    pending_before = windows(run)[1:]

    recovery = plan_recovery(
        run,
        now=aware(7, 4, day=1, month=8),
        states={**ALL_OFF, PUMP: True, VALVE_1: valve_state},
        season_enabled=True,
    )

    assert recovery.outcome is RecoveryOutcome.CLOSED
    assert recovery.live_zone_id == "zone-1"
    zone = run.zone_runs[0]
    assert zone.status is expected_status
    assert zone.actual_end == expected_end
    # Pending zones untouched: full deficit, nothing re-timed.
    assert windows(run)[1:] == pending_before
    assert all(zone.status is ZoneRunStatus.PENDING for zone in run.zone_runs[1:])


async def test_a_later_day_between_zones_closes_without_stamping_anything() -> None:
    """No live zone on a later day: close, every zone as journaled."""
    run = run_of(await crash_snapshot("between"))
    statuses = [zone.status for zone in run.zone_runs]

    recovery = plan_recovery(
        run,
        now=aware(7, 12, day=1, month=8),
        states=ALL_OFF,
        season_enabled=True,
    )

    assert recovery.outcome is RecoveryOutcome.CLOSED
    assert recovery.live_zone_id is None
    assert [zone.status for zone in run.zone_runs] == statuses


async def test_re_timing_accumulates_elapsed_seconds_not_wall_clock() -> None:
    """Windows are laid out in UTC and converted back — DST-safe like `zone_windows`."""
    run = run_of(await crash_snapshot("pending"))
    now = aware(7, 3)

    plan_recovery(run, now=now, states=ALL_OFF, season_enabled=True)

    utc_starts = [zone.planned_start.astimezone(UTC) for zone in run.zone_runs]
    assert [(b - a).total_seconds() for a, b in pairwise(utc_starts)] == [600, 900]


# --------------------------------------------------------------------------
# The sequencer applies the decision: `async_reconcile`
# --------------------------------------------------------------------------


async def test_orphans_are_closed_valves_first_then_the_pump_before_any_decision() -> (
    None
):
    """AC 1: every governed switch that is not OFF is commanded off, valve→pump.

    Two valves ON (a stuck one from zone 1 plus the live zone 2) and the
    pump: three closes in plan order, and only THEN the resume commands.
    """
    sequencer, switches, _, anomalies = await restart(
        "zone-2",
        states={**ALL_OFF, PUMP: True, VALVE_1: True, VALVE_2: True},
    )

    await sequencer.async_reconcile(aware(7, 12))

    assert switches.commands[:3] == [("off", VALVE_1), ("off", VALVE_2), ("off", PUMP)]
    assert switches.commands[3:] == [("on", PUMP), ("on", VALVE_2)]
    assert other_reports(anomalies) == []
    assert sequencer.reconciled is True


async def test_a_switch_reading_unknown_is_commanded_off() -> None:
    """Matrix "switch unavailable": `None` is "not OFF" — commanded off, no credit."""
    sequencer, switches, _, _ = await restart(
        "zone-1",
        states={PUMP: None, VALVE_1: None, VALVE_2: False, VALVE_3: False},
    )

    await sequencer.async_reconcile(aware(7, 4))

    assert switches.commands[:2] == [("off", VALVE_1), ("off", PUMP)]
    # Unproven: the zone re-runs in full from now.
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].actual_start == aware(7, 4)


async def test_a_state_read_that_raises_is_doubtful_not_fatal() -> None:
    """A port leaking from `async_is_on` reads as `None`; the reconcile completes."""
    sequencer, switches, _, _ = await restart("zone-1", states=ALL_OFF)
    switches.reads_raising = {VALVE_1}

    await sequencer.async_reconcile(aware(7, 4))

    assert ("off", VALVE_1) in switches.commands
    assert sequencer.current_run is not None
    assert sequencer.reconciled is True


async def test_unconfirmed_orphan_closes_report_as_usual_and_confirmed_ones_clear() -> (
    None
):
    """The orphan pass uses the verified port: VALVE_CLOSE/PUMP_OFF_UNCONFIRMED."""
    sequencer, switches, _, anomalies = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
    )
    switches.failing = {("off", VALVE_1), ("off", PUMP)}

    await sequencer.async_reconcile(aware(7, 4))

    kinds = [kind for kind, _ in anomalies.reports]
    assert kinds[:2] == [
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        AnomalyKind.PUMP_OFF_UNCONFIRMED,
    ]
    assert anomalies.reports[0][1] == {
        "cycle_id": "2026-07-31-morning",
        "zone_id": "zone-1",
        "entity_id": VALVE_1,
    }
    assert AnomalyKind.CYCLE_RECOVERED in kinds
    # The orphan pass mutates no zone field: the close outcome belongs to
    # the close that ends the slot, and this slot is about to be re-opened.
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].close_confirmed is None


async def test_same_day_resume_re_opens_the_live_zone_and_keeps_its_start() -> None:
    """Matrix row 2 end to end: orphans, pump on, valve re-opened, windows kept.

    `CYCLE_INTERRUPTED` is cleared as superseded, `CYCLE_RECOVERED` reported
    once with the live zone, and the cycle then completes on the journaled
    durations — settling exactly once.
    """
    sequencer, switches, journal, anomalies = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
    )
    clock = VirtualClock(aware(7, 4))

    await sequencer.async_reconcile(clock.now())

    assert switches.commands == [
        ("off", VALVE_1),
        ("off", PUMP),
        ("on", PUMP),
        ("on", VALVE_1),
    ]
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.recovery == "resumed"
    assert run.pump_on_confirmed is True
    zone = run.zone_runs[0]
    assert (zone.status, zone.actual_start, zone.open_confirmed) == (
        ZoneRunStatus.RUNNING,
        aware(7),
        True,
    )
    assert sequencer.next_wakeup(clock.now()) == aware(7, 10)
    assert recovered(anomalies) == [
        {
            "cycle_id": "2026-07-31-morning",
            "kind": "morning",
            "zone_id": "zone-1",
            "outcome": "resumed",
        },
    ]
    assert (AnomalyKind.CYCLE_INTERRUPTED, {"cycle_id": "2026-07-31-morning"}) in (
        anomalies.clears
    )
    assert journal.snapshots[-1]["run"] == run.as_dict()

    await run_to_idle(sequencer, clock)

    assert switches.commands[4:] == [
        ("off", VALVE_1),
        ("on", VALVE_2),
        ("off", VALVE_2),
        ("on", VALVE_3),
        ("off", VALVE_3),
        ("off", PUMP),
    ]
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert finished.recovery == "resumed"
    assert [zone.actual_end for zone in finished.zone_runs] == [
        aware(7, 10),
        aware(7, 25),
        aware(7, 30),
    ]
    assert sequencer.ledger.settled_cycle_id == "2026-07-31-morning"
    assert sequencer.ledger.as_dict()["deficits"] == {}
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert [(entry["status"], entry["recovery"]) for entry in history] == [
        ("completed", "resumed"),
    ]


async def test_a_resume_never_re_quotes_the_journaled_durations() -> None:
    """AC 2: a deficit seeded into the ledger does not touch the resumed run.

    `_build_run`/`Ledger.quote` are never called by recovery: the run keeps
    its snapshotted `duration_s`/`base_s`, and the deficit stays outstanding
    until the NEXT quoted cycle.
    """
    sequencer, _, _, _ = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
        ledger={"settled_cycle_id": "2026-07-30-evening", "deficits": {"zone-2": 300}},
    )

    await sequencer.async_reconcile(aware(7, 4))

    run = sequencer.current_run
    assert run is not None
    assert [
        (zone.duration_s, zone.base_s, zone.carried_s) for zone in run.zone_runs
    ] == [
        (600, 600, 0),
        (900, 900, 0),
        (300, 300, 0),
    ]
    assert sequencer.ledger.deficit_s("zone-2") == 300


async def test_an_unconfirmed_re_open_reports_and_the_cycle_continues_fail_wet() -> (
    None
):
    """Matrix row 2 error column: VALVE_OPEN_UNCONFIRMED, slot consumed, no abort."""
    sequencer, switches, _, anomalies = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
    )
    switches.failing = {("on", VALVE_1)}
    clock = VirtualClock(aware(7, 4))

    await sequencer.async_reconcile(clock.now())

    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].status is ZoneRunStatus.FAILED
    assert AnomalyKind.VALVE_OPEN_UNCONFIRMED in other_reports(anomalies)
    assert sequencer.next_wakeup(clock.now()) == aware(7, 10)

    await run_to_idle(sequencer, clock)
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    # The failed open reads 0 s: the whole slot is water debt.
    assert sequencer.ledger.deficit_s("zone-1") == 600


async def test_an_elapsed_live_slot_is_closed_and_the_next_zone_opened() -> None:
    """Matrix row 3: zone 1 COMPLETED at now, zone 2 opened at once."""
    sequencer, switches, _, _ = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
    )

    await sequencer.async_reconcile(aware(7, 12))

    assert switches.commands == [
        ("off", VALVE_1),
        ("off", PUMP),
        ("on", PUMP),
        ("on", VALVE_2),
    ]
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].actual_end == aware(7, 12)
    assert run.zone_runs[1].actual_start == aware(7, 12)
    assert sequencer.next_wakeup(aware(7, 12)) == aware(7, 27)


async def test_a_live_zone_found_off_re_runs_in_full() -> None:
    """Matrix row 4: reset to PENDING, opened from now — bounded over-watering."""
    sequencer, switches, _, anomalies = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True},
    )

    await sequencer.async_reconcile(aware(7, 4))

    assert switches.commands == [("off", PUMP), ("on", PUMP), ("on", VALVE_1)]
    run = sequencer.current_run
    assert run is not None
    zone = run.zone_runs[0]
    assert (zone.status, zone.actual_start, zone.planned_end) == (
        ZoneRunStatus.RUNNING,
        aware(7, 4),
        aware(7, 14),
    )
    assert recovered(anomalies)[0]["outcome"] == "resumed"
    assert recovered(anomalies)[0]["zone_id"] == "zone-1"


async def test_a_crash_between_zones_resumes_at_the_next_zone() -> None:
    """Matrix row 5: pump likely ON → off, then pump on and zone 2 opened from now."""
    sequencer, switches, _, _ = await restart(
        "between",
        states={**ALL_OFF, PUMP: True},
    )

    await sequencer.async_reconcile(aware(7, 12))

    assert switches.commands == [("off", PUMP), ("on", PUMP), ("on", VALVE_2)]
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].status is ZoneRunStatus.COMPLETED
    assert run.zone_runs[1].actual_start == aware(7, 12)
    assert sequencer.next_wakeup(aware(7, 12)) == aware(7, 27)


async def test_a_pending_run_resumes_through_the_normal_start_path() -> None:
    """Matrix row 6: re-timed from now, started by `_start_cycle` in the same call."""
    sequencer, switches, journal, anomalies = await restart("pending", states=ALL_OFF)

    await sequencer.async_reconcile(aware(7, 3))

    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.scheduled_start == aware(7, 3)
    assert run.zone_runs[0].actual_start == aware(7, 3)
    assert sequencer.next_wakeup(aware(7, 3)) == aware(7, 13)
    assert recovered(anomalies) == [
        {
            "cycle_id": "2026-07-31-morning",
            "kind": "morning",
            "zone_id": None,
            "outcome": "resumed",
        },
    ]
    assert journal.snapshots[-1]["run"] == run.as_dict()


async def test_a_pending_run_under_season_off_is_closed_as_interrupted() -> None:
    """Matrix row 7: nothing commanded, INTERRUPTED, full (capped) deficits book."""
    sequencer, switches, journal, anomalies = await restart(
        "pending",
        states=ALL_OFF,
        season_enabled=False,
    )

    await sequencer.async_reconcile(aware(7, 3))

    assert switches.commands == []
    assert sequencer.current_run is None
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.INTERRUPTED
    assert run.recovery == "closed"
    assert sequencer.ledger.as_dict()["deficits"] == {
        "zone-1": 600,
        "zone-2": 900,
        "zone-3": 300,
    }
    assert recovered(anomalies) == [
        {
            "cycle_id": "2026-07-31-morning",
            "kind": "morning",
            "zone_id": None,
            "outcome": "closed",
        },
    ]
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert [(entry["status"], entry["recovery"]) for entry in history] == [
        ("interrupted", "closed"),
    ]


async def test_a_running_run_under_season_off_still_resumes() -> None:
    """Matrix row 8: a cycle in progress completes (Story 1.6 AC 1)."""
    sequencer, switches, _, _ = await restart(
        "zone-2",
        states={**ALL_OFF, PUMP: True, VALVE_2: True},
        season_enabled=False,
    )

    await sequencer.async_reconcile(aware(7, 12))

    assert switches.commands[-2:] == [("on", PUMP), ("on", VALVE_2)]
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING


async def test_a_later_day_restart_closes_the_run_through_the_completion_path() -> None:
    """Matrix row 9 / AC 3: INTERRUPTED, deficits booked once, history, anomalies.

    The live valve is ON, so zone 1 is credited its (long) span and owes
    nothing; zones 2 and 3 never ran and owe their whole quote, capped at
    `base_s`. `CYCLE_INTERRUPTED` is cleared by the completion,
    `CYCLE_RECOVERED{closed}` reported, and the pump is NOT commanded a
    second time — the orphan pass already turned it off.
    """
    sequencer, switches, journal, anomalies = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
    )
    now = aware(7, 4, day=1, month=8)

    await sequencer.async_reconcile(now)

    assert switches.commands == [("off", VALVE_1), ("off", PUMP)]
    assert sequencer.current_run is None
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.INTERRUPTED
    assert run.recovery == "closed"
    assert run.zone_runs[0].status is ZoneRunStatus.COMPLETED
    assert run.zone_runs[0].actual_end == now
    assert [zone.status for zone in run.zone_runs[1:]] == [
        ZoneRunStatus.PENDING,
        ZoneRunStatus.PENDING,
    ]
    assert sequencer.ledger.settled_cycle_id == "2026-07-31-morning"
    assert sequencer.ledger.as_dict()["deficits"] == {"zone-2": 900, "zone-3": 300}
    assert AnomalyKind.CYCLE_INTERRUPTED in cleared(anomalies)
    assert recovered(anomalies) == [
        {
            "cycle_id": "2026-07-31-morning",
            "kind": "morning",
            "zone_id": "zone-1",
            "outcome": "closed",
        },
    ]
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    assert history[-1]["cycle_id"] == "2026-07-31-morning"
    assert history[-1]["status"] == "interrupted"
    assert history[-1]["recovery"] == "closed"
    assert history[-1]["irrigation_day"] == "2026-07-31"
    # Idle again: only the watchdog's deadline on the next window end, which
    # on the recovery day (2026-08-01) is that morning's own 07:30 (Story 3.3).
    assert sequencer.next_wakeup(aware(7, 4, day=1, month=8)) == aware(
        7, 30, day=1, month=8
    )


async def test_a_later_day_restart_with_the_valve_off_books_the_live_zone_in_full() -> (
    None
):
    """Unproven watering on a later day: FAILED, 0 s, full deficit capped at base."""
    sequencer, _, _, _ = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True},
    )

    await sequencer.async_reconcile(aware(7, 4, day=1, month=8))

    run = sequencer.last_run
    assert run is not None
    assert run.zone_runs[0].status is ZoneRunStatus.FAILED
    assert sequencer.ledger.as_dict()["deficits"] == {
        "zone-1": 600,
        "zone-2": 900,
        "zone-3": 300,
    }


async def test_an_already_settled_close_books_nothing_twice() -> None:
    """Error column of row 9: the ledger refuses the id it already settled."""
    sequencer, _, _, _ = await restart(
        "zone-1",
        states=ALL_OFF,
        ledger={"settled_cycle_id": "2026-07-31-morning", "deficits": {"zone-3": 120}},
    )

    await sequencer.async_reconcile(aware(7, 4, day=1, month=8))

    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.INTERRUPTED
    assert sequencer.ledger.as_dict()["deficits"] == {"zone-3": 120}


async def test_every_slot_spent_completes_the_run_as_resumed() -> None:
    """Matrix row 3 tail: nothing left → COMPLETED, the pump is not re-commanded."""
    sequencer, switches, _, anomalies = await restart(
        "done",
        states={**ALL_OFF, PUMP: True},
    )

    await sequencer.async_reconcile(aware(7, 31))

    assert switches.commands == [("off", PUMP)]
    assert sequencer.current_run is None
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    assert run.recovery == "resumed"
    assert sequencer.ledger.as_dict()["deficits"] == {}
    assert recovered(anomalies)[0]["outcome"] == "resumed"


async def test_a_valve_renamed_before_the_restart_is_read_closed_and_re_opened_under_its_new_id() -> (  # noqa: E501 — the name is the spec
    None
):
    """The plan built at setup carries the rewritten id; the journaled run does not.

    Story 1.7's tracker rewrote the subentry mid-cycle and aliased the id in
    the (now dead) adapter. Without the remap the orphan pass would read and
    command `switch.zone_1_valve`, a dead id, while the real valve stays on.
    The pump is remapped the same way; a zone no longer in the plan keeps the
    only id anybody has.
    """
    run = run_of(await crash_snapshot("zone-1"))
    renamed = make_plan(
        make_zone("zone-1", valve="switch.front_valve", morning_s=600),
        make_zone("zone-2", valve=VALVE_2, morning_s=900),
        # zone-3 dropped from the plan: its journaled id is kept.
        pump="switch.new_pump",
    )
    sequencer, switches, journal, _ = make_sequencer(renamed, run=run)
    switches.states = {
        "switch.front_valve": True,
        "switch.new_pump": True,
        VALVE_2: False,
    }

    await sequencer.async_reconcile(aware(7, 4))

    assert switches.reads == ["switch.front_valve", VALVE_2, VALVE_3, "switch.new_pump"]
    assert switches.commands == [
        ("off", "switch.front_valve"),
        ("off", VALVE_3),  # no state at all: "not OFF", commanded off
        ("off", "switch.new_pump"),
        ("on", "switch.new_pump"),
        ("on", "switch.front_valve"),
    ]
    assert [zone.valve_entity_id for zone in run.zone_runs] == [
        "switch.front_valve",
        VALVE_2,
        VALVE_3,
    ]
    assert run.pump_entity_id == "switch.new_pump"
    journaled = journal.snapshots[-1]["run"]
    assert isinstance(journaled, dict)
    assert journaled["pump_entity_id"] == "switch.new_pump"
    zones = journaled["zones"]
    assert isinstance(zones, list)
    assert zones[0]["valve_entity_id"] == "switch.front_valve"


async def test_an_unreadable_run_closes_every_configured_switch_and_books_nothing() -> (
    None
):
    """Matrix row 10: ALL plan valves then the pump off, `discarded`, run dropped."""
    sequencer, switches, journal, anomalies = make_sequencer(
        run_unreadable={"cycle_id": "2026-07-31-morning", "kind": "morning", "x": 1},
        ledger={"settled_cycle_id": "2026-07-30-evening", "deficits": {"zone-2": 300}},
    )
    switches.states = {**ALL_OFF, PUMP: True, VALVE_2: True}
    assert sequencer.reconciled is False

    await sequencer.async_reconcile(aware(7, 4))

    assert switches.commands == [
        ("off", VALVE_1),
        ("off", VALVE_2),
        ("off", VALVE_3),
        ("off", PUMP),
    ]
    assert anomalies.reports == [
        (
            AnomalyKind.CYCLE_RECOVERED,
            {
                "cycle_id": "2026-07-31-morning",
                "kind": "morning",
                "zone_id": None,
                "outcome": "discarded",
            },
        ),
    ]
    # The interrupted cycle is superseded by its discard like by any recovery.
    assert (
        AnomalyKind.CYCLE_INTERRUPTED,
        {"cycle_id": "2026-07-31-morning"},
    ) in anomalies.clears
    assert sequencer.current_run is None
    assert sequencer.last_run is None
    assert sequencer.ledger.as_dict()["deficits"] == {"zone-2": 300}
    assert sequencer.ledger.settled_cycle_id == "2026-07-30-evening"
    assert journal.snapshots[-1]["run"] is None
    assert sequencer.reconciled is True


async def test_an_unreadable_run_without_a_cycle_id_reports_none() -> None:
    """The raw document is consulted for the id only when it is a string."""
    sequencer, switches, _, anomalies = make_sequencer(run_unreadable={"cycle_id": 7})
    switches.failing = {("off", VALVE_2)}

    await sequencer.async_reconcile(aware(7, 4))

    assert recovered(anomalies) == [
        {"cycle_id": None, "kind": None, "zone_id": None, "outcome": "discarded"},
    ]
    # Unconfirmed closes in the discard pass report as usual, keyed by zone.
    assert (
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        {"cycle_id": None, "kind": None, "zone_id": "zone-2", "entity_id": VALVE_2},
    ) in anomalies.reports


async def test_an_idle_restart_with_fresh_history_does_nothing_at_all() -> None:
    """Matrix row 1 / AC 5: no command, no anomaly, no clear, no journal write.

    `watchdog_since` is seeded because the journal of a controller that has
    run before carries one (Story 3.3): the FIRST reconcile that finds it
    unset stamps it and saves, which is a different matrix row.

    The queue is EMPTY here on purpose: an idle restart commands nothing
    unless a deferral is waiting (Story 3.4, Decision 5 — the test below).
    """
    sequencer, switches, journal, anomalies = make_sequencer(
        history=[{"irrigation_day": "2026-07-30", "cycle_id": "2026-07-30-evening"}],
        watchdog_since=aware(6),
    )
    assert sequencer.reconciled is True

    await sequencer.async_reconcile(aware(7, 4))

    assert switches.commands == []
    assert switches.reads == []
    assert anomalies.reports == []
    assert anomalies.clears == []
    assert journal.snapshots == []
    assert sequencer.current_run is None


async def test_an_idle_restart_drains_a_same_day_deferral() -> None:
    """Story 3.4, Decision 5: a restored deferral waters instead of stranding.

    This amends Story 3.2's "an idle restart commands nothing". The run the
    entry was queued behind, and the manual pause that may have deferred it,
    both died with the process — nothing is ever going to pop it, so the
    reconcile does, right before its `advance`, and the cycle starts in the
    same call. Fail-wet (AD-4).
    """
    sequencer, switches, _, anomalies = make_sequencer(
        deferred=[(CycleKind.EVENING, aware(6, 59))],
        watchdog_since=aware(6),
    )

    await sequencer.async_reconcile(aware(7, 4))

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.EVENING
    assert run.status is CycleStatus.RUNNING
    assert sequencer.deferred_kinds == ()
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]
    # A deferral is ordinary scheduled work, not a failure: nothing reported.
    assert anomalies.reports == []


async def test_an_idle_restart_prunes_stale_history_and_saves_once() -> None:
    """History older than 7 days from `now` is dropped at load — one write, no more."""
    sequencer, switches, journal, anomalies = make_sequencer(
        history=[
            {"irrigation_day": "2026-07-23", "cycle_id": "old"},
            {"irrigation_day": "2026-07-25", "cycle_id": "kept"},
        ],
    )

    await sequencer.async_reconcile(aware(7, 4))

    assert switches.commands == []
    assert anomalies.reports == []
    assert len(journal.snapshots) == 1
    assert journal.snapshots[0]["history"] == [
        {"irrigation_day": "2026-07-25", "cycle_id": "kept"},
    ]


async def test_a_stale_deferred_entry_is_dropped_silently() -> None:
    """Matrix row 11: a deferral on another irrigation day belongs to the watchdog."""
    sequencer, _, journal, anomalies = make_sequencer(
        deferred=[
            (CycleKind.EVENING, aware(19, 59, day=30)),
            (CycleKind.MORNING, aware(6, 59)),
        ],
    )

    await sequencer.async_reconcile(aware(7, 4))

    # The stale entry is gone and the same-day one is drained on the spot
    # (Story 3.4, Decision 5) — so the queue ends empty and the morning
    # cycle, not yesterday's evening, is what waters.
    assert sequencer.deferred_kinds == ()
    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.MORNING
    assert anomalies.reports == []
    assert journal.snapshots[-1]["deferred"] == []


async def test_the_watchdog_is_asked_before_the_drain_installs_anything() -> None:
    """Story 3.4, Decision 5: the drain must not make the day's other kind look live.

    A restart at 21:00 carrying this morning's deferral. Both windows have
    closed: the evening one is a genuine miss the watchdog can still make up.
    A cycle popped first would be `self._run` when `missed_cycles` looked, so
    the evening would be filed `recorded` instead of re-run — burning that
    `(day, kind)` for ever. The watchdog is asked first, takes the machine
    for its re-run, and the deferral waits for that run's completion like
    every deferral.
    """
    sequencer, switches, _, anomalies = make_sequencer(
        deferred=[(CycleKind.MORNING, aware(7))],
        watchdog_since=aware(6),
    )

    await sequencer.async_reconcile(aware(21))

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.EVENING
    assert run.late_rerun is True
    assert run.status is CycleStatus.RUNNING
    assert [
        (kind, context["outcome"])
        for kind, context in anomalies.reports
        if kind is AnomalyKind.MISSED_CYCLE
    ] == [(AnomalyKind.MISSED_CYCLE, "rerun")]
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]

    # ...and the deferred morning reaches the hardware behind it.
    clock = VirtualClock(aware(21))
    while (active := sequencer.current_run) is not None:
        moment = sequencer.next_wakeup(clock.now())
        assert moment is not None
        clock.advance_to(moment)
        await sequencer.advance(clock.now())
        if active.kind is CycleKind.MORNING:
            break
    morning = sequencer.current_run
    assert morning is not None
    assert morning.kind is CycleKind.MORNING
    assert not sequencer.deferred_kinds


async def test_a_same_day_deferral_is_popped_once_the_restored_run_closes() -> None:
    """The completion path pops the kept queue and the `advance` loop starts it."""
    sequencer, switches, _, _ = await restart(
        "zone-1",
        states=ALL_OFF,
        deferred=[(CycleKind.MORNING, aware(7, day=1, month=8))],
    )

    await sequencer.async_reconcile(aware(7, 4, day=1, month=8))

    closed = sequencer.last_run
    assert closed is not None
    assert closed.status is CycleStatus.INTERRUPTED
    run = sequencer.current_run
    assert run is not None
    assert run.cycle_id == "2026-08-01-morning"
    assert run.status is CycleStatus.RUNNING
    assert run.recovery is None
    assert switches.commands == [("on", PUMP), ("on", VALVE_1)]


async def test_a_terminal_journaled_run_is_the_last_run_not_a_recovery() -> None:
    """The completion write files the run under `run`: it is not in flight."""
    source, _, journal, _ = make_sequencer()
    clock = VirtualClock(aware(7))
    await source.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(source, clock)
    completed = journal.snapshots[-1]["run"]
    assert isinstance(completed, dict)
    assert completed["status"] == "completed"
    # The same snapshot's history, as the journal adapter really seeds it: the
    # completion wrote the record and the run in ONE save, so the restored
    # machine knows that morning ran and Story 3.3's watchdog sees no miss.
    stored_history = journal.snapshots[-1]["history"]
    assert isinstance(stored_history, list)
    sequencer, switches, restored_journal, anomalies = make_sequencer(
        run=CycleRun.from_dict(completed, tz=TZ),
        last_run=run_of(await crash_snapshot("done")),
        history=stored_history,
        watchdog_since=aware(6),
    )
    switches.states = dict(ALL_OFF)

    assert sequencer.reconciled is True
    assert sequencer.current_run is None
    last = sequencer.last_run
    assert last is not None
    assert last.status is CycleStatus.COMPLETED

    await sequencer.async_reconcile(aware(9))

    assert switches.commands == []
    assert anomalies.reports == []
    assert restored_journal.snapshots == []


async def test_a_restored_last_run_survives_and_is_journaled_back() -> None:
    """`last_run` is what the zone sensors read; it must not vanish on reload."""
    last_run = run_of(await crash_snapshot("done"))
    sequencer, _, journal, _ = make_sequencer(last_run=last_run)
    clock = VirtualClock(aware(9))

    assert sequencer.last_run is last_run
    await sequencer.async_reconcile(clock.now())
    await sequencer.async_set_season(enabled=False, now=clock.now())

    assert journal.snapshots[-1]["last_run"] == last_run.as_dict()


# --------------------------------------------------------------------------
# The `_reconciled` gate
# --------------------------------------------------------------------------


async def test_nothing_acts_on_a_restored_run_before_reconciliation() -> None:
    """`advance` is a no-op, `request_cycle` defers, run-now and cancel refuse."""
    sequencer, switches, journal, anomalies = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
    )
    assert sequencer.reconciled is False
    now = aware(7, 12)  # zone 1's boundary is long past
    # No time intent either: nothing for the adapter to arm before the resync —
    # not even the watchdog's deadline, which is gated on the same flag.
    assert sequencer.next_wakeup(now) is None

    await sequencer.advance(now)
    assert switches.commands == []
    assert sequencer.current_run is not None
    assert sequencer.current_run.zone_runs[0].actual_end is None

    assert await sequencer.async_run_now(CycleKind.EVENING, now) is False
    assert await sequencer.async_cancel_cycle(now) is False
    assert switches.commands == []
    assert anomalies.reports == []

    await sequencer.request_cycle(CycleKind.EVENING, now)
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)
    assert journal.snapshots[-1]["deferred"] == [
        {"kind": "evening", "reference": "2026-07-31T05:12:00+00:00"},
    ]

    await sequencer.async_reconcile(now)

    assert sequencer.reconciled is True
    # The resume caught up: zone 1 closed at now, zone 2 opened, the
    # deferred evening still queued behind the resumed run.
    assert switches.commands[-1] == ("on", VALVE_2)
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)


async def test_a_request_before_reconciliation_is_deferred_not_lost() -> None:
    """The gate defers with no readable run too, while an unreadable one awaits."""
    sequencer, switches, _, _ = make_sequencer(run_unreadable={})
    switches.states = dict(ALL_OFF)
    now = aware(7)

    await sequencer.request_cycle(CycleKind.MORNING, now)
    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == (CycleKind.MORNING,)

    await sequencer.async_reconcile(now)

    # The discard pass (every valve, the pump), and then the queue itself:
    # since Story 3.4's Decision 5 the reconcile drains what it finds when it
    # ends idle, so the request made while the gate was closed is delayed by
    # the length of the boot and no longer stranded behind a completion that
    # was never coming.
    assert switches.commands == [
        ("off", VALVE_1),
        ("off", VALVE_2),
        ("off", VALVE_3),
        ("off", PUMP),
        ("on", PUMP),
        ("on", VALVE_1),
    ]
    assert not sequencer.deferred_kinds
    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.MORNING
    assert run.status is CycleStatus.RUNNING


async def test_the_gate_lifts_even_when_a_port_raises_mid_reconcile() -> None:
    """A leaked exception must not freeze the machine forever."""
    sequencer, switches, _, _ = await restart(
        "zone-1",
        states={**ALL_OFF, PUMP: True, VALVE_1: True},
    )
    switches.raising = {("off", VALVE_1), ("on", PUMP)}

    await sequencer.async_reconcile(aware(7, 4))

    assert sequencer.reconciled is True
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.pump_on_confirmed is False
