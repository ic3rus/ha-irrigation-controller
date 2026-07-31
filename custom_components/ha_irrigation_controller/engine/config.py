"""Config → plan builder with strict load-time validation (AD-4, NFR1).

Flow-time guards only protect data written THROUGH the flows. Data loaded
from ``.storage`` (a restored backup, a hand edit, future schema drift) is
unvalidated — so this builder validates presence, type and range of every key
it consumes and raises :class:`PlanValidationError` naming the offending
zone/key. Never a silent skip: a zone that cannot be watered correctly must
fail the setup loudly, not vanish from the schedule.

The key literals below ARE the stored contract (const.py holds the same
strings for the HA-side surfaces; this module cannot import it — the engine
package must import as a standalone top-level package with no parent). The
glue tests build a plan from a real flow-created entry, which pins agreement.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING

from .plan import ControllerPlan, ZoneSpec

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# Same bounds as const.py's MIN/MAX_ZONE_DURATION_MINUTES (UI surface);
# pinned against each other by tests/test_init.py.
_MIN_DURATION_MINUTES = 1
_MAX_DURATION_MINUTES = 120
_MIN_RAIN_FACTOR = 0.0
_MAX_RAIN_FACTOR = 10.0
_SECONDS_PER_MINUTE = 60


class PlanValidationError(Exception):
    """Stored configuration cannot be turned into a valid plan.

    The message names the offending zone and key so the operator can repair
    the exact field (surfaced through ``ConfigEntryError`` at setup).
    """


def _string(source: Mapping[str, object], key: str, context: str) -> str:
    """Return `source[key]` as a non-empty string or fail loud."""
    value = source.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{context}: '{key}' must be a non-empty string, got {value!r}"
        raise PlanValidationError(msg)
    return value


def _bool(source: Mapping[str, object], key: str, context: str) -> bool:
    """Return `source[key]` as a real bool or fail loud."""
    value = source.get(key)
    if not isinstance(value, bool):
        msg = f"{context}: '{key}' must be a boolean, got {value!r}"
        raise PlanValidationError(msg)
    return value


def _start_time(source: Mapping[str, object], key: str, context: str) -> time:
    """Parse an 'HH:MM:SS' start-time string or fail loud."""
    value = _string(source, key, context)
    try:
        return time.fromisoformat(value)
    except ValueError as err:
        msg = f"{context}: '{key}' must be an HH:MM:SS time, got {value!r}"
        raise PlanValidationError(msg) from err


def _duration_s(source: Mapping[str, object], key: str, context: str) -> int:
    """Return a stored minute duration as seconds, range-checked, or fail loud.

    bool is explicitly rejected: it IS an int subclass, and True minutes is
    exactly the kind of storage drift this builder exists to catch.
    """
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{context}: '{key}' must be whole minutes, got {value!r}"
        raise PlanValidationError(msg)
    if not _MIN_DURATION_MINUTES <= value <= _MAX_DURATION_MINUTES:
        msg = (
            f"{context}: '{key}' must be {_MIN_DURATION_MINUTES}-"
            f"{_MAX_DURATION_MINUTES} minutes, got {value!r}"
        )
        raise PlanValidationError(msg)
    return value * _SECONDS_PER_MINUTE


def _rain_factor(source: Mapping[str, object], key: str, context: str) -> float:
    """Return the rain factor as a range-checked float or fail loud."""
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{context}: '{key}' must be a number, got {value!r}"
        raise PlanValidationError(msg)
    if not _MIN_RAIN_FACTOR <= value <= _MAX_RAIN_FACTOR:
        msg = (
            f"{context}: '{key}' must be within {_MIN_RAIN_FACTOR}-"
            f"{_MAX_RAIN_FACTOR}, got {value!r}"
        )
        raise PlanValidationError(msg)
    return float(value)


def _zone_spec(zone_id: str, title: str, data: Mapping[str, object]) -> ZoneSpec:
    """Validate one zone's stored data into a spec, naming the zone on failure."""
    context = f"zone {zone_id!r} ({title or 'untitled'})"
    if not title:
        msg = f"{context}: the zone has no name (empty subentry title)"
        raise PlanValidationError(msg)
    return ZoneSpec(
        zone_id=zone_id,
        name=title,
        valve_entity_id=_string(data, "valve_switch", context),
        morning_duration_s=_duration_s(data, "morning_duration", context),
        evening_duration_s=_duration_s(data, "evening_duration", context),
        rain_exposed=_bool(data, "rain_exposed", context),
        rain_factor=_rain_factor(data, "rain_factor", context),
    )


def build_plan(
    options: Mapping[str, object],
    zones: Iterable[tuple[str, str, Mapping[str, object]]],
) -> ControllerPlan:
    """Build the typed plan from stored controller options and zone items.

    `zones` carries ``(subentry_id, title, data)`` in watering order (the
    subentry insertion order) — preserved verbatim, never sorted. Durations
    arrive in minutes (the UI/storage surface) and leave in seconds (the
    engine-internal unit, conventions).
    """
    context = "controller options"
    return ControllerPlan(
        pump_entity_id=_string(options, "pump_switch", context),
        morning_enabled=_bool(options, "morning_enabled", context),
        morning_start=_start_time(options, "morning_start", context),
        evening_start=_start_time(options, "evening_start", context),
        zones=tuple(_zone_spec(zone_id, title, data) for zone_id, title, data in zones),
    )
