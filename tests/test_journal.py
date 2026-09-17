"""Journal adapter: debounced writes to the single versioned Store (AC 4, AD-2)."""

from __future__ import annotations

import logging
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ha_irrigation_controller.adapters.journal import (
    JOURNAL_SAVE_DEBOUNCE_S,
    STORAGE_KEY,
    JournalAdapter,
)
from custom_components.ha_irrigation_controller.const import (
    ATTR_CYCLE,
    DOMAIN,
    SERVICE_CANCEL_CYCLE,
    SERVICE_RUN_NOW,
)
from custom_components.ha_irrigation_controller.engine.plan import CycleKind
from custom_components.ha_irrigation_controller.engine.runs import CycleStatus
from custom_components.ha_irrigation_controller.engine.sequencer import (
    JOURNAL_SCHEMA_VERSION,
)
from tests.common import (
    CONTROLLER_OPTIONS,
    controller_entry,
    crashed_during_zone_a,
    fire_at,
    journal_document,
    register_switch_domain,
    run_document,
    zone_a_document,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant


def snapshot(marker: int) -> dict[str, object]:
    """Build a distinguishable engine-like snapshot."""
    return {"schema_version": JOURNAL_SCHEMA_VERSION, "history": [], "run": marker}


# The ledger section that owes nothing — what every unreadable section reads as.
EMPTY_LEDGER: dict[str, object] = {
    "settled_cycle_id": None,
    "deficits": {},
    "day_credit": None,
    "rain_baselines": {},
    "rain_source": None,
}

# The gauge as the representative controller entry (`CONTROLLER_OPTIONS`) sees it.
GAUGE = "sensor.rain_gauge"


def set_gauge(hass: HomeAssistant, total: str) -> None:
    """Put a cumulative total in mm on the configured rain sensor."""
    hass.states.async_set(GAUGE, total, {"unit_of_measurement": "mm"})


async def test_transitions_coalesce_under_the_debounce(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Rapid saves produce ONE write carrying the latest snapshot (NFR2).

    Move the freezer first, then fire: repeated saves push the Store's next
    write time forward, and the rescheduled handle only writes once the loop
    clock (frozen monotonic) has actually passed it.
    """
    adapter = JournalAdapter(hass)

    await adapter.async_save(snapshot(1))
    await adapter.async_save(snapshot(2))
    await adapter.async_save(snapshot(3))
    assert STORAGE_KEY not in hass_storage  # debounced, not yet written

    freezer.tick(timedelta(seconds=JOURNAL_SAVE_DEBOUNCE_S + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass_storage[STORAGE_KEY]["data"]["run"] == 3


async def test_flush_writes_the_pending_snapshot_immediately(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """The unload flush persists the last transition without waiting."""
    adapter = JournalAdapter(hass)

    await adapter.async_save(snapshot(7))
    await adapter.async_flush()

    assert hass_storage[STORAGE_KEY]["data"]["run"] == 7


async def test_flush_without_pending_data_writes_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Flushing an idle adapter must not write an empty document."""
    adapter = JournalAdapter(hass)

    await adapter.async_flush()

    assert STORAGE_KEY not in hass_storage


async def test_stored_document_carries_version_key_and_engine_payload(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Pin the stored document's shape.

    Store version == engine schema version (so the two cannot drift), the key
    is stable, and the payload keeps its own schema_version + history.
    """
    adapter = JournalAdapter(hass)

    await adapter.async_save(snapshot(1))
    await adapter.async_flush()

    document = hass_storage[STORAGE_KEY]
    assert document["version"] == JOURNAL_SCHEMA_VERSION
    assert document["key"] == STORAGE_KEY
    assert document["data"]["schema_version"] == JOURNAL_SCHEMA_VERSION
    assert document["data"]["history"] == []


async def test_unloading_an_entry_flushes_the_last_transition(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """A reload must never drop the last transition (the regime runs constantly).

    Drives a real cycle to a zone boundary, then unloads: the debounced write
    is still pending, and only the `async_on_unload` flush persists it — the
    document on disk until then is the one setup itself left.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # After the setup: forwarding Platform.SWITCH registers the REAL switch
    # services, and `async_register` is last-wins (see `register_switch_domain`).
    register_switch_domain(hass)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    # Setup itself wrote once — Story 3.3's reconcile stamps the watchdog
    # floor — but the cycle that has just started is still inside the
    # debounce, so the document on disk knows nothing about it yet.
    assert hass_storage[STORAGE_KEY]["data"]["run"] is None
    assert hass_storage[STORAGE_KEY]["data"]["watchdog_since"] is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    stored = hass_storage[STORAGE_KEY]["data"]
    assert stored["schema_version"] == JOURNAL_SCHEMA_VERSION
    assert stored["run"]["cycle_id"] == "2026-07-31-morning"
    assert stored["run"]["status"] == "running"


async def test_a_debounced_write_consumes_the_pending_snapshot(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Once the delayed write lands, the flush has nothing left to re-write.

    Otherwise every unload would re-persist a document already on disk — one
    redundant `.storage` write per reload, forever.
    """
    adapter = JournalAdapter(hass)
    await adapter.async_save(snapshot(1))

    freezer.tick(timedelta(seconds=JOURNAL_SAVE_DEBOUNCE_S + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage[STORAGE_KEY]["data"]["run"] == 1

    # A sentinel the flush would have to overwrite if it still had a snapshot.
    hass_storage[STORAGE_KEY] = {"untouched": True}
    await adapter.async_flush()

    assert hass_storage[STORAGE_KEY] == {"untouched": True}


async def test_a_save_after_the_flush_is_dropped(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """An `advance` still in flight at unload must not schedule a late write.

    The flush persists the state nothing can move any more; a delayed write
    armed afterwards would outlive the entry and race the Store of the one
    replacing it (AD-2: ONE writer).
    """
    adapter = JournalAdapter(hass)
    await adapter.async_save(snapshot(1))
    await adapter.async_flush()

    await adapter.async_save(snapshot(2))
    freezer.tick(timedelta(seconds=JOURNAL_SAVE_DEBOUNCE_S + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass_storage[STORAGE_KEY]["data"]["run"] == 1


async def test_a_delayed_write_without_a_snapshot_raises(
    hass: HomeAssistant,
) -> None:
    """The invariant is enforced, not asserted.

    Unreachable through the public API — reached here directly because
    `assert` is compiled out under `python -O`, where the failure mode would
    be `Store` writing `"data": null` over the journal Story 3.2 reads back.
    """
    adapter = JournalAdapter(hass)

    with pytest.raises(RuntimeError, match="no pending snapshot"):
        adapter._pending_snapshot()  # noqa: SLF001 — defensive branch, no public path


async def test_load_seed_returns_the_fail_wet_defaults_when_no_store_exists(
    hass: HomeAssistant,
) -> None:
    """A first run has no journal file at all — and it must still water."""
    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.history == []
    assert seed.season_enabled is True
    assert seed.ledger == EMPTY_LEDGER


@pytest.mark.parametrize(
    "stored",
    [
        {"schema_version": JOURNAL_SCHEMA_VERSION},  # no history section
        {"history": "not-a-list"},
        {"history": None},
    ],
)
async def test_load_seed_tolerates_a_missing_or_malformed_history_section(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    stored: dict[str, object],
) -> None:
    """`.storage` is unvalidated input; a bad section reads as empty, not as a crash."""
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": stored,
    }

    assert (await JournalAdapter(hass).async_load_seed()).history == []


async def test_load_seed_drops_records_the_engine_cannot_consume(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """A malformed day would raise out of `prune_history` → `advance()`.

    That abandons a cycle mid-flight with the pump on, so the trust boundary
    filters rather than trusts — the same rule `build_plan` applies to options.
    """
    good = {
        "irrigation_day": "2026-07-30",
        "cycle_id": "keep-me",
        # A zone record as Story 2.2 writes it: the quoted duration and the
        # carried deficit ride through the seed untouched, like every other
        # nested value.
        "zones": [
            {
                "zone_id": "zone-1",
                "status": "completed",
                "planned_s": 900,
                "carried_s": 300,
                "rain_credit_s": 0,
                "effective_s": 900,
            },
        ],
    }
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "history": [
                good,
                "not-a-record",
                {"cycle_id": "no-day-at-all"},
                {"irrigation_day": 20260730, "cycle_id": "day-not-a-string"},
                {"irrigation_day": "31-07-2026", "cycle_id": "day-not-iso"},
            ],
        },
    }

    # The survivor comes back with the optional keys later stories added filled
    # in — Story 2.1's manual marker, defaulted to scheduled, and Story 2.3's
    # `waived_by`, defaulted to None.
    assert (await JournalAdapter(hass).async_load_seed()).history == [
        {**good, "manual": False, "waived_by": None},
    ]


@pytest.mark.parametrize(
    ("stored_marker", "expected"),
    [
        ({}, False),  # a document written before Story 2.1
        ({"manual": False}, False),
        ({"manual": True}, True),
        ({"manual": None}, False),
        ({"manual": "true"}, False),
        ({"manual": 1}, False),
    ],
    ids=["absent", "scheduled", "manual", "none", "string", "int"],
)
async def test_load_seed_defaults_a_missing_manual_marker_to_scheduled(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    stored_marker: dict[str, object],
    expected: bool,  # noqa: FBT001 — a parametrized expectation, not a flag
) -> None:
    """A journal written before Story 2.1 loads, and its runs read as scheduled.

    `JOURNAL_SCHEMA_VERSION` carries no migration, so every key a story adds
    has to be optional on read — and drift that merely looks truthy is not a
    manual run either. Only a real `True` is one, which is the direction that
    waters: Story 2.3 turns a manual run into a reason NOT to water.
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "history": [
                {"irrigation_day": "2026-07-30", "cycle_id": "x", **stored_marker},
            ],
        },
    }

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.history[0]["manual"] is expected


@pytest.mark.parametrize(
    ("stored_waiver", "expected"),
    [
        ({}, None),  # a document written before Story 2.3
        ({"waived_by": None}, None),
        ({"waived_by": "2026-07-30-morning"}, "2026-07-30-morning"),
        ({"waived_by": 7}, None),
        ({"waived_by": True}, None),
        ({"waived_by": ["2026-07-30-morning"]}, None),
    ],
    ids=["absent", "none", "str", "int", "bool", "list"],
)
async def test_load_seed_defaults_a_missing_waived_by_to_none(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    stored_waiver: dict[str, object],
    expected: str | None,
) -> None:
    """A journal written before Story 2.3 loads with `waived_by` on every record.

    `history_entry` promises the key on every record it writes; the seed
    backfills it for records written before the promise, so no reader has to
    defend against its absence. Only a `str` — a run-now's cycle id — is kept;
    anything else reads as "this cycle was not waived".
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "history": [
                {"irrigation_day": "2026-07-30", "cycle_id": "x", **stored_waiver},
            ],
        },
    }

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.history[0]["waived_by"] == expected


async def test_the_season_flag_round_trips_through_the_store(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 2: season state survives the reload-per-config-change regime.

    Written by the engine through the one writer, read back by the one reader
    — and the seed is what the rebuilt sequencer starts from, so the switch
    entity reports OFF after the reload rather than snapping back to ON.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)

    await entry.runtime_data.runner.async_set_season(enabled=False)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[STORAGE_KEY]["data"]["season_enabled"] is False

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.sequencer.season_enabled is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "stored",
    [
        {"schema_version": JOURNAL_SCHEMA_VERSION},  # the key never existed
        {"season_enabled": None},
        {"season_enabled": "false"},
        {"season_enabled": 0},
    ],
)
async def test_an_absent_or_non_bool_season_seeds_on(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    stored: dict[str, object],
) -> None:
    """Fail-wet (AD-4): unreadable storage waters.

    A document written before this story has no `season_enabled` key at all,
    and `0`/`"false"` are exactly the truthy-looking drift a bare `bool()`
    coercion would silently honour as "season over".
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": stored,
    }

    assert (await JournalAdapter(hass).async_load_seed()).season_enabled is True


@pytest.mark.parametrize(
    "stored",
    [[], "corrupted", 3, None],
)
async def test_a_document_that_is_not_a_mapping_seeds_the_fail_wet_defaults(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    stored: object,
) -> None:
    """The document TYPE is part of the trust boundary, not an assumption.

    `Store` is generic only in its annotation, so a `data` section that is a
    list, a string or a number is a real shape a hand edit or a restored backup
    produces. Trusting the annotation would raise `AttributeError` out of
    `async_setup_entry` — the opposite of the fail-wet default this seam
    promises, and an integration that refuses to load rather than watering.
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": stored,
    }

    seed = await JournalAdapter(hass).async_load_seed()
    assert seed.history == []
    assert seed.season_enabled is True


async def test_a_stored_false_season_is_honoured(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Only a real `bool` is trusted — and a real False really does suspend."""
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {"season_enabled": False},
    }

    assert (await JournalAdapter(hass).async_load_seed()).season_enabled is False


async def test_a_reload_keeps_the_stored_history(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 4's 7-day retention has to survive the reload-per-config-change regime.

    Runs a cycle to completion, unloads, sets the entry up again and drives a
    save: the first cycle's outcome must still be there afterwards.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert [
        record["cycle_id"] for record in hass_storage[STORAGE_KEY]["data"]["history"]
    ] == ["2026-07-31-morning"]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    # The next day's start is enough to make the rebuilt engine journal again.
    await fire_at(hass, freezer, "2026-08-01 07:00:00+02:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert [
        record["cycle_id"] for record in hass_storage[STORAGE_KEY]["data"]["history"]
    ] == ["2026-07-31-morning"]


async def test_removing_the_entry_removes_the_store_file(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Deleting the integration leaves no orphan `.storage` journal behind."""
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": snapshot(1),
    }

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert STORAGE_KEY not in hass_storage


async def test_cycle_ids_stay_unique_across_a_reload(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """The occurrence suffix has to survive the reload-per-config-change regime.

    Epic 2 keys idempotent settlement off the cycle id, so two runs of one kind
    on one irrigation day may never share one. The occurrence number is counted
    from the seeded history for exactly this reason: a private counter is
    journalled but never read back, so it would reset here and file the second
    run under the first one's id. `run_now` makes that reachable in a single
    service call.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # The reload the regime performs on every config change.
    freezer.move_to("2026-07-31 09:30:00+02:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RUN_NOW,
        {ATTR_CYCLE: "morning"},
        blocking=True,
    )
    await fire_at(hass, freezer, "2026-07-31 09:40:00+02:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    stored = hass_storage[STORAGE_KEY]["data"]["history"]
    assert [record["cycle_id"] for record in stored] == [
        "2026-07-31-morning",
        "2026-07-31-morning-2",
    ]
    assert [record["manual"] for record in stored] == [False, True]
    # The run snapshot carries Story 2.4's quote-time reading and per-zone
    # credit too — no gauge state exists in this test, so None and 0. The
    # completion write still files the run under `run` (released into
    # `last_run` only after that save), so that is where the snapshot is.
    completed = hass_storage[STORAGE_KEY]["data"]["run"]
    assert completed["status"] == "completed"
    assert completed["rain_total_mm"] is None
    assert completed["zones"][0]["rain_credit_s"] == 0
    assert [record["zones"][0]["rain_credit_s"] for record in stored] == [0, 0]


async def test_a_journal_written_before_the_manual_marker_still_sets_up(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.1 AC 4, end to end: an existing install upgrades without a break.

    The stored document is one the pre-2.1 code wrote — a complete history
    record with no `manual` key anywhere. Setup must load it, and the record
    must reach the engine (and the journal it writes back) reading as
    scheduled: a run-now marker invented for a cycle nobody triggered would
    make Story 2.3 waive a day that was never manually watered.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    legacy = {
        "cycle_id": "2026-07-30-evening",
        "irrigation_day": "2026-07-30",
        "kind": "evening",
        "status": "completed",
        "configured_start": "2026-07-30T18:00:00+00:00",
        "scheduled_start": "2026-07-30T18:00:00+00:00",
        "ended_at": "2026-07-30T18:10:00+00:00",
        "zones": [{"zone_id": "zone-1", "status": "completed", "effective_s": 600}],
    }
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {"schema_version": JOURNAL_SCHEMA_VERSION, "history": [legacy]},
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)

    # Running one cycle of the NEW code journals the whole section back: the
    # seeded record survives intact apart from the defaulted marker, and the
    # cycle that just ran is scheduled too.
    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    stored = hass_storage[STORAGE_KEY]["data"]["history"]
    assert stored[0] == {**legacy, "manual": False, "waived_by": None}
    assert [record["cycle_id"] for record in stored] == [
        "2026-07-30-evening",
        "2026-07-31-morning",
    ]
    assert [record["manual"] for record in stored] == [False, False]


# --------------------------------------------------------------------------
# The ledger section (Story 2.2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        {"schema_version": JOURNAL_SCHEMA_VERSION},  # a document written before 2.2
        {"ledger": None},
        {"ledger": "corrupted"},
        {"ledger": []},
        {"ledger": {}},
        {"ledger": {"settled_cycle_id": None, "deficits": "not-a-mapping"}},
        {"ledger": {"settled_cycle_id": None, "deficits": [600]}},
        {"ledger": {"settled_cycle_id": 42, "deficits": {}}},
        {"ledger": {"settled_cycle_id": ["x"], "deficits": {"zone-1": 600}}},
    ],
    ids=[
        "absent",
        "none",
        "string",
        "list",
        "empty-mapping",
        "deficits-string",
        "deficits-list",
        "settled-int",
        "settled-list",
    ],
)
async def test_load_seed_reads_a_missing_or_malformed_ledger_as_empty(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    stored: dict[str, object],
) -> None:
    """Matrix "pre-2.2 journal": no section, or a broken one, is an EMPTY ledger.

    `JOURNAL_SCHEMA_VERSION` carries no migration, so the section is optional
    on read; and the engine trusts the seed, so a shape it cannot do
    arithmetic on must never reach it. Empty is the fail-wet reading (AD-4):
    the next cycle is quoted on base durations, and nothing is logged.
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": stored,
    }

    assert (await JournalAdapter(hass).async_load_seed()).ledger == EMPTY_LEDGER


async def test_load_seed_keeps_only_the_deficits_the_engine_can_consume(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Entries are filtered ONE BY ONE: a bad zone must not forget the others.

    Kept: a `str` key with a non-negative `int` value. Dropped: a negative, a
    numeric string, a float, a `bool` (an `int` to Python — the one shape a
    plain `isinstance` would let through) and a non-string key.
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {
                    "zone-1": 300,
                    "zone-2": 0,
                    "zone-3": -5,
                    "zone-4": "600",
                    "zone-5": True,
                    "zone-6": 1.5,
                    7: 600,
                },
            },
        },
    }

    assert (await JournalAdapter(hass).async_load_seed()).ledger == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300, "zone-2": 0},
        "day_credit": None,
        "rain_baselines": {},
        "rain_source": None,
    }


async def test_load_seed_accepts_a_ledger_with_deficits_but_no_settled_id(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """A hand-written section with only `deficits` seeds those deficits.

    An absent `settled_cycle_id` reads as None — the ledger has nothing to
    replay-guard against — and the deficits are still kept, filtered as usual.
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {"ledger": {"deficits": {"zone-1": 300, "zone-2": "bad"}}},
    }

    assert (await JournalAdapter(hass).async_load_seed()).ledger == {
        "settled_cycle_id": None,
        "deficits": {"zone-1": 300},
        "day_credit": None,
        "rain_baselines": {},
        "rain_source": None,
    }


async def test_a_stored_ledger_round_trips_through_a_reload(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 3: a deficit written by the one writer is read back by the one reader.

    A cancel at +5 min of a 10 min morning slot books 300 s. After the
    unload the stored section carries it; after the reload the rebuilt engine
    owes it, and the evening cycle is quoted 900 + 300.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)
    zone_id = next(iter(entry.subentries))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    freezer.move_to("2026-07-31 07:05:00+02:00")
    await hass.services.async_call(DOMAIN, SERVICE_CANCEL_CYCLE, blocking=True)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[STORAGE_KEY]["data"]["ledger"] == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {zone_id: 300},
        "day_credit": None,
        "rain_baselines": {},
        "rain_source": None,
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.ledger.deficit_s(zone_id) == 300

    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    run = sequencer.current_run
    assert run is not None
    assert (run.zone_runs[0].duration_s, run.zone_runs[0].carried_s) == (1200, 300)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# The day credit in the ledger section (Story 2.3)
# --------------------------------------------------------------------------


# A section written before Story 2.3, and every malformed credit a hand edit or
# a restored backup can produce: each reads as NO credit, and the zone's debt
# next to it is kept — the section is never rejected for a bad credit.
@pytest.mark.parametrize(
    "credit",
    [
        {},  # a document written before Story 2.3: no key at all
        {"day_credit": None},
        {"day_credit": "2026-07-31"},
        {"day_credit": []},
        {"day_credit": {}},
        {"day_credit": {"irrigation_day": 5, "cycle_id": "x"}},
        {"day_credit": {"irrigation_day": "31-07-2026", "cycle_id": "x"}},
        {"day_credit": {"irrigation_day": "2026-07-31"}},
        {"day_credit": {"irrigation_day": "2026-07-31", "cycle_id": 7}},
        {"day_credit": {"cycle_id": "2026-07-31-morning"}},
    ],
    ids=[
        "absent",
        "none",
        "string",
        "list",
        "empty-mapping",
        "day-int",
        "day-not-iso",
        "no-cycle-id",
        "cycle-id-int",
        "no-day",
    ],
)
async def test_load_seed_reads_a_missing_or_malformed_day_credit_as_none(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    credit: dict[str, object],
) -> None:
    """Matrix "pre-2.3 / malformed section": no credit, the debt kept, nothing logged.

    `JOURNAL_SCHEMA_VERSION` stays 1, so the key is optional on read; and a
    doubtful credit reads as "not credited", which waters (AD-4). A bad
    credit must not forget a zone's debt.
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {"zone-1": 300},
                **credit,
            },
        },
    }

    assert (await JournalAdapter(hass).async_load_seed()).ledger == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300},
        "day_credit": None,
        "rain_baselines": {},
        "rain_source": None,
    }


async def test_load_seed_accepts_a_valid_day_credit_and_keeps_only_its_two_keys(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """A credit as the engine wrote it seeds back; a smuggled extra key does not."""
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "ledger": {
                "settled_cycle_id": "2026-07-31-morning",
                "deficits": {},
                "day_credit": {
                    "irrigation_day": "2026-07-31",
                    "cycle_id": "2026-07-31-morning",
                    "extra": True,
                },
            },
        },
    }

    assert (await JournalAdapter(hass).async_load_seed()).ledger == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {},
        "day_credit": {
            "irrigation_day": "2026-07-31",
            "cycle_id": "2026-07-31-morning",
        },
        "rain_baselines": {},
        "rain_source": None,
    }


async def test_a_day_credit_round_trips_through_a_reload_and_waives_the_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Story 2.3 AC 3, end to end: written by the one writer, honoured after reload.

    A run-now at 05:00 completes its 10 min zone; the unloaded document
    carries the credit. After the reload the 07:00 morning start creates
    nothing: history gains a `waived` record naming the run-now, and the
    credit is consumed.
    """
    freezer.move_to("2026-07-31 05:00:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RUN_NOW,
        {ATTR_CYCLE: "morning"},
        blocking=True,
    )
    await fire_at(hass, freezer, "2026-07-31 05:10:00+02:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[STORAGE_KEY]["data"]["ledger"] == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {},
        "day_credit": {
            "irrigation_day": "2026-07-31",
            "cycle_id": "2026-07-31-morning",
        },
        "rain_baselines": {},
        "rain_source": None,
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.ledger.day_credit == "2026-07-31"

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")

    assert sequencer.current_run is None
    # The run-now itself was restored as `last_run` (Story 3.2) — the waived
    # cycle is never `last_run`, so the sensors keep showing the real watering.
    assert sequencer.last_run is not None
    assert sequencer.last_run.cycle_id == "2026-07-31-morning"
    assert sequencer.ledger.day_credit is None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    stored = hass_storage[STORAGE_KEY]["data"]
    assert [
        (record["cycle_id"], record["status"], record["waived_by"])
        for record in stored["history"]
    ] == [
        ("2026-07-31-morning", "completed", None),
        ("2026-07-31-morning-2", "waived", "2026-07-31-morning"),
    ]
    assert stored["ledger"]["day_credit"] is None


# --------------------------------------------------------------------------
# The rain baselines in the ledger section (Story 2.4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "baselines",
    [
        {},  # a document written before Story 2.4: no key at all
        {"rain_baselines": None},
        {"rain_baselines": "wet"},
        {"rain_baselines": [12.0]},
        {"rain_baselines": 12.0},
    ],
    ids=["absent", "none", "string", "list", "number"],
)
async def test_load_seed_reads_a_missing_or_malformed_baselines_key_as_empty(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    baselines: dict[str, object],
) -> None:
    """Matrix "pre-2.4 / malformed section": no baselines, debt kept, nothing logged.

    `JOURNAL_SCHEMA_VERSION` stays 1, so the key is optional on read; and
    "no baseline" is the fail-wet reading — no credit until the zone
    settles a cycle, which waters. A bad key must not forget a zone's debt.
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {"zone-1": 300},
                **baselines,
            },
        },
    }

    assert (await JournalAdapter(hass).async_load_seed()).ledger == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300},
        "day_credit": None,
        "rain_baselines": {},
        "rain_source": None,
    }


async def test_load_seed_keeps_only_the_baselines_the_engine_can_consume(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Matrix "malformed section": `{"z": "wet", "y": -1, "x": True, "w": 3}` keeps `w`.

    Entries are filtered ONE BY ONE — a hand edit that breaks one zone's
    baseline must not reset every other zone's accumulation window. Kept: a
    `str` key with a finite, non-negative `int` or `float`, normalized to
    `float`. Dropped: a string, a negative, a `bool` (an `int` to Python),
    `nan`, `inf` and a non-string key. Deficits and the credit are untouched.
    """
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {"zone-1": 300},
                "day_credit": {
                    "irrigation_day": "2026-07-31",
                    "cycle_id": "2026-07-31-morning",
                },
                "rain_baselines": {
                    "z": "wet",
                    "y": -1,
                    "x": True,
                    "w": 3,
                    "v": 2.5,
                    "u": float("nan"),
                    "t": float("inf"),
                    "s": 0,
                    8: 1.0,
                },
            },
        },
    }

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.ledger == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300},
        "day_credit": {
            "irrigation_day": "2026-07-31",
            "cycle_id": "2026-07-31-morning",
        },
        "rain_baselines": {"w": 3.0, "v": 2.5, "s": 0.0},
        "rain_source": None,
    }
    baselines = seed.ledger["rain_baselines"]
    assert isinstance(baselines, dict)
    assert all(isinstance(total, float) for total in baselines.values())


# A section written before Story 2.5 (baselines, no source) and every malformed
# source a hand edit can produce: each reads as `None` — no gauge is comparable
# to the baselines, so the next cycle waters in full and re-banks — with the
# baselines and the debt next to it kept, and nothing logged.
@pytest.mark.parametrize(
    "source",
    [
        {},  # a document written before Story 2.5: no key at all
        {"rain_source": None},
        {"rain_source": 42},
        {"rain_source": ["a"]},
        {"rain_source": {"entity_id": "sensor.a"}},
        {"rain_source": True},
    ],
    ids=["absent", "none", "int", "list", "mapping", "bool"],
)
async def test_load_seed_reads_a_missing_or_malformed_rain_source_as_none(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    source: dict[str, object],
) -> None:
    """Matrix "pre-2.5 / malformed source": seed None, keep the rest, log nothing."""
    caplog.set_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}")
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {"zone-1": 300},
                "rain_baselines": {"zone-1": 12.0},
                **source,
            },
        },
    }

    assert (await JournalAdapter(hass).async_load_seed()).ledger == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {"zone-1": 300},
        "day_credit": None,
        "rain_baselines": {"zone-1": 12.0},
        "rain_source": None,
    }
    assert [record for record in caplog.records if DOMAIN in record.name] == []


async def test_load_seed_keeps_a_string_rain_source(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """A source as the engine wrote it seeds back next to its baselines."""
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "ledger": {
                "settled_cycle_id": "2026-07-30-evening",
                "deficits": {},
                "rain_baselines": {"zone-1": 12.0},
                "rain_source": GAUGE,
            },
        },
    }

    assert (await JournalAdapter(hass).async_load_seed()).ledger == {
        "settled_cycle_id": "2026-07-30-evening",
        "deficits": {},
        "day_credit": None,
        "rain_baselines": {"zone-1": 12.0},
        "rain_source": GAUGE,
    }


async def test_rain_baselines_round_trip_through_a_reload_and_reduce_the_next_cycle(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """AC 4, end to end: baselines written by the one writer, read by the one reader.

    The morning cycle is quoted with the gauge at 12.0 and banks it — under
    the gauge's entity id (Story 2.5). After the unload the stored section
    carries the baseline and the source; after the reload the same gauge
    reads 15.5, so the baselines are comparable and the evening cycle (15
    min base) is quoted 900 - 210 = 690 s with `rain_credit_s` 210.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    set_gauge(hass, "12.0")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)
    zone_id = next(iter(entry.subentries))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[STORAGE_KEY]["data"]
    assert stored["ledger"] == {
        "settled_cycle_id": "2026-07-31-morning",
        "deficits": {},
        "day_credit": None,
        "rain_baselines": {zone_id: 12.0},
        "rain_source": GAUGE,
    }
    assert (
        stored["run"]["status"],
        stored["run"]["rain_total_mm"],
        stored["run"]["rain_source"],
    ) == ("completed", 12.0, GAUGE)

    set_gauge(hass, "15.5")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    register_switch_domain(hass)
    sequencer = entry.runtime_data.sequencer
    assert sequencer.ledger.rain_baseline_mm(zone_id) == 12.0
    assert sequencer.ledger.rain_source == GAUGE

    await fire_at(hass, freezer, "2026-07-31 20:00:00+02:00")
    run = sequencer.current_run
    assert run is not None
    assert (run.rain_total_mm, run.rain_source) == (15.5, GAUGE)
    zone = run.zone_runs[0]
    assert (zone.duration_s, zone.base_s, zone.rain_credit_s) == (690, 900, 210)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# The machine state (Story 3.2): run, last_run, deferred — the trust boundary
# --------------------------------------------------------------------------


def domain_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return this domain's WARNING messages."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and DOMAIN in record.name
    ]


async def test_load_seed_restores_the_machine_state_ha_local(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    paris: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`run`, `last_run` and `deferred` come back as written, in HA's timezone.

    The instants are stored in UTC and rebuilt in Europe/Paris: the engine's
    contract is HA-local aware datetimes, and the irrigation day is the LOCAL
    calendar date. A clean read logs nothing.
    """
    running = crashed_during_zone_a()
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
    hass_storage[STORAGE_KEY] = journal_document(
        run=running,
        zone_index=0,
        last_run=completed,
        deferred=[{"kind": "evening", "reference": "2026-07-31T17:59:00+00:00"}],
    )

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.run is not None
    assert seed.run.as_dict() == running
    assert seed.run.status is CycleStatus.RUNNING
    assert seed.run.configured_start.isoformat() == "2026-07-31T07:00:00+02:00"
    assert str(seed.run.configured_start.tzinfo) == "Europe/Paris"
    assert seed.run.zone_runs[0].actual_start is not None
    assert seed.run.zone_runs[0].actual_start.isoformat() == "2026-07-31T07:00:00+02:00"
    assert seed.last_run is not None
    assert seed.last_run.as_dict() == completed
    assert seed.last_run.status is CycleStatus.COMPLETED
    assert [(kind, at.isoformat()) for kind, at in seed.deferred] == [
        (CycleKind.EVENING, "2026-07-31T19:59:00+02:00"),
    ]
    assert seed.run_unreadable is None
    assert domain_warnings(caplog) == []


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("2026-07-31T04:00:00+00:00", "2026-07-31T06:00:00+02:00"),
        ("2026-07-31T06:00:00+02:00", "2026-07-31T06:00:00+02:00"),
    ],
    ids=["utc", "offset"],
)
async def test_the_watchdog_floor_round_trips_ha_local(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    paris: None,
    stored: str,
    expected: str,
) -> None:
    """Story 3.3: `watchdog_since` comes back as the instant, in HA's timezone."""
    hass_storage[STORAGE_KEY] = journal_document(watchdog_since=stored)

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.watchdog_since is not None
    assert seed.watchdog_since.isoformat() == expected
    assert str(seed.watchdog_since.tzinfo) == "Europe/Paris"


@pytest.mark.parametrize(
    "stored",
    [None, "", "yesterday", "2026-07-31T06:00:00", 1785474000, {"at": "07:00"}, True],
    ids=[
        "null",
        "empty",
        "unparsable",
        "naive",
        "int",
        "mapping",
        "bool",
    ],
)
async def test_an_unusable_watchdog_floor_reads_as_none(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    paris: None,
    stored: object,
) -> None:
    """The one field whose doubt resolves AWAY from watering (Story 3.3).

    `None` is not "no floor for ever": the next reconcile stamps the floor
    with its own `now`, so the day's already-closed windows are treated as
    never ours. An unreadable stamp that meant "watch everything" would have
    a restored backup water every window of the day at once.
    """
    hass_storage[STORAGE_KEY] = journal_document(watchdog_since=stored)

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.watchdog_since is None


async def test_a_journal_written_before_3_2_seeds_no_machine_state(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No `run`/`last_run`/`deferred` keys at all: idle, nothing unreadable, no log."""
    hass_storage[STORAGE_KEY] = {
        "version": JOURNAL_SCHEMA_VERSION,
        "key": STORAGE_KEY,
        "data": {"schema_version": JOURNAL_SCHEMA_VERSION, "history": []},
    }

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.run is None
    assert seed.last_run is None
    assert seed.deferred == []
    assert seed.run_unreadable is None
    # Story 3.3's floor is absent too, and silently: the next reconcile
    # stamps it, so an upgrade never makes up the day it upgraded on.
    assert seed.watchdog_since is None
    assert domain_warnings(caplog) == []


async def test_an_idle_journal_seeds_no_run(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """`run: None` — the document every completed cycle leaves — is not a recovery."""
    hass_storage[STORAGE_KEY] = journal_document(run=None, last_run=None, deferred=[])

    seed = await JournalAdapter(hass).async_load_seed()

    assert (seed.run, seed.last_run, seed.deferred, seed.run_unreadable) == (
        None,
        None,
        [],
        None,
    )


@pytest.mark.parametrize(
    "raw", ["running", 7, ["run"], True], ids=["str", "int", "list", "bool"]
)
async def test_a_run_that_is_not_a_mapping_is_unreadable(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    raw: object,
) -> None:
    """Matrix "unreadable run": not a mapping → `{}` handed over, one WARNING."""
    hass_storage[STORAGE_KEY] = journal_document(run=raw)

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.run is None
    assert seed.run_unreadable == {}
    warnings = domain_warnings(caplog)
    assert len(warnings) == 1
    assert "not a mapping" in warnings[0]


@pytest.mark.parametrize(
    ("mutation", "key"),
    [
        ({"status": "paused"}, "status"),
        ({"kind": "noon"}, "kind"),
        ({"configured_start": "yesterday"}, "configured_start"),
        ({"scheduled_start": "2026-07-31T05:00:00"}, "scheduled_start"),
        ({"pump_entity_id": None}, "pump_entity_id"),
        ({"zones": {}}, "zones"),
        ({"zones": [zone_a_document(duration_s="600")]}, "zones[0].duration_s"),
        ({"rain_total_mm": "12"}, "rain_total_mm"),
        ({"recovery": False}, "recovery"),
    ],
    ids=[
        "status",
        "kind",
        "start-unparsable",
        "start-naive",
        "pump-none",
        "zones-mapping",
        "zone-duration-str",
        "rain-str",
        "recovery-bool",
    ],
)
async def test_a_drifted_run_is_unreadable_and_names_the_offending_key(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    mutation: dict[str, object],
    key: str,
) -> None:
    """Matrix "unreadable run": raw document handed over, the WARNING names the key."""
    raw = {**crashed_during_zone_a(), **mutation}
    hass_storage[STORAGE_KEY] = journal_document(run=raw)

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.run is None
    assert seed.run_unreadable == raw
    warnings = domain_warnings(caplog)
    assert len(warnings) == 1
    assert "unreadable" in warnings[0]
    assert key in warnings[0]


async def test_an_instant_that_cannot_be_converted_is_refused_not_raised(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An instant at `datetime`'s edge overflows `astimezone`: setup must still load.

    In `run` the document is refused and handed over raw; in `deferred` the
    entry is dropped alone. Neither raises out of `async_load_seed`.
    """
    edge = "9999-12-31T23:00:00-02:00"
    raw = {**crashed_during_zone_a(), "configured_start": edge}
    hass_storage[STORAGE_KEY] = journal_document(
        run=raw,
        deferred=[
            {"kind": "evening", "reference": edge},
            {"kind": "morning", "reference": "2026-07-31T05:00:00+00:00"},
        ],
    )

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.run is None
    assert seed.run_unreadable == raw
    assert [kind for kind, _ in seed.deferred] == [CycleKind.MORNING]
    warnings = domain_warnings(caplog)
    assert len(warnings) == 1
    assert "configured_start" in warnings[0]
    assert "out of range" in warnings[0]


async def test_a_missing_run_key_is_unreadable(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """A key `as_dict` always writes is required on read — strict, not defaulted."""
    raw = crashed_during_zone_a()
    del raw["pump_on_confirmed"]
    hass_storage[STORAGE_KEY] = journal_document(run=raw)

    seed = await JournalAdapter(hass).async_load_seed()

    # `pump_on_confirmed` absent reads as `None` through `.get` — a legal
    # confirmation value — so the strictness is on the TYPE, not the key's
    # presence, for the three-valued confirmations. `cycle_id` has no such
    # legal absence.
    assert seed.run is not None
    del raw["cycle_id"]
    hass_storage[STORAGE_KEY] = journal_document(run=raw)
    seed = await JournalAdapter(hass).async_load_seed()
    assert seed.run is None
    assert seed.run_unreadable == raw


@pytest.mark.parametrize(
    "raw",
    ["done", 3, {**crashed_during_zone_a(), "kind": 3}, {"cycle_id": "x"}],
    ids=["str", "int", "kind-int", "missing-keys"],
)
async def test_a_malformed_last_run_reads_as_none_with_one_warning(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    raw: object,
) -> None:
    """Matrix "last_run malformed": None (sensors `unknown` as before), WARNING once."""
    hass_storage[STORAGE_KEY] = journal_document(run=None, last_run=raw)

    seed = await JournalAdapter(hass).async_load_seed()

    assert seed.last_run is None
    assert seed.run_unreadable is None
    assert len(domain_warnings(caplog)) == 1


async def test_load_seed_keeps_only_the_deferred_entries_it_can_read(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entries are filtered ONE BY ONE, silently: a bad one must not lose the others."""
    hass_storage[STORAGE_KEY] = journal_document(
        deferred=[
            {"kind": "evening", "reference": "2026-07-31T17:59:00+00:00"},
            "evening",
            {"kind": "noon", "reference": "2026-07-31T17:59:00+00:00"},
            {"kind": "morning"},
            {"kind": "morning", "reference": "2026-07-31T05:00:00"},
            {"kind": "morning", "reference": 5},
            {"kind": 7, "reference": "2026-07-31T05:00:00+00:00"},
            {"kind": "morning", "reference": "tomorrow"},
            {"kind": "morning", "reference": "2026-08-01T05:00:00+00:00"},
        ],
    )

    seed = await JournalAdapter(hass).async_load_seed()

    # Compared as instants: this test runs in PHCC's default timezone, and
    # the engine receives the references converted into it.
    assert [(kind, at.astimezone(UTC).isoformat()) for kind, at in seed.deferred] == [
        (CycleKind.EVENING, "2026-07-31T17:59:00+00:00"),
        (CycleKind.MORNING, "2026-08-01T05:00:00+00:00"),
    ]
    assert domain_warnings(caplog) == []


@pytest.mark.parametrize("raw", ["evening", {"kind": "evening"}, None, 3])
async def test_a_deferred_section_that_is_not_a_list_reads_as_empty(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    raw: object,
) -> None:
    """A lost deferral is the watchdog's to notice (Story 3.3), never a setup crash."""
    hass_storage[STORAGE_KEY] = journal_document(deferred=raw)

    assert (await JournalAdapter(hass).async_load_seed()).deferred == []


async def test_the_last_run_round_trips_through_a_reload(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
    paris: None,
) -> None:
    """Written by the one writer at completion, read back by the one reader at setup.

    The completion write files the finished run under `run`; the rebuilt
    engine recognises a terminal run as `last_run`, not as a cycle to
    recover: nothing is commanded, nothing is reported, and the sensors have
    their figure back.
    """
    freezer.move_to("2026-07-31 06:59:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Irrigation Controller",
        data={},
        options=dict(CONTROLLER_OPTIONS),
        subentries_data=[zone_subentry_data("Zone A", "switch.zone_1_valve")],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = register_switch_domain(hass)

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[STORAGE_KEY]["data"]["run"]["status"] == "completed"
    commanded = len(calls)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    sequencer = entry.runtime_data.sequencer
    assert sequencer.current_run is None
    last = sequencer.last_run
    assert last is not None
    assert last.cycle_id == "2026-07-31-morning"
    assert last.status is CycleStatus.COMPLETED
    assert len(calls) == commanded
    assert entry.runtime_data.anomalies.open_anomalies == ()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
