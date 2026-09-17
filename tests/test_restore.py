"""Restore matrix through the real setup path (Story 3.2, AC 1-5) — IU-style.

Each test seeds `.storage` with the journal a crashed engine leaves behind,
puts the switches in the state a restart would find them, sets the entry up
and asserts the whole chain: the switch service sequence, the final
`hass.states`, `current_run`/`last_run`, the history record's `recovery`
marker, the ledger's deficits, the Repairs issue, the single push, the bus
event and the health sensor.

Two setup orders, both real:

- **reload** (`hass` RUNNING): the switch component is loaded FIRST so the
  fake hardware registered before the entry stays the last-wins handler, and
  reconciliation runs inside `async_setup_entry`;
- **boot** (`hass` STARTING): the entry is set up, the hardware appears, and
  reconciliation waits for `EVENT_HOMEASSISTANT_STARTED`.
"""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import CoreState, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_irrigation_controller.adapters.journal import STORAGE_KEY
from custom_components.ha_irrigation_controller.const import (
    DOMAIN,
    EVENT_HA_IRRIGATION_CONTROLLER,
)
from custom_components.ha_irrigation_controller.engine.runs import (
    CycleStatus,
    ZoneRunStatus,
)
from tests.common import (
    CONTROLLER_OPTIONS,
    MORNING_START_UTC,
    PUMP,
    VALVE_1,
    VALVE_2,
    crashed_during_zone_a,
    fire_at,
    history_record,
    journal_document,
    register_notify_domain,
    register_switch_domain,
    run_document,
    zone_a_document,
    zone_b_document,
    zone_subentry_data,
)

if TYPE_CHECKING:
    import pytest
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant, ServiceCall

ALL_OFF: dict[str, str] = {PUMP: STATE_OFF, VALVE_1: STATE_OFF, VALVE_2: STATE_OFF}


def two_zone_entry(*, valve_a: str = VALVE_1) -> MockConfigEntry:
    """Build the controller with Zone A/B under the STABLE ids the journal names."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[
            {**zone_subentry_data("Zone A", valve_a), "subentry_id": "zone-a"},
            {**zone_subentry_data("Zone B", VALVE_2), "subentry_id": "zone-b"},
        ],
    )


def commands(calls: list[ServiceCall]) -> list[tuple[str, str]]:
    """Return the (service, entity_id) sequence the fake hardware received."""
    return [(call.service, call.data[ATTR_ENTITY_ID]) for call in calls]


def record_events(hass: HomeAssistant) -> list[Event]:
    """Collect every event of the ONE bus type, in order."""
    events: list[Event] = []

    @callback
    def _record(event: Event) -> None:
        events.append(event)

    hass.bus.async_listen(EVENT_HA_IRRIGATION_CONTROLLER, _record)
    return events


def anomaly_events(events: list[Event]) -> list[tuple[str, str]]:
    """Return (event_type, anomaly) per event."""
    return [(event.data["event_type"], event.data["anomaly"]) for event in events]


def health_of(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the health binary sensor's state."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor",
        DOMAIN,
        f"{entry.entry_id}_health",
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


def sensor_state(hass: HomeAssistant, unique_id: str) -> Any:
    """Return a sensor's state object by unique id."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def restart(  # noqa: PLR0913 — the matrix's one setup helper: clock, hardware, journal
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    *,
    now: str,
    states: dict[str, str],
    valve_a: str = VALVE_1,
    **sections: object,
) -> tuple[MockConfigEntry, list[ServiceCall], list[Event], list[ServiceCall]]:
    """Seed the journal and the hardware, set the entry up while HA is RUNNING.

    Returns the entry, the switch calls, the bus events and the notify calls.
    `valve_a` is the id Zone A's subentry stores — the fake hardware owns it.
    """
    freezer.move_to(now)
    hass_storage[STORAGE_KEY] = journal_document(**sections)
    assert await async_setup_component(hass, "switch", {})
    calls = register_switch_domain(
        hass,
        extra_hardware=() if valve_a == VALVE_1 else (valve_a,),
        seed_states=False,
    )
    for entity_id, state in states.items():
        hass.states.async_set(entity_id, state)
    pushes = register_notify_domain(hass)
    events = record_events(hass)
    entry = two_zone_entry(valve_a=valve_a)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry, calls, events, pushes


async def unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Unload the entry and settle the loop."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Same irrigation day: resume
# --------------------------------------------------------------------------


async def test_crash_during_a_zone_closes_orphans_first_then_resumes(  # noqa: PLR0915 — the whole chain of one recovery, asserted end to end on purpose
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 1, 2, 4: valve+pump ON at load → off, off, then pump on, valve re-opened.

    Zone A proved its watering (valve ON since 07:00), so it keeps its start
    and finishes at 07:10 on its journaled duration; zone B follows. One
    `cycle_recovered` issue, one push, one `anomaly` event, health `on`
    until acknowledged — and completion settles the cycle once, with the
    history record marked `resumed`.
    """
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 07:04:00+02:00",
        states={PUMP: STATE_ON, VALVE_1: STATE_ON, VALVE_2: STATE_OFF},
        run=crashed_during_zone_a(),
    )

    assert commands(calls) == [
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]
    assert hass.states.is_state(PUMP, STATE_ON)
    assert hass.states.is_state(VALVE_1, STATE_ON)
    assert hass.states.is_state(VALVE_2, STATE_OFF)
    sequencer = entry.runtime_data.sequencer
    run = sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.recovery == "resumed"
    zone_a, zone_b = run.zone_runs
    assert zone_a.status is ZoneRunStatus.RUNNING
    assert zone_a.actual_start is not None
    assert zone_a.actual_start.isoformat() == "2026-07-31T07:00:00+02:00"
    assert zone_a.planned_end.isoformat() == "2026-07-31T07:10:00+02:00"
    assert zone_b.planned_start.isoformat() == "2026-07-31T07:10:00+02:00"
    assert sensor_state(hass, f"{entry.entry_id}_cycle_status").state == "running"

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "cycle_recovered")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["outcome"] == "resumed"
    assert issue.translation_placeholders["zone_id"] == "zone-a"
    assert issue.translation_placeholders["cycle_id"] == "2026-07-31-morning"
    assert [
        record.issue_id for record in entry.runtime_data.anomalies.open_anomalies
    ] == [
        "cycle_recovered",
    ]
    assert anomaly_events(events) == [("anomaly", "cycle_recovered")]
    assert len(pushes) == 1
    assert "cycle recovered" in pushes[0].data["title"]
    assert "outcome resumed" in pushes[0].data["message"]
    assert health_of(hass, entry) == STATE_ON

    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert commands(calls)[4:] == [("turn_off", VALVE_1), ("turn_on", VALVE_2)]
    await fire_at(hass, freezer, "2026-07-31 07:20:00+02:00")
    assert commands(calls)[6:] == [("turn_off", VALVE_2), ("turn_off", PUMP)]
    assert hass.states.is_state(PUMP, STATE_OFF)
    assert hass.states.is_state(VALVE_2, STATE_OFF)
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert finished.recovery == "resumed"
    assert sequencer.ledger.settled_cycle_id == "2026-07-31-morning"
    assert sequencer.ledger.deficit_s("zone-a") == 0
    assert sequencer.ledger.deficit_s("zone-b") == 0
    # Still one issue, still one push: the recovery is acknowledge-only.
    assert len(pushes) == 1
    assert health_of(hass, entry) == STATE_ON

    ir.async_delete_issue(hass, DOMAIN, "cycle_recovered")
    await hass.async_block_till_done()
    assert health_of(hass, entry) == STATE_OFF

    await unload(hass, entry)
    history = hass_storage[STORAGE_KEY]["data"]["history"]
    assert [(entry_["status"], entry_["recovery"]) for entry_ in history] == [
        ("completed", "resumed"),
    ]


async def test_crash_with_the_pump_left_on_and_the_valve_off_re_runs_the_zone(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Matrix "pump left on": orphan pump off; the unproven zone re-runs from now."""
    entry, calls, _, _ = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 07:04:00+02:00",
        states={PUMP: STATE_ON, VALVE_1: STATE_OFF, VALVE_2: STATE_OFF},
        run=crashed_during_zone_a(),
    )

    assert commands(calls) == [
        ("turn_off", PUMP),
        ("turn_on", PUMP),
        ("turn_on", VALVE_1),
    ]
    run = entry.runtime_data.sequencer.current_run
    assert run is not None
    zone_a = run.zone_runs[0]
    assert zone_a.actual_start is not None
    assert zone_a.actual_start.isoformat() == "2026-07-31T07:04:00+02:00"
    assert zone_a.planned_end.isoformat() == "2026-07-31T07:14:00+02:00"
    assert run.zone_runs[1].planned_end.isoformat() == "2026-07-31T07:24:00+02:00"

    await unload(hass, entry)


async def test_a_valve_renamed_between_the_crash_and_the_restart_is_recovered_under_its_new_id(  # noqa: E501 — the name is the scenario
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 1.7 rewrote Zone A's subentry to the new id; the journal names the old one.

    The rename landed mid-cycle (the tracker rewrites the stored config and
    the running adapter's aliases), then HA crashed. At the restart the plan
    says `switch.front_valve`, the journaled run says `switch.zone_1_valve`
    — a dead id with no state. The recovery reads, closes and re-opens the
    REAL valve, and the journal carries the new id from here on.
    """
    renamed = "switch.front_valve"
    entry, calls, _, _ = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 07:04:00+02:00",
        states={PUMP: STATE_ON, renamed: STATE_ON, VALVE_2: STATE_OFF},
        valve_a=renamed,
        run=crashed_during_zone_a(),
    )

    assert commands(calls) == [
        ("turn_off", renamed),
        ("turn_off", PUMP),
        ("turn_on", PUMP),
        ("turn_on", renamed),
    ]
    assert hass.states.is_state(renamed, STATE_ON)
    assert hass.states.get(VALVE_1) is None
    run = entry.runtime_data.sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].valve_entity_id == renamed
    assert run.zone_runs[0].status is ZoneRunStatus.RUNNING
    assert entry.runtime_data.anomalies.open_anomalies[0].issue_id == "cycle_recovered"
    assert len(entry.runtime_data.anomalies.open_anomalies) == 1

    await unload(hass, entry)
    stored_run = hass_storage[STORAGE_KEY]["data"]["run"]
    assert stored_run["zones"][0]["valve_entity_id"] == renamed


async def test_crash_before_start_restarts_the_cycle_from_now(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Matrix "crash before start": PENDING, same day → re-timed and started at once."""
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 07:03:00+02:00",
        states=ALL_OFF,
        run=run_document(zone_a_document(), zone_b_document(), status="pending"),
    )

    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]
    run = entry.runtime_data.sequencer.current_run
    assert run is not None
    assert run.status is CycleStatus.RUNNING
    assert run.scheduled_start.isoformat() == "2026-07-31T07:03:00+02:00"
    assert run.configured_start.isoformat() == "2026-07-31T07:00:00+02:00"
    assert run.zone_runs[1].planned_end.isoformat() == "2026-07-31T07:23:00+02:00"
    assert anomaly_events(events) == [("anomaly", "cycle_recovered")]
    assert len(pushes) == 1

    await unload(hass, entry)


async def test_crash_between_zones_resumes_at_the_next_zone(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Matrix "crash between zones": zone A done, zone B never opened, pump left on."""
    entry, calls, _, _ = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 07:12:00+02:00",
        states={PUMP: STATE_ON, VALVE_1: STATE_OFF, VALVE_2: STATE_OFF},
        run=run_document(
            zone_a_document(
                status="completed",
                actual_start=MORNING_START_UTC,
                actual_end="2026-07-31T05:10:00+00:00",
                open_confirmed=True,
                close_confirmed=True,
            ),
            zone_b_document(),
        ),
        zone_index=1,
    )

    assert commands(calls) == [
        ("turn_off", PUMP),
        ("turn_on", PUMP),
        ("turn_on", VALVE_2),
    ]
    run = entry.runtime_data.sequencer.current_run
    assert run is not None
    zone_b = run.zone_runs[1]
    assert zone_b.actual_start is not None
    assert zone_b.actual_start.isoformat() == "2026-07-31T07:12:00+02:00"
    assert zone_b.planned_end.isoformat() == "2026-07-31T07:22:00+02:00"

    await fire_at(hass, freezer, "2026-07-31 07:22:00+02:00")

    assert commands(calls)[3:] == [("turn_off", VALVE_2), ("turn_off", PUMP)]
    finished = entry.runtime_data.sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert entry.runtime_data.sequencer.ledger.deficit_s("zone-b") == 0

    await unload(hass, entry)


# --------------------------------------------------------------------------
# A later irrigation day: close
# --------------------------------------------------------------------------


async def test_next_day_restart_closes_the_run_and_carries_the_deficits(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 3: orphans off, run `interrupted`, zone B's shortfall in the ledger, `closed`.

    Zone A's valve was found ON, so its (long) span is credited and it owes
    nothing; zone B never ran and owes its whole slot. Nothing is re-opened.
    The zone sensors show the closed run: zone B at 0 s with its debt pending.
    """
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-08-01 07:04:00+02:00",
        states={PUMP: STATE_ON, VALVE_1: STATE_ON, VALVE_2: STATE_OFF},
        run=crashed_during_zone_a(),
    )

    assert commands(calls) == [("turn_off", VALVE_1), ("turn_off", PUMP)]
    assert hass.states.is_state(PUMP, STATE_OFF)
    assert hass.states.is_state(VALVE_1, STATE_OFF)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    run = sequencer.last_run
    assert run is not None
    assert run.status is CycleStatus.INTERRUPTED
    assert run.recovery == "closed"
    assert run.zone_runs[0].status is ZoneRunStatus.COMPLETED
    assert run.zone_runs[1].status is ZoneRunStatus.PENDING
    assert sequencer.ledger.settled_cycle_id == "2026-07-31-morning"
    assert sequencer.ledger.deficit_s("zone-a") == 0
    assert sequencer.ledger.deficit_s("zone-b") == 600

    assert sensor_state(hass, f"{entry.entry_id}_cycle_status").state == "idle"
    zone_b_sensor = sensor_state(
        hass, f"{entry.entry_id}_zone-b_last_watering_duration"
    )
    assert zone_b_sensor.state == "0"
    assert zone_b_sensor.attributes["pending_deficit"] == 600
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "cycle_recovered")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["outcome"] == "closed"
    assert anomaly_events(events) == [("anomaly", "cycle_recovered")]
    assert len(pushes) == 1
    assert "outcome closed" in pushes[0].data["message"]
    assert health_of(hass, entry) == STATE_ON
    # No timer is left on the closed run; what remains is Story 3.3's
    # watchdog deadline on THIS day's own morning window end, still open.
    assert sequencer.next_wakeup(dt_util.now()) == dt_util.parse_datetime(
        "2026-08-01 07:20:00+02:00"
    )

    await unload(hass, entry)
    stored = hass_storage[STORAGE_KEY]["data"]
    history = stored["history"]
    assert [(h["cycle_id"], h["status"], h["recovery"]) for h in history] == [
        ("2026-07-31-morning", "interrupted", "closed"),
    ]
    assert history[0]["zones"][1] == {
        "zone_id": "zone-b",
        "status": "pending",
        "planned_s": 600,
        "carried_s": 0,
        "rain_credit_s": 0,
        "effective_s": 0,
    }
    assert stored["ledger"]["deficits"] == {"zone-b": 600}


async def test_next_day_restart_with_the_valve_off_books_the_live_zone_too(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Unproven watering on a later day: zone A FAILED at 0 s, deficits capped."""
    entry, calls, _, _ = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-08-01 07:04:00+02:00",
        states={PUMP: STATE_ON, VALVE_1: STATE_UNAVAILABLE, VALVE_2: STATE_OFF},
        run=crashed_during_zone_a(),
    )

    # `unavailable` is "not OFF": commanded off (a no-op call on the fake,
    # which then reports OFF and confirms).
    assert commands(calls) == [("turn_off", VALVE_1), ("turn_off", PUMP)]
    sequencer = entry.runtime_data.sequencer
    run = sequencer.last_run
    assert run is not None
    assert run.zone_runs[0].status is ZoneRunStatus.FAILED
    assert sequencer.ledger.deficit_s("zone-a") == 600
    assert sequencer.ledger.deficit_s("zone-b") == 600

    await unload(hass, entry)


# --------------------------------------------------------------------------
# Nothing to recover, or nothing trustworthy
# --------------------------------------------------------------------------


async def test_a_nominal_idle_restart_is_silent(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 5: idle journal, fresh history → no command, no anomaly, no event, no write.

    The stored document is untouched by the whole load/unload — the flush
    has nothing pending because the engine never saved.
    """
    completed = run_document(
        zone_a_document(
            status="completed",
            actual_start="2026-07-30T17:00:00+00:00",
            actual_end="2026-07-30T17:10:00+00:00",
            open_confirmed=True,
            close_confirmed=True,
        ),
        status="completed",
        cycle_id="2026-07-30-evening",
        kind="evening",
        configured_start="2026-07-30T17:00:00+00:00",
        scheduled_start="2026-07-30T17:00:00+00:00",
        pump_off_confirmed=True,
    )
    history = [
        {
            "cycle_id": "2026-07-30-evening",
            "irrigation_day": "2026-07-30",
            "kind": "evening",
            "status": "completed",
            "manual": False,
            "waived_by": None,
            "recovery": None,
            "zones": [],
        },
    ]
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 06:30:00+02:00",
        states=ALL_OFF,
        run=None,
        last_run=completed,
        deferred=[],
        history=history,
    )
    before = copy.deepcopy(hass_storage[STORAGE_KEY])

    assert calls == []
    assert events == []
    assert pushes == []
    assert entry.runtime_data.anomalies.open_anomalies == ()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "cycle_recovered") is None
    assert health_of(hass, entry) == STATE_OFF
    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    assert sequencer.last_run is not None
    assert sequencer.last_run.cycle_id == "2026-07-30-evening"
    assert sensor_state(
        hass, f"{entry.entry_id}_zone-a_last_watering_duration"
    ).state == ("600")

    await unload(hass, entry)
    assert hass_storage[STORAGE_KEY] == before


async def test_a_completed_run_left_under_run_is_the_last_run_not_a_recovery(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The completion write files the finished run under `run`: nominal, silent."""
    completed = run_document(
        zone_a_document(
            status="completed",
            actual_start=MORNING_START_UTC,
            actual_end="2026-07-31T05:10:00+00:00",
            open_confirmed=True,
            close_confirmed=True,
        ),
        zone_b_document(
            status="completed",
            actual_start="2026-07-31T05:10:00+00:00",
            actual_end="2026-07-31T05:20:00+00:00",
            open_confirmed=True,
            close_confirmed=True,
        ),
        status="completed",
        pump_off_confirmed=True,
    )
    entry, calls, events, _ = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 09:00:00+02:00",
        states=ALL_OFF,
        run=completed,
        # The completion write put the record and the run in the SAME save, so
        # a restart reading this journal knows the morning ran — and Story
        # 3.3's watchdog has nothing to make up for.
        history=[history_record()],
    )

    assert calls == []
    assert events == []
    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    assert sequencer.last_run is not None
    assert sequencer.last_run.status is CycleStatus.COMPLETED

    await unload(hass, entry)


async def test_an_unreadable_run_closes_every_configured_switch(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matrix "unreadable run": ALL valves then the pump off, `discarded`, unbooked."""
    hass_storage.clear()
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 07:04:00+02:00",
        states={PUMP: STATE_ON, VALVE_1: STATE_ON, VALVE_2: STATE_ON},
        run={"cycle_id": "2026-07-31-morning", "status": "running", "zones": "?"},
        ledger={"settled_cycle_id": "2026-07-30-evening", "deficits": {"zone-b": 120}},
    )

    assert commands(calls) == [
        ("turn_off", VALVE_1),
        ("turn_off", VALVE_2),
        ("turn_off", PUMP),
    ]
    assert hass.states.is_state(PUMP, STATE_OFF)
    assert hass.states.is_state(VALVE_1, STATE_OFF)
    assert hass.states.is_state(VALVE_2, STATE_OFF)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    assert sequencer.last_run is None
    assert sequencer.ledger.deficit_s("zone-b") == 120
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "cycle_recovered")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["outcome"] == "discarded"
    assert issue.translation_placeholders["cycle_id"] == "2026-07-31-morning"
    assert anomaly_events(events) == [("anomaly", "cycle_recovered")]
    assert len(pushes) == 1
    assert any(
        record.levelno == logging.WARNING
        and DOMAIN in record.name
        and "unreadable" in record.getMessage()
        and "zones" in record.getMessage()
        for record in caplog.records
    )

    await unload(hass, entry)
    assert hass_storage[STORAGE_KEY]["data"]["run"] is None
    assert hass_storage[STORAGE_KEY]["data"]["ledger"]["deficits"] == {"zone-b": 120}


# --------------------------------------------------------------------------
# Boot: reconciliation waits for Home Assistant to have started
# --------------------------------------------------------------------------


async def test_boot_time_recovery_waits_for_homeassistant_started(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """A real boot: the entry loads while switches are still unknown; nothing moves.

    Once `EVENT_HOMEASSISTANT_STARTED` fires the hardware has reported: zone
    A's valve is ON and its slot has elapsed by then, so it is closed as
    COMPLETED at `now` (over-watering booked honestly) and zone B opens.
    """
    freezer.move_to("2026-07-31 07:04:00+02:00")
    hass_storage[STORAGE_KEY] = journal_document(run=crashed_during_zone_a())
    hass.set_state(CoreState.starting)
    events = record_events(hass)
    entry = two_zone_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The hardware's integration loads after ours: only now do the switches
    # have a state — and the fake, registered after the platform forwarding,
    # is the last-wins handler.
    calls = register_switch_domain(hass, seed_states=False)
    hass.states.async_set(PUMP, STATE_ON)
    hass.states.async_set(VALVE_1, STATE_ON)
    hass.states.async_set(VALVE_2, STATE_OFF)

    sequencer = entry.runtime_data.sequencer
    assert sequencer.reconciled is False
    assert calls == []
    assert events == []
    # No daily start and no boundary is armed before the reconcile.
    await fire_at(hass, freezer, "2026-07-31 07:12:00+02:00")
    assert calls == []
    run = sequencer.current_run
    assert run is not None
    assert run.zone_runs[0].actual_end is None

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert sequencer.reconciled is True
    assert commands(calls) == [
        ("turn_off", VALVE_1),
        ("turn_off", PUMP),
        ("turn_on", PUMP),
        ("turn_on", VALVE_2),
    ]
    zone_a, zone_b = run.zone_runs
    assert zone_a.status is ZoneRunStatus.COMPLETED
    assert zone_a.actual_end is not None
    assert zone_a.actual_end.isoformat() == "2026-07-31T07:12:00+02:00"
    assert zone_b.actual_start is not None
    assert zone_b.actual_start.isoformat() == "2026-07-31T07:12:00+02:00"
    assert anomaly_events(events) == [("anomaly", "cycle_recovered")]

    # The re-armed boundary drives the rest.
    await fire_at(hass, freezer, "2026-07-31 07:22:00+02:00")
    assert commands(calls)[4:] == [("turn_off", VALVE_2), ("turn_off", PUMP)]
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED

    hass.set_state(CoreState.running)
    await unload(hass, entry)


# --------------------------------------------------------------------------
# A window Home Assistant slept through: the missed-cycle watchdog (Story 3.3)
# --------------------------------------------------------------------------


async def test_a_slept_through_window_is_re_run_once_and_only_once(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 3.3 AC 1 + AC 2 at the integration level, across two restarts.

    Home Assistant was down from before 07:00 to 09:00 — the journal holds no
    run, no history and nothing to recover — so setup files the morning cycle
    as `missed`, raises ONE `missed_cycle` issue with one push and one event,
    and waters it at once on quoted durations. A second setup the same day,
    reading the journal the first one left, re-runs nothing and says nothing.
    """
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 09:00:00+02:00",
        states=ALL_OFF,
        run=None,
    )

    sequencer = entry.runtime_data.sequencer
    run = sequencer.current_run
    assert run is not None
    assert run.cycle_id == "2026-07-31-morning-2"
    assert run.late_rerun is True
    assert run.scheduled_start.isoformat() == "2026-07-31T09:00:00+02:00"
    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "missed_cycle")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["outcome"] == "rerun"
    assert issue.translation_placeholders["kind"] == "morning"
    assert anomaly_events(events) == [("anomaly", "missed_cycle")]
    assert len(pushes) == 1
    assert health_of(hass, entry) == STATE_ON

    # The make-up cycle runs itself out through the ONE re-armed timer.
    await fire_at(hass, freezer, "2026-07-31 09:10:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 09:20:00+02:00")
    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    await unload(hass, entry)

    stored = hass_storage[STORAGE_KEY]["data"]
    assert [(record["cycle_id"], record["status"]) for record in stored["history"]] == [
        ("2026-07-31-morning", "missed"),
        ("2026-07-31-morning-2", "completed"),
    ]

    # A second restart the same day, on THAT journal: the `missed` record is
    # the at-most-once marker, so nothing is detected, watered or reported.
    freezer.move_to("2026-07-31 09:30:00+02:00")
    again = two_zone_entry()
    again.add_to_hass(hass)
    calls.clear()
    events.clear()
    pushes.clear()
    assert await hass.config_entries.async_setup(again.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert calls == []
    assert events == []
    assert pushes == []
    assert again.runtime_data.sequencer.current_run is None

    await unload(hass, again)


async def test_a_nominal_restart_after_both_cycles_ran_is_silent(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 3.3 AC 3: the counter-metric, at the integration level.

    Both of the day's cycles are in history, so a restart after the evening
    window commands nothing, files nothing and notifies nothing.
    """
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 21:00:00+02:00",
        states=ALL_OFF,
        run=None,
        history=[history_record("morning"), history_record("evening")],
    )

    assert calls == []
    assert events == []
    assert pushes == []
    assert entry.runtime_data.sequencer.current_run is None
    assert health_of(hass, entry) == STATE_OFF

    await unload(hass, entry)


async def test_a_miss_after_the_days_later_cycle_is_recorded_not_re_run(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 3.3 AC 5: `outcome: recorded`, no run, one base duration of debt."""
    entry, calls, _events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 20:40:00+02:00",
        states=ALL_OFF,
        run=None,
        history=[history_record("evening")],
    )

    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    assert calls == []
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "missed_cycle")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["outcome"] == "recorded"
    assert len(pushes) == 1
    # Each zone carries its base duration forward — never more (FR13's cap).
    assert sequencer.ledger.deficit_s("zone-a") == 600
    assert sequencer.ledger.deficit_s("zone-b") == 600

    await unload(hass, entry)


async def test_a_fresh_entry_set_up_past_a_window_end_makes_nothing_up(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 3.3's floor, end to end: a first install is never late for anything.

    No journal at all — a controller configured for the first time at 16:00,
    long past the morning window. There is nothing to recover and, crucially,
    nothing to make up: the cycle closed before this controller existed, so
    nobody skipped it. The reconcile stamps that instant as the floor and
    everything after it is watched normally.

    This is the case that made the config-flow suite time-of-day dependent:
    without the floor, any real-clock test setting an entry up after 07:20
    would file a miss and start watering.
    """
    hass_storage.clear()
    freezer.move_to("2026-07-31 16:00:00+02:00")
    assert await async_setup_component(hass, "switch", {})
    calls = register_switch_domain(hass, seed_states=False)
    for entity_id, state in ALL_OFF.items():
        hass.states.async_set(entity_id, state)
    pushes = register_notify_domain(hass)
    events = record_events(hass)
    entry = two_zone_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    assert calls == []
    assert events == []
    assert pushes == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missed_cycle") is None
    assert health_of(hass, entry) == STATE_OFF

    # The one thing the reconcile DID write is the floor itself (the debounced
    # write lands a few seconds later, which commands nothing).
    await fire_at(hass, freezer, "2026-07-31 16:00:05+02:00")
    stored = hass_storage[STORAGE_KEY]["data"]
    assert stored["history"] == []
    assert stored["run"] is None
    assert stored["watchdog_since"] == "2026-07-31T14:00:00+00:00"
    assert calls == []

    # The evening it WILL be there for runs from its own daily start, and the
    # window end that follows finds it recorded: still nothing made up, and
    # still not one anomaly on a controller installed hours after a window.
    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    run = sequencer.current_run
    assert run is not None
    assert run.cycle_id == "2026-07-31-evening"
    assert run.late_rerun is False
    await fire_at(hass, freezer, "2026-07-31 20:15:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 20:30:00+02:00")

    assert sequencer.current_run is None
    assert events == []
    assert pushes == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missed_cycle") is None

    await unload(hass, entry)


# --------------------------------------------------------------------------
# A start deferred before the restart (Story 3.4, Decision 5)
# --------------------------------------------------------------------------


async def test_a_restored_deferral_waters_when_the_restart_ends_idle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 3.4 AC 5: the pause dies with the process; the queue it fed does not.

    Home Assistant restarts while the operator holds a valve open and the
    morning start is queued behind the pause. `_manual_on` is LIVE state and
    is not journalled, so the pause is simply gone; the queue is in the
    journal, and reconciliation — ending with nothing running and nothing
    held open — drains it. The cycle waters, late rather than lost.
    """
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 07:05:00+02:00",
        states=ALL_OFF,
        deferred=[{"kind": "morning", "reference": MORNING_START_UTC}],
    )

    sequencer = entry.runtime_data.sequencer
    assert sequencer.manual_override is False
    assert sequencer.deferred_kinds == ()
    run = sequencer.current_run
    assert run is not None
    assert run.cycle_id == "2026-07-31-morning"
    assert run.status is CycleStatus.RUNNING
    assert run.late_rerun is False
    assert commands(calls) == [("turn_on", PUMP), ("turn_on", VALVE_1)]
    # A deferred cycle finally watering is not a failure: nothing reported.
    assert events == []
    assert pushes == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missed_cycle") is None

    await fire_at(hass, freezer, "2026-07-31 07:15:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:25:00+02:00")

    finished = sequencer.last_run
    assert finished is not None
    assert finished.status is CycleStatus.COMPLETED
    assert events == []
    assert pushes == []

    await unload(hass, entry)


async def test_a_stale_restored_deferral_is_dropped_and_waters_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Yesterday's queued cycle is the watchdog's business, never the drain's."""
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 06:30:00+02:00",
        states=ALL_OFF,
        deferred=[{"kind": "evening", "reference": "2026-07-30T18:00:00+00:00"}],
    )

    sequencer = entry.runtime_data.sequencer
    assert sequencer.deferred_kinds == ()
    assert sequencer.current_run is None
    assert calls == []
    assert events == []
    assert pushes == []

    await unload(hass, entry)


async def test_a_nominal_restart_with_an_empty_queue_still_does_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The other half of Decision 5: the drain must not make a restart chatty.

    Nothing running, nothing queued, fresh history and a floor already
    stamped: no command, no anomaly, no push and no journal write at all.
    """
    entry, calls, events, pushes = await restart(
        hass,
        hass_storage,
        freezer,
        now="2026-07-31 06:30:00+02:00",
        states=ALL_OFF,
        history=[history_record()],
        deferred=[],
    )
    stored_before = copy.deepcopy(hass_storage[STORAGE_KEY]["data"])

    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    assert sequencer.deferred_kinds == ()
    assert calls == []
    assert events == []
    assert pushes == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missed_cycle") is None

    # The debounced write window passes with nothing to write.
    await fire_at(hass, freezer, "2026-07-31 06:30:05+02:00")
    assert hass_storage[STORAGE_KEY]["data"] == stored_before
    assert calls == []

    await unload(hass, entry)
