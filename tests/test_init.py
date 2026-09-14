"""Smoke tests: config entry sets up and unloads cleanly, version guard fails loudly."""

from __future__ import annotations

import logging
from datetime import time
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr, entity_registry as er
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
    CONF_MORNING_DURATION,
    CONF_RAIN_SENSOR,
    DEFAULT_ACTUATION_TIMEOUT_S,
    DOMAIN,
    MAX_ACTUATION_TIMEOUT_S,
    MAX_RAIN_FACTOR,
    MAX_ZONE_DURATION_MINUTES,
    MIN_ACTUATION_TIMEOUT_S,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
    MIN_RAIN_FACTOR,
    MIN_ZONE_DURATION_MINUTES,
)
from custom_components.ha_irrigation_controller.engine.config import (
    PlanValidationError,
    build_plan,
    parse_actuation_timeout,
)
from custom_components.ha_irrigation_controller.engine.plan import ControllerPlan
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
    register_switch_domain,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

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
    own; this pins the wire between them. Line coverage cannot: the
    construction runs on every setup whatever the value is, and the adapter is
    deliberately not exposed on `runtime_data`.
    """
    configured_s = MAX_ACTUATION_TIMEOUT_S - 1
    captured: list[int] = []

    def _spy(
        hass_: HomeAssistant,
        *,
        timeout_s: int,
        cycle_id_provider: Callable[[], str | None],
    ) -> VerifiedSwitchAdapter:
        captured.append(timeout_s)
        return VerifiedSwitchAdapter(
            hass_,
            timeout_s=timeout_s,
            cycle_id_provider=cycle_id_provider,
        )

    monkeypatch.setattr(f"{_MODULE}.VerifiedSwitchAdapter", _spy)
    entry = controller_entry(
        {**CONTROLLER_OPTIONS, CONF_ACTUATION_TIMEOUT: configured_s},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert captured == [configured_s]

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


async def test_a_forced_reload_mid_cycle_closes_the_valve_and_the_pump(
    hass: HomeAssistant,
    running_entry: MockConfigEntry,
) -> None:
    """Decision 1 end to end: `async_reload` while RUNNING suspends, then reloads.

    The live valve and the pump are commanded off through the real services
    before the engine is torn down, the OLD manager holds `cycle_interrupted`,
    and the entry comes back LOADED — idle, because nothing restores the run
    before Story 3.2.
    """
    entry = running_entry
    anomalies_before = entry.runtime_data.anomalies
    # The fake was registered by the fixture; its calls are what `switch.*`
    # now resolve to, so a fresh recorder is not needed — read the states.
    assert hass.states.is_state(PUMP, "on")
    assert hass.states.is_state(VALVE_1, "on")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.is_state(VALVE_1, "off")
    assert hass.states.is_state(PUMP, "off")
    assert AnomalyKind.CYCLE_INTERRUPTED in anomalies_before.open_anomalies
    last = anomalies_before.last_anomaly
    assert last is not None
    assert last.context["zone_id"] == next(
        subentry.subentry_id
        for subentry in entry.subentries.values()
        if subentry.title == "Zone A"
    )
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.anomalies is not anomalies_before
    assert entry.runtime_data.sequencer.current_run is None


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
    assert AnomalyKind.CYCLE_INTERRUPTED in anomalies_before.open_anomalies
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
