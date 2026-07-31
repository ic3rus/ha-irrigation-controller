"""Config, options and zone subentry flows for HA Irrigation Controller.

The controller is configured entirely in the UI (FR6): pump switch, rain sensor,
optional temperature/humidity sensors and the two cycle start times. Everything
the operator can edit lives in `entry.options`, so the options flow edits a single
source of truth — `entry.data` stays empty.

Zones are config subentries (AD-8): one subentry per zone holding its valve,
per-cycle durations, rain exposure and rain factor. The subentry id is the zone
key everywhere. The notify target joins the controller options in Story 3.1.

No flow in this module reloads the entry itself: the single update listener
registered in `async_setup_entry` schedules the reload on ANY config change
(options edit, zone add/edit/remove — including UI deletion, which never touches
flow code).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
    UnknownSubEntry,
)
from homeassistant.const import (
    CONF_NAME,
    MAJOR_VERSION as HA_MAJOR_VERSION,
    MINOR_VERSION as HA_MINOR_VERSION,
    __version__ as HA_VERSION,  # noqa: N812
)
from homeassistant.core import callback, valid_entity_id
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TimeSelector,
)

from .const import (
    CONF_EVENING_DURATION,
    CONF_EVENING_START,
    CONF_HUMIDITY_SENSOR,
    CONF_MORNING_DURATION,
    CONF_MORNING_ENABLED,
    CONF_MORNING_START,
    CONF_PUMP_SWITCH,
    CONF_RAIN_EXPOSED,
    CONF_RAIN_FACTOR,
    CONF_RAIN_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VALVE_SWITCH,
    DEFAULT_EVENING_START,
    DEFAULT_MORNING_START,
    DEFAULT_RAIN_EXPOSED,
    DEFAULT_RAIN_FACTOR,
    DEFAULT_ZONE_DURATION_MINUTES,
    DOMAIN,
    MAX_RAIN_FACTOR,
    MAX_ZONE_DURATION_MINUTES,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
    MIN_RAIN_FACTOR,
    MIN_ZONE_DURATION_MINUTES,
    SUBENTRY_TYPE_ZONE,
)

# Importing the engine from the flows is the ALLOWED direction (the forbidden
# one is engine → homeassistant.*): the overlap rule lives in ONE place and
# the flows never duplicate its sum-of-durations math.
from .engine.config import PlanValidationError, build_plan
from .engine.plan import CycleKind, overlap_offender

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

# device_class filter strings. Kept as literals rather than importing
# SensorDeviceClass so the integration declares no dependency on the sensor
# component. Verified against HA 2026.7.4: SensorDeviceClass.PRECIPITATION.value
# == "precipitation" — accumulated mm, which is what the rain credit needs.
# NOT "precipitation_intensity", which is a mm/h rate.
_DEVICE_CLASS_PRECIPITATION = "precipitation"
_DEVICE_CLASS_TEMPERATURE = "temperature"
_DEVICE_CLASS_HUMIDITY = "humidity"

ERROR_START_TIMES_CONFLICT = "start_times_conflict"
ERROR_PUMP_IS_ZONE_VALVE = "pump_is_zone_valve"
ERROR_VALVE_IS_PUMP = "valve_is_pump"
ERROR_VALVE_ALREADY_CONFIGURED = "valve_already_configured"
ERROR_VALVE_NOT_FOUND = "valve_not_found"
ERROR_NAME_REQUIRED = "name_required"
ERROR_NAME_ALREADY_CONFIGURED = "name_already_configured"
# Two ids for one rule: the overlap is symmetric, so the offending cycle can be
# either one, and the operator needs to be told which window to shrink. Same
# error attached to a field that cannot clear it is an unusable form.
ERROR_CYCLES_OVERLAP = "cycles_overlap"
ERROR_EVENING_CYCLE_OVERLAP = "evening_cycle_overlap"


def _overlap_error(
    options: Mapping[str, Any],
    zones: list[tuple[str, str, Mapping[str, Any]]],
    *,
    morning_field: str,
    evening_field: str,
) -> dict[str, str]:
    """Return the overlap error for a proposed config, keyed on the guilty field.

    Returns no error when the stored data cannot be turned into a plan at all.
    That is not a silent pass: `async_setup_entry` already refused to load the
    entry loudly, and its message sends the operator to these very forms to
    repair the offending zone. Raising here instead would wedge the only repair
    route the integration offers ("Unknown error occurred" on Configure and on
    Add zone), turning a recoverable bad zone into an unrecoverable one.
    """
    try:
        plan = build_plan(options, zones)
    except PlanValidationError:
        return {}
    offender = overlap_offender(plan)
    if offender is None:
        return {}
    if offender is CycleKind.MORNING:
        return {morning_field: ERROR_CYCLES_OVERLAP}
    return {evening_field: ERROR_EVENING_CYCLE_OVERLAP}


def _zone_items(
    entry: ConfigEntry,
    *,
    exclude_subentry_id: str | None = None,
) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Return the entry's zones as the (id, title, data) items the engine consumes."""
    return [
        (subentry.subentry_id, subentry.title, subentry.data)
        for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)
        if subentry.subentry_id != exclude_subentry_id
    ]


def _resolve_entity_id(hass: HomeAssistant, value: str) -> str:
    """Return `value` as an entity_id, resolving an entity-registry id if needed.

    `EntitySelector` runs `cv.entity_id_or_uuid` and returns a registry id
    BEFORE it applies the `domain` filter (verified in helpers/selector.py), so
    a non-frontend submission can carry a registry id that no string comparison
    in this module would ever match — silently defeating the pump/valve guards.
    Resolving here keeps the stored contract "entity_ids everywhere" while
    preserving the rename resilience registry ids exist for. An id that resolves
    to nothing is left untouched and rejected by the validators below.
    """
    if valid_entity_id(value):
        return value
    registry_entry = er.async_get(hass).async_get(value)
    return registry_entry.entity_id if registry_entry is not None else value


def build_controller_schema() -> vol.Schema:
    """Build the controller form schema shared by the user and options steps.

    The optional sensors deliberately carry no `default=`: prefilling happens via
    `add_suggested_values_to_schema`, which is what lets an operator clear a
    once-set optional picker (a `default` would silently restore it).
    """
    return vol.Schema(
        {
            vol.Required(CONF_PUMP_SWITCH): EntitySelector(
                EntitySelectorConfig(domain="switch"),
            ),
            # Required on purpose: the product exists to modulate watering by
            # rain, so a controller without a rain sensor is a configuration
            # error worth surfacing now rather than a silently degraded mode.
            vol.Required(CONF_RAIN_SENSOR): EntitySelector(
                EntitySelectorConfig(
                    domain="sensor",
                    device_class=_DEVICE_CLASS_PRECIPITATION,
                ),
            ),
            # Stored for the future weather coefficients; unused in v1.
            vol.Optional(CONF_TEMPERATURE_SENSOR): EntitySelector(
                EntitySelectorConfig(
                    domain="sensor",
                    device_class=_DEVICE_CLASS_TEMPERATURE,
                ),
            ),
            vol.Optional(CONF_HUMIDITY_SENSOR): EntitySelector(
                EntitySelectorConfig(
                    domain="sensor",
                    device_class=_DEVICE_CLASS_HUMIDITY,
                ),
            ),
            # Off by default: spring is evening-only. Both start times are always
            # collected; this toggle governs whether the morning cycle runs.
            vol.Required(CONF_MORNING_ENABLED, default=False): BooleanSelector(),
            vol.Required(
                CONF_MORNING_START,
                default=DEFAULT_MORNING_START,
            ): TimeSelector(),
            vol.Required(
                CONF_EVENING_START,
                default=DEFAULT_EVENING_START,
            ): TimeSelector(),
        },
    )


def _normalize_controller_input(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy with canonical `HH:MM:SS` times and a resolved pump entity.

    `cv.time` (the validator behind `TimeSelector`) accepts `"20:00"` and
    `"20:0:0"` as well as `"20:00:00"`, so a non-frontend submission could
    otherwise store a string violating the documented options contract — or
    sneak two representations of the same instant past the equality check.

    The pump is resolved to an entity_id for the same reason zone valves are:
    the two are compared as strings by both flows, and a registry id on either
    side would make that comparison silently miss.
    """
    return {
        **user_input,
        CONF_PUMP_SWITCH: _resolve_entity_id(hass, user_input[CONF_PUMP_SWITCH]),
        CONF_MORNING_START: cv.time(user_input[CONF_MORNING_START]).isoformat(),
        CONF_EVENING_START: cv.time(user_input[CONF_EVENING_START]).isoformat(),
    }


def validate_controller_input(
    user_input: Mapping[str, Any],
    *,
    entry: ConfigEntry | None = None,
) -> dict[str, str]:
    """Return form errors for a submitted controller form, empty when valid.

    Expects input already canonicalized by `_normalize_controller_input`, which
    is what makes the string equalities below comparisons of instants and of
    entities.

    `entry` is None during initial setup (no zones can exist yet) and the live
    entry from the options flow. The pump/valve exclusion is enforced from BOTH
    sides on purpose: guarding it only in the zone flow would let the options
    form point the pump at an existing zone valve, which not only breaks
    sequencing (FR2/FR3) but wedges that zone — its own reconfigure resubmits
    its valve, which would then trip `valve_is_pump` with no way out.
    """
    errors: dict[str, str] = {}
    if entry is not None:
        pump = user_input.get(CONF_PUMP_SWITCH)
        for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE):
            if subentry.data.get(CONF_VALVE_SWITCH) == pump:
                errors[CONF_PUMP_SWITCH] = ERROR_PUMP_IS_ZONE_VALVE
                break
    if user_input.get(CONF_MORNING_ENABLED) and user_input.get(
        CONF_MORNING_START,
    ) == user_input.get(CONF_EVENING_START):
        # Two enabled cycles at the same instant would double-fire the pump.
        # Attached to the morning field so the form highlights what to change.
        # A disabled morning cycle cannot collide with anything.
        errors[CONF_MORNING_START] = ERROR_START_TIMES_CONFLICT
    # The engine's overlap rule on the PROPOSED options over the stored zones
    # (Story 1.4, resolves the 1.2 deferral). Initial setup has no zones —
    # nothing to check (entry is None). When equality already claimed the
    # morning field, it is the more specific message.
    if entry is not None and CONF_MORNING_START not in errors:
        errors |= _overlap_error(
            user_input,
            _zone_items(entry),
            morning_field=CONF_MORNING_START,
            evening_field=CONF_EVENING_START,
        )
    return errors


def build_zone_schema() -> vol.Schema:
    """Build the zone form schema shared by the add and reconfigure steps.

    The name becomes the subentry TITLE, not data — matching core practice and
    giving the zone device its name for free. Every field is required, so plain
    `default=` is fine here (nothing is clearable); prefill on reconfigure still
    goes through `add_suggested_values_to_schema`.
    """
    duration_selector = NumberSelector(
        NumberSelectorConfig(
            min=MIN_ZONE_DURATION_MINUTES,
            max=MAX_ZONE_DURATION_MINUTES,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="min",
        ),
    )
    return vol.Schema(
        {
            vol.Required(CONF_NAME): TextSelector(),
            vol.Required(CONF_VALVE_SWITCH): EntitySelector(
                EntitySelectorConfig(domain="switch"),
            ),
            vol.Required(
                CONF_MORNING_DURATION,
                default=DEFAULT_ZONE_DURATION_MINUTES,
            ): duration_selector,
            vol.Required(
                CONF_EVENING_DURATION,
                default=DEFAULT_ZONE_DURATION_MINUTES,
            ): duration_selector,
            vol.Required(
                CONF_RAIN_EXPOSED,
                default=DEFAULT_RAIN_EXPOSED,
            ): BooleanSelector(),
            # 0 = never reduce this zone by rain.
            vol.Required(
                CONF_RAIN_FACTOR,
                default=DEFAULT_RAIN_FACTOR,
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_RAIN_FACTOR,
                    max=MAX_RAIN_FACTOR,
                    step=0.1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min/mm",
                ),
            ),
        },
    )


def _round_minutes(value: float) -> int:
    """Return `value` rounded half-UP to whole minutes.

    Bare `round()` is half-to-EVEN, so `10.5` and `11.5` would round in
    opposite directions — an arbitrary result for a duration the operator can
    see. The selector has already clamped the value to
    MIN_ZONE_DURATION_MINUTES..MAX_ZONE_DURATION_MINUTES (both positive), so
    the `+ 0.5` floor is safe and cannot escape those bounds.
    """
    return math.floor(value + 0.5)


def _normalize_zone_input(
    hass: HomeAssistant,
    user_input: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy honoring the stored contracts for every zone field.

    `NumberSelector` coerces to float and does not enforce `step` server-side,
    so a frontend submission arrives as `10.0` and a websocket one could carry
    `10.7` — the stored contract (and Story 1.4's parser) wants int minutes.
    The name is stripped because it becomes the subentry title and the zone
    device name, and the valve is resolved to an entity_id so the guards in
    `validate_zone_input` compare like with like.
    """
    return {
        **user_input,
        CONF_NAME: str(user_input[CONF_NAME]).strip(),
        CONF_VALVE_SWITCH: _resolve_entity_id(hass, user_input[CONF_VALVE_SWITCH]),
        CONF_MORNING_DURATION: _round_minutes(user_input[CONF_MORNING_DURATION]),
        CONF_EVENING_DURATION: _round_minutes(user_input[CONF_EVENING_DURATION]),
        CONF_RAIN_FACTOR: float(user_input[CONF_RAIN_FACTOR]),
    }


def validate_zone_input(
    entry: ConfigEntry,
    user_input: Mapping[str, Any],
    *,
    exclude_subentry_id: str | None = None,
) -> dict[str, str]:
    """Return form errors for a submitted zone form, empty when valid.

    The valve guards protect sequencing (FR2/FR3): a "valve" that is actually
    the pump, or one valve driven by two zones, cannot sequence correctly. The
    name guards protect identity: the subentry id is the zone key in code, but
    the name is the ONLY thing that distinguishes two zones for the operator
    (device list, dashboard card, notifications), so it must be present and
    unique. Names are compared case-insensitively — "Lawn" and "lawn" are the
    same zone to a human reading a notification.

    Everything is compared as stored strings: the WS API can bypass the
    frontend picker, so no trust in what the selector "would have" offered.
    Expects input already normalized by `_normalize_zone_input`.
    """
    name = user_input[CONF_NAME]
    if not name:
        # `TextSelector` is `vol.Schema(str)`: "" and "   " both validate.
        return {CONF_NAME: ERROR_NAME_REQUIRED}
    valve = user_input[CONF_VALVE_SWITCH]
    if not valid_entity_id(valve):
        # A registry id `_resolve_entity_id` could not resolve — it references
        # nothing, so storing it would hand Story 1.4 a dangling valve.
        return {CONF_VALVE_SWITCH: ERROR_VALVE_NOT_FOUND}
    if valve == entry.options.get(CONF_PUMP_SWITCH):
        return {CONF_VALVE_SWITCH: ERROR_VALVE_IS_PUMP}
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE):
        if subentry.subentry_id == exclude_subentry_id:
            # A zone keeping its own valve or name on reconfigure is not a
            # duplicate of itself.
            continue
        if subentry.data.get(CONF_VALVE_SWITCH) == valve:
            return {CONF_VALVE_SWITCH: ERROR_VALVE_ALREADY_CONFIGURED}
        if subentry.title.casefold() == name.casefold():
            return {CONF_NAME: ERROR_NAME_ALREADY_CONFIGURED}
    # The engine's overlap rule on the PROPOSED zone merged over its siblings
    # (its own stored values excluded on reconfigure, so shrinking back always
    # works). The error lands on the duration of whichever cycle runs into the
    # other one — flagging the morning duration when the evening window is the
    # offender gives the operator a field no edit can clear.
    proposed = _zone_items(entry, exclude_subentry_id=exclude_subentry_id)
    proposed.append(("proposed", name, _without_name(dict(user_input))))
    return _overlap_error(
        entry.options,
        proposed,
        morning_field=CONF_MORNING_DURATION,
        evening_field=CONF_EVENING_DURATION,
    )


class HaIrrigationControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA Irrigation Controller."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,  # noqa: ARG004
    ) -> OptionsFlow:
        """Return the options flow for an existing controller entry."""
        return HaIrrigationControllerOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,  # noqa: ARG003
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentry flows: one type, the zone (AD-8)."""
        return {SUBENTRY_TYPE_ZONE: ZoneSubentryFlow}

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect the controller configuration and create the entry."""
        # Single-entry enforcement lives in manifest.json (single_config_entry), so
        # HA hides the entry point rather than offering "create" and then aborting.
        # Refusing here too means an unsupported instance never gets an entry that
        # would immediately go SETUP_ERROR (the __init__ guard is the hard backstop).
        if (HA_MAJOR_VERSION, HA_MINOR_VERSION) < (MIN_HA_MAJOR, MIN_HA_MINOR):
            return self.async_abort(
                reason="unsupported_ha_version",
                description_placeholders={
                    "required": MIN_HA_VERSION,
                    "running": HA_VERSION,
                },
            )

        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _normalize_controller_input(self.hass, user_input)
            # No entry yet, so no zones can exist to cross-check the pump against.
            errors = validate_controller_input(user_input)
            if not errors:
                # data={} on purpose: nothing about this controller is immutable,
                # so options is the one place the operator's choices live.
                return self.async_create_entry(
                    title="Irrigation Controller",
                    data={},
                    options=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                build_controller_schema(),
                user_input,
            ),
            errors=errors,
        )


class ZoneSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure one irrigation zone (a config subentry).

    Neither step reloads anything: the manager adds/updates the subentry after
    the step returns, which fires the update listener, which schedules the
    reload. `async_update_reload_and_abort` MUST NOT replace
    `async_update_and_abort` here — it raises ValueError when update listeners
    exist.
    """

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Add a zone: collect its configuration and create the subentry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _normalize_zone_input(self.hass, user_input)
            errors = validate_zone_input(self._get_entry(), user_input)
            if not errors:
                # No unique_id: zones have no natural one — duplicate-valve
                # protection is the flow validation above.
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=_without_name(user_input),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                build_zone_schema(),
                user_input,
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Edit a zone: show its current configuration prefilled, then update it."""
        try:
            subentry = self._get_reconfigure_subentry()
        except UnknownSubEntry:
            # The zone was deleted while this form was open — the UI delete
            # button aborts no flow, so this is reachable from a second tab.
            # Abort cleanly instead of letting UnknownSubEntry escape the step
            # as an "Unknown error occurred" with a wedged dialog.
            return self.async_abort(reason="zone_not_found")
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _normalize_zone_input(self.hass, user_input)
            errors = validate_zone_input(
                self._get_entry(),
                user_input,
                exclude_subentry_id=subentry.subentry_id,
            )
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=user_input[CONF_NAME],
                    data=_without_name(user_input),
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                build_zone_schema(),
                # Re-showing after an error keeps what was typed; opening the
                # flow starts from the stored zone (title carries the name).
                user_input
                if user_input is not None
                else {**subentry.data, CONF_NAME: subentry.title},
            ),
            errors=errors,
        )


def _without_name(user_input: dict[str, Any]) -> dict[str, Any]:
    """Strip the name field: it is the subentry title, never subentry data."""
    return {key: value for key, value in user_input.items() if key != CONF_NAME}


class HaIrrigationControllerOptionsFlow(OptionsFlow):
    """Edit the controller configuration without restarting Home Assistant.

    Plain `OptionsFlow` on purpose: the reload on change (FR8) is performed by
    the single update listener registered in `async_setup_entry`, which also
    covers zone subentry changes. `OptionsFlowWithReload` is incompatible with
    that listener by hard error — no flow-side auto-reload may exist anywhere
    in this integration.
    """

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the controller form prefilled and store the edited options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _normalize_controller_input(self.hass, user_input)
            errors = validate_controller_input(user_input, entry=self.config_entry)
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                build_controller_schema(),
                # Re-showing after an error keeps what was typed; opening the flow
                # starts from the options currently in force.
                user_input if user_input is not None else self.config_entry.options,
            ),
            errors=errors,
        )
