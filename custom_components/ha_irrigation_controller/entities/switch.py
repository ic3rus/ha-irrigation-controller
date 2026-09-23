"""Switch platform — the season ON/OFF switch (FR10, AC 1).

This is the FIRST entity that commands, and that does NOT violate AD-6:
`switch.turn_on` IS a Home Assistant service (AD-10's command channel), and
the entity mutates nothing itself — it calls the runner, which re-arms the ONE
timer and calls the engine. Reading is a plain projection of the state
view's `controller.season_enabled` (`state_view.current_view`, AD-14 since
Story 4.1) — the same document every other surface reads.

No `RestoreEntity`, no optimistic state, no `assumed_state`: the journal is the
authority (AD-2), and the entity re-reads the engine on the dispatcher push the
runner emits.

HA's loader imports `<package>.switch`, so the module it actually loads is the
root-level `switch.py`; this module is what that one re-exports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity

from ..state_view import current_view  # noqa: TID252
from .entity import HaIrrigationControllerEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .. import HaIrrigationConfigEntry  # noqa: TID252
    from ..adapters.timing import CycleRunner  # noqa: TID252

# Mirrors the root `switch.py` (the module HA's loader actually imports): the
# entity is dispatcher-pushed and never polls.
PARALLEL_UPDATES: Final = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 — platform signature
    entry: HaIrrigationConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the season switch to the controller device."""
    # No `config_subentry_id`: the controller base's DeviceInfo already puts
    # this on the controller device, where the cycle-status sensor lives.
    async_add_entities([SeasonSwitch(entry, entry.runtime_data.runner)])


class SeasonSwitch(HaIrrigationControllerEntity, SwitchEntity):
    """Season ON/OFF — one manual toggle that suspends all scheduling (FR10).

    Deliberately NOT a date automation and deliberately NOT stored in
    `entry.options`: an options write fires the update listener, and the reload
    it schedules would kill a cycle in flight with the valve and pump
    energized. AC 1 says a running cycle completes.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        entry: HaIrrigationConfigEntry,
        runner: CycleRunner,
    ) -> None:
        """Bind the switch to the entry it reads and the runner it commands."""
        super().__init__(entry, "season")
        self._runner = runner

    @property
    def is_on(self) -> bool:
        """Return whether scheduling is active — a projection of the view (AD-6)."""
        return current_view(self._entry)["controller"]["season_enabled"]

    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ARG002 — SwitchEntity signature
        """Resume scheduling from this instant on."""
        await self._runner.async_set_season(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ARG002 — SwitchEntity signature
        """Suspend scheduling; a cycle already RUNNING still completes (AC 1)."""
        await self._runner.async_set_season(enabled=False)
