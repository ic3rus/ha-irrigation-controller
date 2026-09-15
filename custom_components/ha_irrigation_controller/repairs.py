"""Repairs platform — the acknowledge flow for every anomaly issue (Story 3.1).

Every issue this integration raises is an ANOMALY the operator should see
once and then dismiss: there is nothing for a flow to fix, so the fix flow is
core's `ConfirmRepairFlow`, which deletes the issue on confirmation. The
anomaly manager observes that deletion through the issue registry and
forgets the anomaly — the health entity turns off within the same loop
iteration. *Ignore* stays available too and is treated the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def async_create_fix_flow(
    hass: HomeAssistant,  # noqa: ARG001 — platform signature
    issue_id: str,  # noqa: ARG001
    data: dict[str, str | int | float | None] | None,  # noqa: ARG001
) -> RepairsFlow:
    """Return the acknowledge flow — the same for every issue id."""
    return ConfirmRepairFlow()
