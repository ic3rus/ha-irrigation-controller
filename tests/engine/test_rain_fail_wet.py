"""Rain fail-wet and late-arriving data, on a virtual clock (Story 2.5, FR15/FR16).

Story 2.4 pinned the credit rule; this suite is where the rest of the input
matrix is measured against the MACHINE: a gauge swapped, renamed or reset, a
reading that arrives late or jumps mid-cycle, a gauge stuck or doubtful for
weeks, a pre-2.5 journal, a restart, and the season switch that forgets the
rain. Every row resolves toward watering — full durations, a DEBUG log at
most, `anomalies.reports == []` throughout — and history says why with
`rain_total_mm`. Driven the way every other engine suite drives the machine:
`while (t := engine.next_wakeup()) is not None`. No sleeps, no wall clock,
no Home Assistant (AD-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleStatus,
    ZoneRunStatus,
    effective_seconds,
)
from tests.engine.common import (
    FakeRainPort,
    VirtualClock,
    aware,
    make_plan,
    make_zone,
)
from tests.engine.test_rain_credit import (
    GAUGE,
    NO_DEBT,
    VALVE_1,
    baselines_of,
    current,
    finished,
    make_sequencer,
    run_to_idle,
    two_zone_plan,
    water_a_morning,
    zone_records,
)

if TYPE_CHECKING:
    from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer
    from tests.engine.common import FakeJournalPort

# Two gauges: the one the baselines were banked under and its replacement.
SENSOR_A = "sensor.a"
SENSOR_B = "sensor.b"


def banked(total: float, *zones: str, source: str = GAUGE) -> dict[str, object]:
    """Return a ledger section with `total` banked for `zones` under `source`."""
    return {
        **NO_DEBT,
        "rain_baselines": dict.fromkeys(zones, total),
        "rain_source": source,
    }


def history_of(journal: FakeJournalPort) -> list[dict[str, object]]:
    """Return the history section of the LAST snapshot the journal saw."""
    history = journal.snapshots[-1]["history"]
    assert isinstance(history, list)
    return history


def stored_ledger(journal: FakeJournalPort) -> dict[str, object]:
    """Return the ledger section of the LAST snapshot the journal saw."""
    ledger = journal.snapshots[-1]["ledger"]
    assert isinstance(ledger, dict)
    return ledger


async def water_an_evening(
    sequencer: Sequencer,
    clock: VirtualClock,
    *,
    day: int,
    month: int = 7,
) -> None:
    """Request and drain the evening cycle of `day`."""
    clock.advance_to(aware(20, day=day, month=month))
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    await run_to_idle(sequencer, clock)


# --------------------------------------------------------------------------
# Gauge identity: a swap or a rename re-banks, never credits
# --------------------------------------------------------------------------


async def test_a_gauge_swapped_for_one_with_a_higher_total_credits_nothing() -> None:
    """Matrix "gauge swapped upward": `sensor.a`/40 banked, `sensor.b` reads 1200.

    Without the source stamp the whole 1200 mm would be credited and both
    zones skipped for the cycle — the fail-dry outcome FR15 forbids. Instead:
    full 600 s, the run snapshots `sensor.b`, settlement re-banks 1200 under
    `sensor.b`, and the NEXT cycle modulates from there (3 mm → 180 s).
    """
    rain = FakeRainPort(total=1200.0, source_id=SENSOR_B)
    sequencer, switches, _, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(40.0, "zone-1", "zone-2", source=SENSOR_A),
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert (run.rain_total_mm, run.rain_source) == (1200.0, SENSOR_B)
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (0, 600),
        (0, 600),
    ]
    assert [effective_seconds(z) for z in run.zone_runs] == [600, 600]
    assert baselines_of(sequencer) == {"zone-1": 1200.0, "zone-2": 1200.0}
    assert sequencer.ledger.rain_source == SENSOR_B
    assert anomalies.reports == []
    assert ("on", VALVE_1) in switches.commands

    rain.total = 1203.0
    await water_an_evening(sequencer, clock, day=31)

    assert [(z.rain_credit_s, z.duration_s) for z in finished(sequencer).zone_runs] == [
        (180, 720),
        (180, 720),
    ]
    assert anomalies.reports == []


async def test_a_gauge_swapped_for_one_with_a_lower_total_re_banks_the_same_way() -> (
    None
):
    """Matrix "gauge swapped downward": foreign source AND below the baseline → 0.

    Two reasons to credit nothing, one outcome: full durations, and settle
    banks 0.5 under `sensor.b`.
    """
    rain = FakeRainPort(total=0.5, source_id=SENSOR_B)
    sequencer, _, _, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(40.0, "zone-1", "zone-2", source=SENSOR_A),
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (0, 600),
        (0, 600),
    ]
    assert baselines_of(sequencer) == {"zone-1": 0.5, "zone-2": 0.5}
    assert sequencer.ledger.rain_source == SENSOR_B
    assert anomalies.reports == []


async def test_a_renamed_gauge_costs_exactly_one_banking_cycle() -> None:
    """Matrix "renamed gauge": `sensor.a` → `sensor.a2`, adapter rebuilt on reload.

    The reload rebuilds the sequencer from the journal and the adapter with
    the new id; the same physical gauge keeps counting. The first cycle
    after the rename credits nothing and banks under `sensor.a2`; the one
    after modulates.
    """
    rain = FakeRainPort(total=12.0, source_id=SENSOR_A)
    before, _, journal, _ = make_sequencer(two_zone_plan(), rain=rain)
    clock = VirtualClock(aware(7))
    await water_a_morning(before, clock, day=31)
    assert stored_ledger(journal)["rain_source"] == SENSOR_A

    renamed = FakeRainPort(total=15.5, source_id="sensor.a2")
    after, _, _, anomalies = make_sequencer(
        two_zone_plan(), rain=renamed, ledger=stored_ledger(journal)
    )
    await water_an_evening(after, clock, day=31)

    run = finished(after)
    assert run.rain_source == "sensor.a2"
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (0, 900),
        (0, 900),
    ]
    assert baselines_of(after) == {"zone-1": 15.5, "zone-2": 15.5}
    assert after.ledger.rain_source == "sensor.a2"

    renamed.total = 17.5
    await water_a_morning(after, clock, day=1, month=8)

    assert [(z.rain_credit_s, z.duration_s) for z in finished(after).zone_runs] == [
        (120, 480),
        (120, 480),
    ]
    assert anomalies.reports == []


async def test_a_pre_2_5_journal_quotes_one_unmodulated_cycle_then_modulates() -> None:
    """Matrix "pre-2.5 journal": baselines and no `rain_source` → seed None.

    The 2.4 section's baselines are not comparable to any gauge (no source),
    so the first cycle after the upgrade waters in full and settlement
    stamps the source; the second cycle modulates.
    """
    rain = FakeRainPort(total=15.0)
    sequencer, _, _, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger={
            "settled_cycle_id": "2026-07-30-evening",
            "deficits": {},
            "rain_baselines": {"zone-1": 10.0, "zone-2": 10.0},
        },
    )
    assert sequencer.ledger.rain_source is None
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    run = finished(sequencer)
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (0, 600),
        (0, 600),
    ]
    assert baselines_of(sequencer) == {"zone-1": 15.0, "zone-2": 15.0}
    assert sequencer.ledger.rain_source == GAUGE

    rain.total = 17.0
    await water_an_evening(sequencer, clock, day=31)

    assert [(z.rain_credit_s, z.duration_s) for z in finished(sequencer).zone_runs] == [
        (120, 780),
        (120, 780),
    ]
    assert anomalies.reports == []


async def test_a_foreign_source_never_reduces_a_zone_in_debt() -> None:
    """Matrix "foreign source, zone in debt": full base + 300 carried → 900 s."""
    rain = FakeRainPort(total=1200.0, source_id=SENSOR_B)
    sequencer, _, _, _ = make_sequencer(
        make_plan(make_zone("zone-1", valve=VALVE_1, morning_s=600)),
        rain=rain,
        ledger={
            "settled_cycle_id": "2026-07-30-evening",
            "deficits": {"zone-1": 300},
            "rain_baselines": {"zone-1": 40.0},
            "rain_source": SENSOR_A,
        },
    )
    clock = VirtualClock(aware(7))

    await sequencer.request_cycle(CycleKind.MORNING, clock.now())

    zone = current(sequencer).zone_runs[0]
    assert (zone.duration_s, zone.rain_credit_s, zone.carried_s) == (900, 0, 300)


# --------------------------------------------------------------------------
# Late-arriving data (FR16): credits the next unquoted cycle, revisits nothing
# --------------------------------------------------------------------------


async def test_a_doubtful_quote_is_backfilled_into_the_next_cycle() -> None:
    """Matrix "doubtful at quote, backfill later".

    Mon 07:00 banked 10. Mon 20:00 the gauge is `unavailable`: full 900 s,
    baselines kept, `rain_total_mm` None on the run and on its history
    record. Tue 07:00 the gauge reads 30 — the whole 20 mm since 10 is
    credited (1200 s ≥ 600: both zones skipped), including rain that fell
    before Monday evening's cycle. Monday evening's record is untouched and
    it left no deficit. No anomaly at any point.
    """
    rain = FakeRainPort(total=None)
    sequencer, switches, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(10.0, "zone-1", "zone-2"),
    )
    clock = VirtualClock(aware(20))

    await water_an_evening(sequencer, clock, day=31)

    monday = finished(sequencer)
    assert monday.rain_total_mm is None
    assert [(z.rain_credit_s, z.duration_s) for z in monday.zone_runs] == [
        (0, 900),
        (0, 900),
    ]
    assert baselines_of(sequencer) == {"zone-1": 10.0, "zone-2": 10.0}
    monday_record = dict(history_of(journal)[-1])
    assert monday_record["cycle_id"] == "2026-07-31-evening"
    assert monday_record["rain_total_mm"] is None
    assert sequencer.ledger.as_dict()["deficits"] == {}
    switches.commands.clear()

    rain.total = 30.0
    await water_a_morning(sequencer, clock, day=1, month=8)

    tuesday = finished(sequencer)
    assert tuesday.rain_total_mm == 30.0
    assert [(z.status, z.rain_credit_s, z.duration_s) for z in tuesday.zone_runs] == [
        (ZoneRunStatus.SKIPPED, 1200, 0),
        (ZoneRunStatus.SKIPPED, 1200, 0),
    ]
    assert switches.commands == []
    assert history_of(journal)[-2] == monday_record
    assert history_of(journal)[-1]["rain_total_mm"] == 30.0
    assert baselines_of(sequencer) == {"zone-1": 30.0, "zone-2": 30.0}
    assert sequencer.ledger.as_dict()["deficits"] == {}
    assert anomalies.reports == []


async def test_a_backfill_after_completion_never_revisits_the_completed_run() -> None:
    """Matrix "backfill after completion": quoted at 10, completes, gauge → 14.

    The completed run's snapshot, its history record and the baselines it
    banked stand; the next quote credits the 4 mm (240 s).
    """
    rain = FakeRainPort(total=10.0)
    sequencer, _, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(10.0, "zone-1", "zone-2"),
    )
    clock = VirtualClock(aware(7))
    await water_a_morning(sequencer, clock, day=31)
    completed = finished(sequencer)
    record = dict(history_of(journal)[-1])
    ledger_after = dict(sequencer.ledger.as_dict())
    saves = len(journal.snapshots)

    rain.total = 14.0

    assert finished(sequencer) is completed
    assert (completed.rain_total_mm, completed.status) == (10.0, CycleStatus.COMPLETED)
    assert sequencer.ledger.as_dict() == ledger_after
    assert len(journal.snapshots) == saves

    await water_an_evening(sequencer, clock, day=31)

    assert history_of(journal)[-2] == record
    assert [(z.rain_credit_s, z.duration_s) for z in finished(sequencer).zone_runs] == [
        (240, 660),
        (240, 660),
    ]
    assert anomalies.reports == []


async def test_a_jump_during_a_running_cycle_credits_the_next_one() -> None:
    """Matrix "jump during a running cycle": quote 10, gauge 25 mid-cycle.

    The running zones keep their durations (AD-8), the baseline is set to
    the QUOTE-TIME 10 at settlement, and the next quote credits all 15 mm
    (900 s — the evening base exactly, so both zones are skipped).
    """
    rain = FakeRainPort(total=10.0)
    sequencer, _, _, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(10.0, "zone-1", "zone-2"),
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    clock.advance_to(aware(7, 5))
    rain.total = 25.0
    await sequencer.advance(clock.now())

    run = current(sequencer)
    assert run.rain_total_mm == 10.0
    assert [z.duration_s for z in run.zone_runs] == [600, 600]
    assert rain.reads == 1

    await run_to_idle(sequencer, clock)
    assert [effective_seconds(z) for z in finished(sequencer).zone_runs] == [600, 600]
    assert baselines_of(sequencer) == {"zone-1": 10.0, "zone-2": 10.0}

    await water_an_evening(sequencer, clock, day=31)

    assert [
        (z.status, z.rain_credit_s, z.duration_s) for z in finished(sequencer).zone_runs
    ] == [
        (ZoneRunStatus.SKIPPED, 900, 0),
        (ZoneRunStatus.SKIPPED, 900, 0),
    ]
    assert anomalies.reports == []


async def test_a_stuck_gauge_waters_in_full_every_cycle() -> None:
    """Matrix "stuck gauge": the total never moves → delta 0, full durations daily."""
    rain = FakeRainPort(total=12.0)
    sequencer, _, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(12.0, "zone-1", "zone-2"),
    )
    clock = VirtualClock(aware(7))

    for day in (1, 2, 3):
        await water_a_morning(sequencer, clock, day=day, month=8)
        await water_an_evening(sequencer, clock, day=day, month=8)
        run = finished(sequencer)
        assert run.rain_total_mm == 12.0
        assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
            (0, 900),
            (0, 900),
        ]
        assert baselines_of(sequencer) == {"zone-1": 12.0, "zone-2": 12.0}

    assert [record["rain_total_mm"] for record in history_of(journal)] == [12.0] * 6
    assert anomalies.reports == []


async def test_a_reset_gauge_credits_nothing_then_counts_from_the_new_zero() -> None:
    """Matrix "gauge reset then rain": baseline 40, reset to 0, 3 mm since.

    3 < 40 → credit 0 and the baseline becomes 3 (no "rain since zero"
    inference — the clamp is the whole reset policy). Rain after that is
    credited from 3: the next quote at 5 credits 2 mm (120 s).
    """
    rain = FakeRainPort(total=3.0)
    sequencer, _, _, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(40.0, "zone-1", "zone-2"),
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)

    assert [(z.rain_credit_s, z.duration_s) for z in finished(sequencer).zone_runs] == [
        (0, 600),
        (0, 600),
    ]
    assert baselines_of(sequencer) == {"zone-1": 3.0, "zone-2": 3.0}

    rain.total = 5.0
    await water_an_evening(sequencer, clock, day=31)

    assert [(z.rain_credit_s, z.duration_s) for z in finished(sequencer).zone_runs] == [
        (120, 780),
        (120, 780),
    ]
    assert anomalies.reports == []


async def test_baselines_and_source_survive_a_restart_together() -> None:
    """Matrix "restart": the journal's section — source included — re-seeds the engine.

    The rebuilt sequencer reads the SAME gauge, so the baselines are
    comparable and the first quote after the restart modulates.
    """
    rain = FakeRainPort(total=12.0, source_id=SENSOR_A)
    before, _, journal, _ = make_sequencer(two_zone_plan(), rain=rain)
    clock = VirtualClock(aware(7))
    await water_a_morning(before, clock, day=31)
    stored = stored_ledger(journal)
    assert stored["rain_baselines"] == {"zone-1": 12.0, "zone-2": 12.0}
    assert stored["rain_source"] == SENSOR_A

    rain.total = 15.5
    rebuilt, _, _, _ = make_sequencer(two_zone_plan(), rain=rain, ledger=stored)
    assert rebuilt.ledger.rain_source == SENSOR_A
    await water_an_evening(rebuilt, clock, day=31)

    assert [(z.rain_credit_s, z.duration_s) for z in finished(rebuilt).zone_runs] == [
        (210, 690),
        (210, 690),
    ]


# --------------------------------------------------------------------------
# History records why: `rain_total_mm`
# --------------------------------------------------------------------------


async def test_history_records_the_quote_time_reading_or_null_when_doubtful() -> None:
    """Matrix "history of a good / doubtful quote": 15.0 on one record, then null.

    `null` is how Epic 4 tells "modulation was off" from "no rain" without
    an anomaly; the zones of the doubtful cycle carry `rain_credit_s: 0`.
    """
    rain = FakeRainPort(total=15.0)
    sequencer, _, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(12.0, "zone-1", "zone-2"),
    )
    clock = VirtualClock(aware(7))

    await water_a_morning(sequencer, clock, day=31)
    good = history_of(journal)[-1]
    assert good["rain_total_mm"] == 15.0
    assert [z["rain_credit_s"] for z in zone_records(journal)] == [180, 180]

    rain.total = None
    await water_an_evening(sequencer, clock, day=31)

    doubtful = history_of(journal)[-1]
    assert doubtful["cycle_id"] == "2026-07-31-evening"
    assert doubtful["rain_total_mm"] is None
    assert [z["rain_credit_s"] for z in zone_records(journal)] == [0, 0]
    assert anomalies.reports == []


async def test_a_gauge_doubtful_for_weeks_stays_silent() -> None:
    """Matrix "doubtful for weeks": ten `unavailable` quotes in a row.

    Ten full cycles, `rain_total_mm: null` on every record, the baselines
    kept throughout, and — the decision — no anomaly kind and no counter:
    `anomalies.reports == []` after the tenth as after the first. The
    operator reads the nulls in history.
    """
    rain = FakeRainPort(total=None)
    sequencer, _, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(10.0, "zone-1", "zone-2"),
    )
    clock = VirtualClock(aware(7))

    for day in range(1, 6):
        await water_a_morning(sequencer, clock, day=day, month=8)
        await water_an_evening(sequencer, clock, day=day, month=8)

    history = history_of(journal)
    assert len(history) == 10
    assert [record["rain_total_mm"] for record in history] == [None] * 10
    for record in history:
        zones = record["zones"]
        assert isinstance(zones, list)
        assert [
            (z["rain_credit_s"], z["planned_s"], z["effective_s"]) for z in zones
        ] == [
            (
                0,
                900 if record["kind"] == "evening" else 600,
                900 if record["kind"] == "evening" else 600,
            ),
        ] * 2
    assert baselines_of(sequencer) == {"zone-1": 10.0, "zone-2": 10.0}
    assert sequencer.ledger.rain_source == GAUGE
    assert sequencer.ledger.as_dict()["deficits"] == {}
    assert anomalies.reports == []
    assert rain.reads == 10


# --------------------------------------------------------------------------
# The season switch: turning it ON forgets the rain
# --------------------------------------------------------------------------


async def test_turning_the_season_on_forgets_the_rain_and_re_banks() -> None:
    """Matrix "season ON after winter": 40 banked under `sensor.a`, gauge now at 300.

    OFF→ON clears the baselines AND the source and journals it. Without
    that, the 260 mm of winter rain would skip the first spring cycle. That
    cycle credits 0, waters in full and banks 300 under `sensor.a`; the next
    one modulates (2 mm → 120 s).
    """
    rain = FakeRainPort(total=300.0, source_id=SENSOR_A)
    sequencer, _, journal, anomalies = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(40.0, "zone-1", "zone-2", source=SENSOR_A),
    )
    clock = VirtualClock(aware(12, day=1, month=3))
    assert await sequencer.async_set_season(enabled=False, now=clock.now()) is True
    assert baselines_of(sequencer) == {"zone-1": 40.0, "zone-2": 40.0}
    saves = len(journal.snapshots)

    clock.advance_to(aware(12, day=1, month=4))
    assert await sequencer.async_set_season(enabled=True, now=clock.now()) is True

    assert baselines_of(sequencer) == {}
    assert sequencer.ledger.rain_source is None
    assert len(journal.snapshots) == saves + 1
    assert stored_ledger(journal)["rain_baselines"] == {}
    assert stored_ledger(journal)["rain_source"] is None

    await water_a_morning(sequencer, clock, day=2, month=4)

    run = finished(sequencer)
    assert (run.rain_total_mm, run.rain_source) == (300.0, SENSOR_A)
    assert [(z.rain_credit_s, z.duration_s) for z in run.zone_runs] == [
        (0, 600),
        (0, 600),
    ]
    assert baselines_of(sequencer) == {"zone-1": 300.0, "zone-2": 300.0}
    assert sequencer.ledger.rain_source == SENSOR_A

    rain.total = 302.0
    await water_an_evening(sequencer, clock, day=2, month=4)

    assert [(z.rain_credit_s, z.duration_s) for z in finished(sequencer).zone_runs] == [
        (120, 780),
        (120, 780),
    ]
    assert anomalies.reports == []


async def test_turning_the_season_on_keeps_the_debt_and_the_day_credit() -> None:
    """`forget_rain` is about rain only: deficits and the day credit are untouched."""
    sequencer, _, _, _ = make_sequencer(
        two_zone_plan(),
        rain=FakeRainPort(total=300.0),
        ledger={
            "settled_cycle_id": "2026-07-30-evening",
            "deficits": {"zone-1": 300},
            "day_credit": {
                "irrigation_day": "2026-07-31",
                "cycle_id": "2026-07-31-morning",
            },
            "rain_baselines": {"zone-1": 40.0, "zone-2": 40.0},
            "rain_source": GAUGE,
        },
    )
    clock = VirtualClock(aware(6))
    await sequencer.async_set_season(enabled=False, now=clock.now())

    await sequencer.async_set_season(enabled=True, now=clock.now())

    assert sequencer.ledger.as_dict() == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300},
        "day_credit": {
            "irrigation_day": "2026-07-31",
            "cycle_id": "2026-07-31-morning",
        },
        "rain_baselines": {},
        "rain_source": None,
    }


async def test_setting_the_season_to_on_when_already_on_keeps_the_rain() -> None:
    """Matrix "season toggled to its current value": False, baselines kept, no save."""
    sequencer, _, journal, _ = make_sequencer(
        two_zone_plan(),
        rain=FakeRainPort(total=45.0),
        ledger=banked(40.0, "zone-1", "zone-2"),
    )
    clock = VirtualClock(aware(6))

    assert await sequencer.async_set_season(enabled=True, now=clock.now()) is False

    assert journal.snapshots == []
    assert baselines_of(sequencer) == {"zone-1": 40.0, "zone-2": 40.0}
    assert sequencer.ledger.rain_source == GAUGE

    await water_a_morning(sequencer, clock, day=31)
    assert [z.rain_credit_s for z in finished(sequencer).zone_runs] == [300, 300]


async def test_turning_the_season_off_keeps_the_rain() -> None:
    """Matrix "season OFF": the deferred queue goes (existing), the baselines stay.

    A run-now while the season is off is rain-reduced from them like any
    cycle — only the switch going ON starts a new accumulation period.
    """
    rain = FakeRainPort(total=45.0)
    sequencer, _, journal, _ = make_sequencer(
        two_zone_plan(),
        rain=rain,
        ledger=banked(40.0, "zone-1", "zone-2"),
    )
    clock = VirtualClock(aware(7))
    await sequencer.request_cycle(CycleKind.MORNING, clock.now())
    await sequencer.advance(clock.now())
    await sequencer.request_cycle(CycleKind.EVENING, clock.now())
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)

    assert await sequencer.async_set_season(enabled=False, now=clock.now()) is True

    assert not sequencer.deferred_kinds
    assert baselines_of(sequencer) == {"zone-1": 40.0, "zone-2": 40.0}
    assert stored_ledger(journal)["rain_baselines"] == {"zone-1": 40.0, "zone-2": 40.0}
    assert stored_ledger(journal)["rain_source"] == GAUGE
    await run_to_idle(sequencer, clock)
    assert baselines_of(sequencer) == {"zone-1": 45.0, "zone-2": 45.0}

    rain.total = 47.0
    clock.advance_to(aware(12))
    assert await sequencer.async_run_now(CycleKind.MORNING, clock.now()) is True

    assert [z.rain_credit_s for z in current(sequencer).zone_runs] == [120, 120]
    assert baselines_of(sequencer) == {"zone-1": 45.0, "zone-2": 45.0}
