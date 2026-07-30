"""Config flow for HA Irrigation Controller.

Minimal single-confirm-step placeholder — the real flow (controller pickers,
options flow) is Story 1.2.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class HaIrrigationControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA Irrigation Controller."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial (and only) confirm step."""
        self._async_abort_entries_match()
        if user_input is not None:
            return self.async_create_entry(title="Irrigation Controller", data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
