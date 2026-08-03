"""Anomaly-manager seed: log + bus event + in-memory record (AC 3, AD-9 seed)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.ha_irrigation_controller.adapters.anomalies import (
    AnomalyManager,
)
from custom_components.ha_irrigation_controller.const import (
    EVENT_HA_IRRIGATION_CONTROLLER,
)
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import Event, HomeAssistant


async def test_report_logs_a_warning(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every anomaly lands in the log with its kind and context."""
    manager = AnomalyManager(hass)

    manager.report(
        AnomalyKind.VALVE_OPEN_UNCONFIRMED,
        {"cycle_id": "2026-07-31-morning", "zone_id": "zone-1"},
    )

    assert "valve_open_unconfirmed" in caplog.text
    assert "2026-07-31-morning" in caplog.text


async def test_report_fires_the_single_bus_event(hass: HomeAssistant) -> None:
    """The ONE bus event type carries the anomaly payload (conventions)."""
    events: list[Event] = []
    unsubscribe = hass.bus.async_listen(
        EVENT_HA_IRRIGATION_CONTROLLER,
        events.append,
    )
    manager = AnomalyManager(hass)

    manager.report(
        AnomalyKind.PUMP_ON_UNCONFIRMED,
        {"cycle_id": "2026-07-31-morning", "entity_id": "switch.pool_pump"},
    )
    await hass.async_block_till_done()
    unsubscribe()

    assert len(events) == 1
    assert events[0].data == {
        "event_type": "anomaly",
        "anomaly": "pump_on_unconfirmed",
        "cycle_id": "2026-07-31-morning",
        "entity_id": "switch.pool_pump",
    }


async def test_report_records_open_anomalies_and_last_anomaly(
    hass: HomeAssistant,
) -> None:
    """The health projection reads open kinds and the most recent report."""
    manager = AnomalyManager(hass)
    assert manager.open_anomalies == frozenset()
    assert manager.last_anomaly is None

    manager.report(AnomalyKind.PUMP_ON_UNCONFIRMED, {"cycle_id": "c-1"})
    manager.report(
        AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
        {"cycle_id": "c-1", "zone_id": "zone-1"},
    )
    await hass.async_block_till_done()

    assert manager.open_anomalies == frozenset(
        {AnomalyKind.PUMP_ON_UNCONFIRMED, AnomalyKind.VALVE_CLOSE_UNCONFIRMED},
    )
    last = manager.last_anomaly
    assert last is not None
    assert last.kind is AnomalyKind.VALVE_CLOSE_UNCONFIRMED
    assert last.context == {"cycle_id": "c-1", "zone_id": "zone-1"}


async def test_recorded_state_is_isolated_from_callers(hass: HomeAssistant) -> None:
    """Exposed state is read-only.

    Mutating the context a caller handed in never changes the record the
    manager kept — the health projection reads its own copy.
    """
    manager = AnomalyManager(hass)
    context: dict[str, object] = {"cycle_id": "c-1"}
    manager.report(AnomalyKind.JOURNAL_SAVE_FAILED, context)
    context["cycle_id"] = "mutated"
    await hass.async_block_till_done()

    last = manager.last_anomaly
    assert last is not None
    assert last.context == {"cycle_id": "c-1"}
