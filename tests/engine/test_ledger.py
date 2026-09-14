"""Pure unit tests of the water-debt ledger (Story 2.2, AD-5).

No clock, no sequencer, no Home Assistant: `Ledger` is arithmetic over plan
zones and run objects, and every matrix row that is ledger-only is pinned
here — the caps at both ends, replay, zero drop, the removed zone, the dict
round-trip and the (empty) rain slot.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.ha_irrigation_controller.engine.ledger import (
    NO_RAIN_CREDIT,
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
    watered_s: int | None = 600,
    status: ZoneRunStatus = ZoneRunStatus.COMPLETED,
) -> ZoneRun:
    """Build one finished zone slot that watered `watered_s` seconds.

    `watered_s=None` leaves the slot never reached (PENDING, no instants) —
    what a cancel leaves behind for the zones after the live one.
    """
    if watered_s is None:
        return ZoneRun(
            zone_id=zone_id,
            name=zone_id,
            valve_entity_id=VALVE,
            duration_s=quoted_s,
            base_s=base_s,
            carried_s=carried_s,
            planned_start=aware(7),
            planned_end=aware(7, 10),
        )
    return ZoneRun(
        zone_id=zone_id,
        name=zone_id,
        valve_entity_id=VALVE,
        duration_s=quoted_s,
        base_s=base_s,
        carried_s=carried_s,
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
) -> CycleRun:
    """Build one terminal cycle run carrying `zones`."""
    return CycleRun(
        cycle_id=cycle_id,
        kind=CycleKind.MORNING,
        configured_start=aware(7),
        scheduled_start=aware(7),
        pump_entity_id="switch.pool_pump",
        zone_runs=zones,
        status=status,
    )


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
    }


def test_a_deficit_for_a_zone_not_in_the_plan_is_never_quoted() -> None:
    """Matrix "zone removed": the ledger only speaks about the zones it is asked for."""
    ledger = Ledger({"zone-gone": 600})

    quotes = ledger.quote((make_zone("zone-1"),), CycleKind.MORNING)

    assert [quote.zone_id for quote in quotes] == ["zone-1"]
    assert quotes[0].carried_s == 0


def test_the_rain_credit_slot_defaults_to_zero_and_subtracts_when_given() -> None:
    """The formula has a rain slot (Story 2.4 fills it); today it is empty.

    Pinned so 2.4 cannot change the arithmetic by accident: the credit comes
    off the base BEFORE the deficit is added, and it never drives the base
    part below zero.
    """
    ledger = Ledger({"zone-1": 100})
    zones = (make_zone("zone-1", morning_s=600),)

    assert NO_RAIN_CREDIT == {}
    (plain,) = ledger.quote(zones, CycleKind.MORNING)
    (credited,) = ledger.quote(zones, CycleKind.MORNING, {"zone-1": 200})
    (soaked,) = ledger.quote(zones, CycleKind.MORNING, {"zone-1": 5000})

    assert (plain.rain_credit_s, plain.quoted_s) == (0, 700)
    assert (credited.rain_credit_s, credited.quoted_s) == (200, 500)
    assert (soaked.rain_credit_s, soaked.quoted_s) == (5000, 100)


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

    assert ledger.as_dict() == {"settled_cycle_id": None, "deficits": {}}
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
