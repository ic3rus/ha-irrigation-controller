"""Timing adapter: daily starts + the ONE re-armed point-in-time timer (AC 1).

The runner is the same loop `tests/engine/` drives on a virtual clock, with
`async_track_point_in_time` in place of `VirtualClock.advance_to`:

    arm ONE timer at sequencer.next_wakeup()
      → on fire: await sequencer.advance(dt_util.now())
      → re-arm
"""

from __future__ import annotations

import asyncio
from datetime import time, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
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
    CycleStatus,
    ZoneRunStatus,
)
from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer
from tests.common import (
    CONTROLLER_OPTIONS,
    PUMP,
    VALVE_1,
    VALVE_2,
    fire_at,
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


def make_runner(
    hass: HomeAssistant,
    plan: ControllerPlan,
) -> tuple[CycleRunner, Sequencer]:
    """Wire a runner over the real adapters, exactly as `async_setup_entry` does."""
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
        anomalies=AnomalyManager(hass),
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

    # The completed cycle leaves nothing armed: next_wakeup() is None.
    assert peak == 1
    assert live == 0

    runner.async_shutdown()


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
    runner, sequencer = make_runner(hass, make_plan())
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
        anomalies=AnomalyManager(hass),
    )
    runner = CycleRunner(hass, entry, sequencer=sequencer, clock=clock)
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
    assert sequencer.next_wakeup() is not None
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
    assert sequencer.next_wakeup() is None

    runner.async_shutdown()


async def test_the_runner_arms_nothing_while_idle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Starting an idle runner arms the daily starts only (no point-in-time)."""
    calls = register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, sequencer = make_runner(hass, make_plan())

    await runner.async_start()

    # A point-in-time handle exists only when a cycle has a pending boundary;
    # firing 30 s later must therefore command nothing.
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert calls == []
    assert sequencer.current_run is None
    assert sequencer.next_wakeup() is None

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
    assert AnomalyKind.VALVE_OPEN_UNCONFIRMED in anomalies.open_anomalies
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
    runner, sequencer = make_runner(hass, make_plan())
    await runner.async_start()
    await runner.async_set_season(enabled=False)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    assert sequencer.current_run is None
    assert calls == []
    assert live == 0
    assert sequencer.next_wakeup() is None

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
    assert sequencer.next_wakeup() is None
    assert live == 0

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
        anomalies=AnomalyManager(hass),
    )
    runner = CycleRunner(hass, entry, sequencer=sequencer, clock=HaClock())
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
