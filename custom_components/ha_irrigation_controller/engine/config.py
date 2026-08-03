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

import re
from datetime import time
from typing import TYPE_CHECKING

from .plan import ControllerPlan, ZoneSpec

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# Same bounds as const.py's MIN/MAX_ZONE_DURATION_MINUTES and
# MIN/MAX_RAIN_FACTOR (the UI surfaces); every one of the four is pinned
# against its const.py twin by tests/test_init.py.
_MIN_DURATION_MINUTES = 1
_MAX_DURATION_MINUTES = 120
_MIN_RAIN_FACTOR = 0.0
_MAX_RAIN_FACTOR = 10.0
_SECONDS_PER_MINUTE = 60

# Same values as const.py's DEFAULT/MIN/MAX_ACTUATION_TIMEOUT_S, pinned by
# tests/test_init.py like the four bounds above. Seconds at every surface.
_DEFAULT_ACTUATION_TIMEOUT_S = 10
_MIN_ACTUATION_TIMEOUT_S = 1
_MAX_ACTUATION_TIMEOUT_S = 120

# Home Assistant's entity-id grammar, restated here because the engine cannot
# import `homeassistant.core.valid_entity_id` (AD-1). Shape only: whether the
# entity exists is a runtime question the switch port answers.
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class PlanValidationError(Exception):
    """Stored configuration cannot be turned into a valid plan.

    The message names the offending zone and key so the operator can repair
    the exact field (surfaced through ``ConfigEntryError`` at setup).
    """


def _string(source: Mapping[str, object], key: str, context: str) -> str:
    """Return `source[key]` as a non-blank string or fail loud.

    Stripped before the emptiness test: a whitespace-only value is storage
    drift that would otherwise ride through as a valid-looking entity id.
    """
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"{context}: '{key}' must be a non-empty string, got {value!r}"
        raise PlanValidationError(msg)
    return value.strip()


def _entity_id(source: Mapping[str, object], key: str, context: str) -> str:
    """Return `source[key]` as a well-formed entity id or fail loud."""
    value = _string(source, key, context)
    if not _ENTITY_ID.match(value):
        msg = f"{context}: '{key}' must be an entity id like 'switch.x', got {value!r}"
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
        valve_entity_id=_entity_id(data, "valve_switch", context),
        morning_duration_s=_duration_s(data, "morning_duration", context),
        evening_duration_s=_duration_s(data, "evening_duration", context),
        rain_exposed=_bool(data, "rain_exposed", context),
        rain_factor=_rain_factor(data, "rain_factor", context),
    )


def parse_actuation_timeout(options: Mapping[str, object]) -> int:
    """Return the stored actuation timeout in seconds, validated, or fail loud.

    Same trust boundary as ``build_plan``: `.storage` data is unvalidated.
    An ABSENT key is the default, not an error — entries created before the
    key existed (Story 1.5) must keep loading. A present key is validated for
    type (bool rejected: it is an int subclass and True seconds is storage
    drift) and range.
    """
    context = "controller options"
    value = options.get("actuation_timeout", _DEFAULT_ACTUATION_TIMEOUT_S)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{context}: 'actuation_timeout' must be whole seconds, got {value!r}"
        raise PlanValidationError(msg)
    if not _MIN_ACTUATION_TIMEOUT_S <= value <= _MAX_ACTUATION_TIMEOUT_S:
        msg = (
            f"{context}: 'actuation_timeout' must be {_MIN_ACTUATION_TIMEOUT_S}-"
            f"{_MAX_ACTUATION_TIMEOUT_S} seconds, got {value!r}"
        )
        raise PlanValidationError(msg)
    return value


def build_plan(
    options: Mapping[str, object],
    zones: Iterable[tuple[str, str, Mapping[str, object]]],
) -> ControllerPlan:
    """Build the typed plan from stored controller options and zone items.

    `zones` carries ``(subentry_id, title, data)`` in watering order (the
    subentry insertion order) — preserved verbatim, never sorted. Durations
    arrive in minutes (the UI/storage surface) and leave in seconds (the
    engine-internal unit, conventions).

    The cross-zone invariants are re-checked here, not only at flow time: they
    are what makes sequencing possible at all (FR2/FR3), and a restored backup
    or a hand-edited `.storage` reaches this builder without ever passing a
    flow. A zone pointed at the pump would have the pump switched off as if it
    were a valve at the first zone boundary, killing pressure for the rest of
    the cycle — silently.
    """
    context = "controller options"
    pump_entity_id = _entity_id(options, "pump_switch", context)
    specs = tuple(_zone_spec(zone_id, title, data) for zone_id, title, data in zones)
    seen: dict[str, str] = {}
    for spec in specs:
        zone_context = f"zone {spec.zone_id!r} ({spec.name})"
        if spec.valve_entity_id == pump_entity_id:
            msg = (
                f"{zone_context}: 'valve_switch' is the pump switch "
                f"({pump_entity_id!r}); a zone cannot be driven by the pump"
            )
            raise PlanValidationError(msg)
        if spec.valve_entity_id in seen:
            msg = (
                f"{zone_context}: 'valve_switch' {spec.valve_entity_id!r} is "
                f"already used by zone {seen[spec.valve_entity_id]!r}"
            )
            raise PlanValidationError(msg)
        seen[spec.valve_entity_id] = spec.zone_id
    return ControllerPlan(
        pump_entity_id=pump_entity_id,
        morning_enabled=_bool(options, "morning_enabled", context),
        morning_start=_start_time(options, "morning_start", context),
        evening_start=_start_time(options, "evening_start", context),
        zones=specs,
    )
