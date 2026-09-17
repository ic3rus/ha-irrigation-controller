"""Both DST transitions through the real timers, in Europe/Paris (Story 3.3).

A tod-style suite: the integration's two timing primitives — the daily
`async_track_time_change` starts and the ONE re-armed
`async_track_point_in_time` — driven with the freezer across the 23-hour day
(2027-03-28) and the 25-hour one (2027-10-31).

What is pinned here:

- a wall-clock start OUTSIDE the transition hour keeps its wall-clock time on
  both days — the NFR3 contract of `async_track_time_change`;
- a start INSIDE the spring-forward gap is never fired by the daily tracker
  (02:30 does not exist that day). That silent skip is exactly what the
  missed-cycle watchdog exists to notice: it files the cycle `missed`, raises
  one anomaly and waters it late, on the same irrigation day;
- a start inside the fall-back hour fires TWICE (02:30 happens twice). That is
  existing, deliberately deferred behaviour: the second run is a real cycle
  with a real record, so the watchdog must stay silent about it — no miss, no
  anomaly;
- the watchdog deadline itself is derived from the plan every time, so it is
  right on both a short and a long day.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.adapters.anomalies import AnomalyManager
from custom_components.ha_irrigation_controller.adapters.journal import JournalAdapter
from custom_components.ha_irrigation_controller.adapters.switches import (
    VerifiedSwitchAdapter,
)
from custom_components.ha_irrigation_controller.adapters.timing import (
    CycleRunner,
    HaClock,
)
from custom_components.ha_irrigation_controller.const import DOMAIN
from custom_components.ha_irrigation_controller.engine.plan import (
    ControllerPlan,
    ZoneSpec,
)
from custom_components.ha_irrigation_controller.engine.runs import CycleStatus
from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer
from tests.common import (
    PUMP,
    VALVE_1,
    VALVE_2,
    fire_at,
    register_switch_domain,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant, ServiceCall

# Both transition days, as Europe/Paris lives them.
SPRING_FORWARD = "2027-03-28"  # 02:00 → 03:00; 02:30 never happens
FALL_BACK = "2027-10-31"  # 03:00 → 02:00; 02:30 happens twice


def dst_plan(morning_start: time) -> ControllerPlan:
    """Two zones, ten minutes each; `morning_start` local, evening 20:00."""
    return ControllerPlan(
        pump_entity_id=PUMP,
        morning_enabled=True,
        morning_start=morning_start,
        evening_start=time(20, 0),
        zones=(
            ZoneSpec(
                zone_id="zone-1",
                name="Front Lawn",
                valve_entity_id=VALVE_1,
                morning_duration_s=600,
                evening_duration_s=600,
                rain_exposed=True,
                rain_factor=1.0,
            ),
            ZoneSpec(
                zone_id="zone-2",
                name="Back Beds",
                valve_entity_id=VALVE_2,
                morning_duration_s=600,
                evening_duration_s=600,
                rain_exposed=True,
                rain_factor=1.0,
            ),
        ),
    )


def make_runner(
    hass: HomeAssistant,
    plan: ControllerPlan,
    *,
    history: list[dict[str, object]] | None = None,
) -> tuple[CycleRunner, Sequencer, MockConfigEntry]:
    """Wire a runner over the real adapters, exactly as `async_setup_entry` does."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    sequencer = Sequencer(
        plan,
        switches=VerifiedSwitchAdapter(
            hass,
            timeout_s=5,
            cycle_id_provider=lambda: (
                sequencer.current_run.cycle_id if sequencer.current_run else None
            ),
        ),
        journal=JournalAdapter(hass),
        anomalies=AnomalyManager(hass, entry),
        history=history,
    )
    return (
        CycleRunner(hass, entry, sequencer=sequencer, clock=HaClock()),
        sequencer,
        entry,
    )


def commands(calls: list[ServiceCall]) -> list[tuple[str, str]]:
    """Return the (service, entity_id) sequence the fake hardware received."""
    return [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls]


# --------------------------------------------------------------------------
# Spring forward: 2027-03-28 is 23 hours long
# --------------------------------------------------------------------------


async def test_a_start_outside_the_gap_holds_its_wall_clock_time(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """NFR3: a 07:00 cycle fires at 07:00 local on the short day too."""
    calls = register_switch_domain(hass)
    freezer.move_to("2027-03-27 21:00:00+01:00")
    runner, sequencer, _ = make_runner(hass, dst_plan(time(7, 0)))
    await runner.async_start()

    await fire_at(hass, freezer, f"{SPRING_FORWARD} 07:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.configured_start.isoformat() == f"{SPRING_FORWARD}T07:00:00+02:00"
    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]
    # The next intent is this cycle's own boundary, not the watchdog's.
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        f"{SPRING_FORWARD} 07:10:00+02:00"
    )

    runner.async_shutdown()


async def test_a_start_inside_the_gap_is_skipped_and_the_watchdog_waters_it(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 3.3 AC 6: the spring-forward skip is noticed and made up the same day.

    02:30 never happens on 2027-03-28, so `async_track_time_change` does not
    fire and no cycle is ever requested — the silent skip Epic 3's context
    parks on this story. The plan's window still closes (02:30+01:00 is
    03:30 on the wall clock, so the two zones end at 03:50), the ONE re-armed
    timer serves that deadline, and the watchdog files the cycle `missed` and
    re-dispatches it at once.
    """
    calls = register_switch_domain(hass)
    freezer.move_to(f"{SPRING_FORWARD} 00:30:00+01:00")
    runner, sequencer, _ = make_runner(hass, dst_plan(time(2, 30)))
    await runner.async_start()

    # The watchdog owns the idle timer, and its deadline is the window end.
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        f"{SPRING_FORWARD} 03:50:00+02:00"
    )

    # The transition happens; the daily tracker never fires.
    await fire_at(hass, freezer, f"{SPRING_FORWARD} 03:40:00+02:00")
    assert calls == []
    assert sequencer.current_run is None

    await fire_at(hass, freezer, f"{SPRING_FORWARD} 03:50:00+02:00")

    run = sequencer.current_run
    assert run is not None
    assert run.late_rerun is True
    assert run.cycle_id == f"{SPRING_FORWARD}-morning-2"
    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "missed_cycle")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["outcome"] == "rerun"

    # It waters on the SAME irrigation day it was scheduled for.
    assert run.configured_start.date().isoformat() == SPRING_FORWARD

    await fire_at(hass, freezer, f"{SPRING_FORWARD} 04:00:00+02:00")
    await fire_at(hass, freezer, f"{SPRING_FORWARD} 04:10:00+02:00")
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED

    runner.async_shutdown()


# --------------------------------------------------------------------------
# Fall back: 2027-10-31 is 25 hours long
# --------------------------------------------------------------------------


async def test_a_start_outside_the_repeated_hour_holds_its_wall_clock_time(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """NFR3, the other direction: 07:00 is 07:00 on the long day as well."""
    calls = register_switch_domain(hass)
    freezer.move_to("2027-10-30 21:00:00+02:00")
    runner, sequencer, _ = make_runner(hass, dst_plan(time(7, 0)))
    await runner.async_start()

    await fire_at(hass, freezer, f"{FALL_BACK} 07:00:00+01:00")

    run = sequencer.current_run
    assert run is not None
    assert run.configured_start.isoformat() == f"{FALL_BACK}T07:00:00+01:00"
    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]

    runner.async_shutdown()


async def test_the_watchdog_deadlines_of_the_twenty_five_hour_day_are_the_plans(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Every deadline is re-derived from the plan, so the extra hour costs nothing.

    A stored "now + 24 h" would name 06:20 on the morning after the long day.
    """
    register_switch_domain(hass)
    freezer.move_to("2027-10-30 21:00:00+02:00")
    runner, sequencer, _ = make_runner(hass, dst_plan(time(7, 0)))
    await runner.async_start()

    # Tomorrow's morning window end, across the transition.
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        f"{FALL_BACK} 07:20:00+01:00"
    )
    freezer.move_to(f"{FALL_BACK} 08:00:00+01:00")
    # ...then that same (long) day's evening window end...
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        f"{FALL_BACK} 20:20:00+01:00"
    )
    freezer.move_to(f"{FALL_BACK} 21:00:00+01:00")
    # ...then the next day's morning, still at its wall-clock time.
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2027-11-01 07:20:00+01:00"
    )

    runner.async_shutdown()


async def test_a_start_inside_the_repeated_hour_fires_twice_and_raises_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The fall-back double fire: deliberately deferred, and silent to the watchdog.

    02:30 happens twice on 2027-10-31, so the daily tracker requests the
    morning cycle twice and two real cycles run. That over-watering is bounded
    and still parked in `deferred-work.md`; what Story 3.3 owes is that the
    watchdog never ALSO reads the day as missed — history has the records, so
    no `missed` entry and no anomaly are produced.
    """
    calls = register_switch_domain(hass)
    freezer.move_to(f"{FALL_BACK} 01:30:00+02:00")
    runner, sequencer, _ = make_runner(hass, dst_plan(time(2, 30)))
    await runner.async_start()

    # First pass through 02:30 (+02:00), run to completion.
    await fire_at(hass, freezer, f"{FALL_BACK} 02:30:00+02:00")
    await fire_at(hass, freezer, f"{FALL_BACK} 02:40:00+02:00")
    await fire_at(hass, freezer, f"{FALL_BACK} 02:50:00+02:00")
    first = sequencer.last_run
    assert first is not None
    assert first.cycle_id == f"{FALL_BACK}-morning"
    assert first.status is CycleStatus.COMPLETED

    # Second pass through 02:30, one real hour later (+01:00).
    await fire_at(hass, freezer, f"{FALL_BACK} 02:30:00+01:00")
    await fire_at(hass, freezer, f"{FALL_BACK} 02:40:00+01:00")
    await fire_at(hass, freezer, f"{FALL_BACK} 02:50:00+01:00")

    second = sequencer.last_run
    assert second is not None
    assert second.cycle_id == f"{FALL_BACK}-morning-2"
    assert second.status is CycleStatus.COMPLETED
    assert len(commands(calls)) == 12  # two complete cycles, nothing more
    # The watchdog saw two records for the day and said nothing at all.
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missed_cycle") is None

    runner.async_shutdown()
