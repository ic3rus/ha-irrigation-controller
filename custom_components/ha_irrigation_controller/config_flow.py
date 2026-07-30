"""Config and options flows for HA Irrigation Controller.

The controller is configured entirely in the UI (FR6): pump switch, rain sensor,
optional temperature/humidity sensors and the two cycle start times. Everything
the operator can edit lives in `entry.options`, so the options flow edits a single
source of truth — `entry.data` stays empty.

Zones are config subentries and arrive with Story 1.3; the notify target joins the
controller options in Story 3.1. Neither belongs here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
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
    TimeSelector,
)

from .const import (
    CONF_EVENING_START,
    CONF_HUMIDITY_SENSOR,
    CONF_MORNING_ENABLED,
    CONF_MORNING_START,
    CONF_PUMP_SWITCH,
    CONF_RAIN_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    DEFAULT_EVENING_START,
    DEFAULT_MORNING_START,
    DOMAIN,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry, OptionsFlow

# device_class filter strings. Kept as literals rather than importing
# SensorDeviceClass so the integration declares no dependency on the sensor
# component. Verified against HA 2026.7.4: SensorDeviceClass.PRECIPITATION.value
# == "precipitation" — accumulated mm, which is what the rain credit needs.
# NOT "precipitation_intensity", which is a mm/h rate.
_DEVICE_CLASS_PRECIPITATION = "precipitation"
_DEVICE_CLASS_TEMPERATURE = "temperature"
_DEVICE_CLASS_HUMIDITY = "humidity"

ERROR_START_TIMES_CONFLICT = "start_times_conflict"


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


class HaIrrigationControllerOptionsFlow(OptionsFlowWithReload):
    """Edit the controller configuration without restarting Home Assistant.

    `OptionsFlowWithReload` reloads the entry itself when the options change
    (FR8). No config-entry update listener may be registered anywhere in this
    integration — the base class forbids combining the two.
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
