"""The three verb-named actions (FR5, AD-10, AC 3, AC 5).

Registered in `async_setup`, resolved to the one loaded entry per call, and
never removed on unload (`action-setup`) — an automation referencing one must
stay editable, and the call must explain itself when nothing is loaded.

Every error AC 5 enumerates is a translated `ServiceValidationError`, never a
bare `return`: the range and identity checks live in the handlers precisely
because a schema failure is not what AC 5 asks for, and the `services.yaml`
selectors are UI affordances the WebSocket API bypasses. A value of the wrong
TYPE still fails voluptuous as an untranslated `vol.Invalid`, exactly as it
does in core — none of those is one of AC 5's cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, Platform
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.const import (
    ATTR_CYCLE,
    ATTR_DURATION,
    ATTR_ENABLED,
    ATTR_ZONE_ID,
    CONF_EVENING_DURATION,
    CONF_EVENING_START,
    CONF_MORNING_DURATION,
    DOMAIN,
    MAX_ZONE_DURATION_MINUTES,
    MIN_ZONE_DURATION_MINUTES,
    SERVICE_CANCEL_CYCLE,
    SERVICE_SET_SEASON,
    SERVICE_SET_ZONE_DURATION,
)
from custom_components.ha_irrigation_controller.engine.runs import CycleStatus
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
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant

ALL_SERVICES = (SERVICE_CANCEL_CYCLE, SERVICE_SET_SEASON, SERVICE_SET_ZONE_DURATION)


def zone_of(entry: MockConfigEntry, title: str) -> ConfigSubentry:
    """Return the live subentry carrying `title`."""
    subentries: dict[str, ConfigSubentry] = entry.subentries
    return next(subentry for subentry in subentries.values() if subentry.title == title)


async def call(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Call one of our actions, blocking so validation errors surface here."""
    await hass.services.async_call(DOMAIN, service, data or {}, blocking=True)
    await hass.async_block_till_done()


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> MockConfigEntry:
    """Set up a two-zone controller just before its morning start.

    Ten morning minutes each from 07:00 and fifteen evening minutes each from
    20:00 — room to grow a duration without overlapping, and little enough
    that a 120-minute one collides.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            zone_subentry_data("Zone A", VALVE_1),
            zone_subentry_data("Zone B", VALVE_2),
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


# --------------------------------------------------------------------------
# Registration (AC 3)
# --------------------------------------------------------------------------


async def test_the_three_services_are_registered(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """AC 3: the verb-named actions exist once the integration is loaded."""
    assert all(hass.services.has_service(DOMAIN, name) for name in ALL_SERVICES)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_services_survive_an_unload(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """`action-setup`: registered in `async_setup`, never removed on unload.

    An automation referencing an action of an unloaded entry must stay
    editable rather than turn into an "unknown service" the operator cannot
    even open — and the call itself must explain what is wrong.
    """
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert all(hass.services.has_service(DOMAIN, name) for name in ALL_SERVICES)


async def test_calling_an_action_with_no_entry_loaded_explains_itself(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """AC 5: never a silent failure — a translated `ServiceValidationError`."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await call(hass, SERVICE_CANCEL_CYCLE)
    with pytest.raises(ServiceValidationError):
        await call(hass, SERVICE_SET_SEASON, {ATTR_ENABLED: False})


# --------------------------------------------------------------------------
# cancel_cycle (AC 4, AC 5)
# --------------------------------------------------------------------------


async def test_cancel_cycle_stops_the_running_cycle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """AC 4 through the service: live valve off, then the pump, filed CANCELLED."""
    calls = register_switch_domain(hass)
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    freezer.move_to("2026-07-31 07:04:00+02:00")
    await call(hass, SERVICE_CANCEL_CYCLE)

    assert [(one.service, one.data[ATTR_ENTITY_ID]) for one in calls] == [
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
    ]
    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.CANCELLED
    # A permitted non-watering cause: an operator cancel is an intent, not a
    # fault, so nothing is raised (AD-4).
    assert entry.runtime_data.anomalies.open_anomalies == frozenset()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_cancel_cycle_with_nothing_running_raises(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """AC 5: the engine returns False; the SERVICE is what tells the operator."""
    with pytest.raises(ServiceValidationError):
        await call(hass, SERVICE_CANCEL_CYCLE)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# set_season (AC 1, AC 2)
# --------------------------------------------------------------------------


async def test_set_season_false_then_a_daily_start_waters_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """AC 2 through the service surface (the switch covers the other one)."""
    calls = register_switch_domain(hass)

    await call(hass, SERVICE_SET_SEASON, {ATTR_ENABLED: False})
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    assert entry.runtime_data.sequencer.current_run is None
    assert calls == []
    assert entry.runtime_data.anomalies.open_anomalies == frozenset()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_set_season_is_idempotent(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """One idempotent action, not a season_on/season_off pair — twice is fine."""
    await call(hass, SERVICE_SET_SEASON, {ATTR_ENABLED: False})
    await call(hass, SERVICE_SET_SEASON, {ATTR_ENABLED: False})

    assert entry.runtime_data.sequencer.season_enabled is False

    await call(hass, SERVICE_SET_SEASON, {ATTR_ENABLED: True})
    assert entry.runtime_data.sequencer.season_enabled is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_set_season_and_the_switch_drive_the_one_flag(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """Two surfaces, ONE engine flag — the switch follows a service call."""
    season = er.async_get(hass).async_get_entity_id(
        Platform.SWITCH,
        DOMAIN,
        f"{entry.entry_id}_season",
    )
    assert season is not None
    assert (state := hass.states.get(season)) is not None
    assert state.state == STATE_ON

    await call(hass, SERVICE_SET_SEASON, {ATTR_ENABLED: False})

    assert (state := hass.states.get(season)) is not None
    assert state.state == STATE_OFF

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# set_zone_duration (AC 5)
# --------------------------------------------------------------------------


async def test_set_zone_duration_writes_the_subentry_and_reloads_once(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """FR8: a duration edit applies without a restart, through the ONE listener.

    The write goes through `async_update_subentry` — never by assigning to
    `subentry.data` — and it is the update listener, not the service, that
    reloads. Exactly one reload: a second write path would double it.
    """
    reloads: list[str] = []
    real_reload = hass.config_entries.async_schedule_reload

    def _spy(entry_id: str) -> None:
        reloads.append(entry_id)

    hass.config_entries.async_schedule_reload = _spy  # type: ignore[method-assign]
    try:
        await call(
            hass,
            SERVICE_SET_ZONE_DURATION,
            {
                ATTR_ZONE_ID: zone_of(entry, "Zone A").subentry_id,
                ATTR_CYCLE: "morning",
                ATTR_DURATION: 12,
            },
        )
    finally:
        hass.config_entries.async_schedule_reload = real_reload  # type: ignore[method-assign]

    assert zone_of(entry, "Zone A").data[CONF_MORNING_DURATION] == 12
    # Untouched: only the addressed cycle's key changes.
    assert zone_of(entry, "Zone A").data[CONF_EVENING_DURATION] == 15
    assert zone_of(entry, "Zone B").data[CONF_MORNING_DURATION] == 10
    assert reloads == [entry.entry_id]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_new_duration_reaches_the_engine_after_the_reload(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """The write is only half of FR8 — the rebuilt plan is what waters."""
    await call(
        hass,
        SERVICE_SET_ZONE_DURATION,
        {
            ATTR_ZONE_ID: zone_of(entry, "Zone B").subentry_id,
            ATTR_CYCLE: "evening",
            ATTR_DURATION: 20,
        },
    )
    await hass.async_block_till_done()

    plan = entry.runtime_data.plan
    zone = next(one for one in plan.zones if one.name == "Zone B")
    assert zone.evening_duration_s == 20 * 60

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_unknown_zone_id_names_the_zones_it_could_have_been(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """AC 5: a hex id no human can guess needs the known ids in the message."""
    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            SERVICE_SET_ZONE_DURATION,
            {
                ATTR_ZONE_ID: "not-a-zone",
                ATTR_CYCLE: "morning",
                ATTR_DURATION: 12,
            },
        )

    assert raised.value.translation_key == "unknown_zone"
    placeholders = raised.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["zone_id"] == "not-a-zone"
    for title in ("Zone A", "Zone B"):
        assert title in placeholders["known"]
        assert zone_of(entry, title).subentry_id in placeholders["known"]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_unknown_zone_message_says_so_when_there_are_no_zones(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The message is the operator's only recovery route — it must not trail off.

    A controller with no zones is a supported state (the engine runs zero-zone
    plans), and it is the state in which a wrong `zone_id` is most likely. A
    bare join would render "The configured zones are: ." and tell them nothing.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    config_entry = controller_entry()
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            SERVICE_SET_ZONE_DURATION,
            {
                ATTR_ZONE_ID: "not-a-zone",
                ATTR_CYCLE: "morning",
                ATTR_DURATION: 12,
            },
        )

    assert raised.value.translation_key == "unknown_zone"
    placeholders = raised.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["known"] == "none — this controller has no zones configured"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_a_subentry_that_is_not_a_zone_is_rejected(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """A stored subentry of another type must not be duration-edited as a zone.

    `zone` is the only type registered today, but a restored backup or a
    future type would otherwise be accepted by the id lookup alone.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            zone_subentry_data("Zone A", VALVE_1),
            ConfigSubentryData(
                data={},
                subentry_type="something_else",
                title="Not a zone",
                unique_id=None,
            ),
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    foreign = next(
        subentry
        for subentry in config_entry.subentries.values()
        if subentry.title == "Not a zone"
    )
    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            SERVICE_SET_ZONE_DURATION,
            {
                ATTR_ZONE_ID: foreign.subentry_id,
                ATTR_CYCLE: "morning",
                ATTR_DURATION: 12,
            },
        )

    assert raised.value.translation_key == "unknown_zone"

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "duration",
    [MIN_ZONE_DURATION_MINUTES - 1, MAX_ZONE_DURATION_MINUTES + 1],
)
async def test_a_duration_outside_the_bounds_raises(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    duration: int,
) -> None:
    """AC 5: the range check lives in the HANDLER, so the error is translated.

    A `vol.In`/`vol.Range` in the schema would raise `vol.Invalid`, which is
    not a `ServiceValidationError` — and the `services.yaml` number selector
    bounds the UI slider only. The WebSocket API bypasses both.
    """
    stored = dict(zone_of(entry, "Zone A").data)

    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            SERVICE_SET_ZONE_DURATION,
            {
                ATTR_ZONE_ID: zone_of(entry, "Zone A").subentry_id,
                ATTR_CYCLE: "morning",
                ATTR_DURATION: duration,
            },
        )

    assert raised.value.translation_key == "invalid_duration"
    placeholders = raised.value.translation_placeholders
    assert placeholders is not None
    assert placeholders["min"] == str(MIN_ZONE_DURATION_MINUTES)
    assert placeholders["max"] == str(MAX_ZONE_DURATION_MINUTES)
    # Nothing was written — compared against a snapshot, since
    # `async_update_subentry` mutates the live object in place.
    assert dict(zone_of(entry, "Zone A").data) == stored

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "duration",
    [MIN_ZONE_DURATION_MINUTES, MAX_ZONE_DURATION_MINUTES],
    ids=["min", "max"],
)
async def test_the_bounds_themselves_are_accepted(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    duration: int,
) -> None:
    """Both edges are INSIDE the range — an off-by-one here is invisible."""
    # The maximum on one zone would collide with the evening cycle, so the
    # other zone is shortened to its minimum first.
    await call(
        hass,
        SERVICE_SET_ZONE_DURATION,
        {
            ATTR_ZONE_ID: zone_of(entry, "Zone B").subentry_id,
            ATTR_CYCLE: "morning",
            ATTR_DURATION: MIN_ZONE_DURATION_MINUTES,
        },
    )
    await call(
        hass,
        SERVICE_SET_ZONE_DURATION,
        {
            ATTR_ZONE_ID: zone_of(entry, "Zone A").subentry_id,
            ATTR_CYCLE: "morning",
            ATTR_DURATION: duration,
        },
    )

    assert zone_of(entry, "Zone A").data[CONF_MORNING_DURATION] == duration

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_duration_that_would_overlap_the_cycles_raises(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 5: the engine's overlap rule guards the service, not just the flows.

    Needs its own entry: with the representative 07:00/20:00 starts, thirteen
    hours separate the cycles and no duration inside the 1-120 minute range
    could ever collide. Here the evening cycle starts at 08:00, so two zones
    of ten morning minutes fit (07:00-07:20) and stretching one to 60 does
    not (07:00-08:10).

    The math itself comes from `overlap_offender` — the service re-implements
    none of it (AD-5), which is the point of the assertion.
    """
    freezer.move_to("2026-07-31 06:00:00+02:00")
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options={**CONTROLLER_OPTIONS, CONF_EVENING_START: "08:00:00"},
        subentries_data=[
            zone_subentry_data("Zone A", VALVE_1),
            zone_subentry_data("Zone B", VALVE_2),
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    stored = dict(zone_of(config_entry, "Zone A").data)

    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass,
            SERVICE_SET_ZONE_DURATION,
            {
                ATTR_ZONE_ID: zone_of(config_entry, "Zone A").subentry_id,
                ATTR_CYCLE: "morning",
                ATTR_DURATION: 60,
            },
        )

    assert raised.value.translation_key == "cycles_overlap"
    assert dict(zone_of(config_entry, "Zone A").data) == stored

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_set_zone_duration_does_not_refuse_a_running_cycle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
) -> None:
    """FR8 says "edit durations at any moment" — refusing would contradict it.

    The service grows no "is a cycle running?" branch (this test pins that
    deliberate choice so a well-meaning guard cannot slip in unnoticed): the
    write lands at once, the ONE update listener defers the reload until the
    running cycle completes (Story 1.7), the cycle waters its snapshotted
    durations, and the new value reaches the engine after it.
    """
    register_switch_domain(hass)
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    runtime_data_before = entry.runtime_data
    sequencer_before = runtime_data_before.sequencer
    assert sequencer_before.current_run is not None

    await call(
        hass,
        SERVICE_SET_ZONE_DURATION,
        {
            ATTR_ZONE_ID: zone_of(entry, "Zone A").subentry_id,
            ATTR_CYCLE: "morning",
            ATTR_DURATION: 12,
        },
    )
    await hass.async_block_till_done()

    # Written at once, no reload while running, the flag raised.
    assert zone_of(entry, "Zone A").data[CONF_MORNING_DURATION] == 12
    assert entry.runtime_data is runtime_data_before
    assert entry.runtime_data.runner.reload_pending is True

    # Zone A still closes at its snapshotted 07:10 (ten minutes, not twelve).
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    run = sequencer_before.current_run
    assert run is not None
    assert run.zone_runs[0].actual_end is not None
    assert run.zone_runs[0].duration_s == 600
    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")

    # The cycle completed and the ONE reload rebuilt the engine on 12 minutes.
    assert run.status is CycleStatus.COMPLETED
    assert entry.runtime_data is not runtime_data_before
    plan = entry.runtime_data.plan
    zone = next(one for one in plan.zones if one.name == "Zone A")
    assert zone.morning_duration_s == 12 * 60

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
