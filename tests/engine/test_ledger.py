"""Pure unit tests of the water-debt ledger (Stories 2.2 and 2.3, AD-5).

No clock, no sequencer, no Home Assistant: `Ledger` is arithmetic over plan
zones and run objects, and every matrix row that is ledger-only is pinned
here — the caps at both ends, replay, zero drop, the removed zone, the dict
round-trip — plus Story 2.3's day credit (who earns it, who consumes it, and
how it survives the journal) and Story 2.4's rain credit: the mm → seconds
rule, who has a baseline, and how settlement advances it.
"""

from __future__ import annotations

from datetime import date, timedelta

from custom_components.ha_irrigation_controller.engine.ledger import (
    Ledger,
    ZoneQuote,
)
from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleRun,
    CycleStatus,
    ZoneRun,
    ZoneRunStatus,
    effective_seconds,
)
from tests.engine.common import aware, make_zone

VALVE = "switch.zone_valve"


def zone_run(  # noqa: PLR0913 — one keyword per field the ledger reads is the readable shape
    zone_id: str = "zone-1",
    *,
    quoted_s: int = 600,
    base_s: int = 600,
    carried_s: int = 0,
    rain_credit_s: int = 0,
    watered_s: int | None = 600,
    status: ZoneRunStatus = ZoneRunStatus.COMPLETED,
) -> ZoneRun:
    """Build one finished zone slot that watered `watered_s` seconds.

    `watered_s=None` leaves the slot never reached (PENDING, no instants) —
    what a cancel leaves behind for the zones after the live one, and what a
    rain-SKIPPED slot looks like too (Story 2.4).
    """
    if watered_s is None:
        return ZoneRun(
            zone_id=zone_id,
            name=zone_id,
            valve_entity_id=VALVE,
            duration_s=quoted_s,
            base_s=base_s,
            carried_s=carried_s,
            rain_credit_s=rain_credit_s,
            planned_start=aware(7),
            planned_end=aware(7, 10),
            status=status if status is ZoneRunStatus.SKIPPED else ZoneRunStatus.PENDING,
        )
    return ZoneRun(
        zone_id=zone_id,
        name=zone_id,
        valve_entity_id=VALVE,
        duration_s=quoted_s,
        base_s=base_s,
        carried_s=carried_s,
        rain_credit_s=rain_credit_s,
        planned_start=aware(7),
        planned_end=aware(7, 10),
        status=status,
        actual_start=aware(7),
        actual_end=aware(7) + timedelta(seconds=watered_s),
    )


def cycle_run(
    *zones: ZoneRun,
    cycle_id: str = "2026-07-31-morning",
    status: CycleStatus = CycleStatus.COMPLETED,
    manual: bool = False,
    rain_total_mm: float | None = None,
) -> CycleRun:
    """Build one terminal cycle run carrying `zones`; `manual` marks a run-now.

    `rain_total_mm` is the quote-time gauge reading the run snapshotted
    (Story 2.4) — what settlement advances every zone's baseline to.
    """
    return CycleRun(
        cycle_id=cycle_id,
        kind=CycleKind.MORNING,
        configured_start=aware(7),
        scheduled_start=aware(7),
        pump_entity_id="switch.pool_pump",
        zone_runs=zones,
        status=status,
        manual=manual,
        rain_total_mm=rain_total_mm,
    )


# The credit a completed run-now of 2026-07-31's morning cycle leaves behind.
CREDIT: dict[str, object] = {
    "irrigation_day": "2026-07-31",
    "cycle_id": "2026-07-31-morning",
}


# --------------------------------------------------------------------------
# Quote
# --------------------------------------------------------------------------


def test_a_fresh_ledger_quotes_every_zone_on_its_base() -> None:
    """Nothing owed: effective == base, the arithmetic fields all say so."""
    ledger = Ledger()
    zones = (make_zone("zone-1", morning_s=600), make_zone("zone-2", morning_s=300))

    assert ledger.quote(zones, CycleKind.MORNING) == (
        ZoneQuote("zone-1", base_s=600, rain_credit_s=0, carried_s=0, quoted_s=600),
        ZoneQuote("zone-2", base_s=300, rain_credit_s=0, carried_s=0, quoted_s=300),
    )


def test_quote_adds_the_carried_deficit_for_the_quoted_kind() -> None:
    """A deficit extends the run: effective = base + carried."""
    ledger = Ledger({"zone-1": 240})
    zone = make_zone("zone-1", morning_s=600, evening_s=900)

    (morning,) = ledger.quote((zone,), CycleKind.MORNING)
    (evening,) = ledger.quote((zone,), CycleKind.EVENING)

    assert (morning.carried_s, morning.quoted_s) == (240, 840)
    assert (evening.carried_s, evening.quoted_s) == (240, 1140)


def test_quote_caps_the_carried_deficit_at_the_quoted_kinds_base() -> None:
    """Matrix "cap at quote": 900 owed on the evening base, morning base 600.

    Carried is 600 and effective 1200 — never more than 2x THIS kind's base,
    whatever base the deficit was settled on. The remainder is discarded, not
    kept for later: the cap is a ceiling on the debt itself.
    """
    ledger = Ledger({"zone-1": 900})
    zone = make_zone("zone-1", morning_s=600, evening_s=900)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING)

    assert quote == ZoneQuote(
        "zone-1",
        base_s=600,
        rain_credit_s=0,
        carried_s=600,
        quoted_s=1200,
    )


def test_quote_is_pure() -> None:
    """Quoting reads the ledger and writes nothing — twice gives the same answer.

    A quoted cycle that never settles (a crash, a suspend) must leave the
    deficit in place so it is re-applied next time (AD-4: doubt waters).
    """
    ledger = Ledger({"zone-1": 300}, settled_cycle_id="2026-07-30-evening")
    zones = (make_zone("zone-1"),)

    first = ledger.quote(zones, CycleKind.MORNING)
    second = ledger.quote(zones, CycleKind.MORNING)

    assert first == second
    assert ledger.deficit_s("zone-1") == 300
    assert ledger.as_dict() == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300},
        "day_credit": None,
        "rain_baselines": {},
    }


def test_a_deficit_for_a_zone_not_in_the_plan_is_never_quoted() -> None:
    """Matrix "zone removed": the ledger only speaks about the zones it is asked for."""
    ledger = Ledger({"zone-gone": 600})

    quotes = ledger.quote((make_zone("zone-1"),), CycleKind.MORNING)

    assert [quote.zone_id for quote in quotes] == ["zone-1"]
    assert quotes[0].carried_s == 0


def test_the_rain_credit_comes_off_the_base_before_the_deficit_is_added() -> None:
    """The formula order: `max(0, base - credit) + carried` (Story 2.4 fills the slot).

    100 s owed, 600 s base, baseline 10 mm. No reading → no credit, 700.
    13.333 mm → 200 s credit, 500. 100 mm → 5400 s credit: the base part
    clamps at 0 and the debt still runs — 100, never negative. The credit
    field records the UNCLAMPED worth of the rain.
    """
    ledger = Ledger({"zone-1": 100}, rain_baselines={"zone-1": 10.0})
    zones = (make_zone("zone-1", morning_s=600),)

    (plain,) = ledger.quote(zones, CycleKind.MORNING)
    (credited,) = ledger.quote(zones, CycleKind.MORNING, 10.0 + 200 / 60)
    (soaked,) = ledger.quote(zones, CycleKind.MORNING, 100.0)

    assert (plain.rain_credit_s, plain.quoted_s) == (0, 700)
    assert (credited.rain_credit_s, credited.quoted_s) == (200, 500)
    assert (soaked.rain_credit_s, soaked.quoted_s) == (5400, 100)


# --------------------------------------------------------------------------
# Rain credit (Story 2.4): the mm → seconds rule
# --------------------------------------------------------------------------


def test_rain_since_the_baseline_is_credited_at_factor_times_sixty() -> None:
    """Matrix "rain since last cycle": baseline 12.0, total 15.5, factor 1 → 210 s.

    3.5 mm * 1 min/mm * 60 = 210 s off a 600 s base: quoted 390.
    """
    ledger = Ledger(rain_baselines={"zone-1": 12.0})
    zone = make_zone("zone-1", morning_s=600, rain_exposed=True, rain_factor=1.0)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING, 15.5)

    assert quote == ZoneQuote(
        "zone-1",
        base_s=600,
        rain_credit_s=210,
        carried_s=0,
        quoted_s=390,
    )


def test_the_rain_factor_scales_the_credit() -> None:
    """2.5 min/mm on 2 mm is 300 s; 0.5 min/mm on the same rain is 60 s."""
    ledger = Ledger(rain_baselines={"zone-1": 0.0, "zone-2": 0.0})
    zones = (
        make_zone("zone-1", morning_s=600, rain_factor=2.5),
        make_zone("zone-2", morning_s=600, rain_factor=0.5),
    )

    heavy, light = ledger.quote(zones, CycleKind.MORNING, 2.0)

    assert (heavy.rain_credit_s, heavy.quoted_s) == (300, 300)
    assert (light.rain_credit_s, light.quoted_s) == (60, 540)


def test_a_credit_covering_the_whole_base_quotes_zero() -> None:
    """Matrix "fully covered": baseline 0, total 12.0, base 600 → credit 720, quoted 0.

    Zero is the skip signal for the sequencer; the credit itself is not
    clamped, so history can show the rain was worth more than the zone.
    """
    ledger = Ledger(rain_baselines={"zone-1": 0.0})
    zone = make_zone("zone-1", morning_s=600)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING, 12.0)

    assert (quote.rain_credit_s, quote.quoted_s) == (720, 0)


def test_a_covered_zone_in_debt_still_runs_its_carried_seconds() -> None:
    """Matrix "covered but in debt": credit ≥ base, 300 s carried → quoted 300.

    The debt floor holds (FR13): rain excuses the base, never the debt.
    """
    ledger = Ledger({"zone-1": 300}, rain_baselines={"zone-1": 0.0})
    zone = make_zone("zone-1", morning_s=600)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING, 12.0)

    assert quote == ZoneQuote(
        "zone-1",
        base_s=600,
        rain_credit_s=720,
        carried_s=300,
        quoted_s=300,
    )


def test_a_sheltered_zone_is_never_credited() -> None:
    """Matrix "sheltered zone": `rain_exposed=False` waters its full duration."""
    ledger = Ledger(rain_baselines={"zone-1": 0.0})
    zone = make_zone("zone-1", morning_s=600, rain_exposed=False)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING, 50.0)

    assert (quote.rain_credit_s, quote.quoted_s) == (0, 600)


def test_a_factor_of_zero_disables_the_credit() -> None:
    """Matrix "factor 0": exposed, but 0 min/mm credits nothing."""
    ledger = Ledger(rain_baselines={"zone-1": 0.0})
    zone = make_zone("zone-1", morning_s=600, rain_factor=0.0)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING, 50.0)

    assert (quote.rain_credit_s, quote.quoted_s) == (0, 600)


def test_a_zone_with_no_baseline_is_not_credited() -> None:
    """Matrix "first cycle ever": no baseline → credit 0, whatever the total.

    The first cycle BANKS the reading (at settlement); it never spends rain
    nobody measured a start for.
    """
    ledger = Ledger()
    zone = make_zone("zone-1", morning_s=600)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING, 12.0)

    assert (quote.rain_credit_s, quote.quoted_s) == (0, 600)
    assert ledger.rain_baseline_mm("zone-1") is None


def test_a_none_reading_credits_nothing_to_any_zone() -> None:
    """Matrix "gauge unavailable at quote": None → every zone on full duration."""
    ledger = Ledger(rain_baselines={"zone-1": 0.0, "zone-2": 0.0})
    zones = (make_zone("zone-1", morning_s=600), make_zone("zone-2", morning_s=300))

    quotes = ledger.quote(zones, CycleKind.MORNING, None)

    assert [(q.rain_credit_s, q.quoted_s) for q in quotes] == [(0, 600), (0, 300)]
    quotes = ledger.quote(zones, CycleKind.MORNING)
    assert [(q.rain_credit_s, q.quoted_s) for q in quotes] == [(0, 600), (0, 300)]


def test_a_total_below_the_baseline_clamps_to_no_credit() -> None:
    """Matrix "decreasing total": baseline 40.0, total 0.5 → credit 0, not negative."""
    ledger = Ledger(rain_baselines={"zone-1": 40.0})
    zone = make_zone("zone-1", morning_s=600)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING, 0.5)

    assert (quote.rain_credit_s, quote.quoted_s) == (0, 600)


def test_the_credit_is_floored_to_whole_seconds() -> None:
    """1.234 mm * 1 min/mm * 60 = 74.04 s → 74: less credit waters more (AD-4)."""
    ledger = Ledger(rain_baselines={"zone-1": 0.0})
    zone = make_zone("zone-1", morning_s=600)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING, 1.234)

    assert quote.rain_credit_s == 74
    assert isinstance(quote.rain_credit_s, int)


def test_a_binary_float_artefact_does_not_shave_a_second_off_the_credit() -> None:
    """`(10.0 - 9.9) * 60` is `5.999…` in binary floats — the credit is 6 s, not 5."""
    ledger = Ledger(rain_baselines={"zone-1": 9.9, "zone-2": 0.1})
    zones = (make_zone("zone-1", morning_s=600), make_zone("zone-2", morning_s=600))

    (fine,) = ledger.quote((zones[0],), CycleKind.MORNING, 10.0)
    (coarse,) = ledger.quote((zones[1],), CycleKind.MORNING, 0.3)

    assert (fine.rain_credit_s, fine.quoted_s) == (6, 594)
    assert (coarse.rain_credit_s, coarse.quoted_s) == (12, 588)


def test_an_absurd_total_overflowing_the_credit_reads_as_no_credit() -> None:
    """1e307 mm at 10 min/mm overflows to `inf`: fail-wet, full base, no exception."""
    ledger = Ledger(rain_baselines={"zone-1": 0.0})
    zone = make_zone("zone-1", morning_s=600, rain_factor=10.0)

    (quote,) = ledger.quote((zone,), CycleKind.MORNING, 1e307)

    assert (quote.rain_credit_s, quote.quoted_s) == (0, 600)


def test_quoting_with_rain_writes_no_baseline() -> None:
    """Quote stays pure: the gauge reading is banked only when a run SETTLES."""
    ledger = Ledger(rain_baselines={"zone-1": 12.0})
    zones = (make_zone("zone-1"), make_zone("zone-2"))

    ledger.quote(zones, CycleKind.MORNING, 15.5)

    assert ledger.as_dict()["rain_baselines"] == {"zone-1": 12.0}
    assert ledger.rain_baseline_mm("zone-2") is None


# --------------------------------------------------------------------------
# Rain baselines (Story 2.4): only settle() advances them
# --------------------------------------------------------------------------


def test_settle_sets_every_zones_baseline_to_the_runs_quote_time_reading() -> None:
    """Matrix "first cycle ever" / "rain during the cycle": the snapshot is banked.

    The run was quoted at 10.0 mm; whatever the gauge says at settlement is
    not consulted (settle takes no reading), so the next quote credits the
    rain from 10.0 onward — rain during the cycle credits the next one.
    """
    ledger = Ledger()
    run = cycle_run(zone_run("zone-1"), zone_run("zone-2"), rain_total_mm=10.0)

    assert ledger.settle(run) is True

    assert ledger.as_dict()["rain_baselines"] == {"zone-1": 10.0, "zone-2": 10.0}
    assert ledger.rain_baseline_mm("zone-1") == 10.0
    (quote,) = ledger.quote(
        (make_zone("zone-1", morning_s=600),), CycleKind.MORNING, 14.0
    )
    assert quote.rain_credit_s == 240


def test_settle_advances_the_baseline_of_a_failed_and_a_cancelled_zone_too() -> None:
    """Matrix "cancelled run": the credit was already spent on the quote.

    Keeping the old baseline would credit the same rain twice, so FAILED
    and never-reached zones advance like the rest — their shortfall carries
    as a deficit instead.
    """
    ledger = Ledger(rain_baselines={"zone-1": 0.0, "zone-2": 0.0, "zone-3": 0.0})
    run = cycle_run(
        zone_run("zone-1", quoted_s=420, rain_credit_s=180, watered_s=420),
        zone_run("zone-2", quoted_s=420, rain_credit_s=180, watered_s=120),
        zone_run("zone-3", quoted_s=420, rain_credit_s=180, watered_s=None),
        status=CycleStatus.CANCELLED,
        rain_total_mm=3.0,
    )

    ledger.settle(run)

    assert ledger.as_dict()["rain_baselines"] == {
        "zone-1": 3.0,
        "zone-2": 3.0,
        "zone-3": 3.0,
    }
    assert ledger.as_dict()["deficits"] == {"zone-2": 300, "zone-3": 420}


def test_a_none_reading_at_settle_keeps_each_zones_baseline() -> None:
    """Matrix "gauge unavailable at quote": nothing was credited, nothing is spent."""
    ledger = Ledger(rain_baselines={"zone-1": 5.0})
    run = cycle_run(zone_run("zone-1"), zone_run("zone-2"), rain_total_mm=None)

    ledger.settle(run)

    assert ledger.as_dict()["rain_baselines"] == {"zone-1": 5.0}


def test_a_decreasing_total_still_becomes_the_new_baseline() -> None:
    """Matrix "decreasing total": settle banks 0.5 — a reset gauge resets the window."""
    ledger = Ledger(rain_baselines={"zone-1": 40.0})

    ledger.settle(cycle_run(zone_run("zone-1"), rain_total_mm=0.5))

    assert ledger.rain_baseline_mm("zone-1") == 0.5


def test_a_baseline_for_a_zone_absent_from_the_settled_run_is_dropped() -> None:
    """Matrix "zone removed": baselines are REPLACED by the run's zones, as deficits."""
    ledger = Ledger(rain_baselines={"zone-gone": 3.0, "zone-1": 1.0})

    ledger.settle(cycle_run(zone_run("zone-1"), rain_total_mm=7.0))

    assert ledger.as_dict()["rain_baselines"] == {"zone-1": 7.0}
    assert ledger.rain_baseline_mm("zone-gone") is None


def test_a_none_reading_also_drops_a_removed_zones_baseline() -> None:
    """The replacement rule holds whatever the reading: absent from the run → gone."""
    ledger = Ledger(rain_baselines={"zone-gone": 3.0, "zone-1": 1.0})

    ledger.settle(cycle_run(zone_run("zone-1"), rain_total_mm=None))

    assert ledger.as_dict()["rain_baselines"] == {"zone-1": 1.0}


def test_a_replayed_settlement_leaves_the_baselines_alone() -> None:
    """Matrix "replay": the same cycle id settling again changes no baseline."""
    ledger = Ledger()
    assert ledger.settle(cycle_run(zone_run("zone-1"), rain_total_mm=10.0)) is True

    assert ledger.settle(cycle_run(zone_run("zone-1"), rain_total_mm=99.0)) is False

    assert ledger.as_dict()["rain_baselines"] == {"zone-1": 10.0}


def test_a_skipped_zone_settles_to_no_deficit() -> None:
    """Matrix "fully covered": quoted 0, watered 0 → nothing owed, baseline advanced."""
    ledger = Ledger(rain_baselines={"zone-1": 0.0})
    run = cycle_run(
        zone_run(
            "zone-1",
            quoted_s=0,
            rain_credit_s=720,
            watered_s=None,
            status=ZoneRunStatus.SKIPPED,
        ),
        rain_total_mm=12.0,
    )

    ledger.settle(run)

    assert ledger.as_dict()["deficits"] == {}
    assert ledger.rain_baseline_mm("zone-1") == 12.0


def test_an_all_skipped_run_now_credits_the_day() -> None:
    """Matrix "run-now after rain": manual, COMPLETED, no shortfall → day credit."""
    ledger = Ledger(rain_baselines={"zone-1": 0.0})
    run = cycle_run(
        zone_run(
            "zone-1",
            quoted_s=0,
            rain_credit_s=720,
            watered_s=None,
            status=ZoneRunStatus.SKIPPED,
        ),
        manual=True,
        rain_total_mm=12.0,
    )

    ledger.settle(run)

    assert ledger.day_credit == "2026-07-31"


# --------------------------------------------------------------------------
# Rain baselines: the journal round-trip
# --------------------------------------------------------------------------


def test_as_dict_from_dict_round_trip_with_baselines() -> None:
    """Matrix "restart": the baselines survive the journal and quote from there."""
    ledger = Ledger({"zone-1": 300}, settled_cycle_id="2026-07-30-evening")
    ledger.settle(cycle_run(zone_run("zone-1"), zone_run("zone-2"), rain_total_mm=12.0))

    rebuilt = Ledger.from_dict(ledger.as_dict())

    assert rebuilt.as_dict() == ledger.as_dict()
    assert rebuilt.as_dict()["rain_baselines"] == {"zone-1": 12.0, "zone-2": 12.0}
    (quote,) = rebuilt.quote(
        (make_zone("zone-1", morning_s=600),), CycleKind.MORNING, 15.5
    )
    assert quote.rain_credit_s == 210


def test_from_dict_of_a_pre_2_4_section_has_no_baselines() -> None:
    """A section written before this story has no `rain_baselines` key — and loads."""
    ledger = Ledger.from_dict({"settled_cycle_id": None, "deficits": {"zone-1": 300}})

    assert ledger.as_dict()["rain_baselines"] == {}
    assert ledger.rain_baseline_mm("zone-1") is None
    assert ledger.deficit_s("zone-1") == 300


def test_from_dict_narrows_baselines_of_the_wrong_type_to_none() -> None:
    """Type narrowing of the serialized `object` value — validation is the adapter's."""
    assert Ledger.from_dict({"rain_baselines": [1.0]}).as_dict()["rain_baselines"] == {}


def test_seeded_baselines_are_read_back_as_floats() -> None:
    """An `int` in the seed is a legal baseline and comes back as `3.0`."""
    ledger = Ledger(rain_baselines={"zone-1": 3})

    assert ledger.as_dict()["rain_baselines"] == {"zone-1": 3.0}
    assert isinstance(ledger.rain_baseline_mm("zone-1"), float)


def test_as_dict_returns_a_copy_of_the_baselines() -> None:
    """Mutating the section must not write through to the ledger (AD-6)."""
    ledger = Ledger(rain_baselines={"zone-1": 3.0})
    section = ledger.as_dict()
    baselines = section["rain_baselines"]
    assert isinstance(baselines, dict)
    baselines["zone-1"] = 99.0

    assert ledger.rain_baseline_mm("zone-1") == 3.0


# --------------------------------------------------------------------------
# Settle
# --------------------------------------------------------------------------


def test_a_failed_open_books_the_whole_quoted_duration() -> None:
    """Matrix "failed open": FAILED reads 0 s watered, so the deficit is 600."""
    ledger = Ledger()
    run = cycle_run(zone_run(status=ZoneRunStatus.FAILED, watered_s=0))

    assert ledger.settle(run) is True

    assert ledger.deficit_s("zone-1") == 600
    assert ledger.settled_cycle_id == "2026-07-31-morning"


def test_a_partial_cancel_books_each_zones_shortfall() -> None:
    """Matrix "partial cancel": full zone 0, cut zone 480, unreached zone 600."""
    ledger = Ledger()
    run = cycle_run(
        zone_run("zone-1", watered_s=600),
        zone_run("zone-2", watered_s=120),
        zone_run("zone-3", watered_s=None),
        status=CycleStatus.CANCELLED,
    )

    ledger.settle(run)

    assert ledger.as_dict()["deficits"] == {"zone-2": 480, "zone-3": 600}


def test_settle_caps_the_new_deficit_at_the_snapshotted_base() -> None:
    """Matrix "cap at settle": quoted 1200 (600 + 600 carried), watered 0 → 600.

    Deficits never compound: three failures in a row still leave one base
    owed, never two.
    """
    ledger = Ledger({"zone-1": 600})
    run = cycle_run(
        zone_run(
            quoted_s=1200,
            base_s=600,
            carried_s=600,
            status=ZoneRunStatus.FAILED,
            watered_s=0,
        ),
    )

    ledger.settle(run)

    assert ledger.deficit_s("zone-1") == 600


def test_settle_uses_the_runs_base_not_a_live_plan() -> None:
    """The cap reads the run's snapshotted `base_s` (AD-8) — there is no plan here.

    A run quoted on a 900 s base and watered 100 s owes 800, even if the
    operator has since shortened the zone to 300 s: the run completes on the
    parameters it started with.
    """
    ledger = Ledger()
    run = cycle_run(zone_run(quoted_s=900, base_s=900, watered_s=100))

    ledger.settle(run)

    assert ledger.deficit_s("zone-1") == 800


def test_full_watering_removes_the_deficit_entry() -> None:
    """Matrix "full watering": effective == quoted → the entry is GONE, not zero."""
    ledger = Ledger({"zone-1": 300})
    run = cycle_run(zone_run(quoted_s=900, base_s=600, carried_s=300, watered_s=900))

    ledger.settle(run)

    assert ledger.as_dict() == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {},
        "day_credit": None,
        "rain_baselines": {},
    }


def test_over_watering_never_books_a_negative_deficit() -> None:
    """An unconfirmed close that ran long clamps at 0 — it is not a credit."""
    ledger = Ledger()
    run = cycle_run(zone_run(quoted_s=600, watered_s=900))

    ledger.settle(run)

    assert ledger.deficit_s("zone-1") == 0
    assert ledger.as_dict()["deficits"] == {}


def test_a_zone_absent_from_the_settled_run_is_dropped() -> None:
    """Matrix "zone removed": the ledger is replaced by the settled run's zones."""
    ledger = Ledger({"zone-gone": 600, "zone-1": 100})
    run = cycle_run(zone_run("zone-1", quoted_s=700, carried_s=100, watered_s=700))

    ledger.settle(run)

    assert ledger.deficit_s("zone-gone") == 0
    assert ledger.as_dict()["deficits"] == {}


def test_a_zero_dwell_zone_carries_its_full_deficit() -> None:
    """Matrix "zero-dwell catch-up": opened and closed at one instant is 0 s watered.

    A late `advance` that drains every boundary at once files the zones
    COMPLETED with `actual_start == actual_end`. The ledger does NOT read that
    as watered — fail-wet — so the whole quoted duration carries.
    """
    ledger = Ledger()
    run = cycle_run(zone_run(watered_s=0))

    ledger.settle(run)

    assert run.zone_runs[0].status is ZoneRunStatus.COMPLETED
    assert ledger.deficit_s("zone-1") == 600


def test_settling_the_same_cycle_id_twice_is_a_no_op() -> None:
    """Matrix "replay": the second `settle` returns False and changes nothing.

    Deliberately pinned with a DIFFERENT run object carrying the same id —
    a replay after a reload is not the same Python object.
    """
    ledger = Ledger({"zone-1": 300})
    first = cycle_run(zone_run(quoted_s=900, carried_s=300, watered_s=100))
    replay = cycle_run(zone_run(quoted_s=900, carried_s=300, watered_s=900))

    assert ledger.settle(first) is True
    before = ledger.as_dict()

    assert ledger.settle(replay) is False
    assert (
        ledger.as_dict()
        == before
        == {
            "settled_cycle_id": "2026-07-31-morning",
            "deficits": {"zone-1": 600},
            "day_credit": None,
            "rain_baselines": {},
        }
    )


def test_a_different_cycle_id_settles_again() -> None:
    """Only the LAST settled id is remembered — the next cycle settles normally."""
    ledger = Ledger()
    ledger.settle(cycle_run(zone_run(watered_s=0), cycle_id="2026-07-31-morning"))

    assert (
        ledger.settle(
            cycle_run(
                zone_run(quoted_s=1200, carried_s=600, watered_s=1200),
                cycle_id="2026-07-31-evening",
            ),
        )
        is True
    )
    assert ledger.deficit_s("zone-1") == 0
    assert ledger.settled_cycle_id == "2026-07-31-evening"


def test_a_slot_a_few_hundred_ms_short_settles_to_no_deficit() -> None:
    """Timer jitter is not a shortfall: 599.6 s reads 600 and owes nothing.

    Both instants are real wall-clock timer fires, so an open that landed a
    few hundred ms late spans just under its planned duration. Truncating
    would read 599 and book a spurious 1 s deficit on roughly every other
    healthy zone; `effective_seconds` rounds to the nearest second instead.
    """
    zone = zone_run(watered_s=0)
    zone.actual_end = aware(7) + timedelta(seconds=599, milliseconds=600)
    ledger = Ledger()

    assert effective_seconds(zone) == 600
    ledger.settle(cycle_run(zone))
    assert ledger.as_dict()["deficits"] == {}


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def test_as_dict_from_dict_round_trip() -> None:
    """The journal section rebuilds the same ledger — the restart path."""
    ledger = Ledger(
        {"zone-1": 300, "zone-2": 600},
        settled_cycle_id="2026-07-30-evening",
    )

    rebuilt = Ledger.from_dict(ledger.as_dict())

    assert rebuilt.as_dict() == ledger.as_dict()
    assert rebuilt.deficit_s("zone-2") == 600
    assert rebuilt.settled_cycle_id == "2026-07-30-evening"


def test_from_dict_of_none_is_an_empty_ledger() -> None:
    """A journal written before this story seeds nothing — quotes on base."""
    ledger = Ledger.from_dict(None)

    assert ledger.as_dict() == {
        "settled_cycle_id": None,
        "deficits": {},
        "day_credit": None,
        "rain_baselines": {},
    }
    assert ledger.deficit_s("zone-1") == 0


def test_zero_deficits_are_never_stored() -> None:
    """A zero in the seed (or the constructor) is dropped, not carried around."""
    ledger = Ledger.from_dict({"settled_cycle_id": None, "deficits": {"zone-1": 0}})

    assert ledger.as_dict()["deficits"] == {}


def test_as_dict_returns_a_copy() -> None:
    """Mutating the section must not write through to the ledger (AD-6)."""
    ledger = Ledger({"zone-1": 300})
    section = ledger.as_dict()
    deficits = section["deficits"]
    assert isinstance(deficits, dict)
    deficits["zone-1"] = 1

    assert ledger.deficit_s("zone-1") == 300


# --------------------------------------------------------------------------
# Day credit (Story 2.3): only settle() writes it
# --------------------------------------------------------------------------


def test_a_completed_run_now_with_no_shortfall_credits_its_day() -> None:
    """Manual + COMPLETED + every zone watered its quote → the day is credited.

    The credit names the run-now, so the waived record can point back at it.
    """
    ledger = Ledger()
    run = cycle_run(zone_run("zone-1"), zone_run("zone-2"), manual=True)

    assert ledger.settle(run) is True

    assert ledger.day_credit == "2026-07-31"
    assert ledger.as_dict() == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {},
        "day_credit": CREDIT,
        "rain_baselines": {},
    }


def test_a_scheduled_cycle_never_credits_however_well_it_watered() -> None:
    """The marker is the gate: a perfect scheduled cycle leaves no credit."""
    ledger = Ledger()

    ledger.settle(cycle_run(zone_run("zone-1"), zone_run("zone-2")))

    assert ledger.day_credit is None
    assert ledger.as_dict()["day_credit"] is None


def test_a_run_now_with_a_failed_zone_credits_nothing() -> None:
    """Matrix "partial run-now": one FAILED open is a shortfall, so no credit.

    The status is COMPLETED — the cycle ran its course — but the settlement
    booked a deficit, and "no shortfall" is the rule. Doubt waters.
    """
    ledger = Ledger()
    run = cycle_run(
        zone_run("zone-1"),
        zone_run("zone-2", status=ZoneRunStatus.FAILED, watered_s=0),
        manual=True,
    )

    ledger.settle(run)

    assert ledger.day_credit is None
    assert ledger.deficit_s("zone-2") == 600


def test_a_cancelled_run_now_credits_nothing() -> None:
    """Matrix "cancelled run-now": CANCELLED never credits, and the debt is booked."""
    ledger = Ledger()
    run = cycle_run(
        zone_run("zone-1"),
        zone_run("zone-2", watered_s=120),
        zone_run("zone-3", watered_s=None),
        status=CycleStatus.CANCELLED,
        manual=True,
    )

    ledger.settle(run)

    assert ledger.day_credit is None
    assert ledger.as_dict()["deficits"] == {"zone-2": 480, "zone-3": 600}


def test_a_zero_dwell_run_now_credits_nothing() -> None:
    """A late catch-up that opened and closed at one instant watered nothing.

    It is COMPLETED and manual, but the ledger reads 0 s and books the whole
    quote — the same rule that stops it from clearing a debt stops it from
    crediting the day.
    """
    ledger = Ledger()

    ledger.settle(cycle_run(zone_run(watered_s=0), manual=True))

    assert ledger.day_credit is None
    assert ledger.deficit_s("zone-1") == 600


def test_a_jittery_but_healthy_run_now_still_credits() -> None:
    """599.6 s reads 600 (rounded `effective_seconds`) — no deficit, so a credit.

    Without the rounding, timer jitter would disqualify roughly every other
    healthy run-now from crediting its day.
    """
    zone = zone_run(watered_s=0)
    zone.actual_end = aware(7) + timedelta(seconds=599, milliseconds=600)
    ledger = Ledger()

    ledger.settle(cycle_run(zone, manual=True))

    assert ledger.day_credit == "2026-07-31"


def test_a_settlement_that_credits_nothing_leaves_an_existing_credit_untouched() -> (
    None
):
    """Only `waive` consumes: a later FAILED or scheduled settlement is not a reader.

    A credit recorded at 06:00 must survive whatever settles next without
    asking — otherwise a cancelled second run-now would silently un-credit the
    day the first one watered.
    """
    ledger = Ledger(day_credit=CREDIT)

    ledger.settle(
        cycle_run(
            zone_run(status=ZoneRunStatus.FAILED, watered_s=0),
            cycle_id="2026-07-31-morning-2",
            manual=True,
        ),
    )
    ledger.settle(cycle_run(zone_run(), cycle_id="2026-07-31-evening"))

    assert ledger.as_dict()["day_credit"] == CREDIT


def test_a_later_completed_run_now_refreshes_the_credit() -> None:
    """Matrix "two run-nows same day": ONE credit, naming the second run-now."""
    ledger = Ledger()

    ledger.settle(cycle_run(zone_run(), manual=True))
    ledger.settle(cycle_run(zone_run(), cycle_id="2026-07-31-morning-2", manual=True))

    assert ledger.as_dict()["day_credit"] == {
        "irrigation_day": "2026-07-31",
        "cycle_id": "2026-07-31-morning-2",
    }


def test_a_replayed_settlement_changes_neither_debt_nor_credit() -> None:
    """Matrix "replay": the same cycle id settling again is a no-op for the credit too.

    Pinned with a replay that would NOT qualify (a FAILED zone): were the
    guard missing, the replay would book a debt and — worse for this story —
    the credit would survive next to it.
    """
    ledger = Ledger()
    assert ledger.settle(cycle_run(zone_run(), manual=True)) is True
    replay = cycle_run(zone_run(status=ZoneRunStatus.FAILED, watered_s=0), manual=True)

    assert ledger.settle(replay) is False

    assert ledger.as_dict() == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {},
        "day_credit": CREDIT,
        "rain_baselines": {},
    }


def test_the_credit_is_the_configured_starts_day_not_the_completion_day() -> None:
    """Matrix "past midnight": a run-now started 23:50 credits day D.

    `configured_start` is the plan's evening start on the fire day — the
    same instant the cycle id and the history record key off — so the credit
    files under 2026-07-31 even though every zone closed on 2026-08-01.
    """
    ledger = Ledger()
    zone = zone_run(watered_s=0)
    zone.actual_start = aware(23, 50)
    zone.actual_end = aware(0, day=1, month=8)
    run = CycleRun(
        cycle_id="2026-07-31-evening",
        kind=CycleKind.EVENING,
        configured_start=aware(20),
        scheduled_start=aware(23, 50),
        pump_entity_id="switch.pool_pump",
        zone_runs=(zone,),
        status=CycleStatus.COMPLETED,
        manual=True,
    )

    ledger.settle(run)

    assert ledger.day_credit == "2026-07-31"


# --------------------------------------------------------------------------
# Day credit: only waive() reads it, and reading consumes it
# --------------------------------------------------------------------------


def test_waive_on_the_credited_day_returns_the_run_now_and_clears_the_credit() -> None:
    """Match: the crediting run-now's id comes back, and the credit is gone.

    One credit, one decision — the second scheduled cycle of the same day
    asks and gets nothing.
    """
    ledger = Ledger(day_credit=CREDIT)

    assert ledger.waive(date(2026, 7, 31)) == "2026-07-31-morning"

    assert ledger.day_credit is None
    assert ledger.waive(date(2026, 7, 31)) is None


def test_waive_on_another_day_returns_none_and_clears_the_stale_credit() -> None:
    """Matrix "stale credit": mismatch → the cycle runs AND the credit is discarded.

    Keeping it would need an expiry policy; discarding it at the first
    decision means the operator can only lose a waiver, never water.
    """
    ledger = Ledger(day_credit=CREDIT)

    assert ledger.waive(date(2026, 8, 1)) is None

    assert ledger.day_credit is None
    assert ledger.as_dict()["day_credit"] is None


def test_waive_with_no_credit_is_none_and_harmless() -> None:
    """The common case: nothing credited, the scheduled cycle simply runs."""
    ledger = Ledger()

    assert ledger.waive(date(2026, 7, 31)) is None
    assert ledger.day_credit is None


def test_waive_touches_neither_the_deficits_nor_the_settled_id() -> None:
    """Matrix "outstanding deficit + waiver": the debt stays for the next real cycle.

    A waived cycle is never settled, so whatever the ledger owes before the
    decision it still owes after it — and the replay guard is untouched too.
    """
    ledger = Ledger(
        {"zone-1": 300},
        settled_cycle_id="2026-07-30-evening",
        day_credit=CREDIT,
    )

    ledger.waive(date(2026, 7, 31))

    assert ledger.as_dict() == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300},
        "day_credit": None,
        "rain_baselines": {},
    }


def test_reading_day_credit_consumes_nothing() -> None:
    """The sensor's property is a projection: reading it twice is the same answer."""
    ledger = Ledger(day_credit=CREDIT)

    assert ledger.day_credit == ledger.day_credit == "2026-07-31"
    assert ledger.as_dict()["day_credit"] == CREDIT


# --------------------------------------------------------------------------
# Day credit: the journal round-trip
# --------------------------------------------------------------------------


def test_as_dict_from_dict_round_trip_with_a_credit() -> None:
    """Matrix "restart between credit and cycle": the credit survives the journal."""
    ledger = Ledger({"zone-1": 300}, settled_cycle_id="2026-07-31-morning")
    ledger.settle(cycle_run(zone_run(), cycle_id="2026-07-31-morning-2", manual=True))

    rebuilt = Ledger.from_dict(ledger.as_dict())

    assert rebuilt.as_dict() == ledger.as_dict()
    assert rebuilt.waive(date(2026, 7, 31)) == "2026-07-31-morning-2"


def test_from_dict_of_a_pre_2_3_section_has_no_credit() -> None:
    """A section written before this story has no `day_credit` key — and loads."""
    ledger = Ledger.from_dict({"settled_cycle_id": None, "deficits": {"zone-1": 300}})

    assert ledger.day_credit is None
    assert ledger.deficit_s("zone-1") == 300


def test_from_dict_narrows_a_credit_of_the_wrong_type_to_none() -> None:
    """Type narrowing of the serialized `object` values, not validation.

    The adapter refuses such a section before it gets here; the engine
    merely has to stay type-safe on the values it is handed.
    """
    assert Ledger.from_dict({"day_credit": "2026-07-31"}).day_credit is None
    assert Ledger(day_credit={"irrigation_day": 5, "cycle_id": "x"}).day_credit is None
