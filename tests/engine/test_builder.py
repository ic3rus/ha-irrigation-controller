"""Config → plan builder: strict load-time validation (Task 6, AD-4/NFR1).

Data loaded from `.storage` is unvalidated (restored backups, hand edits,
schema drift), so the builder validates presence, type and range of every key
it consumes and raises a typed error naming the offending zone/key — never a
silent skip.
"""

from __future__ import annotations

from datetime import time
from typing import Any

import pytest

from custom_components.ha_irrigation_controller.engine.config import (
    PlanValidationError,
    build_plan,
)

# Literal keys on purpose: these ARE the stored-contract key strings the
# builder consumes. The glue tests in tests/test_init.py prove they agree with
# const.py by building a plan from a real flow-created entry.
OPTIONS: dict[str, Any] = {
    "pump_switch": "switch.pool_pump",
    "rain_sensor": "sensor.rain_gauge",
    "morning_enabled": True,
    "morning_start": "07:00:00",
    "evening_start": "20:00:00",
}

ZONE_DATA: dict[str, Any] = {
    "valve_switch": "switch.zone_1_valve",
    "morning_duration": 10,
    "evening_duration": 15,
    "rain_exposed": True,
    "rain_factor": 1.0,
}


def zone_item(
    zone_id: str = "zone-1",
    title: str = "Front Lawn",
    **overrides: Any,
) -> tuple[str, str, dict[str, Any]]:
    """Build one (subentry_id, title, data) item with representative defaults."""
    return (zone_id, title, {**ZONE_DATA, **overrides})


def test_build_plan_happy_path() -> None:
    """A valid config maps onto the typed plan, minutes converted to seconds."""
    plan = build_plan(
        OPTIONS,
        [
            zone_item(),
            zone_item("zone-2", "Back Beds", valve_switch="switch.zone_2_valve"),
        ],
    )

    assert plan.pump_entity_id == "switch.pool_pump"
    assert plan.morning_enabled is True
    assert plan.morning_start == time(7, 0)
    assert plan.evening_start == time(20, 0)
    assert [zone.zone_id for zone in plan.zones] == ["zone-1", "zone-2"]
    zone = plan.zones[0]
    assert zone.name == "Front Lawn"
    assert zone.valve_entity_id == "switch.zone_1_valve"
    assert zone.morning_duration_s == 600
    assert zone.evening_duration_s == 900
    assert zone.rain_exposed is True
    assert zone.rain_factor == 1.0


def test_build_plan_preserves_zone_order() -> None:
    """Zones come out exactly in the order given — never sorted."""
    plan = build_plan(
        OPTIONS,
        [
            zone_item("zebra", "Zebra", valve_switch="switch.z"),
            zone_item("alpha", "Alpha", valve_switch="switch.a"),
        ],
    )
    assert [zone.zone_id for zone in plan.zones] == ["zebra", "alpha"]


def test_build_plan_accepts_integral_rain_factor() -> None:
    """An int rain factor (JSON round-trip artefact) is coerced to float."""
    plan = build_plan(OPTIONS, [zone_item(rain_factor=2)])
    assert plan.zones[0].rain_factor == 2.0


def test_build_plan_without_zones() -> None:
    """A controller without zones yet builds an empty, valid plan."""
    assert build_plan(OPTIONS, []).zones == ()


@pytest.mark.parametrize(
    "missing", ["pump_switch", "morning_enabled", "morning_start", "evening_start"]
)
def test_missing_controller_key_fails_loud(missing: str) -> None:
    """Every consumed controller key must be present (missing-key class)."""
    options = {key: value for key, value in OPTIONS.items() if key != missing}
    with pytest.raises(PlanValidationError, match=missing):
        build_plan(options, [zone_item()])


@pytest.mark.parametrize("bad_time", ["7 o'clock", "25:00:00", "", 700])
def test_bad_start_time_fails_loud(bad_time: Any) -> None:
    """Unparseable start times are rejected naming the key (bad-string class)."""
    with pytest.raises(PlanValidationError, match="morning_start"):
        build_plan({**OPTIONS, "morning_start": bad_time}, [])


def test_non_bool_morning_enabled_fails_loud() -> None:
    """A truthy non-bool is a type error, not a truthiness coincidence."""
    with pytest.raises(PlanValidationError, match="morning_enabled"):
        build_plan({**OPTIONS, "morning_enabled": "yes"}, [])


def test_non_string_pump_fails_loud() -> None:
    """The pump entity id must be a non-empty string."""
    with pytest.raises(PlanValidationError, match="pump_switch"):
        build_plan({**OPTIONS, "pump_switch": ""}, [])


def test_missing_zone_key_names_zone_and_key() -> None:
    """Zone errors name BOTH the zone and the offending key (missing-key class)."""
    data = {key: value for key, value in ZONE_DATA.items() if key != "valve_switch"}
    with pytest.raises(PlanValidationError, match="zone-1") as excinfo:
        build_plan(OPTIONS, [("zone-1", "Front Lawn", data)])
    assert "valve_switch" in str(excinfo.value)


@pytest.mark.parametrize("bad_duration", ["10", 10.0, None])
def test_non_int_duration_fails_loud(bad_duration: Any) -> None:
    """Durations must be ints — a stringly '10' is storage drift (type class)."""
    with pytest.raises(PlanValidationError, match="morning_duration"):
        build_plan(OPTIONS, [zone_item(morning_duration=bad_duration)])


def test_bool_duration_is_a_type_error() -> None:
    """Bool is an int subclass; True minutes must still be rejected."""
    with pytest.raises(PlanValidationError, match="evening_duration"):
        build_plan(OPTIONS, [zone_item(evening_duration=True)])


@pytest.mark.parametrize("out_of_range", [0, 121, -5])
def test_out_of_range_duration_fails_loud(out_of_range: int) -> None:
    """Durations outside 1..120 minutes are rejected (range class)."""
    with pytest.raises(PlanValidationError, match="morning_duration"):
        build_plan(OPTIONS, [zone_item(morning_duration=out_of_range)])


@pytest.mark.parametrize("bad_factor", [-0.5, 10.5, "1.0", True])
def test_bad_rain_factor_fails_loud(bad_factor: Any) -> None:
    """The rain factor must be a number within 0..10 (type + range classes)."""
    with pytest.raises(PlanValidationError, match="rain_factor"):
        build_plan(OPTIONS, [zone_item(rain_factor=bad_factor)])


def test_non_bool_rain_exposed_fails_loud() -> None:
    """Rain exposure must be a real bool."""
    with pytest.raises(PlanValidationError, match="rain_exposed"):
        build_plan(OPTIONS, [zone_item(rain_exposed=1)])


def test_empty_zone_title_fails_loud() -> None:
    """A zone must carry its operator-given name (the subentry title)."""
    with pytest.raises(PlanValidationError, match="zone-1"):
        build_plan(OPTIONS, [zone_item(title="")])


def test_empty_valve_fails_loud() -> None:
    """An empty valve entity id would hand the sequencer a dangling command."""
    with pytest.raises(PlanValidationError, match="valve_switch"):
        build_plan(OPTIONS, [zone_item(valve_switch="")])


@pytest.mark.parametrize("blank", ["   ", "\t", " \n "])
def test_whitespace_only_valve_fails_loud(blank: str) -> None:
    """A blank-but-truthy string is storage drift, not a valid entity id."""
    with pytest.raises(PlanValidationError, match="valve_switch"):
        build_plan(OPTIONS, [zone_item(valve_switch=blank)])


@pytest.mark.parametrize(
    "malformed", ["zone_1_valve", "switch.", ".valve", "Switch.Zone", "switch.a.b"]
)
def test_malformed_entity_id_fails_loud(malformed: str) -> None:
    """The valve must look like an entity id — the sequencer commands it verbatim."""
    with pytest.raises(PlanValidationError, match="valve_switch"):
        build_plan(OPTIONS, [zone_item(valve_switch=malformed)])


def test_malformed_pump_entity_id_fails_loud() -> None:
    """The pump goes through the same shape check as the valves."""
    with pytest.raises(PlanValidationError, match="pump_switch"):
        build_plan({**OPTIONS, "pump_switch": "pool_pump"}, [])


def test_zone_valve_equal_to_the_pump_fails_loud() -> None:
    """A zone driven by the pump switch breaks sequencing (FR2/FR3).

    Flow-time guards catch this for data written through the UI; a restored
    backup or a hand edit reaches the builder without ever passing one, and
    the sequencer would switch the pump off as if it were a valve at the first
    zone boundary — no pressure for the rest of the cycle, silently.
    """
    with pytest.raises(PlanValidationError, match="pump") as excinfo:
        build_plan(OPTIONS, [zone_item(valve_switch="switch.pool_pump")])
    assert "zone-1" in str(excinfo.value)


def test_two_zones_sharing_one_valve_fails_loud() -> None:
    """One valve per zone: two zones on one valve cannot sequence."""
    with pytest.raises(PlanValidationError, match="already used") as excinfo:
        build_plan(
            OPTIONS,
            [zone_item(), zone_item("zone-2", "Back Beds")],
        )
    assert "zone-2" in str(excinfo.value)
