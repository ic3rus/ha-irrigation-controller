"""HA Irrigation Controller — deterministic irrigation scheduling for Home Assistant.

For more details about this integration, please refer to
https://github.com/ic3rus/ha-irrigation-controller
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    MAJOR_VERSION as HA_MAJOR_VERSION,
    MINOR_VERSION as HA_MINOR_VERSION,
    Platform,
    __version__ as HA_VERSION,  # noqa: N812
)
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.storage import Store

from .adapters.anomalies import AnomalyManager, async_delete_domain_issues
from .adapters.journal import STORAGE_KEY, JournalAdapter
from .adapters.notify import HaNotifyAdapter, parse_notify_target
from .adapters.rain import RainSensorAdapter
from .adapters.registry import ConfiguredEntityTracker
from .adapters.switches import VerifiedSwitchAdapter
from .adapters.timing import CycleRunner, HaClock
from .const import (
    CONF_RAIN_SENSOR,
    DOMAIN,
    LOGGER,
    MIN_HA_MAJOR,
    MIN_HA_MINOR,
    MIN_HA_VERSION,
    SUBENTRY_TYPE_ZONE,
)
from .engine.config import PlanValidationError, build_plan, parse_actuation_timeout
from .engine.sequencer import JOURNAL_SCHEMA_VERSION, Sequencer
from .services import async_setup_services

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

    from .engine.plan import ControllerPlan

type HaIrrigationConfigEntry = ConfigEntry[HaIrrigationRuntimeData]

# What the engine is built from: the options plus every subentry's identity,
# type, title and data, in watering order. Deliberately NOT the entry title or
# its prefs — nothing of ours reads them, and a rename of the entry must not
# reload (let alone defer) anything.
type ConfigFingerprint = tuple[
    dict[str, Any],
    tuple[tuple[str, str, str, dict[str, Any]], ...],
]


def config_fingerprint(entry: ConfigEntry) -> ConfigFingerprint:
    """Return the comparable snapshot of everything the plan is built from.

    A pure function of the entry, taken at setup and compared on every update
    listener call: an equal fingerprint is a cosmetic update (title, prefs)
    and costs nothing; a different one is a real config change.
    """
    return (
        dict(entry.options),
        tuple(
            (
                subentry.subentry_id,
                subentry.subentry_type,
                subentry.title,
                dict(subentry.data),
            )
            for subentry in entry.subentries.values()
        ),
    )


# Required the moment `async_setup` exists on a config-entry-only integration:
# it declares that nothing of ours may be configured from `configuration.yaml`.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# The platforms this integration forwards. One home for the list (here, not in
# const.py): `async_unload_entry` unloads whatever it names, so adding a
# platform is a one-line change.
PLATFORMS: Final = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


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
    tracker: ConfiguredEntityTracker
    # The config the running engine was built from (or last given, when a
    # reload is deferred): the update listener's "did anything change?" test.
    config_fingerprint: ConfigFingerprint


async def async_setup(
    hass: HomeAssistant,
    config: ConfigType,  # noqa: ARG001 — component setup signature
) -> bool:
    """Register the integration's actions at COMPONENT setup (`action-setup`).

    Deliberately not in `async_setup_entry`: services registered per entry
    disappear on unload, and an automation referencing one would then become
    an action the operator cannot even open. Registered once here, they stay
    for the process lifetime and each call resolves the entry itself — so a
    call made while nothing is loaded explains that instead of vanishing.
    """
    async_setup_services(hass)
    return True


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
    # never touches flow code), `set_zone_duration`, a registry rename the
    # tracker rewrote — fires it, and it is the ONE seam that decides how the
    # change applies without restarting HA (FR8): a reload at once while the
    # engine is idle, a reload deferred until the running cycle completes
    # otherwise (Story 1.7). No flow and no service performs its own reload,
    # and none of them refuses an edit because a cycle is running.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    device_registry = dr.async_get(hass)
    # The controller is a virtual service device; zone devices link to it below
    # by device id (`via_device_id` — the identifier-tuple form was deprecated
    # in HA 2026.9), so its registry entry is kept.
    controller_device = device_registry.async_get_or_create(
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
            via_device_id=controller_device.id,
            manufacturer="ha-irrigation-controller",
            name=subentry.title,
        )

    # The engine and its adapters. The switch adapter's cycle_id provider
    # closes over the sequencer built on the next statement — late-bound on
    # purpose, since the two reference each other (AD-7 needs the running
    # cycle's id to mint its one Context).
    clock = HaClock()
    journal = JournalAdapter(hass)
    # The anomaly manager (Story 3.1) is the ONE fan-out: Repairs, the push,
    # the bus event and health. The push target is a `notify.*` entity read
    # from the options — absent or unreadable means no push, said once in the
    # log, never a setup failure. Options edits reach it through the reload
    # regime, so a new target applies without an HA restart.
    notify_target = parse_notify_target(entry.options)
    anomalies = AnomalyManager(
        hass,
        entry,
        notify=None if notify_target is None else HaNotifyAdapter(hass, notify_target),
    )
    # The rain gauge (Story 2.4): read by the engine at quote time only, so
    # nothing is subscribed here and no `after_dependencies` is declared. The
    # option is required by the flow, but a stored entry is unvalidated input
    # — anything but an entity id string means no gauge, and every cycle is
    # quoted on full durations (fail-wet). A registry rename rewrites the
    # option and reloads, so the adapter is rebuilt with the new id.
    rain_entity_id = entry.options.get(CONF_RAIN_SENSOR)
    rain = (
        RainSensorAdapter(hass, rain_entity_id)
        if isinstance(rain_entity_id, str)
        else None
    )
    switches = VerifiedSwitchAdapter(
        hass,
        timeout_s=actuation_timeout_s,
        cycle_id_provider=lambda: (
            sequencer.current_run.cycle_id if sequencer.current_run else None
        ),
    )
    # The ONE read of the journal: the outcome history AC 4 promises to
    # retain for 7 days, the season mode flag (Story 1.6), the water-debt
    # ledger (Story 2.2) and — Story 3.2, AD-11's recovery path — the
    # machine state: the in-flight run, the last completed run and the
    # deferred queue, each validated at the adapter's trust boundary. Seeding
    # them acts on nothing: the runner's `async_start` below runs the
    # engine's `async_reconcile` before it arms a single timer, and that is
    # the only code that resumes or closes a restored run. An unreadable run
    # reaches the engine as `run_unreadable`, raw, so the reconciler can make
    # the hardware safe without trusting it.
    seed = await journal.async_load_seed()
    sequencer = Sequencer(
        plan,
        switches=switches,
        journal=journal,
        anomalies=anomalies,
        rain=rain,
        history=seed.history,
        season_enabled=seed.season_enabled,
        ledger=seed.ledger,
        run=seed.run,
        last_run=seed.last_run,
        deferred=seed.deferred,
        run_unreadable=seed.run_unreadable,
    )
    runner = CycleRunner(hass, entry, sequencer=sequencer, clock=clock)
    # Follows the configured entities through the registry: renames rewrite
    # the stored ids (which fires the listener above), disappearance raises an
    # anomaly. Built here, started below once the unload hooks can own it.
    tracker = ConfiguredEntityTracker(
        hass,
        entry,
        anomalies=anomalies,
        switches=switches,
    )

    # BEFORE forwarding: platform setup reads runtime_data.
    entry.runtime_data = HaIrrigationRuntimeData(
        plan=plan,
        sequencer=sequencer,
        runner=runner,
        journal=journal,
        anomalies=anomalies,
        tracker=tracker,
        config_fingerprint=config_fingerprint(entry),
    )
    # ALSO before forwarding: the manager re-seeds its open set from the
    # Repairs issues that survived the previous load, and the health binary
    # sensor reads that set the moment it is added. Its stop is registered
    # right after its start, like the tracker's below.
    anomalies.async_start()
    entry.async_on_unload(anomalies.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # All three must run on every unload path: a surviving timer fails PHCC's
    # verify_cleanup and leaks per reload, a dropped flush loses the last
    # transition (the reload regime runs constantly), and a surviving registry
    # subscription would keep rewriting a dead entry's options.
    #
    # ORDER CONTRACT: HA processes these LIFO, so the LAST registration runs
    # FIRST — the timers are cancelled before the tracker stops and the
    # journal is flushed, and the flush therefore persists a state nothing
    # can still move.
    #
    # Registered BEFORE the timers are armed: HA runs the on-unload callbacks
    # when setup itself fails (`ConfigEntry.async_setup`'s `finally`), so a
    # daily start armed while a later one raises would otherwise have no
    # registered cancel and leak for the process lifetime. The tracker's stop
    # is registered IMMEDIATELY after its start for the same reason.
    #
    # `runner.async_start` is LAST: it reconciles the restored run against
    # the hardware (Story 3.2) — commanding switches through the adapter and
    # possibly completing a cycle through the journal — so everything it may
    # touch is wired and every unload hook that must undo it is registered.
    entry.async_on_unload(journal.async_flush)
    tracker.async_start()
    entry.async_on_unload(tracker.async_stop)
    entry.async_on_unload(runner.async_shutdown)
    await runner.async_start()
    return True


async def _async_entry_updated(
    hass: HomeAssistant,
    entry: HaIrrigationConfigEntry,
) -> None:
    """Apply a config change: reload now when idle, after the cycle otherwise.

    The restart-free half of FR8, made cycle-aware (Story 1.7). Three gates,
    in order:

    1. **No runtime to consult** — a defensive guard. The listener is
       registered before `runtime_data` exists, so an update landing in the
       mid-setup window (setup awaits the journal seed and the platform
       forwarding) reaches it on a SETUP_IN_PROGRESS entry; any call on a
       non-LOADED entry falls back to the pre-1.7 behaviour, a reload now.
       (A failed setup does NOT leave the listener behind: HA runs the
       on-unload hooks on failure, so a SETUP_ERROR entry has no listener
       and the operator's fix still needs the manual reload the
       `invalid_stored_config` message asks for.)
    2. **Nothing the plan is built from changed** — a title rename or a pref
       flip fires the listener too; an equal fingerprint costs nothing.
    3. **Idle or busy** — idle reloads at once through the runner. Busy swaps
       the engine's plan so a cycle created before the reload lands (a
       deferred-queue pop inside `_complete_cycle`, a daily start firing
       meanwhile) already uses the new parameters, re-arms the daily starts
       from it (an edited start time must not be skipped for the day), then
       asks the runner to reload once the cycle completes. The running cycle
       itself only ever reads its AD-8 snapshot. A new plan that does not
       validate keeps the old one in the engine (fail-wet) and lets the
       eventual reload fail setup with the existing `invalid_stored_config`.
       The tracker re-subscribes either way, so an entity configured
       mid-cycle is followed before the reload too.
    """
    if entry.state is not ConfigEntryState.LOADED or not hasattr(
        entry,
        "runtime_data",
    ):
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    data = entry.runtime_data
    fingerprint = config_fingerprint(entry)
    if fingerprint == data.config_fingerprint:
        return
    if data.sequencer.current_run is None:
        data.runner.async_request_reload()
        return
    try:
        plan = build_plan(
            entry.options,
            [
                (subentry.subentry_id, subentry.title, subentry.data)
                for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)
            ],
        )
    except PlanValidationError as err:
        LOGGER.warning(
            "Configuration edited mid-cycle does not validate; the running cycle "
            "keeps its plan and the reload after it will report the problem: %s",
            err,
        )
    else:
        data.sequencer.plan = plan
        data.plan = plan
        data.runner.async_replan()
    data.config_fingerprint = fingerprint
    data.tracker.async_start()
    data.runner.async_request_reload()


async def async_unload_entry(
    hass: HomeAssistant,
    entry: HaIrrigationConfigEntry,
) -> bool:
    """Unload a config entry.

    `runner.async_suspend()` runs FIRST, while the engine and its adapters are
    still whole: when this unload is anything but the engine's own deferred
    reload (which only ever fires idle), a cycle may be RUNNING, and the live
    valve and the pump must be commanded off before the platforms and the
    timers go — the run itself is left in the journal for Story 3.2. Idle, it
    is a no-op.

    The timer cancel, the tracker stop and the journal flush ride on
    `async_on_unload`, so they are deliberately NOT repeated here — one
    registration, one home (the suspend's own shutdown call makes the timer
    cancel idempotent).
    """
    await entry.runtime_data.runner.async_suspend()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant,
    entry: HaIrrigationConfigEntry,  # noqa: ARG001
) -> None:
    """Delete the journal store and every Repairs issue when the integration goes.

    A throwaway Store with the same key: `runtime_data` is gone by the time
    HA calls this hook, and `async_remove` only needs the key to delete the
    `.storage` file. The issues are keyed by domain, not by entry, so they
    need no runtime either.
    """
    async_delete_domain_issues(hass)
    await Store(hass, JOURNAL_SCHEMA_VERSION, STORAGE_KEY).async_remove()
