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

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_NAME,
    MAJOR_VERSION as HA_MAJOR_VERSION,
    MINOR_VERSION as HA_MINOR_VERSION,
    __version__ as HA_VERSION,  # noqa: N812
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
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
    MAX_ZONE_DURATION_MINUTES,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
    MIN_ZONE_DURATION_MINUTES,
    SUBENTRY_TYPE_ZONE,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry

# device_class filter strings. Kept as literals rather than importing
# SensorDeviceClass so the integration declares no dependency on the sensor
# component. Verified against HA 2026.7.4: SensorDeviceClass.PRECIPITATION.value
# == "precipitation" — accumulated mm, which is what the rain credit needs.
# NOT "precipitation_intensity", which is a mm/h rate.
_DEVICE_CLASS_PRECIPITATION = "precipitation"
_DEVICE_CLASS_TEMPERATURE = "temperature"
_DEVICE_CLASS_HUMIDITY = "humidity"

ERROR_START_TIMES_CONFLICT = "start_times_conflict"
ERROR_VALVE_IS_PUMP = "valve_is_pump"
ERROR_VALVE_ALREADY_CONFIGURED = "valve_already_configured"


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


def _normalize_start_times(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with both start times as canonical `HH:MM:SS` strings.

    `cv.time` (the validator behind `TimeSelector`) accepts `"20:00"` and
    `"20:0:0"` as well as `"20:00:00"`, so a non-frontend submission could
    otherwise store a string violating the documented options contract — or
    sneak two representations of the same instant past the equality check.
    """
    return {
        **user_input,
        CONF_MORNING_START: cv.time(user_input[CONF_MORNING_START]).isoformat(),
        CONF_EVENING_START: cv.time(user_input[CONF_EVENING_START]).isoformat(),
    }


def validate_controller_input(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Return form errors for a submitted controller form, empty when valid.

    Expects start times already canonicalized by `_normalize_start_times`, which
    is what makes the string equality below a comparison of instants.
    """
    if not user_input.get(CONF_MORNING_ENABLED):
        # A disabled morning cycle cannot collide with anything.
        return {}
    if user_input.get(CONF_MORNING_START) == user_input.get(CONF_EVENING_START):
        # Two enabled cycles at the same instant would double-fire the pump.
        # Attached to the morning field so the form highlights what to change.
        return {CONF_MORNING_START: ERROR_START_TIMES_CONFLICT}
    return {}


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
                    min=0,
                    max=10,
                    step=0.1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min/mm",
                ),
            ),
        },
    )


def _normalize_zone_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return a copy honoring the units contract: int minutes, float min/mm.

    `NumberSelector` coerces to float and does not enforce `step` server-side,
    so a frontend submission arrives as `10.0` and a websocket one could carry
    `10.7` — the stored contract (and Story 1.4's parser) wants int minutes.
    """
    return {
        **user_input,
        CONF_MORNING_DURATION: round(user_input[CONF_MORNING_DURATION]),
        CONF_EVENING_DURATION: round(user_input[CONF_EVENING_DURATION]),
        CONF_RAIN_FACTOR: float(user_input[CONF_RAIN_FACTOR]),
    }


def validate_zone_input(
    entry: ConfigEntry,
    user_input: Mapping[str, Any],
    *,
    exclude_subentry_id: str | None = None,
) -> dict[str, str]:
    """Return form errors for a submitted zone form, empty when valid.

    Both guards protect sequencing (FR2/FR3): a "valve" that is actually the
    pump, or one valve driven by two zones, cannot sequence correctly. Entity
    ids are compared as stored strings — the WS API can bypass the frontend
    picker, so no trust in what the selector "would have" offered.
    """
    valve = user_input[CONF_VALVE_SWITCH]
    if valve == entry.options.get(CONF_PUMP_SWITCH):
        return {CONF_VALVE_SWITCH: ERROR_VALVE_IS_PUMP}
    for subentry in entry.subentries.values():
        if subentry.subentry_id == exclude_subentry_id:
            # A zone keeping its own valve on reconfigure is not a duplicate.
            continue
        if subentry.data.get(CONF_VALVE_SWITCH) == valve:
            return {CONF_VALVE_SWITCH: ERROR_VALVE_ALREADY_CONFIGURED}
    return {}


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
            user_input = _normalize_start_times(user_input)
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
            user_input = _normalize_zone_input(user_input)
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
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _normalize_zone_input(user_input)
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
            user_input = _normalize_start_times(user_input)
            errors = validate_controller_input(user_input)
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
