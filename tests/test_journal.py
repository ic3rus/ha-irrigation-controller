"""Journal adapter: debounced writes to the single versioned Store (AC 4, AD-2)."""

from __future__ import annotations

from datetime import timedelta
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
from custom_components.ha_irrigation_controller.const import DOMAIN
from custom_components.ha_irrigation_controller.engine.sequencer import (
    JOURNAL_SCHEMA_VERSION,
)
from tests.common import (
    CONTROLLER_OPTIONS,
    controller_entry,
    fire_at,
    register_switch_domain,
    zone_subentry_data,
)

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant


def snapshot(marker: int) -> dict[str, object]:
    """Build a distinguishable engine-like snapshot."""
    return {"schema_version": JOURNAL_SCHEMA_VERSION, "history": [], "run": marker}


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
    is still pending, and only the `async_on_unload` flush persists it.
    """
    register_switch_domain(hass)
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

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    assert STORAGE_KEY not in hass_storage  # still inside the debounce

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


async def test_load_history_returns_nothing_when_no_store_exists(
    hass: HomeAssistant,
) -> None:
    """A first run has no journal file at all."""
    assert await JournalAdapter(hass).async_load_history() == []


@pytest.mark.parametrize(
    "stored",
    [
        {"schema_version": JOURNAL_SCHEMA_VERSION},  # no history section
        {"history": "not-a-list"},
        {"history": None},
    ],
)
async def test_load_history_tolerates_a_missing_or_malformed_section(
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

    assert await JournalAdapter(hass).async_load_history() == []


async def test_load_history_drops_records_the_engine_cannot_consume(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """A malformed day would raise out of `prune_history` → `advance()`.

    That abandons a cycle mid-flight with the pump on, so the trust boundary
    filters rather than trusts — the same rule `build_plan` applies to options.
    """
    good = {"irrigation_day": "2026-07-30", "cycle_id": "keep-me"}
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

    assert await JournalAdapter(hass).async_load_history() == [good]


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
    register_switch_domain(hass)
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

    await fire_at(hass, freezer, "2026-07-31 07:00:00+02:00")
    await fire_at(hass, freezer, "2026-07-31 07:10:00+02:00")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert [
        record["cycle_id"] for record in hass_storage[STORAGE_KEY]["data"]["history"]
    ] == ["2026-07-31-morning"]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
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
