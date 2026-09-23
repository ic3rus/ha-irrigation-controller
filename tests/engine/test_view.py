"""The engine state view on a virtual clock (Story 4.1, AD-14).

Hass-free like the rest of `tests/engine/`: the view is composed from a real
`Sequencer` driven the way the runner drives it, and from hand-made stored
records for the history matrix. What is proven here is the SCHEMA — the
sections, the wire conventions, the outcome stamping and its collapse rule,
the legacy defaults, the read-path prune — where it lives, so the WebSocket
suite only has to prove delivery.
"""

from __future__ import annotations

import json
from datetime import UTC, date
from types import NoneType
from typing import TYPE_CHECKING

import pytest

from custom_components.ha_irrigation_controller.engine.history import history_entry
from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleRun,
    CycleStatus,
    ZoneRun,
    ZoneRunStatus,
)
from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer
from custom_components.ha_irrigation_controller.engine.view import (
    STATE_SCHEMA_VERSION,
    Outcome,
    build_view,
    history_rows,
    outcome_of,
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

if TYPE_CHECKING:
    from custom_components.ha_irrigation_controller.engine.plan import ControllerPlan
    from custom_components.ha_irrigation_controller.engine.view import (
        HealthView,
        StateView,
    )

PUMP = "switch.pool_pump"
VALVE_1 = "switch.zone_1_valve"
VALVE_2 = "switch.zone_2_valve"

TODAY = date(2026, 7, 31)
NO_HEALTH: HealthView = {"open": [], "last": None}


def two_zone_plan(*, morning_enabled: bool = True) -> ControllerPlan:
    """Build a two-zone plan: ten morning minutes each, fifteen in the evening."""
    return make_plan(
        make_zone("zone-1", valve=VALVE_1, morning_s=600, evening_s=900),
        make_zone(
            "zone-2", name="Back Lawn", valve=VALVE_2, morning_s=600, evening_s=900
        ),
        morning_enabled=morning_enabled,
    )


def make_sequencer(
    plan: ControllerPlan,
    *,
    history: list[dict[str, object]] | None = None,
    ledger: dict[str, object] | None = None,
    run: CycleRun | None = None,
) -> Sequencer:
    """Wire a sequencer to fresh recording fakes, optionally seeded."""
    return Sequencer(
        plan,
        switches=FakeSwitchPort(),
        journal=FakeJournalPort(),
        anomalies=FakeAnomalyPort(),
        history=history,
        ledger=ledger,
        run=run,
    )


def view_of(
    sequencer: Sequencer,
    clock: VirtualClock,
    *,
    health: HealthView = NO_HEALTH,
    config_change_pending: bool = False,
) -> StateView:
    """Compose the document as the HA composer would, on the virtual clock."""
    return build_view(
        sequencer,
        clock.now(),
        entry_id="entry-1",
        version="0.1.0",
        health=health,
        config_change_pending=config_change_pending,
    )


async def run_to_idle(sequencer: Sequencer, clock: VirtualClock) -> None:
    """Drive the ACTIVE cycle with the exact wake-up loop the runner uses."""
    while sequencer.current_run is not None:
        moment = sequencer.next_wakeup(clock.now())
        if moment is None:
            break
        clock.advance_to(moment)
        await sequencer.advance(clock.now())


def zone_record(  # noqa: PLR0913 — one keyword per record field is the readable shape
    zone_id: str = "zone-1",
    *,
    status: str = "completed",
    planned_s: int = 600,
    carried_s: int = 0,
    rain_credit_s: int = 0,
    effective_s: int = 600,
) -> dict[str, object]:
    """Build one zone record as `history_entry` writes it."""
    return {
        "zone_id": zone_id,
        "status": status,
        "planned_s": planned_s,
        "carried_s": carried_s,
        "rain_credit_s": rain_credit_s,
        "effective_s": effective_s,
    }


def record(
    *,
    day: object = "2026-07-31",
    kind: object = "morning",
    status: object = "completed",
    cycle_id: object = "2026-07-31-morning",
    zones: object = None,
    **overrides: object,
) -> dict[str, object]:
    """Build one stored history record in today's full shape, then override.

    Every field is typed `object` on purpose: the malformed-record cases feed
    the reader exactly the garbage a hand-edited journal could hold.
    """
    return {
        "cycle_id": cycle_id,
        "irrigation_day": day,
        "kind": kind,
        "status": status,
        "manual": False,
        "waived_by": None,
        "recovery": None,
        "late_rerun": False,
        "configured_start": "2026-07-31T05:00:00+00:00",
        "scheduled_start": "2026-07-31T05:00:00+00:00",
        "ended_at": "2026-07-31T05:20:00+00:00",
        "rain_total_mm": 12.0,
        "zones": [zone_record()] if zones is None else zones,
        **overrides,
    }


def utc(hour: int, minute: int = 0, *, day: int = 31) -> str:
    """Return the UTC ISO string of `aware(hour, minute)` — the wire form."""
    return aware(hour, minute, day=day).astimezone(UTC).isoformat()


# --------------------------------------------------------------------------
# Documents from a real sequencer
# --------------------------------------------------------------------------


async def test_the_idle_document_after_a_full_cycle() -> None:
    """Matrix "Fetch, idle": every section, the last run, today's plan, one row."""
    sequencer = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)
    clock.advance_to(aware(7, 30))

    view = view_of(sequencer, clock)

    assert view["schema_version"] == STATE_SCHEMA_VERSION == 1
    assert view["version"] == "0.1.0"
    assert view["generated_at"] == utc(7, 30)
    assert view["controller"] == {
        "entry_id": "entry-1",
        "season_enabled": True,
        "reconciled": True,
        "manual_override": False,
        "config_change_pending": False,
        "deferred": [],
        # Idle: the watchdog's next deadline, the evening window end.
        "next_wakeup": utc(20, 30),
    }
    assert view["runs"]["current"] is None
    last = view["runs"]["last"]
    assert last is not None
    assert last["cycle_id"] == "2026-07-31-morning"
    assert last["status"] == "completed"
    assert last["live_zone_id"] is None
    assert [zone["effective_s"] for zone in last["zones"]] == [600, 600]
    assert view["plan"]["today"]["irrigation_day"] == "2026-07-31"
    assert [cycle["kind"] for cycle in view["plan"]["today"]["cycles"]] == [
        "morning",
        "evening",
    ]
    assert view["ledger"]["settled_cycle_id"] == "2026-07-31-morning"
    assert view["ledger"]["zones"] == {
        "zone-1": {"deficit_s": 0, "rain_baseline_mm": None},
        "zone-2": {"deficit_s": 0, "rain_baseline_mm": None},
    }
    assert [(row["kind"], row["outcome"], row["runs"]) for row in view["history"]] == [
        ("morning", "ran", 1),
    ]
    assert view["health"] == {"open": [], "last": None}


async def test_the_mid_cycle_document_names_the_live_zone() -> None:
    """Matrix "Push mid-cycle": `live_zone_id`, its status and its start instant."""
    sequencer = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())

    view = view_of(sequencer, clock)

    current = view["runs"]["current"]
    assert current is not None
    assert current["status"] == "running"
    assert current["live_zone_id"] == "zone-1"
    first, second = current["zones"]
    assert first["status"] == "running"
    assert first["actual_start"] == utc(7)
    assert first["actual_end"] is None
    assert first["effective_s"] == 0
    assert first["planned_start"] == utc(7)
    assert first["planned_end"] == utc(7, 10)
    assert second["status"] == "pending"
    assert second["name"] == "Back Lawn"
    assert view["controller"]["next_wakeup"] == utc(7, 10)
    assert view["history"] == []


async def test_a_cancel_is_visible_in_the_last_run_and_in_history() -> None:
    """Matrix "Cancel": the parked "cancelled is unobservable" item, closed."""
    sequencer = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 5))
    assert await sequencer.async_cancel_cycle(clock.now())

    view = view_of(sequencer, clock)

    assert view["runs"]["current"] is None
    last = view["runs"]["last"]
    assert last is not None
    assert last["status"] == "cancelled"
    assert last["live_zone_id"] is None
    reached, unreached = last["zones"]
    assert reached["effective_s"] == 300
    assert unreached["effective_s"] == 0
    assert unreached["actual_end"] is None
    (row,) = view["history"]
    assert row["irrigation_day"] == "2026-07-31"
    assert row["kind"] == "morning"
    assert row["outcome"] == "cancelled"
    assert row["status"] == "cancelled"
    assert row["cycle_id"] == "2026-07-31-morning"
    assert row["ended_at"] == utc(7, 5)
    assert row["planned_s"] == 1200
    assert row["effective_s"] == 300


async def test_a_deferred_kind_and_the_plan_sections_are_projected() -> None:
    """`controller.deferred` names the queued kind; `plan` is the plan verbatim."""
    sequencer = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())

    view = view_of(sequencer, clock)

    assert view["controller"]["deferred"] == ["evening"]
    plan = view["plan"]
    assert plan["pump_entity_id"] == PUMP
    assert plan["morning_enabled"] is True
    assert plan["morning_start"] == "07:00:00"
    assert plan["evening_start"] == "20:00:00"
    assert plan["manual_timeout_s"] == 1800
    assert plan["zones"][0] == {
        "zone_id": "zone-1",
        "name": "Front Lawn",
        "valve_entity_id": VALVE_1,
        "morning_duration_s": 600,
        "evening_duration_s": 900,
        "rain_exposed": True,
        "rain_factor": 1.0,
    }
    morning, evening = plan["today"]["cycles"]
    assert morning == {
        "kind": "morning",
        "start": utc(7),
        "end": utc(7, 20),
        "zones": [
            {"zone_id": "zone-1", "start": utc(7), "end": utc(7, 10)},
            {"zone_id": "zone-2", "start": utc(7, 10), "end": utc(7, 20)},
        ],
    }
    assert evening["start"] == utc(20)
    assert evening["end"] == utc(20, 30)


def test_plan_today_lists_only_the_evening_when_the_morning_is_disabled() -> None:
    """A disabled morning cycle is not drawn — `today` follows the plan."""
    sequencer = make_sequencer(two_zone_plan(morning_enabled=False))
    clock = VirtualClock(aware(12))

    view = view_of(sequencer, clock)

    assert view["plan"]["morning_enabled"] is False
    assert [cycle["kind"] for cycle in view["plan"]["today"]["cycles"]] == ["evening"]


def test_next_wakeup_is_null_and_reconciled_false_before_reconcile() -> None:
    """A restored in-flight run gates the engine; the view says so, no guess."""
    restored = CycleRun(
        cycle_id="2026-07-31-morning",
        kind=CycleKind.MORNING,
        configured_start=aware(7),
        scheduled_start=aware(7),
        pump_entity_id=PUMP,
        zone_runs=(
            ZoneRun(
                zone_id="zone-1",
                name="Front Lawn",
                valve_entity_id=VALVE_1,
                duration_s=600,
                base_s=600,
                planned_start=aware(7),
                planned_end=aware(7, 10),
            ),
        ),
    )
    sequencer = make_sequencer(two_zone_plan(), run=restored)
    clock = VirtualClock(aware(7, 3))

    view = view_of(sequencer, clock)

    assert view["controller"]["reconciled"] is False
    assert view["controller"]["next_wakeup"] is None
    current = view["runs"]["current"]
    assert current is not None
    assert current["status"] == "pending"
    assert current["live_zone_id"] is None


def test_the_ledger_summary_reads_the_ledger_per_plan_zone() -> None:
    """Deficits, baselines, the credit and the source — reads, never `as_dict`."""
    sequencer = make_sequencer(
        two_zone_plan(),
        ledger={
            "settled_cycle_id": "2026-07-30-evening",
            "deficits": {"zone-1": 300, "zone-gone": 120},
            "day_credit": {"irrigation_day": "2026-07-31", "cycle_id": "run-now"},
            "rain_baselines": {"zone-2": 12.5},
            "rain_source": "sensor.rain_gauge",
        },
    )

    ledger = view_of(sequencer, VirtualClock(aware(12)))["ledger"]

    assert ledger == {
        "settled_cycle_id": "2026-07-30-evening",
        "day_credit": "2026-07-31",
        "rain_source": "sensor.rain_gauge",
        # Keyed by the PLAN's zones: a zone no longer configured has no row.
        "zones": {
            "zone-1": {"deficit_s": 300, "rain_baseline_mm": None},
            "zone-2": {"deficit_s": 0, "rain_baseline_mm": 12.5},
        },
    }


def test_health_and_the_reload_flag_enter_as_plain_data() -> None:
    """The two facts the engine cannot know ride through verbatim."""
    health: HealthView = {
        "open": [{"anomaly": "valve_open_unconfirmed", "zone_id": "zone-1"}],
        "last": {
            "anomaly": "valve_open_unconfirmed",
            "cycle_id": "2026-07-31-morning",
            "zone_id": "zone-1",
            "entity_id": VALVE_1,
        },
    }
    sequencer = make_sequencer(two_zone_plan())

    view = view_of(
        sequencer,
        VirtualClock(aware(12)),
        health=health,
        config_change_pending=True,
    )

    assert view["health"] == health
    assert view["controller"]["config_change_pending"] is True


async def test_the_document_is_json_primitives_only_and_round_trips() -> None:
    """No `datetime`, `date`, `Enum` or dataclass leaf anywhere; the dump proves it."""
    sequencer = make_sequencer(
        two_zone_plan(),
        history=[
            record(day="2026-07-30", status="missed", cycle_id="2026-07-30-morning")
        ],
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())

    view = view_of(sequencer, clock)

    def leaves(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert type(key) is str, key
                leaves(value)
        elif isinstance(node, list):
            for item in node:
                leaves(item)
        else:
            # `type(...) is`, not `isinstance`: a StrEnum IS a str and would
            # slip through `isinstance` — and it is exactly the leaf forbidden.
            assert type(node) in (str, int, float, bool, NoneType), node

    leaves(view)
    assert json.loads(json.dumps(view)) == view


# --------------------------------------------------------------------------
# Outcomes: the per-record stamp
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, Outcome.RAN),
        ({"status": "missed"}, Outcome.MISSED),
        ({"status": "cancelled"}, Outcome.CANCELLED),
        ({"status": "waived", "waived_by": "2026-07-31-morning"}, Outcome.WAIVED),
        ({"status": "interrupted"}, Outcome.RECOVERED),
        ({"recovery": "resumed"}, Outcome.RECOVERED),
        ({"recovery": "closed", "status": "interrupted"}, Outcome.RECOVERED),
        ({"late_rerun": True}, Outcome.RECOVERED),
        ({"zones": [zone_record(rain_credit_s=120, effective_s=480)]}, Outcome.REDUCED),
        (
            {"zones": [zone_record(status="skipped", planned_s=0, effective_s=0)]},
            Outcome.REDUCED,
        ),
        # A reduced cycle the reconciler resumed reads recovered (precedence).
        (
            {"recovery": "resumed", "zones": [zone_record(rain_credit_s=120)]},
            Outcome.RECOVERED,
        ),
        # A cancelled cycle that had a rain credit is still a cancel.
        (
            {"status": "cancelled", "zones": [zone_record(rain_credit_s=60)]},
            Outcome.CANCELLED,
        ),
        # The legacy defaults: no markers at all reads as a plain run.
        ({"recovery": None, "late_rerun": False, "waived_by": None}, Outcome.RAN),
    ],
)
def test_outcome_of_stamps_the_six_values_in_precedence_order(
    overrides: dict[str, object],
    expected: Outcome,
) -> None:
    """One record, one outcome — the rule of Decision 1, per record."""
    assert outcome_of(record(**overrides)) is expected


@pytest.mark.parametrize("status", ["bogus", 7, None])
def test_outcome_of_an_unknown_status_is_none(status: object) -> None:
    """Not a `CycleStatus` value → None, never an exception."""
    assert outcome_of(record(status=status)) is None


def test_outcome_of_reads_markers_conservatively() -> None:
    """Only a real `True` is a late re-run and only a `str` is a recovery marker."""
    assert outcome_of(record(late_rerun="yes")) is Outcome.RAN
    assert outcome_of(record(late_rerun=1)) is Outcome.RAN
    assert outcome_of(record(recovery=7)) is Outcome.RAN


# --------------------------------------------------------------------------
# History rows: collapse, defaults, drops, prune, order
# --------------------------------------------------------------------------


def test_a_missed_marker_and_its_late_rerun_collapse_to_recovered() -> None:
    """Matrix "Missed then made up": ONE row, recovered, two runs, the make-up's id."""
    rows = history_rows(
        [
            record(
                status="missed",
                zones=[zone_record(status="pending", effective_s=0)],
            ),
            record(
                cycle_id="2026-07-31-morning-2",
                late_rerun=True,
                ended_at="2026-07-31T07:45:00+00:00",
            ),
        ],
        TODAY,
    )

    (row,) = rows
    assert row["outcome"] == "recovered"
    assert row["runs"] == 2
    assert row["cycle_id"] == "2026-07-31-morning-2"
    assert row["status"] == "completed"
    assert row["late_rerun"] is True
    assert row["ended_at"] == "2026-07-31T07:45:00+00:00"
    assert row["effective_s"] == 600


def test_a_run_now_and_the_waived_scheduled_cycle_collapse_to_waived() -> None:
    """Matrix "Waived by run-now": the waiver wins and names the run-now."""
    rows = history_rows(
        [
            record(manual=True, ended_at="2026-07-31T03:20:00+00:00"),
            record(
                cycle_id="2026-07-31-morning-2",
                status="waived",
                waived_by="2026-07-31-morning",
                zones=[zone_record(status="pending", effective_s=0)],
            ),
        ],
        TODAY,
    )

    (row,) = rows
    assert row["outcome"] == "waived"
    assert row["waived_by"] == "2026-07-31-morning"
    assert row["cycle_id"] == "2026-07-31-morning-2"
    assert row["manual"] is False
    assert row["runs"] == 2
    assert row["effective_s"] == 0


def test_a_reduced_and_resumed_cycle_reads_recovered_with_its_credit() -> None:
    """Matrix "Reduced and resumed": precedence picks recovered, the credit stays."""
    (row,) = history_rows(
        [
            record(
                recovery="resumed",
                zones=[
                    zone_record(rain_credit_s=120, planned_s=480, effective_s=480),
                    zone_record("zone-2", carried_s=60, planned_s=660, effective_s=660),
                ],
            ),
        ],
        TODAY,
    )

    assert row["outcome"] == "recovered"
    assert row["recovery"] == "resumed"
    assert row["rain_credit_s"] == 120
    assert row["carried_s"] == 60
    assert row["planned_s"] == 1140
    assert row["effective_s"] == 1140
    assert row["rain_total_mm"] == 12.0


def test_a_later_manual_run_never_buries_a_missed_cycle() -> None:
    """Why precedence, not "latest": a run-now after a miss that was never made up."""
    (row,) = history_rows(
        [
            record(
                status="missed", zones=[zone_record(status="pending", effective_s=0)]
            ),
            record(cycle_id="2026-07-31-morning-2", manual=True),
        ],
        TODAY,
    )

    assert row["outcome"] == "missed"
    assert row["cycle_id"] == "2026-07-31-morning"
    assert row["runs"] == 2


def test_a_later_run_now_never_buries_a_cancel() -> None:
    """Decision 1: `cancelled` sits above `ran` and `reduced`, below the anomalies."""
    (row,) = history_rows(
        [
            record(status="cancelled", zones=[zone_record(effective_s=300)]),
            record(
                cycle_id="2026-07-31-morning-2",
                manual=True,
                zones=[zone_record(rain_credit_s=60, effective_s=540)],
            ),
        ],
        TODAY,
    )

    assert row["outcome"] == "cancelled"
    assert row["cycle_id"] == "2026-07-31-morning"


def test_two_completed_runs_on_one_day_show_the_later_one() -> None:
    """Equal precedence: the LATER record supplies the row (two run-nows)."""
    (row,) = history_rows(
        [
            record(manual=True),
            record(cycle_id="2026-07-31-morning-2", manual=True),
        ],
        TODAY,
    )

    assert row["outcome"] == "ran"
    assert row["cycle_id"] == "2026-07-31-morning-2"
    assert row["manual"] is True
    assert row["runs"] == 2


def test_the_two_kinds_of_one_day_are_two_rows() -> None:
    """Rows are keyed by (irrigation day, kind), never by day alone."""
    rows = history_rows(
        [
            record(),
            record(kind="evening", cycle_id="2026-07-31-evening", status="cancelled"),
        ],
        TODAY,
    )

    assert [(row["kind"], row["outcome"]) for row in rows] == [
        ("morning", "ran"),
        ("evening", "cancelled"),
    ]


def test_a_legacy_record_is_read_with_the_defaults() -> None:
    """Matrix "Legacy record": the pre-2.2 shape builds a row and nothing raises."""
    legacy: dict[str, object] = {
        "cycle_id": "2026-07-31-morning",
        "irrigation_day": "2026-07-31",
        "kind": "morning",
        "status": "completed",
        "zones": [{"zone_id": "zone-1", "status": "completed", "effective_s": 600}],
    }

    (row,) = history_rows([legacy], TODAY)

    assert row == {
        "irrigation_day": "2026-07-31",
        "kind": "morning",
        "outcome": "ran",
        "cycle_id": "2026-07-31-morning",
        "status": "completed",
        "manual": False,
        "waived_by": None,
        "recovery": None,
        "late_rerun": False,
        "runs": 1,
        "ended_at": None,
        "rain_total_mm": None,
        "planned_s": 0,
        "carried_s": 0,
        "effective_s": 600,
        "rain_credit_s": 0,
    }


def test_garbage_in_a_record_reads_as_the_defaults_and_never_raises() -> None:
    """Every optional field is read by type: wrong types are the absent value."""
    (row,) = history_rows(
        [
            record(
                manual="yes",
                waived_by=7,
                recovery=["resumed"],
                late_rerun="true",
                ended_at=12,
                rain_total_mm=True,
                zones=[
                    "not a zone",
                    {"zone_id": "zone-1", "planned_s": True, "effective_s": "600"},
                ],
            ),
            record(kind="evening", cycle_id="2026-07-31-evening", zones="nope"),
        ],
        TODAY,
    )[:1]

    assert row["manual"] is False
    assert row["waived_by"] is None
    assert row["recovery"] is None
    assert row["late_rerun"] is False
    assert row["ended_at"] is None
    assert row["rain_total_mm"] is None
    assert row["planned_s"] == 0
    assert row["effective_s"] == 0


@pytest.mark.parametrize(
    "malformed",
    [
        record(status="bogus"),
        record(kind=7),
        record(kind="afternoon"),
        record(cycle_id=None),
    ],
)
def test_a_malformed_record_is_dropped_and_the_rest_of_the_view_is_intact(
    malformed: dict[str, object],
) -> None:
    """Matrix "Malformed record": omitted silently, the neighbours untouched."""
    rows = history_rows(
        [
            record(day="2026-07-30", cycle_id="2026-07-30-morning"),
            malformed,
            record(kind="evening", cycle_id="2026-07-31-evening"),
        ],
        TODAY,
    )

    assert [(row["irrigation_day"], row["kind"]) for row in rows] == [
        ("2026-07-30", "morning"),
        ("2026-07-31", "evening"),
    ]


def test_stale_records_are_pruned_on_read_and_the_stored_list_is_untouched() -> None:
    """Matrix "Stale rows": a 9-day-old record leaves the view, stays in storage."""
    stale = record(day="2026-07-22", cycle_id="2026-07-22-morning")
    fresh = record(day="2026-07-25", cycle_id="2026-07-25-morning")
    sequencer = make_sequencer(two_zone_plan(), history=[stale, fresh])

    view = view_of(sequencer, VirtualClock(aware(12)))

    assert [row["irrigation_day"] for row in view["history"]] == ["2026-07-25"]
    assert sequencer.history == (stale, fresh)


def test_rows_come_oldest_day_first_whatever_the_filing_order() -> None:
    """A miss for yesterday filed after today's morning still sorts before it."""
    rows = history_rows(
        [
            record(),
            record(
                day="2026-07-30",
                status="missed",
                cycle_id="2026-07-30-evening",
                kind="evening",
            ),
        ],
        TODAY,
    )

    assert [(row["irrigation_day"], row["outcome"]) for row in rows] == [
        ("2026-07-30", "missed"),
        ("2026-07-31", "ran"),
    ]


async def test_rows_built_from_records_the_engine_itself_filed() -> None:
    """The reader and `history_entry` agree: a real cycle's record stamps `ran`."""
    sequencer = make_sequencer(two_zone_plan())
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await run_to_idle(sequencer, clock)
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    assert all(zone.status is ZoneRunStatus.COMPLETED for zone in run.zone_runs)

    (row,) = history_rows([history_entry(run, clock.now())], TODAY)

    assert row["outcome"] == "ran"
    assert row["planned_s"] == 1200
    assert row["effective_s"] == 1200
    assert row["ended_at"] == utc(7, 20)
    assert row["rain_total_mm"] is None
