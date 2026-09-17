"""Timing adapter: daily starts + the ONE re-armed point-in-time timer (AC 1).

The runner is the same loop `tests/engine/` drives on a virtual clock, with
`async_track_point_in_time` in place of `VirtualClock.advance_to`:

    arm ONE timer at sequencer.next_wakeup(dt_util.now())
      → on fire: await sequencer.advance(dt_util.now())
      → re-arm
"""

from __future__ import annotations

import asyncio
import logging
from datetime import time, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import CoreState
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ha_irrigation_controller.adapters import timing
from custom_components.ha_irrigation_controller.adapters.anomalies import AnomalyManager
from custom_components.ha_irrigation_controller.adapters.journal import JournalAdapter
from custom_components.ha_irrigation_controller.adapters.rain import (
    RainSensorAdapter,
)
from custom_components.ha_irrigation_controller.adapters.switches import (
    VerifiedSwitchAdapter,
)
from custom_components.ha_irrigation_controller.adapters.timing import (
    CycleRunner,
    HaClock,
)
from custom_components.ha_irrigation_controller.const import (
    CONF_ACTUATION_TIMEOUT,
    DOMAIN,
    EVENT_HA_IRRIGATION_CONTROLLER,
    engine_state_signal,
)
from custom_components.ha_irrigation_controller.engine.plan import (
    ControllerPlan,
    CycleKind,
    ZoneSpec,
)
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleRun,
    CycleStatus,
    ZoneRunStatus,
)
from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer
from tests.common import (
    CONTROLLER_OPTIONS,
    PUMP,
    VALVE_1,
    VALVE_2,
    crashed_during_zone_a,
    fire_at,
    history_record,
    register_switch_domain,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant, ServiceCall


def make_plan(*, morning_enabled: bool = True) -> ControllerPlan:
    """Build a two-zone plan: morning 07:00, ten minutes per zone."""
    return ControllerPlan(
        pump_entity_id=PUMP,
        morning_enabled=morning_enabled,
        morning_start=time(7, 0),
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


def missed_issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    """Return the controller-level `missed_cycle` Repairs issue, if one is open."""
    return ir.async_get(hass).async_get_issue(DOMAIN, "missed_cycle")


def day_done(day: str) -> list[dict[str, object]]:
    """Return the history a fully-watered `day` leaves behind (both cycles).

    Story 3.3's watchdog reads an empty history as "this day's windows closed
    with nothing to show for them" and rightly makes the cycles up. A test
    whose clock starts AFTER a window end therefore seeds the day as done
    unless the miss is what it is about.
    """
    return [history_record(kind, day=day) for kind in ("morning", "evening")]


def make_runner(
    hass: HomeAssistant,
    plan: ControllerPlan,
    *,
    rain: RainSensorAdapter | None = None,
    history: list[dict[str, object]] | None = None,
) -> tuple[CycleRunner, Sequencer]:
    """Wire a runner over the real adapters, exactly as `async_setup_entry` does.

    `rain` is the gauge adapter (Story 2.4); None is an entry with no gauge.
    `history` is the journal's 7-day section (Story 3.3: what the watchdog
    reads to tell a missed cycle from a performed one).
    """
    entry = MockConfigEntry(domain="ha_irrigation_controller")
    clock = HaClock()
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
        rain=rain,
        history=history,
    )
    return CycleRunner(hass, entry, sequencer=sequencer, clock=clock), sequencer


async def test_daily_start_fires_the_cycle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The configured wall-clock start opens the first zone (AC 1)."""
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]

    runner.async_shutdown()


async def test_a_disabled_morning_cycle_never_fires(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """`morning_enabled` governs whether the morning daily start is armed."""
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan(morning_enabled=False))
    await runner.async_start()

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    assert sequencer.current_run is None
    assert calls == []

    runner.async_shutdown()


async def test_a_full_cycle_drives_the_real_switch_services_in_order(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Pump → zones strictly sequential → pump off, through real service calls."""
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_on", VALVE_2),
        ("turn_off", VALVE_2),
        ("turn_off", PUMP),
    ]
    assert sequencer.current_run is None
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.COMPLETED
    # Every command of one cycle carries that cycle's single Context (AD-7).
    assert len({call.context.id for call in calls}) == 1

    runner.async_shutdown()


async def test_the_rain_gauge_reduces_the_second_cycle_through_the_real_timers(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.4, end to end through the timing adapter: two cycles, the second reduced.

    The morning at 12.0 mm waters both zones in full and banks the reading.
    The evening at 15.5 mm quotes each zone 390 s: the timer fires at
    20:06:30 (zone 1 closes, zone 2 opens) and 20:13:00 (cycle complete) —
    the re-armed wake-ups follow the reduced windows, and the service calls
    prove the valves really switched at those instants.
    """
    calls = register_switch_domain(hass)
    hass.states.async_set("sensor.rain_gauge", "12.0", {"unit_of_measurement": "mm"})
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(
        hass,
        make_plan(),
        rain=RainSensorAdapter(hass, "sensor.rain_gauge"),
    )
    await runner.async_start()

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")
    first = sequencer.last_run
    assert first is not None
    assert first.rain_total_mm == 12.0
    assert [zone.rain_credit_s for zone in first.zone_runs] == [0, 0]
    assert sequencer.ledger.as_dict()["rain_baselines"] == {
        "zone-1": 12.0,
        "zone-2": 12.0,
    }
    calls.clear()

    hass.states.async_set("sensor.rain_gauge", "15.5", {"unit_of_measurement": "mm"})
    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    run = sequencer.current_run
    assert run is not None
    assert (run.kind, run.status) == (CycleKind.EVENING, CycleStatus.RUNNING)
    assert run.rain_total_mm == 15.5
    assert [(zone.duration_s, zone.rain_credit_s) for zone in run.zone_runs] == [
        (390, 210),
        (390, 210),
    ]
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]

    # One second before the reduced boundary nothing moves; at it, zone 1
    # closes and zone 2 opens.
    await fire_at(hass, freezer, "2026-07-31 20:06:29+02:00")
    assert len(calls) == 2
    await fire_at(hass, freezer, "2026-07-31 20:06:30+02:00")
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls][2:] == [
        ("turn_off", VALVE_1),
        ("turn_on", VALVE_2),
    ]
    await fire_at(hass, freezer, "2026-07-31 20:13:00+02:00")

    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls][4:] == [
        ("turn_off", VALVE_2),
        ("turn_off", PUMP),
    ]
    assert sequencer.current_run is None
    second = sequencer.last_run
    assert second is not None
    assert second.status is CycleStatus.COMPLETED
    assert sequencer.ledger.as_dict()["rain_baselines"] == {
        "zone-1": 15.5,
        "zone-2": 15.5,
    }

    runner.async_shutdown()


async def test_at_most_one_point_in_time_registration_at_any_instant(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """AC 1: intra-cycle wake-ups go through exactly ONE re-armed timer.

    A second live registration would break AD-3 and the Epic 3 stories that
    hang their own time intents off this single callback.
    """
    live = 0
    peak = 0
    # getattr: the name is an import inside the module, not an export, and
    # strict mypy rejects reading a non-exported attribute directly.
    real = getattr(timing, "async_track_point_in_time")  # noqa: B009

    def _tracked(*args: Any, **kwargs: Any) -> Callable[[], None]:
        nonlocal live, peak
        unsubscribe = real(*args, **kwargs)
        live += 1
        peak = max(peak, live)

        def _wrapped() -> None:
            nonlocal live
            live -= 1
            unsubscribe()

        return _wrapped

    monkeypatch.setattr(timing, "async_track_point_in_time", _tracked)

    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, _ = make_runner(hass, make_plan())
    await runner.async_start()

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert peak == 1
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert peak == 1
    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    # The completed cycle leaves no BOUNDARY armed — what the one live handle
    # now holds is Story 3.3's watchdog deadline on the evening window end,
    # which is exactly the point of AD-3: two intents, one registration.
    assert peak == 1
    assert live == 1

    runner.async_shutdown()
    assert live == 0


async def test_daily_start_holds_its_wall_clock_time_across_a_dst_change(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """NFR3: the cycle fires at 07:00 LOCAL on the spring-forward day too.

    Europe/Paris moves +01:00 → +02:00 on 2027-03-28. A stored "now + 24 h"
    would fire an hour late in local terms; `async_track_time_change` is the
    only DST-safe way to express a daily wall-clock start.
    """
    calls = register_switch_domain(hass)
    freezer.move_to("2027-03-27 20:30:00+01:00")
    # The 27th is already watered, so the watchdog has nothing to make up for
    # and the only commands below are the 28th's own daily start (Story 3.3).
    runner, sequencer = make_runner(hass, make_plan(), history=day_done("2027-03-27"))
    await runner.async_start()

    await fire_at(hass, freezer, "2027-03-28 07:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]

    runner.async_shutdown()


async def test_each_engine_step_pushes_the_dispatcher_signal(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Entities are fed by dispatcher after each step (AD-6, AC 5)."""
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(domain="ha_irrigation_controller")
    clock = HaClock()
    sequencer = Sequencer(
        make_plan(),
        switches=VerifiedSwitchAdapter(
            hass,
            timeout_s=5,
            cycle_id_provider=lambda: None,
        ),
        journal=JournalAdapter(hass),
        anomalies=AnomalyManager(hass, entry),
    )
    runner = CycleRunner(hass, entry, sequencer=sequencer, clock=clock)
    pushes = 0

    def _on_signal() -> None:
        nonlocal pushes
        pushes += 1

    # Subscribed AFTER the start: `async_start` reconciles and pushes once
    # itself (Story 3.2), and these tests count the pushes of the STEPS.
    await runner.async_start()
    unsubscribe = async_dispatcher_connect(
        hass,
        engine_state_signal(entry.entry_id),
        _on_signal,
    )

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert pushes == 1
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert pushes == 2

    unsubscribe()
    runner.async_shutdown()


async def test_shutdown_cancels_every_timer_and_is_idempotent(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Unload must leave nothing armed — the reload regime runs this constantly.

    PHCC's `verify_cleanup` enforces it implicitly; asserted here explicitly
    because a surviving daily timer would otherwise only show as a confusing
    lingering-timer report in an unrelated test.
    """
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    runner.async_shutdown()
    runner.async_shutdown()  # idempotent: unload can reach it twice

    commanded = len(calls)
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    # Neither the re-armed zone boundary nor tomorrow's daily start survives.
    assert len(calls) == commanded
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING


async def test_shutdown_during_an_in_flight_advance_arms_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Unloading mid-step must not let that step re-arm behind shutdown's back.

    A verified service call takes real time, so an unload (every config change
    reloads the entry) can land while `advance` is still awaiting one. Without
    the shutdown guard the in-flight step re-arms afterwards and the entry
    leaks one timer per reload — which PHCC's `verify_cleanup` reports as a
    lingering timer in whatever test runs next.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())

    calls: list[ServiceCall] = []

    async def _handle(call: ServiceCall) -> None:
        # Unload lands while the engine is mid-transition, awaiting this call.
        calls.append(call)
        runner.async_shutdown()
        state = STATE_ON if call.service == "turn_on" else STATE_OFF
        hass.states.async_set(call.data[ATTR_ENTITY_ID], state, context=call.context)

    hass.services.async_register("switch", "turn_on", _handle)
    hass.services.async_register("switch", "turn_off", _handle)
    for entity_id in (PUMP, VALVE_1, VALVE_2):
        hass.states.async_set(entity_id, STATE_OFF)

    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    # The cycle really did start (the step ran to completion) — pump on, first
    # valve open — and a zone boundary is still pending...
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert sequencer.next_wakeup(dt_util.now()) is not None
    commanded = len(calls)

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    # ...but nothing was left armed to serve it: no further command at all.
    assert len(calls) == commanded


async def test_a_wakeup_already_in_the_past_still_converges(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """A late fire drains every elapsed boundary and re-arms to nothing.

    `async_track_point_in_time` fires a past instant immediately, so the loop
    converges instead of stalling — the known late-`advance()` behavior
    (deferred to Epic 3) must at least terminate.
    """
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()

    # One fire, far past the end of the whole cycle.
    await fire_at(hass, freezer, "2026-07-31 09:00:00+02:00")

    assert [call.service for call in calls] == [
        "turn_on",
        "turn_on",
        "turn_off",
        "turn_on",
        "turn_off",
        "turn_off",
    ]
    assert sequencer.current_run is None
    # Re-armed to the watchdog's next window end, never to a past boundary.
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2026-07-31 20:20:00+02:00"
    )

    runner.async_shutdown()


async def test_the_runner_arms_the_watchdog_deadline_while_idle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """An idle runner arms the daily starts and the ONE watchdog deadline.

    Before Story 3.3 an idle engine named no time intent at all; now it names
    the next cycle-window end, still through the single point-in-time
    registration. Nothing is COMMANDED while idle, which is what this checks:
    the handle exists, it is 20 minutes away, and a tick 30 s later serves it
    no more than it serves a zone boundary that has not arrived.
    """
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())

    await runner.async_start()

    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert calls == []
    assert sequencer.current_run is None
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2026-07-31 07:20:00+02:00"
    )

    runner.async_shutdown()


# The configurable minimum (`MIN_ACTUATION_TIMEOUT_S`): the shortest wait a
# real entry can be configured with, so the wired timeout test stays quick.
UNCONFIRMED_TIMEOUT_S = 1


async def drain_asyncio_timers(hass: HomeAssistant) -> None:
    """Let the loop run the `asyncio.timeout` handles that are already due.

    `async_block_till_done` waits on hass-tracked tasks; the adapter's timeout
    is a plain loop timer, so a few explicit yields are what actually give the
    loop the iterations it needs to fire it.
    """
    for _ in range(5):
        await asyncio.sleep(0)
    await hass.async_block_till_done()


async def test_an_exception_from_advance_still_rearms_the_timer(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """A raise must never be what disarms the ONE timer (AC 1).

    This handle has ALREADY fired, so the re-arm alone keeps the loop alive.
    Without the `finally`, anything the engine does not swallow leaves the
    cycle with no timer at all and the open valve and pump energized until a
    daily start that only DEFERS the next cycle rather than closing them.
    """
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    real_advance = sequencer.advance

    async def _boom(now: datetime) -> None:
        msg = f"engine blew up mid-transition at {now}"
        raise RuntimeError(msg)

    sequencer.advance = _boom  # type: ignore[method-assign]

    # Driven through the fire callback directly rather than through a timer:
    # an exception raised inside a loop callback is unretrievable, and the
    # point here is the `finally`, not how HA logs the failure.
    freezer.move_to("2026-07-31 07:10:00+02:00")
    with pytest.raises(RuntimeError, match="blew up"):
        await runner._async_fire(dt_util.now())  # noqa: SLF001 — the failure path

    sequencer.advance = real_advance  # type: ignore[method-assign]
    # Nothing was commanded by the failed step...
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]

    # ...but the surviving registration is what lets the very next tick drain
    # the boundary for real. Without the `finally` no timer is left to fire.
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_on", VALVE_2),
    ]

    runner.async_shutdown()


def register_hanging_valve(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> list[ServiceCall]:
    """Switch services where VALVE_1 accepts the command but never flips.

    The real shape of an unconfirmed actuation: no error, no state change —
    only the adapter's timeout can end the wait.

    The handler pushes the clock past that timeout, because `asyncio.timeout`
    is scheduled on the event loop's clock and the freezer holds it still: an
    unmoved clock would hang the wait rather than expire it.
    """
    calls: list[ServiceCall] = []

    async def _handle(call: ServiceCall) -> None:
        calls.append(call)
        if call.data[ATTR_ENTITY_ID] == VALVE_1 and call.service == "turn_on":
            freezer.tick(timedelta(seconds=UNCONFIRMED_TIMEOUT_S + 1))
            return
        state = STATE_ON if call.service == "turn_on" else STATE_OFF
        hass.states.async_set(call.data[ATTR_ENTITY_ID], state, context=call.context)

    hass.services.async_register("switch", "turn_on", _handle)
    hass.services.async_register("switch", "turn_off", _handle)
    for entity_id in (PUMP, VALVE_1):
        hass.states.async_set(entity_id, STATE_OFF)
    return calls


async def test_an_unconfirmed_actuation_raises_an_anomaly_and_the_cycle_continues(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 3 end to end, through the timeout branch and the wired adapters.

    The unit suites cover the adapter's timeout and the manager's fan-out
    separately; this joins them on the real setup path — a valve that never
    confirms must produce the anomaly AND leave the cycle running fail-wet.

    Uses the configurable minimum so the wait is short; the hanging handler
    moves the clock past it.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    events: list[Event] = []
    unsubscribe = hass.bus.async_listen(EVENT_HA_IRRIGATION_CONTROLLER, events.append)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options={**CONTROLLER_OPTIONS, CONF_ACTUATION_TIMEOUT: UNCONFIRMED_TIMEOUT_S},
        subentries_data=[zone_subentry_data("Zone A", VALVE_1)],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # After the setup: forwarding Platform.SWITCH registers the REAL switch
    # services, and `async_register` is last-wins (see `register_switch_domain`).
    calls = register_hanging_valve(hass, freezer)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await drain_asyncio_timers(hass)
    unsubscribe()

    anomalies = entry.runtime_data.anomalies
    assert AnomalyKind.VALVE_OPEN_UNCONFIRMED in {
        record.kind for record in anomalies.open_anomalies
    }
    last = anomalies.last_anomaly
    assert last is not None
    assert last.context["entity_id"] == VALVE_1
    assert [event.data["anomaly"] for event in events] == ["valve_open_unconfirmed"]

    # Fail-wet: the slot is consumed, not aborted — the pump stayed on and the
    # cycle is still running with its zone commanded.
    run = entry.runtime_data.sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.zone_runs[0].status is ZoneRunStatus.FAILED
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_season_off_makes_a_daily_start_arm_nothing_at_all(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """AC 1/2: the daily tracker stays armed; the engine's gate is what refuses.

    A season toggle must NOT tear down or re-register the daily trackers (a
    second arming path breaks AD-3), so the proof is on the other side: the
    start really fires, and it creates no run and arms no point-in-time handle.
    """
    live = 0
    real = getattr(timing, "async_track_point_in_time")  # noqa: B009 — an import, not an export

    def _tracked(*args: Any, **kwargs: Any) -> Callable[[], None]:
        nonlocal live
        unsubscribe = real(*args, **kwargs)
        live += 1

        def _wrapped() -> None:
            nonlocal live
            live -= 1
            unsubscribe()

        return _wrapped

    monkeypatch.setattr(timing, "async_track_point_in_time", _tracked)

    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    # The morning is seeded as already watered: the watchdog would otherwise
    # (rightly) make it up the moment the season comes back on, and what this
    # test is about is the daily START being gated, not the make-up.
    runner, sequencer = make_runner(
        hass, make_plan(), history=[history_record("morning")]
    )
    await runner.async_start()
    await runner.async_set_season(enabled=False)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    assert sequencer.current_run is None
    assert calls == []
    # Nothing armed at all: the season gate refuses the run, and the watchdog
    # names no deadline while it could only ever find nothing (Story 3.3).
    assert live == 0
    assert sequencer.next_wakeup(dt_util.now()) is None

    # ...and turning it back on honours the very next daily start — the same
    # day's evening one, so the assertion needs no clock jump across a second
    # morning tracker.
    await runner.async_set_season(enabled=True)
    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    assert run.kind is CycleKind.EVENING
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]

    runner.async_shutdown()


async def test_a_season_turned_back_on_mid_day_makes_up_at_the_next_window_end(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 3.3: the make-up waits for a window end — it never rides the toggle.

    The season was off across the 07:00 window, so nothing ran and nothing
    recorded it; the engine keeps no memory of WHEN it was off, so once it is
    back on that morning reads as missed like any other. What this pins is
    WHEN: turning the switch on commands nothing and arms the watchdog's next
    deadline, and the make-up happens there — at the evening window end,
    where the evening's own start has already run and the morning is the
    day's earlier cycle, so it is recorded rather than watered a second time.
    """
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()
    await runner.async_set_season(enabled=False)
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert calls == []

    freezer.move_to("2026-07-31 12:00:00+02:00")
    await runner.async_set_season(enabled=True)

    # Nothing is checked, commanded or recorded by the toggle itself...
    assert calls == []
    assert sequencer.current_run is None
    assert missed_issue(hass) is None
    # ...only the deadline on the evening window end is armed.
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2026-07-31 20:20:00+02:00"
    )

    # The evening runs normally from its own daily start...
    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 20:10:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 20:20:00+02:00")

    # ...and the window end that follows records the morning without
    # watering it: the day's later cycle has already run.
    last = sequencer.last_run
    assert last is not None
    assert last.kind is CycleKind.EVENING
    assert sequencer.current_run is None
    issue = missed_issue(hass)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["outcome"] == "recorded"
    assert issue.translation_placeholders["kind"] == "morning"
    # Nothing is booked for it, and deliberately so: a cycle that runs its
    # planned duration finishes at the very instant its window closes, so the
    # deadline lands while the evening run is still live — and a debt written
    # there would be replaced by that run's own settlement moments later.
    assert sequencer.ledger.settled_cycle_id == "2026-07-31-evening"
    assert sequencer.ledger.as_dict()["deficits"] == {}

    runner.async_shutdown()


async def test_cancel_drives_the_real_switch_services_and_leaves_nothing_armed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """AC 4 end to end: live valve then pump, through the real service calls.

    The re-arm half is asserted with the registration counter rather than
    assumed: after a cancel `next_wakeup()` is `None`, so `_rearm` must cancel
    the point-in-time handle — "re-armed to nothing".
    """
    live = 0
    real = getattr(timing, "async_track_point_in_time")  # noqa: B009 — an import, not an export

    def _tracked(*args: Any, **kwargs: Any) -> Callable[[], None]:
        nonlocal live
        unsubscribe = real(*args, **kwargs)
        live += 1

        def _wrapped() -> None:
            nonlocal live
            live -= 1
            unsubscribe()

        return _wrapped

    monkeypatch.setattr(timing, "async_track_point_in_time", _tracked)

    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert live == 1

    freezer.move_to("2026-07-31 07:04:00+02:00")
    assert await runner.async_cancel_cycle() is True

    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
    ]
    assert sequencer.current_run is None
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.CANCELLED
    # No boundary of the cancelled cycle survives; the ONE handle now holds
    # the watchdog's deadline, and the cancelled record makes it a no-op.
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2026-07-31 07:20:00+02:00"
    )
    assert live == 1

    # The zone boundary the cancelled cycle would have had never fires.
    commanded = len(calls)
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert len(calls) == commanded

    runner.async_shutdown()


async def test_cancelling_with_nothing_running_reports_false(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The runner relays the engine's answer; the SERVICE is what raises."""
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, _ = make_runner(hass, make_plan())
    await runner.async_start()

    assert await runner.async_cancel_cycle() is False

    assert calls == []

    runner.async_shutdown()


async def test_run_now_starts_in_the_same_tick_and_arms_the_next_boundary(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.1: the runner's `advance` is what makes run-now immediate.

    The new run is dispatched at `now`, so without the advance the pump would
    wait for a re-armed timer to fire on an instant already in the past. The
    boundary that follows is armed by the same `finally` every other command
    uses, and the cycle then runs itself out through the ONE timer.
    """
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 09:30:00+02:00")
    # 09:30 is past the morning window, so the day is seeded as watered: the
    # operator's run-now is what this test is about, not a watchdog make-up.
    runner, sequencer = make_runner(hass, make_plan(), history=day_done("2026-07-31"))
    await runner.async_start()

    assert await runner.async_run_now(CycleKind.MORNING) is True

    run = sequencer.current_run
    assert run is not None
    assert run.manual is True
    assert run.status is CycleStatus.RUNNING
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2026-07-31 09:40:00+02:00",
    )

    await fire_at(hass, freezer, "2026-07-31 09:40:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 09:50:00+02:00")

    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_on", VALVE_2),
        ("turn_off", VALVE_2),
        ("turn_off", PUMP),
    ]
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert finished.manual is True
    # Idle again: the ONE handle holds the watchdog's evening deadline.
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2026-07-31 20:20:00+02:00"
    )

    runner.async_shutdown()


async def test_a_refused_run_now_reports_false_and_leaves_the_timer_alone(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The runner relays the engine's answer; the SERVICE is what raises.

    The unconditional `advance` must not disturb the live cycle: the boundary
    it was armed for is still the boundary it is armed for afterwards, and no
    valve was commanded in between.
    """
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    live = sequencer.current_run
    commanded = len(calls)
    wakeup = sequencer.next_wakeup(dt_util.now())

    freezer.move_to("2026-07-31 07:04:00+02:00")
    assert await runner.async_run_now(CycleKind.EVENING) is False

    assert sequencer.current_run is live
    assert len(calls) == commanded
    assert sequencer.next_wakeup(dt_util.now()) == wakeup

    # And the boundary the live cycle was waiting for still fires.
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert len(calls) == commanded + 2

    runner.async_shutdown()


async def test_run_now_fires_the_pending_reload_once_the_cycle_completes(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """Story 1.7's deferral covers a manual cycle exactly as a scheduled one.

    The `_async_maybe_reload` in the new command's `finally` is what makes the
    refused path fire an already-pending reload immediately — a config change
    must not wait behind a cycle that never started.
    """
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 09:30:00+02:00")
    reloads = spy_reloads(hass, monkeypatch)
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()

    await runner.async_run_now(CycleKind.MORNING)
    runner.async_request_reload()
    assert reloads == []
    assert runner.reload_pending is True

    await fire_at(hass, freezer, "2026-07-31 09:40:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 09:50:00+02:00")

    assert sequencer.current_run is None
    assert len(reloads) == 1
    assert runner.reload_pending is False

    runner.async_shutdown()


async def test_a_command_wrapper_rearms_even_when_the_engine_raises(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The same `finally` rule the fire callbacks follow (AC 1, AC 4).

    An exception must never be what leaves the engine with no timer: the
    cancel raising here would otherwise strand the running cycle with its
    valve and pump energized and nothing armed to close them.
    """
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    async def _boom(now: datetime) -> bool:
        msg = f"engine blew up cancelling at {now}"
        raise RuntimeError(msg)

    sequencer.async_cancel_cycle = _boom  # type: ignore[method-assign]

    freezer.move_to("2026-07-31 07:04:00+02:00")
    with pytest.raises(RuntimeError, match="blew up"):
        await runner.async_cancel_cycle()

    del sequencer.async_cancel_cycle

    # The surviving registration is what still drains the boundary for real.
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].actual_end is not None

    runner.async_shutdown()


async def test_a_command_wrapper_pushes_the_dispatcher_signal(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Both surfaces feed the passive projections the same way (AD-6)."""
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(domain="ha_irrigation_controller")
    sequencer = Sequencer(
        make_plan(),
        switches=VerifiedSwitchAdapter(
            hass,
            timeout_s=5,
            cycle_id_provider=lambda: None,
        ),
        journal=JournalAdapter(hass),
        anomalies=AnomalyManager(hass, entry),
    )
    runner = CycleRunner(hass, entry, sequencer=sequencer, clock=HaClock())
    pushes = 0

    def _on_signal() -> None:
        nonlocal pushes
        pushes += 1

    # Subscribed AFTER the start: `async_start` reconciles and pushes once
    # itself (Story 3.2), and these tests count the pushes of the STEPS.
    await runner.async_start()
    unsubscribe = async_dispatcher_connect(
        hass,
        engine_state_signal(entry.entry_id),
        _on_signal,
    )

    # `async_dispatcher_send` schedules the non-callback target, so the block
    # is what actually delivers it — not an assertion timing quirk.
    await runner.async_set_season(enabled=False)
    await hass.async_block_till_done()
    assert pushes == 1

    # Even a no-op set still pushes: the wrapper's `finally` is unconditional,
    # and an entity that missed a push would be stale until the next step.
    await runner.async_set_season(enabled=False)
    await hass.async_block_till_done()
    assert pushes == 2

    await runner.async_cancel_cycle()
    await hass.async_block_till_done()
    assert pushes == 3

    unsubscribe()
    runner.async_shutdown()


# --------------------------------------------------------------------------
# Deferred reload and suspend (Story 1.7)
# --------------------------------------------------------------------------


def spy_reloads(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace `async_schedule_reload` with a recorder.

    The runner-level entries are never added to hass, so the real method would
    raise `UnknownEntry`; what these tests pin is WHEN the runner asks for a
    reload, not what HA does with it.
    """
    reloads: list[str] = []

    def _spy(entry_id: str) -> None:
        reloads.append(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", _spy)
    return reloads


def make_entry_runner(
    hass: HomeAssistant,
    plan: ControllerPlan,
) -> tuple[CycleRunner, Sequencer, MockConfigEntry]:
    """`make_runner`, also returning the entry whose id the reload must carry."""
    entry = MockConfigEntry(domain="ha_irrigation_controller")
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
    )
    runner = CycleRunner(hass, entry, sequencer=sequencer, clock=HaClock())
    return runner, sequencer, entry


async def test_request_reload_while_idle_reloads_at_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """Nothing to protect: an idle request is the pre-1.7 immediate reload."""
    reloads = spy_reloads(hass, monkeypatch)
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, _, entry = make_entry_runner(hass, make_plan())
    await runner.async_start()

    runner.async_request_reload()

    assert reloads == [entry.entry_id]
    assert runner.reload_pending is False

    runner.async_shutdown()


async def test_request_reload_while_busy_waits_for_the_cycle_to_complete(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """AC 1: no reload before `_complete_cycle`, exactly one after it.

    The flag is visible while it waits (`reload_pending`) and cleared by the
    very step that completes the cycle, so the sensor's last push of that
    cycle already reads false.
    """
    reloads = spy_reloads(hass, monkeypatch)
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer, entry = make_entry_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    runner.async_request_reload()
    assert reloads == []
    assert runner.reload_pending is True

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    # A zone boundary is not the end of the cycle.
    assert reloads == []
    assert runner.reload_pending is True

    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")
    assert sequencer.current_run is None
    assert [call.service for call in calls][-1] == "turn_off"
    assert reloads == [entry.entry_id]
    assert runner.reload_pending is False

    runner.async_shutdown()


async def test_several_requests_during_one_cycle_coalesce_into_one_reload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """Three edits, one flag, one reload."""
    reloads = spy_reloads(hass, monkeypatch)
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, _, entry = make_entry_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    for _ in range(3):
        runner.async_request_reload()
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    assert reloads == [entry.entry_id]

    runner.async_shutdown()


async def test_a_pending_run_counts_as_busy(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """A PENDING run holds a snapshot too — the reload waits for it as well."""
    reloads = spy_reloads(hass, monkeypatch)
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer, _ = make_entry_runner(hass, make_plan())
    await runner.async_start()
    # Requested at 06:59 for the 07:00 start: PENDING until then.
    await sequencer.request_cycle(CycleKind.MORNING, dt_util.now())
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.PENDING

    runner.async_request_reload()

    assert reloads == []
    assert runner.reload_pending is True

    runner.async_shutdown()


async def test_replan_rearms_the_daily_starts_from_the_new_plan(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """A start time edited mid-cycle is honoured the same day, not skipped.

    The daily trackers were armed from the old plan; after the listener swaps
    the plan it calls `async_replan`, and an evening start moved to 07:15 —
    inside the running morning cycle — must request the evening cycle at
    07:15 (deferred behind the morning one) and run it with the new plan once
    the morning cycle completes. Still exactly one point-in-time registration.
    """
    live = 0
    real = getattr(timing, "async_track_point_in_time")  # noqa: B009 — an import, not an export

    def _tracked(*args: Any, **kwargs: Any) -> Callable[[], None]:
        nonlocal live
        unsubscribe = real(*args, **kwargs)
        live += 1

        def _wrapped() -> None:
            nonlocal live
            live -= 1
            unsubscribe()

        return _wrapped

    monkeypatch.setattr(timing, "async_track_point_in_time", _tracked)
    reloads = spy_reloads(hass, monkeypatch)
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer, entry = make_entry_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    runner.async_request_reload()
    new_plan = ControllerPlan(
        pump_entity_id=PUMP,
        morning_enabled=True,
        morning_start=time(7, 0),
        evening_start=time(7, 15),
        zones=(
            ZoneSpec(
                zone_id="zone-1",
                name="Front Lawn",
                valve_entity_id=VALVE_1,
                morning_duration_s=600,
                evening_duration_s=120,
                rain_exposed=True,
                rain_factor=1.0,
            ),
        ),
    )
    sequencer.plan = new_plan
    runner.async_replan()

    await fire_at(hass, freezer, "2026-07-31 07:15:00+02:00")
    assert sequencer.deferred_kinds == (CycleKind.EVENING,)
    assert live == 1

    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")
    # The morning cycle completed and the evening one started from the NEW
    # plan (one zone, two minutes) — the reload still waits for it.
    evening = sequencer.current_run
    assert evening is not None
    assert evening.kind is CycleKind.EVENING
    assert [zone.duration_s for zone in evening.zone_runs] == [120]
    assert reloads == []
    assert live == 1

    await fire_at(hass, freezer, "2026-07-31 07:22:00+02:00")
    assert sequencer.current_run is None
    assert reloads == [entry.entry_id]
    # One live registration throughout — now the watchdog's deadline rather
    # than a cycle boundary, still exactly one (AD-3).
    assert live == 1
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls][-3:] == [
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
    ]

    runner.async_shutdown()


async def test_replan_after_shutdown_arms_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Re-arming behind an unload would leak a daily timer per reload."""
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer, _ = make_entry_runner(hass, make_plan())
    await runner.async_start()
    runner.async_shutdown()

    runner.async_replan()

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert calls == []
    assert sequencer.current_run is None


async def test_cancel_fires_the_pending_reload(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """The cancel wrapper's `finally` is the same tail: cancel, then the reload."""
    reloads = spy_reloads(hass, monkeypatch)
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer, entry = make_entry_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    runner.async_request_reload()

    freezer.move_to("2026-07-31 07:04:00+02:00")
    assert await runner.async_cancel_cycle() is True

    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls][-2:] == [
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
    ]
    assert sequencer.current_run is None
    assert reloads == [entry.entry_id]
    assert runner.reload_pending is False

    runner.async_shutdown()


async def test_a_pending_reload_never_fires_after_shutdown(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """The shutdown guard covers the reload too, not only the re-arm.

    The unload lands while the LAST step of the cycle is in flight (awaiting
    the pump-off): that step completes the cycle after shutdown ran, and it
    must schedule nothing — a reload from a dying runner would race the one
    replacing it.
    """
    reloads = spy_reloads(hass, monkeypatch)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer, _ = make_entry_runner(hass, make_plan())

    async def _handle(call: ServiceCall) -> None:
        if call.service == "turn_off" and call.data[ATTR_ENTITY_ID] == PUMP:
            runner.async_shutdown()
        state = STATE_ON if call.service == "turn_on" else STATE_OFF
        hass.states.async_set(call.data[ATTR_ENTITY_ID], state, context=call.context)

    hass.services.async_register("switch", "turn_on", _handle)
    hass.services.async_register("switch", "turn_off", _handle)
    for entity_id in (PUMP, VALVE_1, VALVE_2):
        hass.states.async_set(entity_id, STATE_OFF)
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    runner.async_request_reload()
    assert runner.reload_pending is True

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    assert sequencer.current_run is None
    assert reloads == []


async def test_request_reload_after_shutdown_is_a_no_op(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """A request reaching a shut-down runner neither reloads nor flags."""
    reloads = spy_reloads(hass, monkeypatch)
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, _, _ = make_entry_runner(hass, make_plan())
    await runner.async_start()
    runner.async_shutdown()

    runner.async_request_reload()

    assert reloads == []
    assert runner.reload_pending is False


async def test_request_reload_while_busy_pushes_the_dispatcher_signal_at_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """The sensor attribute flips the moment the edit lands, not at the next step.

    Idempotent: a second request while already pending pushes nothing new.
    """
    spy_reloads(hass, monkeypatch)
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, _, entry = make_entry_runner(hass, make_plan())
    pushes = 0

    def _on_signal() -> None:
        nonlocal pushes
        pushes += 1

    # Subscribed AFTER the start: `async_start` reconciles and pushes once
    # itself (Story 3.2), and these tests count the pushes of the STEPS.
    await runner.async_start()
    unsubscribe = async_dispatcher_connect(
        hass,
        engine_state_signal(entry.entry_id),
        _on_signal,
    )
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert pushes == 1

    runner.async_request_reload()
    await hass.async_block_till_done()
    assert pushes == 2

    runner.async_request_reload()
    await hass.async_block_till_done()
    assert pushes == 2

    unsubscribe()
    runner.async_shutdown()


async def test_suspend_drives_the_real_switch_services_and_arms_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """Forced unload mid-cycle: valve then pump off, timers gone, run untouched.

    The registration counter proves "arms nothing"; the bus proves the
    interruption was reported; the run's status proves nothing was mutated.
    """
    live = 0
    real = getattr(timing, "async_track_point_in_time")  # noqa: B009 — an import, not an export

    def _tracked(*args: Any, **kwargs: Any) -> Callable[[], None]:
        nonlocal live
        unsubscribe = real(*args, **kwargs)
        live += 1

        def _wrapped() -> None:
            nonlocal live
            live -= 1
            unsubscribe()

        return _wrapped

    monkeypatch.setattr(timing, "async_track_point_in_time", _tracked)
    events: list[Event] = []
    unsubscribe_bus = hass.bus.async_listen(
        EVENT_HA_IRRIGATION_CONTROLLER,
        events.append,
    )

    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer, _ = make_entry_runner(hass, make_plan())
    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert live == 1

    freezer.move_to("2026-07-31 07:04:00+02:00")
    assert await runner.async_suspend() is True
    await hass.async_block_till_done()
    unsubscribe_bus()

    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
    ]
    assert live == 0
    assert [event.data["anomaly"] for event in events] == ["cycle_interrupted"]
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING

    # Neither the zone boundary nor a daily start survives the suspend.
    commanded = len(calls)
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    assert len(calls) == commanded


async def test_suspend_while_idle_is_a_no_op_that_still_shuts_down(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Our own deferred reload always unloads idle: False, no command, timers gone."""
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, _, _ = make_entry_runner(hass, make_plan())
    await runner.async_start()

    assert await runner.async_suspend() is False

    assert calls == []
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert calls == []


async def test_suspend_during_an_in_flight_advance_arms_nothing_and_reloads_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """Unloading mid-step: the step must not re-arm, reopen or reload behind it.

    Modelled on the shutdown-during-advance test. The suspend lands while the
    engine is awaiting the first valve's confirmation, with a reload already
    pending; `async_shutdown()` runs before the suspend waits for the lock,
    so when the in-flight step finishes its `finally` arms nothing and
    schedules nothing — and only then does the suspend close what the step
    opened.
    """
    live = 0
    real = getattr(timing, "async_track_point_in_time")  # noqa: B009 — an import, not an export

    def _tracked(*args: Any, **kwargs: Any) -> Callable[[], None]:
        nonlocal live
        unsubscribe = real(*args, **kwargs)
        live += 1

        def _wrapped() -> None:
            nonlocal live
            live -= 1
            unsubscribe()

        return _wrapped

    monkeypatch.setattr(timing, "async_track_point_in_time", _tracked)
    reloads = spy_reloads(hass, monkeypatch)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer, _ = make_entry_runner(hass, make_plan())
    calls: list[ServiceCall] = []
    suspends: list[asyncio.Task[bool]] = []

    async def _handle(call: ServiceCall) -> None:
        calls.append(call)
        if call.service == "turn_on" and call.data[ATTR_ENTITY_ID] == VALVE_1:
            # An edit landed, then the unload — both while this call is in flight.
            runner.async_request_reload()
            suspends.append(hass.async_create_task(runner.async_suspend()))
        state = STATE_ON if call.service == "turn_on" else STATE_OFF
        hass.states.async_set(call.data[ATTR_ENTITY_ID], state, context=call.context)

    hass.services.async_register("switch", "turn_on", _handle)
    hass.services.async_register("switch", "turn_off", _handle)
    for entity_id in (PUMP, VALVE_1, VALVE_2):
        hass.states.async_set(entity_id, STATE_OFF)

    await runner.async_start()
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert len(suspends) == 1
    assert await suspends[0] is True

    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
    ]
    assert live == 0
    assert reloads == []
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING

    commanded = len(calls)
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert len(calls) == commanded


def test_ha_clock_returns_aware_local_time(hass: HomeAssistant) -> None:
    """The engine's contract: aware, HA-local — never `datetime.now()`."""
    moment = HaClock().now()

    assert moment.tzinfo is not None
    assert moment.utcoffset() is not None


def test_the_runner_imports_no_second_timer_primitive() -> None:
    """AD-3 guard: the module's namespace holds only the two allowed timers.

    An `async_call_later`, an interval tracker or a `time.sleep` sneaking in
    would be a second, feature-owned timing authority.
    """
    # `getattr` rather than attribute access: these names are imports, not
    # part of the module's public surface, and strict mypy rejects reading a
    # non-exported attribute — which is exactly what this test must do.
    assert getattr(timing, "async_track_point_in_time") is (  # noqa: B009
        async_track_point_in_time
    )
    assert getattr(timing, "async_track_time_change") is (  # noqa: B009
        async_track_time_change
    )
    for forbidden in (
        "async_call_later",
        "async_track_time_interval",
        "async_track_utc_time_change",
        "asyncio",
        "sleep",
    ):
        assert not hasattr(timing, forbidden), forbidden


# --------------------------------------------------------------------------
# Startup reconciliation before anything is armed (Story 3.2)
# --------------------------------------------------------------------------


def make_restored_runner(
    hass: HomeAssistant,
) -> tuple[CycleRunner, Sequencer, MockConfigEntry]:
    """`make_entry_runner` with the crash-during-zone-A run restored from the journal.

    The plan's zone ids are `zone-1`/`zone-2` while the journaled run says
    `zone-a`/`zone-b` — deliberately: a resumed run is its OWN snapshot
    (AD-8), the plan is never consulted for it.
    """
    entry = MockConfigEntry(domain="ha_irrigation_controller")
    sequencer = Sequencer(
        make_plan(),
        switches=VerifiedSwitchAdapter(
            hass,
            timeout_s=5,
            cycle_id_provider=lambda: (
                sequencer.current_run.cycle_id if sequencer.current_run else None
            ),
        ),
        journal=JournalAdapter(hass),
        anomalies=AnomalyManager(hass, entry),
        run=CycleRun.from_dict(
            crashed_during_zone_a(),
            tz=dt_util.get_default_time_zone(),
        ),
    )
    runner = CycleRunner(hass, entry, sequencer=sequencer, clock=HaClock())
    return runner, sequencer, entry


def spy_arming(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    """Record every timer registration as "arm" in `order`, pass it through."""
    for name in ("async_track_point_in_time", "async_track_time_change"):
        real = getattr(timing, name)

        def _tracked(*args: Any, _real: Any = real, **kwargs: Any) -> Any:
            order.append("arm")
            return _real(*args, **kwargs)

        monkeypatch.setattr(timing, name, _tracked)


async def test_start_reconciles_before_arming_any_timer(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
) -> None:
    """AC 1: orphans closed and the run resynced BEFORE a daily start or re-arm exists.

    HA is running (a reload): the reconcile is immediate. The order of
    operations is recorded — the engine's reconcile first, every arming
    after — and the resumed boundary then fires through the ONE timer.
    """
    order: list[str] = []
    spy_arming(monkeypatch, order)
    calls = register_switch_domain(hass)
    hass.states.async_set(PUMP, STATE_ON)
    hass.states.async_set(VALVE_1, STATE_ON)
    freezer.move_to("2026-07-31 07:04:00+02:00")
    runner, sequencer, _ = make_restored_runner(hass)
    real_reconcile = sequencer.async_reconcile

    async def _spied(now: datetime) -> None:
        order.append("reconcile")
        await real_reconcile(now)

    monkeypatch.setattr(sequencer, "async_reconcile", _spied)
    assert sequencer.reconciled is False

    await runner.async_start()

    assert order[0] == "reconcile"
    assert set(order[1:]) == {"arm"}
    assert len(order) >= 3  # daily start(s) + the point-in-time re-arm
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.recovery == "resumed"
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2026-07-31 07:10:00+02:00"
    )

    # The re-armed boundary serves the resumed cycle: zone A closes, zone B
    # (the snapshot's `switch.zone_2_valve`) opens.
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls][4:] == [
        ("turn_off", VALVE_1),
        ("turn_on", VALVE_2),
    ]

    runner.async_shutdown()


async def test_start_waits_for_homeassistant_started_during_boot(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """During boot nothing happens until `EVENT_HOMEASSISTANT_STARTED`.

    The governed switches may still read `unknown` while HA starts, and
    `hass.is_running` is True while STARTING too, so the gate is the explicit
    RUNNING state. Before the event: no command, no timer, the run untouched
    and the engine's gate closed. After it: the reconcile, then the arming.
    """
    calls = register_switch_domain(hass)
    hass.states.async_set(PUMP, STATE_ON)
    hass.states.async_set(VALVE_1, STATE_ON)
    freezer.move_to("2026-07-31 07:04:00+02:00")
    hass.set_state(CoreState.starting)
    runner, sequencer, _ = make_restored_runner(hass)

    await runner.async_start()
    await hass.async_block_till_done()

    assert calls == []
    assert sequencer.reconciled is False
    run = sequencer.current_run
    assert run is not None
    assert run.recovery is None
    # No daily start and no boundary exists yet: a tick serves nothing.
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert calls == []

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert sequencer.reconciled is True
    assert [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls] == [
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2026-07-31 07:10:00+02:00"
    )

    hass.set_state(CoreState.running)
    runner.async_shutdown()


async def test_an_unload_before_homeassistant_started_leaves_no_listener(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Shutdown cancels the started-listener: a later start event reaches nothing."""
    baseline = hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STARTED, 0)
    calls = register_switch_domain(hass)
    hass.states.async_set(PUMP, STATE_ON)
    freezer.move_to("2026-07-31 07:04:00+02:00")
    hass.set_state(CoreState.starting)
    runner, sequencer, _ = make_restored_runner(hass)
    await runner.async_start()
    assert (
        hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STARTED, 0) == baseline + 1
    )

    runner.async_shutdown()
    runner.async_shutdown()  # idempotent, like every unload hook

    assert hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STARTED, 0) == baseline
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert calls == []
    assert sequencer.reconciled is False
    hass.set_state(CoreState.running)


async def test_a_reconcile_that_raises_is_logged_and_the_timers_are_still_armed(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    paris: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A defect in the reconciler must not fail the setup (AD-4): log, arm, water.

    The engine defends every port call, so an escaping exception is a bug;
    the one thing it may not do is stop the integration from loading.
    """
    order: list[str] = []
    spy_arming(monkeypatch, order)
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 07:04:00+02:00")
    runner, sequencer, _ = make_restored_runner(hass)

    async def _boom(now: datetime) -> None:
        msg = f"reconciler blew up at {now}"
        raise RuntimeError(msg)

    monkeypatch.setattr(sequencer, "async_reconcile", _boom)

    await runner.async_start()

    # Both daily starts armed (morning and evening); no point-in-time handle,
    # since the engine reports no wake-up while the gate still holds the run.
    assert order == ["arm", "arm"]
    assert sequencer.next_wakeup(dt_util.now()) is None
    errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and "Startup reconciliation failed" in record.getMessage()
    ]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "reconciler blew up" in caplog.text

    runner.async_shutdown()


async def test_shutdown_during_an_in_flight_reconcile_arms_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """An unload landing mid-reconcile must not let its tail arm a daily start.

    Modelled on the in-flight `advance` test: the first verified close of
    the orphan pass is where the unload lands. The reconcile runs to its
    end (the hardware is made safe), but the `finally` tail arms nothing,
    so the plan's next daily start commands nothing.
    """
    freezer.move_to("2026-07-31 07:04:00+02:00")
    hass.set_state(CoreState.starting)
    runner, sequencer, _ = make_restored_runner(hass)
    calls: list[ServiceCall] = []

    async def _handle(call: ServiceCall) -> None:
        calls.append(call)
        if call.service == "turn_off" and len(calls) == 1:
            runner.async_shutdown()
        state = STATE_ON if call.service == "turn_on" else STATE_OFF
        hass.states.async_set(call.data[ATTR_ENTITY_ID], state, context=call.context)

    hass.services.async_register("switch", "turn_on", _handle)
    hass.services.async_register("switch", "turn_off", _handle)
    hass.states.async_set(PUMP, STATE_ON)
    hass.states.async_set(VALVE_1, STATE_ON)
    hass.states.async_set(VALVE_2, STATE_OFF)
    await runner.async_start()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)

    # The reconcile ran to completion behind the shutdown...
    assert sequencer.reconciled is True
    assert calls[0].service == "turn_off"
    commanded = len(calls)
    assert commanded >= 2

    # ...but nothing was armed: the evening start fires into nothing.
    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    assert len(calls) == commanded
    assert sequencer.deferred_kinds == ()

    hass.set_state(CoreState.running)


async def test_start_pushes_the_dispatcher_signal_once_after_recovery(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The recovery is visible on the entities at once: the start tail pushes.

    Two signals for a recovery: the anomaly manager's own on the
    `cycle_recovered` report, and the runner's in the start tail — the one
    that carries the resumed run's status to the cycle sensor.
    """
    register_switch_domain(hass)
    hass.states.async_set(PUMP, STATE_ON)
    hass.states.async_set(VALVE_1, STATE_ON)
    freezer.move_to("2026-07-31 07:04:00+02:00")
    runner, _, entry = make_restored_runner(hass)
    pushes = 0

    def _on_signal() -> None:
        nonlocal pushes
        pushes += 1

    unsubscribe = async_dispatcher_connect(
        hass,
        engine_state_signal(entry.entry_id),
        _on_signal,
    )

    await runner.async_start()
    await hass.async_block_till_done()

    assert pushes == 2

    unsubscribe()
    runner.async_shutdown()
