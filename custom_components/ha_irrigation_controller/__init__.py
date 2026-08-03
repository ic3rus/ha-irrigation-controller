"""HA Irrigation Controller — deterministic irrigation scheduling for Home Assistant.

For more details about this integration, please refer to
https://github.com/ic3rus/ha-irrigation-controller
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from homeassistant.const import (
    MAJOR_VERSION as HA_MAJOR_VERSION,
    MINOR_VERSION as HA_MINOR_VERSION,
    Platform,
    __version__ as HA_VERSION,  # noqa: N812
)
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.storage import Store

from .adapters.anomalies import AnomalyManager
from .adapters.journal import STORAGE_KEY, JournalAdapter
from .adapters.switches import VerifiedSwitchAdapter
from .adapters.timing import CycleRunner, HaClock
from .const import (
    DOMAIN,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
    SUBENTRY_TYPE_ZONE,
)
from .engine.config import PlanValidationError, build_plan, parse_actuation_timeout
from .engine.sequencer import JOURNAL_SCHEMA_VERSION, Sequencer

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .engine.plan import ControllerPlan

type HaIrrigationConfigEntry = ConfigEntry[HaIrrigationRuntimeData]

# The only platform this integration forwards today; Story 1.6 adds SWITCH.
PLATFORMS: Final = [Platform.SENSOR]


@dataclass
class HaIrrigationRuntimeData:
    """Non-persistent runtime objects for a loaded entry (AD-2).

    Everything here is rebuilt from scratch on every reload — no object in
    this dataclass is ever a source of persistent truth (the journal Store is,
    and only the journal adapter writes it).
    """

    plan: ControllerPlan
    sequencer: Sequencer
    runner: CycleRunner
    journal: JournalAdapter
    anomalies: AnomalyManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaIrrigationConfigEntry,
) -> bool:
    """Set up HA Irrigation Controller from a config entry."""
    # Compared on (major, minor) so prereleases of the minimum month release are
    # accepted; passing no message lets HA render the English text from
    # translations/en.json, so entry.reason stays informative AND translatable.
    if (HA_MAJOR_VERSION, HA_MINOR_VERSION) < (MIN_HA_MAJOR, MIN_HA_MINOR):
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="unsupported_ha_version",
            translation_placeholders={
                "required": MIN_HA_VERSION,
                "running": HA_VERSION,
            },
        )

    # Build and validate the engine plan BEFORE any side effect: data loaded
    # from .storage (a restored backup, a hand edit, schema drift) is
    # unvalidated, and a zone the engine cannot water correctly must fail the
    # setup loudly (AD-4, NFR1) — never be skipped silently. Construction is
    # cheap and side-effect-free, so the reload-per-config-change regime can
    # afford it.
    try:
        plan = build_plan(
            entry.options,
            [
                (subentry.subentry_id, subentry.title, subentry.data)
                for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)
            ],
        )
        actuation_timeout_s = parse_actuation_timeout(entry.options)
    except PlanValidationError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="invalid_stored_config",
            translation_placeholders={"detail": str(err)},
        ) from err

    # The ONE update listener of this integration: every config change —
    # options edit, zone subentry add/edit/remove (including UI deletion, which
    # never touches flow code) — fires it, and it schedules a reload so the
    # change applies without restarting HA (FR8). No flow performs its own
    # reload; Story 1.7 will teach this seam to defer while a cycle runs.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    device_registry = dr.async_get(hass)
    # The controller is a virtual service device; zone devices link to it below.
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer="ha-irrigation-controller",
        name="Irrigation Controller",
    )

    # One device per zone subentry, keyed by the subentry id (the zone key
    # everywhere, AD-8). Removal needs no manual cleanup: async_remove_subentry
    # clears the subentry's devices and entities from both registries itself.
    #
    # Filtered by type rather than assuming every subentry is a zone: `zone` is
    # the only type registered today, but a stored subentry of another type
    # (restored backup, future type added without revisiting this loop) would
    # otherwise silently acquire a zone device wired to the controller.
    #
    # ORDER CONTRACT: `entry.subentries` is insertion-ordered and persisted as
    # an ordered list, and `get_subentries_of_type` preserves that order. THIS
    # iteration order IS the watering order Story 1.4's sequencer consumes —
    # it is the "explicit" zone order AC 2 requires. Editing a zone keeps its
    # position; removing and re-adding one appends it at the end.
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE):
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry.subentry_id,
            identifiers={(DOMAIN, subentry.subentry_id)},
            via_device=(DOMAIN, entry.entry_id),
            manufacturer="ha-irrigation-controller",
            name=subentry.title,
        )

    # The engine and its four adapters. The switch adapter's cycle_id provider
    # closes over the sequencer built on the next statement — late-bound on
    # purpose, since the two reference each other (AD-7 needs the running
    # cycle's id to mint its one Context).
    clock = HaClock()
    journal = JournalAdapter(hass)
    anomalies = AnomalyManager(hass)
    switches = VerifiedSwitchAdapter(
        hass,
        timeout_s=actuation_timeout_s,
        cycle_id_provider=lambda: (
            sequencer.current_run.cycle_id if sequencer.current_run else None
        ),
    )
    sequencer = Sequencer(
        plan,
        switches=switches,
        journal=journal,
        anomalies=anomalies,
    )
    runner = CycleRunner(hass, entry, sequencer=sequencer, clock=clock)

    # BEFORE forwarding: platform setup reads runtime_data.
    entry.runtime_data = HaIrrigationRuntimeData(
        plan=plan,
        sequencer=sequencer,
        runner=runner,
        journal=journal,
        anomalies=anomalies,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await runner.async_start()
    # Both must run on every unload path: a surviving timer fails PHCC's
    # verify_cleanup and leaks per reload, and a dropped flush loses the last
    # transition (the reload regime runs constantly).
    #
    # ORDER CONTRACT: HA processes these LIFO, so the LAST registration runs
    # FIRST — the timers are cancelled before the journal is flushed, and the
    # flush therefore persists a state nothing can still move.
    entry.async_on_unload(journal.async_flush)
    entry.async_on_unload(runner.async_shutdown)
    return True


async def _async_entry_updated(
    hass: HomeAssistant,
    entry: HaIrrigationConfigEntry,
) -> None:
    """Reload the entry on any config change — the restart-free half of FR8."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: HaIrrigationConfigEntry,
) -> bool:
    """Unload a config entry.

    The timer cancel and the journal flush ride on `async_on_unload`, so they
    are deliberately NOT repeated here — one registration, one home.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant,
    entry: HaIrrigationConfigEntry,  # noqa: ARG001
) -> None:
    """Delete the journal store when the integration is removed.

    A throwaway Store with the same key: `runtime_data` is gone by the time
    HA calls this hook, and `async_remove` only needs the key to delete the
    `.storage` file.
    """
    await Store(hass, JOURNAL_SCHEMA_VERSION, STORAGE_KEY).async_remove()
