"""Config flow for HA Irrigation Controller.

Minimal single-confirm-step placeholder — the real flow (controller pickers,
options flow) is Story 1.2.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    MAJOR_VERSION as HA_MAJOR_VERSION,
    MINOR_VERSION as HA_MINOR_VERSION,
    __version__ as HA_VERSION,  # noqa: N812
)

from .const import DOMAIN, MIN_HA_MAJOR, MIN_HA_MINOR, MIN_HA_VERSION


class HaIrrigationControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA Irrigation Controller."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial (and only) confirm step."""
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

        if user_input is not None:
            return self.async_create_entry(title="Irrigation Controller", data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
