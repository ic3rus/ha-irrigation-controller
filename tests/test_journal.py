"""Journal adapter: debounced writes to the single versioned Store (AC 4, AD-2)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

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
    zone_subentry_data,
)
from tests.test_runner import (  # noqa: F401 — fixture re-export
    fire_at,
    paris,
    register_switch_domain,
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
    paris: None,  # noqa: F811 — the timezone fixture
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
