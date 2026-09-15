"""Rain sensor adapter: the ONE implementation of `RainPort` (Story 2.4).

The adapter's whole job is the fail-wet guard on the reading and the unit
conversion the engine must never do (AD-1): every doubtful reading is `None`
with a debug log, every known precipitation unit becomes millimetres — and
(Story 2.5) the configured entity id is the port's `source_id`, the identity
the ledger banks its baselines under. What happens to a total once it is a
number (resets, swaps, late data) is the ledger's arithmetic, pinned in
`tests/engine`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPrecipitationDepth,
)

from custom_components.ha_irrigation_controller.adapters.rain import (
    RainSensorAdapter,
)
from custom_components.ha_irrigation_controller.const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

GAUGE = "sensor.rain_gauge"


def set_gauge(hass: HomeAssistant, state: str, unit: object = "mm") -> None:
    """Put `state` on the gauge; `unit=None` sets NO unit attribute at all."""
    attributes = {} if unit is None else {ATTR_UNIT_OF_MEASUREMENT: unit}
    hass.states.async_set(GAUGE, state, attributes)


@pytest.mark.parametrize(
    ("state", "unit", "expected"),
    [
        ("12.0", "mm", 12.0),
        ("12.0", UnitOfPrecipitationDepth.MILLIMETERS, 12.0),
        ("1.2", "cm", 12.0),
        ("0.5", "in", 12.7),
        ("12.5", None, 12.5),
        ("0", "mm", 0.0),
        (" 3.5 ", "mm", 3.5),
    ],
    ids=["mm", "mm-enum", "cm", "in", "no-unit", "zero", "whitespace"],
)
async def test_a_readable_gauge_is_converted_to_millimetres(
    hass: HomeAssistant,
    state: str,
    unit: object,
    expected: float,
) -> None:
    """Matrix "unit in" and friends: mm as is, cm *10, in *25.4, no unit as mm."""
    set_gauge(hass, state, unit)

    total = RainSensorAdapter(hass, GAUGE).total_mm()

    assert total == pytest.approx(expected)
    assert isinstance(total, float)


async def test_a_missing_entity_reads_as_none(hass: HomeAssistant) -> None:
    """Matrix "gauge unavailable at quote": no state at all → None."""
    assert RainSensorAdapter(hass, GAUGE).total_mm() is None


@pytest.mark.parametrize(
    ("state", "unit"),
    [
        (STATE_UNKNOWN, "mm"),
        (STATE_UNAVAILABLE, "mm"),
        ("wet", "mm"),
        ("", "mm"),
        ("nan", "mm"),
        ("inf", "mm"),
        ("-inf", "mm"),
        ("-1", "mm"),
        ("-0.1", "in"),
        ("12.0", "%"),
        ("12.0", "mm/h"),
        ("12.0", ""),
        ("12.0", ["mm"]),
        ("12.0", 7),
    ],
    ids=[
        "unknown",
        "unavailable",
        "non-numeric",
        "empty",
        "nan",
        "inf",
        "neg-inf",
        "negative",
        "negative-inches",
        "percent",
        "rate-unit",
        "empty-unit",
        "list-unit",
        "int-unit",
    ],
)
async def test_a_doubtful_reading_is_none_with_a_debug_log(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    state: str,
    unit: object,
) -> None:
    """Fail-wet (AD-4): every doubtful state or unit disables modulation, quietly.

    `None` is the whole answer — no exception, no anomaly, and nothing above
    DEBUG in the log: a transient sensor gap is not an operator-facing fault.
    """
    set_gauge(hass, state, unit)
    caplog.set_level(logging.DEBUG, logger=f"custom_components.{DOMAIN}")

    assert RainSensorAdapter(hass, GAUGE).total_mm() is None

    ours = [record for record in caplog.records if DOMAIN in record.name]
    assert len(ours) == 1
    assert ours[0].levelno == logging.DEBUG
    assert GAUGE in ours[0].getMessage()
    assert "not modulating" in ours[0].getMessage()


async def test_the_adapter_reads_the_live_state_on_every_call(
    hass: HomeAssistant,
) -> None:
    """Stateless: what the state machine says NOW is what the engine is given."""
    adapter = RainSensorAdapter(hass, GAUGE)
    set_gauge(hass, "12.0")
    assert adapter.total_mm() == 12.0

    set_gauge(hass, "15.5")
    assert adapter.total_mm() == 15.5

    set_gauge(hass, STATE_UNAVAILABLE)
    assert adapter.total_mm() is None

    set_gauge(hass, "16.0")
    assert adapter.total_mm() == 16.0


async def test_source_id_is_the_configured_entity_id(hass: HomeAssistant) -> None:
    """Story 2.5: the identity baselines are banked under is the entity id, verbatim.

    It does not depend on the entity having a state (a missing gauge still
    has an identity, so a doubtful quote still snapshots WHICH gauge was
    doubtful), and a different id is a different source — which is what
    makes a swap or a rename cost one banking cycle instead of crediting the
    new gauge's lifetime total.
    """
    adapter = RainSensorAdapter(hass, GAUGE)
    assert adapter.source_id == GAUGE

    set_gauge(hass, "12.0")
    assert adapter.source_id == GAUGE
    assert (
        RainSensorAdapter(hass, "sensor.other_gauge").source_id == "sensor.other_gauge"
    )
