"""Smoke tests: config entry sets up and unloads cleanly, version guard fails loudly."""

from __future__ import annotations

import asyncio
import logging
from datetime import time
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
)
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller import (
    HaIrrigationRuntimeData,
    _async_entry_updated,
    config_fingerprint,
)
from custom_components.ha_irrigation_controller.adapters.anomalies import AnomalyManager
from custom_components.ha_irrigation_controller.adapters.journal import (
    STORAGE_KEY,
    JournalAdapter,
)
from custom_components.ha_irrigation_controller.adapters.switches import (
    VerifiedSwitchAdapter,
)
from custom_components.ha_irrigation_controller.adapters.timing import CycleRunner
from custom_components.ha_irrigation_controller.const import (
    CONF_ACTUATION_TIMEOUT,
    CONF_EVENING_START,
    CONF_MANUAL_TIMEOUT,
    CONF_MORNING_DURATION,
    CONF_NOTIFY_TARGET,
    CONF_PUMP_SWITCH,
    CONF_RAIN_SENSOR,
    DEFAULT_ACTUATION_TIMEOUT_S,
    DEFAULT_MANUAL_TIMEOUT_MINUTES,
    DOMAIN,
    EVENT_HA_IRRIGATION_CONTROLLER,
    MAX_ACTUATION_TIMEOUT_S,
    MAX_MANUAL_TIMEOUT_MINUTES,
    MAX_RAIN_FACTOR,
    MAX_ZONE_DURATION_MINUTES,
    MIN_ACTUATION_TIMEOUT_S,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
    MIN_MANUAL_TIMEOUT_MINUTES,
    MIN_RAIN_FACTOR,
    MIN_ZONE_DURATION_MINUTES,
)
from custom_components.ha_irrigation_controller.engine.config import (
    PlanValidationError,
    build_plan,
    parse_actuation_timeout,
)
from custom_components.ha_irrigation_controller.engine.plan import (
    ControllerPlan,
    CycleKind,
)
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from custom_components.ha_irrigation_controller.engine.runs import CycleStatus
from custom_components.ha_irrigation_controller.engine.sequencer import (
    JOURNAL_SCHEMA_VERSION,
    Sequencer,
)
from tests.common import (
    CONTROLLER_OPTIONS,
    PUMP,
    VALVE_1,
    VALVE_2,
    controller_entry,
    fire_at,
    register_notify_domain,
    register_switch_domain,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant, ServiceCall

_MODULE = "custom_components.ha_irrigation_controller"


def _patch_ha_version(
    monkeypatch: pytest.MonkeyPatch,
    major: int,
    minor: int,
    version: str,
) -> None:
    """Pin the HA version the setup guard sees."""
    monkeypatch.setattr(f"{_MODULE}.HA_MAJOR_VERSION", major)
    monkeypatch.setattr(f"{_MODULE}.HA_MINOR_VERSION", minor)
    monkeypatch.setattr(f"{_MODULE}.HA_VERSION", version)


async def test_setup_and_unload_entry(hass: HomeAssistant) -> None:
    """A config entry sets up, exposes runtime_data, and unloads cleanly."""
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, HaIrrigationRuntimeData)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # mypy narrowed entry.state to LOADED above and cannot see that async_unload
    # mutates it, hence the ignore.
    assert entry.state is ConfigEntryState.NOT_LOADED  # type: ignore[comparison-overlap]


async def test_setup_wires_the_engine_and_its_adapters(hass: HomeAssistant) -> None:
    """runtime_data carries the engine and the four adapters (Story 1.5, AC 1-5).

    Everything here is rebuilt per load; none of it is a persistence authority
    (AD-2 — only the journal adapter writes the Store).
    """
    entry = _entry_with_zones(zone_subentry_data("Zone A", "switch.zone_a_valve"))
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    data = entry.runtime_data
    assert isinstance(data.sequencer, Sequencer)
    assert isinstance(data.runner, CycleRunner)
    assert isinstance(data.journal, JournalAdapter)
    assert isinstance(data.anomalies, AnomalyManager)
    # The engine runs on the plan the setup validated — one plan, one home.
    assert data.sequencer.plan is data.plan
    assert data.sequencer.current_run is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_configured_timeout_reaches_the_switch_adapter(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3's timeout is configurable only if the parsed value gets wired in.

    The parser, the selector bounds and the adapter are each covered on their
    own; this pins the wire between them — and, since Story 3.4, the two
    manual-override providers alongside it. Line coverage cannot: the
    construction runs on every setup whatever the values are, and the adapter
    is deliberately not exposed on `runtime_data`.
    """
    configured_s = MAX_ACTUATION_TIMEOUT_S - 1
    captured: list[int] = []
    governed: list[Callable[[], tuple[str, ...]]] = []
    observers: list[Callable[[str, bool, bool], None]] = []

    def _spy(
        hass_: HomeAssistant,
        *,
        timeout_s: int,
        cycle_id_provider: Callable[[], str | None],
        governed_entities: Callable[[], tuple[str, ...]],
        on_observed: Callable[[str, bool, bool], None],
    ) -> VerifiedSwitchAdapter:
        captured.append(timeout_s)
        governed.append(governed_entities)
        observers.append(on_observed)
        return VerifiedSwitchAdapter(
            hass_,
            timeout_s=timeout_s,
            cycle_id_provider=cycle_id_provider,
            governed_entities=governed_entities,
            on_observed=on_observed,
        )

    monkeypatch.setattr(f"{_MODULE}.VerifiedSwitchAdapter", _spy)
    entry = controller_entry(
        {**CONTROLLER_OPTIONS, CONF_ACTUATION_TIMEOUT: configured_s},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert captured == [configured_s]
    # Both Story 3.4 providers are passed BY KEYWORD (the spy's signature is
    # the assertion) and answer the live plan: this controller has no zone,
    # so the governed set is its pump alone.
    assert len(governed) == 1
    assert governed[0]() == (CONTROLLER_OPTIONS[CONF_PUMP_SWITCH],)
    assert len(observers) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_unload_removes_the_platform_entities_and_leaves_no_timer(
    hass: HomeAssistant,
) -> None:
    """Unload tears the platform down; PHCC's verify_cleanup covers the timers.

    A surviving daily `async_track_time_change` registration would leak on
    every reload, and the reload-per-config-change regime runs constantly.
    """
    entry = _entry_with_zones(zone_subentry_data("Zone A", "switch.zone_a_valve"))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_cycle_status",
    )
    live = hass.states.async_all("sensor")
    assert len(live) == 2  # controller status + the zone's duration
    assert all(state.state != STATE_UNAVAILABLE for state in live)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    # The registry keeps the entities (a reload restores them); their states
    # go unavailable, which is what "the platform was torn down" looks like.
    assert all(
        state.state == STATE_UNAVAILABLE for state in hass.states.async_all("sensor")
    )


async def test_setup_emits_no_deprecation_report(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Setup (controller + zone devices) trips no Home Assistant deprecation.

    For custom integrations `frame.report_usage` only LOGS a deprecated call
    (the beta CI leg is `continue-on-error`), so nothing else would flag the
    next `async_get_or_create` deprecation before its removal release. The
    `via_device` → `via_device_id` migration is what this guards.
    """
    entry = _entry_with_zones(zone_subentry_data("Zone A", VALVE_1))
    entry.add_to_hass(hass)
    caplog.set_level(logging.WARNING)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reports = [
        record.getMessage()
        for record in caplog.records
        if "deprecated" in record.getMessage().lower()
        or "will stop working" in record.getMessage()
    ]
    assert reports == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_registers_the_reload_listener(hass: HomeAssistant) -> None:
    """Setup registers exactly one update listener: the reload-on-change seam.

    Subentry add/edit/remove (including the UI delete button) only fire update
    listeners — this listener is the ONLY path by which those changes take
    effect without a restart (AC 5). It is unregistered on unload.
    """
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.update_listeners == [_async_entry_updated]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.update_listeners == []


async def test_setup_creates_the_controller_device(hass: HomeAssistant) -> None:
    """Setup registers the controller device zones will hang off later (AC 3)."""
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    assert device.entry_type is DeviceEntryType.SERVICE
    assert device.name == "Irrigation Controller"
    assert device.manufacturer == "ha-irrigation-controller"
    assert entry.entry_id in device.config_entries


def _entry_with_zones(*zones: Any) -> MockConfigEntry:
    """Build a controller entry carrying stored zone subentries."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=list(zones),
    )


async def test_setup_builds_the_validated_plan_into_runtime_data(
    hass: HomeAssistant,
) -> None:
    """Setup turns the stored config into the typed engine plan (Story 1.4, AC 4).

    Built through the real const.py-keyed storage shapes, this also pins that
    the engine builder's literal key strings agree with const.py.
    """
    entry = _entry_with_zones(
        zone_subentry_data("Zone A", "switch.zone_a_valve"),
        zone_subentry_data("Zone B", "switch.zone_b_valve"),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    plan = entry.runtime_data.plan
    assert isinstance(plan, ControllerPlan)
    assert plan.pump_entity_id == "switch.pool_pump"
    assert plan.morning_enabled is True
    assert plan.morning_start == time(7, 0)
    assert plan.evening_start == time(20, 0)
    # Insertion order preserved; zone key is the subentry id; minutes → seconds.
    assert [zone.name for zone in plan.zones] == ["Zone A", "Zone B"]
    assert [zone.zone_id for zone in plan.zones] == [
        subentry.subentry_id for subentry in entry.subentries.values()
    ]
    zone = plan.zones[0]
    assert zone.valve_entity_id == "switch.zone_a_valve"
    assert zone.morning_duration_s == 600
    assert zone.evening_duration_s == 900
    assert zone.rain_exposed is True
    assert zone.rain_factor == 1.0


async def test_setup_fails_loudly_on_malformed_stored_zone(
    hass: HomeAssistant,
) -> None:
    """Malformed .storage data (a restored backup, a hand edit) fails the setup.

    Load-time validation was explicitly deferred from the 1.3 review to this
    story: a zone the engine cannot water correctly must fail loudly (AD-4),
    never be skipped silently.
    """
    entry = _entry_with_zones(
        zone_subentry_data("Zone A", "switch.zone_a_valve", morning_duration="ten"),
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    # The reason names the offending zone and key so the operator can fix it.
    assert "morning_duration" in entry.reason
    assert "Zone A" in entry.reason


@pytest.mark.parametrize(
    ("key", "inside", "outside"),
    [
        ("morning_duration", MIN_ZONE_DURATION_MINUTES, MIN_ZONE_DURATION_MINUTES - 1),
        ("morning_duration", MAX_ZONE_DURATION_MINUTES, MAX_ZONE_DURATION_MINUTES + 1),
        ("evening_duration", MIN_ZONE_DURATION_MINUTES, MIN_ZONE_DURATION_MINUTES - 1),
        ("evening_duration", MAX_ZONE_DURATION_MINUTES, MAX_ZONE_DURATION_MINUTES + 1),
        ("rain_factor", MIN_RAIN_FACTOR, MIN_RAIN_FACTOR - 0.1),
        ("rain_factor", MAX_RAIN_FACTOR, MAX_RAIN_FACTOR + 0.1),
    ],
)
def test_every_engine_bound_agrees_with_const(
    key: str,
    inside: float,
    outside: float,
) -> None:
    """Each const.py bound is exactly the engine's own bound, on both edges.

    The engine cannot import const.py (it must stay importable hass-free as a
    standalone package), so all four bounds are duplicated there. Pinning only
    one of them — as this suite did until the 1.4 review — leaves the other
    three free to drift apart from the UI selectors silently.
    """
    zone = dict(zone_subentry_data("Zone A", "switch.zone_a_valve")["data"])

    build_plan(CONTROLLER_OPTIONS, [("zone-1", "Zone A", {**zone, key: inside})])

    with pytest.raises(PlanValidationError, match=key):
        build_plan(CONTROLLER_OPTIONS, [("zone-1", "Zone A", {**zone, key: outside})])


@pytest.mark.parametrize(
    ("inside", "outside"),
    [
        (MIN_ACTUATION_TIMEOUT_S, MIN_ACTUATION_TIMEOUT_S - 1),
        (MAX_ACTUATION_TIMEOUT_S, MAX_ACTUATION_TIMEOUT_S + 1),
    ],
)
def test_actuation_timeout_bounds_agree_with_const(inside: int, outside: int) -> None:
    """The timeout bounds and default duplicated in the engine match const.py.

    Same drift defense as the four zone bounds above: the engine cannot import
    const.py, so its module-private twins are pinned here on both edges.
    """
    options = {**CONTROLLER_OPTIONS, CONF_ACTUATION_TIMEOUT: inside}
    assert parse_actuation_timeout(options) == inside

    with pytest.raises(PlanValidationError, match=CONF_ACTUATION_TIMEOUT):
        parse_actuation_timeout({**CONTROLLER_OPTIONS, CONF_ACTUATION_TIMEOUT: outside})


def test_actuation_timeout_default_agrees_with_const() -> None:
    """An absent key parses to exactly the const.py default (legacy entries)."""
    options = {
        key: value
        for key, value in CONTROLLER_OPTIONS.items()
        if key != CONF_ACTUATION_TIMEOUT
    }
    assert parse_actuation_timeout(options) == DEFAULT_ACTUATION_TIMEOUT_S


async def test_entry_without_actuation_timeout_still_loads(
    hass: HomeAssistant,
) -> None:
    """An entry created before Story 1.5 has no timeout key and must keep loading."""
    options = {
        key: value
        for key, value in CONTROLLER_OPTIONS.items()
        if key != CONF_ACTUATION_TIMEOUT
    }
    entry = controller_entry(options)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    ("inside", "outside"),
    [
        (MIN_MANUAL_TIMEOUT_MINUTES, MIN_MANUAL_TIMEOUT_MINUTES - 1),
        (MAX_MANUAL_TIMEOUT_MINUTES, MAX_MANUAL_TIMEOUT_MINUTES + 1),
    ],
)
def test_manual_timeout_bounds_agree_with_const(inside: int, outside: int) -> None:
    """The safety-timeout bounds duplicated in the engine match const.py (3.5).

    Same drift defense as the actuation timeout above, and on both edges. The
    plan carries SECONDS, so the pin is on the converted value — which is
    also what catches a units mix-up in the builder.
    """
    plan = build_plan(
        {**CONTROLLER_OPTIONS, CONF_MANUAL_TIMEOUT: inside},
        [("zone-1", "Zone A", zone_subentry_data("Zone A", "switch.a")["data"])],
    )
    assert plan.manual_timeout_s == inside * 60

    with pytest.raises(PlanValidationError, match=CONF_MANUAL_TIMEOUT):
        build_plan(
            {**CONTROLLER_OPTIONS, CONF_MANUAL_TIMEOUT: outside},
            [("zone-1", "Zone A", zone_subentry_data("Zone A", "switch.a")["data"])],
        )


def test_manual_timeout_default_agrees_with_const() -> None:
    """An absent key parses to exactly the const.py default (legacy entries)."""
    options = {
        key: value
        for key, value in CONTROLLER_OPTIONS.items()
        if key != CONF_MANUAL_TIMEOUT
    }
    plan = build_plan(
        options,
        [("zone-1", "Zone A", zone_subentry_data("Zone A", "switch.a")["data"])],
    )
    assert plan.manual_timeout_s == DEFAULT_MANUAL_TIMEOUT_MINUTES * 60


async def test_entry_without_manual_timeout_still_loads(
    hass: HomeAssistant,
) -> None:
    """An entry created before Story 3.5 has no timeout key and must keep loading."""
    options = {
        key: value
        for key, value in CONTROLLER_OPTIONS.items()
        if key != CONF_MANUAL_TIMEOUT
    }
    entry = controller_entry(options)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_engine_duration_bounds_agree_with_const(hass: HomeAssistant) -> None:
    """The const maximum loads through the real setup path, one past it fails.

    The bound values themselves are pinned above; this one proves the builder
    the entry actually goes through is the one carrying them.
    """
    at_max = _entry_with_zones(
        zone_subentry_data(
            "Zone A",
            "switch.zone_a_valve",
            morning_duration=MAX_ZONE_DURATION_MINUTES,
        ),
    )
    at_max.add_to_hass(hass)
    assert await hass.config_entries.async_setup(at_max.entry_id)
    await hass.async_block_till_done()
    assert at_max.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(at_max.entry_id)
    await hass.async_block_till_done()

    past_max = _entry_with_zones(
        zone_subentry_data(
            "Zone B",
            "switch.zone_b_valve",
            morning_duration=MAX_ZONE_DURATION_MINUTES + 1,
        ),
    )
    past_max.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(past_max.entry_id)
    await hass.async_block_till_done()
    assert past_max.state is ConfigEntryState.SETUP_ERROR


async def test_setup_fails_loudly_below_min_ha_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the minimum HA version, setup raises ConfigEntryError (AC 3)."""
    _patch_ha_version(monkeypatch, 2026, 6, "2026.6.4")
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    assert MIN_HA_VERSION in entry.reason
    assert "2026.6.4" in entry.reason


async def test_setup_succeeds_at_exact_min_ha_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At exactly the minimum HA version, setup succeeds (boundary check)."""
    _patch_ha_version(monkeypatch, MIN_HA_MAJOR, MIN_HA_MINOR, MIN_HA_VERSION)
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


# --------------------------------------------------------------------------
# Config edits never interrupt a running cycle (Story 1.7)
# --------------------------------------------------------------------------


@pytest.fixture
async def running_entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> MockConfigEntry:
    """Set up a two-zone controller and start its morning cycle at 07:00."""
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = _entry_with_zones(
        zone_subentry_data("Zone A", VALVE_1),
        zone_subentry_data("Zone B", VALVE_2),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    run = entry.runtime_data.sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    return entry


async def test_a_mid_cycle_options_edit_is_applied_after_the_cycle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    running_entry: MockConfigEntry,
) -> None:
    """AC 1/2 through the real listener: no reload until the cycle ends, then one.

    `runtime_data` is the proof of "not reloaded": HA rebuilds it on every
    setup, so the same object means the engine was never torn down.
    """
    entry = running_entry
    runtime_data_before = entry.runtime_data
    sequencer_before = runtime_data_before.sequencer

    hass.config_entries.async_update_entry(
        entry,
        options={**CONTROLLER_OPTIONS, CONF_EVENING_START: "21:30:00"},
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is runtime_data_before
    assert entry.runtime_data.runner.reload_pending is True
    # The engine already holds the new plan for the NEXT cycle...
    assert sequencer_before.plan.evening_start == time(21, 30)
    assert entry.runtime_data.plan is sequencer_before.plan
    # ...while the running one is untouched.
    assert sequencer_before.current_run is not None

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert entry.runtime_data is runtime_data_before

    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    assert sequencer_before.current_run is None
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not runtime_data_before
    assert entry.runtime_data.plan.evening_start == time(21, 30)
    assert entry.runtime_data.runner.reload_pending is False


async def test_a_title_rename_neither_reloads_nor_defers(
    hass: HomeAssistant,
    running_entry: MockConfigEntry,
) -> None:
    """A cosmetic update fires the listener too; an equal fingerprint costs nothing."""
    entry = running_entry
    runtime_data_before = entry.runtime_data

    hass.config_entries.async_update_entry(entry, title="Garden")
    await hass.async_block_till_done()

    assert entry.title == "Garden"
    assert entry.runtime_data is runtime_data_before
    assert entry.runtime_data.runner.reload_pending is False


async def test_a_zone_deleted_mid_cycle_is_still_watered_by_the_running_cycle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    running_entry: MockConfigEntry,
) -> None:
    """The run's AD-8 snapshot outlives the zone's subentry (matrix: zone deleted).

    Deleting Zone B while Zone A waters removes its subentry at once; the
    running cycle still opens Zone B's valve at 07:10 from its snapshot, and
    the ONE reload after completion rebuilds the engine on a one-zone plan.
    """
    entry = running_entry
    runtime_data_before = entry.runtime_data
    zone_b = next(
        subentry for subentry in entry.subentries.values() if subentry.title == "Zone B"
    )

    assert hass.config_entries.async_remove_subentry(entry, zone_b.subentry_id)
    await hass.async_block_till_done()

    assert [subentry.title for subentry in entry.subentries.values()] == ["Zone A"]
    assert entry.runtime_data is runtime_data_before
    assert entry.runtime_data.runner.reload_pending is True
    # The engine already holds the one-zone plan for the NEXT cycle...
    assert [zone.name for zone in entry.runtime_data.sequencer.plan.zones] == ["Zone A"]

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")

    # ...while the running one still waters the deleted zone from its snapshot.
    assert entry.runtime_data is runtime_data_before
    assert hass.states.is_state(VALVE_1, "off")
    assert hass.states.is_state(VALVE_2, "on")

    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    assert hass.states.is_state(VALVE_2, "off")
    assert hass.states.is_state(PUMP, "off")
    assert entry.runtime_data is not runtime_data_before
    assert [zone.name for zone in entry.runtime_data.plan.zones] == ["Zone A"]


async def test_an_invalid_mid_cycle_edit_keeps_the_old_plan_until_the_reload_fails(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    running_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail-wet: the engine keeps its plan, warns, and the deferred reload reports.

    Nothing writing through `async_update_subentry` validates, so a broken
    value can land mid-cycle. The running engine must not swap to a plan it
    cannot build; the reload after the cycle then fails setup with the
    existing `invalid_stored_config`, exactly as a restart would.
    """
    entry = running_entry
    runtime_data_before = entry.runtime_data
    plan_before = runtime_data_before.plan
    zone_b = next(
        subentry for subentry in entry.subentries.values() if subentry.title == "Zone B"
    )

    hass.config_entries.async_update_subentry(
        entry,
        zone_b,
        data={**zone_b.data, CONF_MORNING_DURATION: "ten"},
    )
    await hass.async_block_till_done()

    assert entry.runtime_data is runtime_data_before
    assert entry.runtime_data.plan is plan_before
    assert entry.runtime_data.sequencer.plan is plan_before
    assert entry.runtime_data.runner.reload_pending is True
    assert "does not validate" in caplog.text
    assert "morning_duration" in caplog.text

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None
    assert "morning_duration" in entry.reason


async def test_a_forced_reload_mid_cycle_suspends_then_resumes_the_cycle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    running_entry: MockConfigEntry,
) -> None:
    """Decision 1 end to end: `async_reload` while RUNNING suspends, then RESUMES.

    The live valve and the pump are commanded off through the real services
    before the engine is torn down and the OLD manager holds
    `cycle_interrupted`. The rebuilt entry (Story 3.2) reads the RUNNING
    intent back from the journal and reconciles: the orphan pass finds
    everything already OFF, and a live zone whose valve is OFF cannot prove
    what it watered — so it is re-run IN FULL from `now` (fail-wet, bounded
    by one zone duration; the 1.7 suspend keeps its semantics), the pump
    goes back on, `cycle_recovered{outcome: resumed}` is reported and
    `cycle_interrupted` cleared as superseded. Zone B follows on its
    journaled duration, re-timed back-to-back.
    """
    entry = running_entry
    anomalies_before = entry.runtime_data.anomalies
    run_before = entry.runtime_data.sequencer.current_run
    assert run_before is not None
    zone_a = next(
        subentry.subentry_id
        for subentry in entry.subentries.values()
        if subentry.title == "Zone A"
    )
    # The fake was registered by the fixture; its calls are what `switch.*`
    # now resolve to, so a fresh recorder is not needed — read the states.
    assert hass.states.is_state(PUMP, "on")
    assert hass.states.is_state(VALVE_1, "on")

    freezer.move_to("2026-07-31 07:04:00+02:00")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # The suspend's anomaly is on the OLD manager...
    assert AnomalyKind.CYCLE_INTERRUPTED in {
        record.kind for record in anomalies_before.open_anomalies
    }
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.anomalies is not anomalies_before
    # ...and the rebuilt entry resumed the cycle: valve and pump back on, the
    # SAME run (id, actual_start, planned windows) RUNNING on zone A.
    run = entry.runtime_data.sequencer.current_run
    assert run is not None
    assert run.cycle_id == run_before.cycle_id
    assert run.status is CycleStatus.RUNNING
    assert run.recovery == "resumed"
    zone_run_a, zone_run_b = run.zone_runs
    assert zone_run_a.actual_start is not None
    assert zone_run_a.actual_start.isoformat() == "2026-07-31T07:04:00+02:00"
    assert zone_run_a.planned_end.isoformat() == "2026-07-31T07:14:00+02:00"
    assert zone_run_b.planned_start.isoformat() == "2026-07-31T07:14:00+02:00"
    assert zone_run_b.planned_end.isoformat() == "2026-07-31T07:24:00+02:00"
    assert [zone.duration_s for zone in run.zone_runs] == [600, 600]
    assert hass.states.is_state(PUMP, "on")
    assert hass.states.is_state(VALVE_1, "on")
    # One `cycle_recovered` issue, `cycle_interrupted` cleared as superseded.
    assert [
        record.issue_id for record in entry.runtime_data.anomalies.open_anomalies
    ] == ["cycle_recovered"]
    last = entry.runtime_data.anomalies.last_anomaly
    assert last is not None
    assert last.context["outcome"] == "resumed"
    assert last.context["zone_id"] == zone_a
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "cycle_recovered") is not None
    assert registry.async_get_issue(DOMAIN, "cycle_interrupted") is None

    # The resumed cycle runs out on its re-timed windows, never re-quoted.
    await fire_at(hass, freezer, "2026-07-31 07:13:59+02:00")
    assert hass.states.is_state(VALVE_1, "on")
    await fire_at(hass, freezer, "2026-07-31 07:14:00+02:00")
    assert hass.states.is_state(VALVE_1, "off")
    assert hass.states.is_state(VALVE_2, "on")
    await fire_at(hass, freezer, "2026-07-31 07:24:00+02:00")
    assert hass.states.is_state(VALVE_2, "off")
    assert hass.states.is_state(PUMP, "off")
    finished = entry.runtime_data.sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert finished.recovery == "resumed"
    assert entry.runtime_data.sequencer.ledger.deficit_s(zone_a) == 0


async def test_the_cycle_interrupted_push_survives_the_unload_that_raised_it(
    hass: HomeAssistant,
    running_entry: MockConfigEntry,
) -> None:
    """The push task is hass-owned: the entry unload must not cancel it.

    `runner.async_suspend()` raises `cycle_interrupted` from inside the
    unload; an entry-owned background task would be cancelled right after
    the on-unload hooks, before a real notify target (which awaits network)
    ever delivered. The handler here parks on an event until the reload has
    fully completed, then records — a cancelled task never reaches the
    record. The rebuilt entry's resume (Story 3.2) pushes `cycle_recovered`
    as well: two anomalies, two pushes.
    """
    entry = running_entry
    delivered: list[dict[str, Any]] = []
    release = asyncio.Event()

    async def _slow_notify(call: ServiceCall) -> None:
        await release.wait()
        delivered.append(dict(call.data))

    hass.services.async_register("notify", "send_message", _slow_notify)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert delivered == []

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [call[ATTR_ENTITY_ID] for call in delivered] == [
        "notify.mobile_app_phone",
        "notify.mobile_app_phone",
    ]
    assert "cycle interrupted" in delivered[0]["title"]
    assert "cycle recovered" in delivered[1]["title"]


async def test_the_listener_reloads_at_once_when_the_entry_has_no_runtime(
    hass: HomeAssistant,
) -> None:
    """The listener guards its own precondition: no runtime → reload now.

    Exercised DIRECTLY, because no HA path reaches the listener after a
    failed setup: HA runs the on-unload hooks on failure, which removes it.
    The guard exists for the mid-setup window (the listener is registered
    before `runtime_data` exists) and for any caller reaching it on a
    non-LOADED entry — both must fall back to the pre-1.7 reload-now.
    """
    entry = _entry_with_zones(
        zone_subentry_data("Zone A", VALVE_1, morning_duration="ten"),
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not hasattr(entry, "runtime_data")

    zone = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        entry,
        zone,
        data={**zone.data, CONF_MORNING_DURATION: 10},
    )
    await _async_entry_updated(hass, entry)
    await hass.async_block_till_done()

    # mypy narrowed entry.state to SETUP_ERROR above and cannot see the reload.
    assert entry.state is ConfigEntryState.LOADED  # type: ignore[comparison-overlap]
    assert isinstance(entry.runtime_data, HaIrrigationRuntimeData)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_the_fingerprint_covers_options_and_every_subentry_field() -> None:
    """The fingerprint is what the plan is built from — and nothing else.

    Every input of the plan moves it (an option, a zone's title, a zone's
    data, the zone ORDER — which is the watering order); the entry title,
    which nothing reads, does not.
    """
    zone_a = zone_subentry_data("Zone A", VALVE_1)
    zone_b = zone_subentry_data("Zone B", VALVE_2)
    entry = _entry_with_zones(zone_a, zone_b)
    before = config_fingerprint(entry)
    zones = list(entry.subentries.values())

    assert config_fingerprint(entry) == before
    assert before == (
        dict(CONTROLLER_OPTIONS),
        tuple(
            (zone.subentry_id, "zone", zone.title, dict(zone.data)) for zone in zones
        ),
    )

    def _variant(**overrides: Any) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            title=overrides.get("title", "Irrigation Controller"),
            data={},
            options=overrides.get("options", dict(CONTROLLER_OPTIONS)),
            subentries_data=overrides.get("subentries", [zone_a, zone_b]),
        )

    # The subentry ids are minted per entry, so the variants are compared on
    # the id-free projection of the fingerprint.
    def _shape(
        fingerprint: tuple[
            dict[str, Any], tuple[tuple[str, str, str, dict[str, Any]], ...]
        ],
    ) -> tuple[dict[str, Any], tuple[tuple[str, str, dict[str, Any]], ...]]:
        options, subentries = fingerprint
        return options, tuple(
            (subentry_type, title, data) for _, subentry_type, title, data in subentries
        )

    same_shape = _shape(before)
    assert _shape(config_fingerprint(_variant())) == same_shape
    assert _shape(config_fingerprint(_variant(title="Garden"))) == same_shape
    assert (
        _shape(
            config_fingerprint(
                _variant(
                    options={**CONTROLLER_OPTIONS, CONF_EVENING_START: "21:30:00"}
                ),
            ),
        )
        != same_shape
    )
    assert (
        _shape(
            config_fingerprint(
                _variant(subentries=[zone_subentry_data("Zone Z", VALVE_1), zone_b]),
            ),
        )
        != same_shape
    )
    assert (
        _shape(
            config_fingerprint(
                _variant(
                    subentries=[
                        zone_subentry_data("Zone A", VALVE_1, morning_duration=3),
                        zone_b,
                    ],
                ),
            ),
        )
        != same_shape
    )
    assert (
        _shape(config_fingerprint(_variant(subentries=[zone_b, zone_a]))) != same_shape
    )


async def test_a_forced_unload_mid_cycle_closes_the_valve_and_the_pump(
    hass: HomeAssistant,
    running_entry: MockConfigEntry,
) -> None:
    """Decision 1 for a plain unload (disable/remove): suspend, then torn down.

    Same hardware outcome as the forced reload, but the entry stays down:
    NOT_LOADED, the OLD manager holding `cycle_interrupted`.
    """
    entry = running_entry
    anomalies_before = entry.runtime_data.anomalies
    assert hass.states.is_state(PUMP, "on")
    assert hass.states.is_state(VALVE_1, "on")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.is_state(VALVE_1, "off")
    assert hass.states.is_state(PUMP, "off")
    assert AnomalyKind.CYCLE_INTERRUPTED in {
        record.kind for record in anomalies_before.open_anomalies
    }
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(entry, "runtime_data")


async def test_the_tracker_leaks_no_registry_listener_across_a_reload(
    hass: HomeAssistant,
) -> None:
    """A reload per config change must not accumulate registry listeners.

    `verify_cleanup` cannot see bus listeners, so the count is asserted
    directly: after setup → reload → unload it is back to the baseline.
    """
    baseline = hass.bus.async_listeners().get(EVENT_ENTITY_REGISTRY_UPDATED, 0)
    entry = _entry_with_zones(zone_subentry_data("Zone A", VALVE_1))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.bus.async_listeners().get(EVENT_ENTITY_REGISTRY_UPDATED, 0) == baseline


async def test_the_manual_override_watch_leaks_no_state_listener(
    hass: HomeAssistant,
) -> None:
    """Story 3.4: ONE standing watch, gone after unload, not doubled by a reload.

    The watch rides `entry.async_on_unload` right after the tracker's stop,
    inside the ORDER CONTRACT block — and it is re-armed on the busy-reload
    path too, which cancels the previous subscription first. `verify_cleanup`
    cannot see bus listeners, so the count is asserted directly.
    """
    baseline = hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)
    entry = _entry_with_zones(zone_subentry_data("Zone A", VALVE_1))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    armed = hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0)
    assert armed > baseline

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0) == armed

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.bus.async_listeners().get(EVENT_STATE_CHANGED, 0) == baseline


async def test_setup_succeeds_on_prerelease_of_min_ha_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A beta of the minimum month release is supported, not rejected.

    Regression guard: a full-string comparison sorts X.Y.0b5 BELOW X.Y.0
    and locked beta-channel users out of the exact minimum release.
    """
    _patch_ha_version(
        monkeypatch,
        MIN_HA_MAJOR,
        MIN_HA_MINOR,
        f"{MIN_HA_MAJOR}.{MIN_HA_MINOR}.0b5",
    )
    entry = controller_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


# --------------------------------------------------------------------------
# The ledger seed reaches the engine (Story 2.2)
# --------------------------------------------------------------------------


async def test_a_journal_written_before_the_ledger_sets_up_and_quotes_on_base(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 2: a pre-2.2 document loads, and the first cycle is quoted on base.

    The stored document is one the pre-2.2 code wrote — history and the
    season flag, no `ledger` key anywhere. Setup must not fail and must not
    invent a debt: the engine starts with an empty ledger and the 10 min
    zone waters 600 s.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [
                {
                    "cycle_id": "2026-07-30-evening",
                    "irrigation_day": "2026-07-30",
                    "kind": "evening",
                    "status": "completed",
                    "manual": False,
                    "configured_start": "2026-07-30T18:00:00+00:00",
                    "scheduled_start": "2026-07-30T18:00:00+00:00",
                    "ended_at": "2026-07-30T18:15:00+00:00",
                    "zones": [
                        {
                            "zone_id": "zone-a",
                            "status": "completed",
                            "effective_s": 900,
                        },
                    ],
                },
            ],
            "season_enabled": True,
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", VALVE_1), "subentry_id": "zone-a"},
        ],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.ledger.as_dict() == {
        "settled_cycle_id": None,
        "deficits": {},
        "day_credit": None,
        "rain_baselines": {},
        "rain_source": None,
    }

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    zone = run.zone_runs[0]
    assert (zone.duration_s, zone.base_s, zone.carried_s) == (600, 600, 0)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_seeded_ledger_reaches_the_engine_and_extends_the_first_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 3: a deficit recorded before an HA restart is applied after setup.

    `async_setup_entry` hands `seed.ledger` to the sequencer — the wiring
    this test pins. The 300 s owed by `zone-a` extend its morning slot to
    900 s, and the zone after it starts 5 min later than the plan says.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [],
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {"zone-a": 300},
            },
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", VALVE_1), "subentry_id": "zone-a"},
            {**zone_subentry_data("Zone B", VALVE_2), "subentry_id": "zone-b"},
        ],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.ledger.deficit_s("zone-a") == 300
    assert sequencer.ledger.deficit_s("zone-b") == 0

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    zone_a, zone_b = run.zone_runs
    assert (zone_a.duration_s, zone_a.carried_s) == (900, 300)
    assert (zone_b.duration_s, zone_b.carried_s) == (600, 0)
    assert zone_b.planned_start.isoformat() == "2026-07-31T07:15:00+02:00"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# The day credit seed reaches the engine (Story 2.3)
# --------------------------------------------------------------------------


async def test_a_seeded_day_credit_waives_the_same_days_first_scheduled_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.3 AC 3: a credit recorded before an HA restart waives after setup.

    `async_setup_entry` hands `seed.ledger` — credit included — to the
    sequencer. The 07:00 start then creates no run and commands nothing;
    the waived record names the run-now the previous life completed, and the
    seeded history record it sits next to is untouched.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    run_now = {
        "cycle_id": "2026-07-31-morning",
        "irrigation_day": "2026-07-31",
        "kind": "morning",
        "status": "completed",
        "manual": True,
        "waived_by": None,
        "configured_start": "2026-07-31T05:00:00+00:00",
        "scheduled_start": "2026-07-31T03:00:00+00:00",
        "ended_at": "2026-07-31T03:10:00+00:00",
        "zones": [
            {
                "zone_id": "zone-a",
                "status": "completed",
                "planned_s": 600,
                "carried_s": 0,
                "rain_credit_s": 0,
                "effective_s": 600,
            },
        ],
    }
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [run_now],
            "ledger": {
                "settled_cycle_id": "2026-07-31-morning",
                "deficits": {},
                "day_credit": {
                    "irrigation_day": "2026-07-31",
                    "cycle_id": "2026-07-31-morning",
                },
                "rain_baselines": {},
                "rain_source": None,
            },
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", VALVE_1), "subentry_id": "zone-a"},
        ],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.ledger.day_credit == "2026-07-31"

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    assert sequencer.current_run is None
    assert sequencer.last_run is None
    assert calls == []
    assert sequencer.ledger.day_credit is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[STORAGE_KEY]["data"]["history"]
    assert stored[0] == run_now
    assert [
        (record["cycle_id"], record["status"], record["waived_by"]) for record in stored
    ] == [
        ("2026-07-31-morning", "completed", None),
        ("2026-07-31-morning-2", "waived", "2026-07-31-morning"),
    ]


# --------------------------------------------------------------------------
# The rain gauge reaches the engine (Story 2.4)
# --------------------------------------------------------------------------


def set_gauge(hass: HomeAssistant, total: str) -> None:
    """Put a cumulative total in mm on the representative entry's rain sensor."""
    hass.states.async_set("sensor.rain_gauge", total, {"unit_of_measurement": "mm"})


async def test_seeded_rain_baselines_reach_the_engine_and_reduce_the_first_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.4 AC 4: baselines written before an HA restart credit after setup.

    `async_setup_entry` hands `seed.ledger` — baselines included — to the
    sequencer and wires the gauge adapter from `CONF_RAIN_SENSOR`. Zone A
    banked 12.0 in a previous life; the gauge reads 15.5 now, so its 10 min
    slot is quoted 390 s. Zone B has no baseline and waters in full.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    set_gauge(hass, "15.5")
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [],
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {},
                "day_credit": None,
                "rain_baselines": {"zone-a": 12.0},
                "rain_source": "sensor.rain_gauge",
            },
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", VALVE_1), "subentry_id": "zone-a"},
            {**zone_subentry_data("Zone B", VALVE_2), "subentry_id": "zone-b"},
        ],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.ledger.rain_baseline_mm("zone-a") == 12.0
    assert sequencer.ledger.rain_baseline_mm("zone-b") is None

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    assert run.rain_total_mm == 15.5
    zone_a, zone_b = run.zone_runs
    assert (zone_a.duration_s, zone_a.rain_credit_s) == (390, 210)
    assert (zone_b.duration_s, zone_b.rain_credit_s) == (600, 0)
    assert zone_b.planned_start.isoformat() == "2026-07-31T07:06:30+02:00"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_journal_written_before_rain_baselines_quotes_full_durations(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.4 AC 5: a pre-2.4 ledger section loads with no baselines.

    The gauge is readable, but no zone has a baseline, so the first cycle is
    quoted in full (debt included) — and banks the reading for the next one.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    set_gauge(hass, "15.5")
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [],
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {"zone-a": 300},
                "day_credit": None,
            },
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", VALVE_1), "subentry_id": "zone-a"},
        ],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.ledger.as_dict()["rain_baselines"] == {}

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    assert run.rain_total_mm == 15.5
    zone = run.zone_runs[0]
    assert (zone.duration_s, zone.carried_s, zone.rain_credit_s) == (900, 300, 0)

    await fire_at(hass, freezer, "2026-07-31 07:15:00+02:00")
    assert sequencer.ledger.as_dict()["rain_baselines"] == {"zone-a": 15.5}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_journal_written_by_2_4_waters_in_full_once_then_modulates(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.5 AC 3: a pre-2.5 section (baselines, no `rain_source`) loads.

    Zone A banked 12.0 under no known gauge, so the 15.5 reading is not
    comparable: the morning cycle waters its full 600 s (no anomaly) and
    settlement stamps the configured gauge as the source next to 15.5. The
    evening cycle, with the gauge at 17.5, is modulated: 900 - 120 = 780 s.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    set_gauge(hass, "15.5")
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [],
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {},
                "day_credit": None,
                "rain_baselines": {"zone-a": 12.0},
            },
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", VALVE_1), "subentry_id": "zone-a"},
        ],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.ledger.rain_baseline_mm("zone-a") == 12.0
    assert sequencer.ledger.rain_source is None

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    run = sequencer.current_run
    assert run is not None
    assert (run.rain_total_mm, run.rain_source) == (15.5, "sensor.rain_gauge")
    assert (run.zone_runs[0].duration_s, run.zone_runs[0].rain_credit_s) == (600, 0)
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert sequencer.ledger.as_dict()["rain_baselines"] == {"zone-a": 15.5}
    assert sequencer.ledger.rain_source == "sensor.rain_gauge"

    set_gauge(hass, "17.5")
    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    assert (run.zone_runs[0].duration_s, run.zone_runs[0].rain_credit_s) == (780, 120)
    assert entry.runtime_data.anomalies.open_anomalies == ()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_changing_the_rain_sensor_re_banks_on_the_next_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.5 AC 1 through the options: a new sensor is a new source.

    The morning cycle banks 12.0 under `sensor.rain_gauge`. The operator
    then picks `sensor.rain_gauge_2`, whose lifetime total is 1200 mm; the
    entry reloads at once (nothing running) with an adapter bound to the
    new id. The evening cycle credits NOTHING — without the source stamp it
    would have skipped the zone on 1188 mm of rain that never fell here —
    and settles 1200 under the new source; the next morning, at 1203, is
    modulated by the 3 mm since (180 s).
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    set_gauge(hass, "12.0")
    hass.states.async_set(
        "sensor.rain_gauge_2", "1200.0", {"unit_of_measurement": "mm"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", VALVE_1), "subentry_id": "zone-a"},
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    runtime_before = entry.runtime_data

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert runtime_before.sequencer.ledger.as_dict()["rain_baselines"] == {
        "zone-a": 12.0
    }
    assert runtime_before.sequencer.ledger.rain_source == "sensor.rain_gauge"

    hass.config_entries.async_update_entry(
        entry,
        options={**CONTROLLER_OPTIONS, CONF_RAIN_SENSOR: "sensor.rain_gauge_2"},
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not runtime_before
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    # The reload re-seeded the section written under the OLD gauge.
    assert sequencer.ledger.rain_baseline_mm("zone-a") == 12.0
    assert sequencer.ledger.rain_source == "sensor.rain_gauge"

    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    run = sequencer.current_run
    assert run is not None
    assert (run.rain_total_mm, run.rain_source) == (1200.0, "sensor.rain_gauge_2")
    assert (run.zone_runs[0].duration_s, run.zone_runs[0].rain_credit_s) == (900, 0)
    await fire_at(hass, freezer, "2026-07-31 20:15:00+02:00")
    assert sequencer.current_run is None
    assert sequencer.ledger.as_dict()["rain_baselines"] == {"zone-a": 1200.0}
    assert sequencer.ledger.rain_source == "sensor.rain_gauge_2"
    assert entry.runtime_data.anomalies.open_anomalies == ()

    hass.states.async_set(
        "sensor.rain_gauge_2", "1203.0", {"unit_of_measurement": "mm"}
    )
    await fire_at(hass, freezer, "2026-08-01 07:00:00+02:00")

    run = sequencer.current_run
    assert run is not None
    assert (run.zone_runs[0].duration_s, run.zone_runs[0].rain_credit_s) == (420, 180)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # The unload flushes the debounced journal: the new source is on disk.
    assert hass_storage[STORAGE_KEY]["data"]["ledger"]["rain_source"] == (
        "sensor.rain_gauge_2"
    )


async def test_an_entry_without_a_rain_sensor_quotes_unmodulated(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """No `CONF_RAIN_SENSOR` (or not a string) means no gauge: `rain=None`.

    The flow requires the option, but a stored entry is unvalidated input.
    Setup succeeds and every cycle is quoted on full durations with no
    reading snapshotted.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    options = {
        key: value
        for key, value in CONTROLLER_OPTIONS.items()
        if key != CONF_RAIN_SENSOR
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=options,
        subentries_data=[zone_subentry_data("Zone A", VALVE_1)],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    run = entry.runtime_data.sequencer.current_run
    assert run is not None
    assert run.rain_total_mm is None
    assert (run.zone_runs[0].duration_s, run.zone_runs[0].rain_credit_s) == (600, 0)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Story 3.1 — the anomaly pipeline wired through setup, reload and removal
# --------------------------------------------------------------------------


def domain_issue_ids(hass: HomeAssistant) -> set[str]:
    """Return the ids of every Repairs issue of this domain."""
    return {
        issue_id for domain, issue_id in ir.async_get(hass).issues if domain == DOMAIN
    }


async def test_a_new_notify_target_is_used_after_the_options_edit(
    hass: HomeAssistant,
) -> None:
    """AC 1: the next newly opened anomaly is pushed to the NEW target, no restart.

    The options edit reloads the entry (idle), which rebuilds the manager
    with an adapter bound to the new entity id.
    """
    calls = register_notify_domain(hass)
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.config_entries.async_update_entry(
        entry,
        options={**CONTROLLER_OPTIONS, CONF_NOTIFY_TARGET: "notify.mobile_app_tablet"},
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    entry.runtime_data.anomalies.report(
        AnomalyKind.PUMP_ON_UNCONFIRMED,
        {"cycle_id": "2026-07-31-morning", "entity_id": PUMP},
    )
    await hass.async_block_till_done()

    assert [call.data[ATTR_ENTITY_ID] for call in calls] == ["notify.mobile_app_tablet"]
    assert "pump on unconfirmed" in calls[0].data["title"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_entry_without_a_notify_target_sets_up_and_pushes_nothing(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matrix "No/invalid target": one WARNING at setup, never a setup failure."""
    calls = register_notify_domain(hass)
    entry = controller_entry(
        {
            key: value
            for key, value in CONTROLLER_OPTIONS.items()
            if key != CONF_NOTIFY_TARGET
        },
    )
    entry.add_to_hass(hass)
    caplog.set_level(logging.WARNING)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.anomalies.report(
        AnomalyKind.PUMP_ON_UNCONFIRMED,
        {"cycle_id": "2026-07-31-morning", "entity_id": PUMP},
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert calls == []
    assert domain_issue_ids(hass) == {f"pump_on_unconfirmed:{PUMP}"}
    assert (
        len(
            [
                r
                for r in caplog.records
                if "No notify target is configured" in r.getMessage()
            ]
        )
        == 1
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_reload_re_seeds_the_open_anomaly_without_a_second_push(
    hass: HomeAssistant,
) -> None:
    """Matrix "Reload with open issue": health survives, the operator is not re-told."""
    calls = register_notify_domain(hass)
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.anomalies.report(
        AnomalyKind.PUMP_ON_UNCONFIRMED,
        {"cycle_id": "2026-07-31-morning", "entity_id": PUMP},
    )
    await hass.async_block_till_done()
    manager_before = entry.runtime_data.anomalies

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    manager = entry.runtime_data.anomalies
    assert manager is not manager_before
    assert [record.issue_id for record in manager.open_anomalies] == [
        f"pump_on_unconfirmed:{PUMP}",
    ]
    assert len(calls) == 1
    # Re-reported after the reload: still not new, still no push.
    manager.report(
        AnomalyKind.PUMP_ON_UNCONFIRMED,
        {"cycle_id": "2026-07-31-evening", "entity_id": PUMP},
    )
    await hass.async_block_till_done()
    assert len(calls) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_removing_the_entry_deletes_every_repairs_issue(
    hass: HomeAssistant,
) -> None:
    """`async_remove_entry` leaves no issue of the domain behind."""
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.anomalies.report(
        AnomalyKind.PUMP_ON_UNCONFIRMED,
        {"cycle_id": "2026-07-31-morning", "entity_id": PUMP},
    )
    entry.runtime_data.anomalies.report(AnomalyKind.JOURNAL_SAVE_FAILED, {})
    await hass.async_block_till_done()
    assert len(domain_issue_ids(hass)) == 2

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == set()


async def test_a_nominal_day_pushes_nothing_opens_nothing_and_fires_no_anomaly(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 4: rain-reduced morning, run-now at noon, waived evening — silence.

    Zone A banked 10.0 mm under the configured gauge; at 13.0 the morning
    cycle is credited 180 s (420 s instead of 600). The run-now at noon
    credits the day, so the 20:00 evening cycle is waived. Not one notify
    call, not one Repairs issue, not one `anomaly` event across the day —
    and the health entity stays `off`.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    set_gauge(hass, "13.0")
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "history": [],
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {},
                "day_credit": None,
                "rain_baselines": {"zone-a": 10.0},
                "rain_source": "sensor.rain_gauge",
            },
        },
    }
    notify_calls = register_notify_domain(hass)
    anomaly_events: list[Any] = []
    hass.bus.async_listen(
        EVENT_HA_IRRIGATION_CONTROLLER,
        lambda event: (
            anomaly_events.append(event)
            if event.data.get("event_type") == "anomaly"
            else None
        ),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", VALVE_1), "subentry_id": "zone-a"},
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    run = sequencer.current_run
    assert run is not None
    assert (run.zone_runs[0].duration_s, run.zone_runs[0].rain_credit_s) == (420, 180)
    await fire_at(hass, freezer, "2026-07-31 07:07:00+02:00")
    assert sequencer.current_run is None
    assert run.status is CycleStatus.COMPLETED

    freezer.move_to("2026-07-31 12:00:00+02:00")
    assert await entry.runtime_data.runner.async_run_now(CycleKind.EVENING)
    await hass.async_block_till_done()
    await fire_at(hass, freezer, "2026-07-31 12:15:00+02:00")
    assert sequencer.current_run is None
    assert sequencer.ledger.day_credit == "2026-07-31"

    # 20:00: the scheduled evening cycle is waived by the run-now's credit —
    # nothing is created (no `current_run`) and the credit is consumed.
    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    assert sequencer.current_run is None
    assert sequencer.ledger.day_credit is None

    assert notify_calls == []
    assert anomaly_events == []
    assert domain_issue_ids(hass) == set()
    assert entry.runtime_data.anomalies.open_anomalies == ()
    health = er.async_get(hass).async_get_entity_id(
        "binary_sensor",
        DOMAIN,
        f"{entry.entry_id}_health",
    )
    assert health is not None
    assert hass.states.is_state(health, "off")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
