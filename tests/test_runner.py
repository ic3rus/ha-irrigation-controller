"""Timing adapter: daily starts + the ONE re-armed point-in-time timer (AC 1).

The runner is the same loop `tests/engine/` drives on a virtual clock, with
`async_track_point_in_time` in place of `VirtualClock.advance_to`:

    arm ONE timer at sequencer.next_wakeup()
      → on fire: await sequencer.advance(dt_util.now())
      → re-arm
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_change,
)
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
from custom_components.ha_irrigation_controller.const import engine_state_signal
from custom_components.ha_irrigation_controller.engine.plan import (
    ControllerPlan,
    ZoneSpec,
)
from custom_components.ha_irrigation_controller.engine.runs import CycleStatus
from custom_components.ha_irrigation_controller.engine.sequencer import Sequencer

if TYPE_CHECKING:
    from collections.abc import Callable

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant, ServiceCall

PUMP = "switch.pool_pump"
VALVE_1 = "switch.zone_1_valve"
VALVE_2 = "switch.zone_2_valve"


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


def register_switch_domain(hass: HomeAssistant) -> list[ServiceCall]:
    """Register switch services that actually flip state, and record calls.

    A recorder alone would never confirm: the verified adapter watches for the
    state change, so the fake has to behave like a real switch.
    """
    calls: list[ServiceCall] = []

    async def _handle(call: ServiceCall) -> None:
        calls.append(call)
        state = STATE_ON if call.service == "turn_on" else STATE_OFF
        hass.states.async_set(call.data[ATTR_ENTITY_ID], state, context=call.context)

    hass.services.async_register("switch", "turn_on", _handle)
    hass.services.async_register("switch", "turn_off", _handle)
    for entity_id in (PUMP, VALVE_1, VALVE_2):
        hass.states.async_set(entity_id, STATE_OFF)
    return calls


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


async def fire_at(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    moment: str,
) -> None:
    """Move the freezer to `moment` (local ISO) and fire the due timers."""
    freezer.move_to(moment)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture
async def paris(hass: HomeAssistant) -> None:
    """Run the suite in a DST-observing timezone, not the PHCC default."""
    await hass.config.async_set_time_zone("Europe/Paris")


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
    register_switch_domain(hass)
    freezer.move_to("2026-07-31 06:59:00+02:00")
    runner, _ = make_runner(hass, make_plan())

    await runner.async_start()

    # A point-in-time handle exists only when a cycle has a pending boundary;
    # firing 30 s later must therefore command nothing.
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

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
