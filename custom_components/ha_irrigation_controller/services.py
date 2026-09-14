"""The integration's verb-named actions (FR5, AD-10, AC 3).

**Declared deviation:** the architecture's structural seed comments put service
registration in `__init__.py`. Three handlers with their schemas and their
validation would roughly double that file, and both Home Assistant core
practice (`tado/services.py` and friends) and the `common-modules` quality-scale
rule favour the split. `__init__.py` keeps the registration CALL; the handlers
live here.

Five rules hold this module together:

1. Registered in `async_setup`, ONCE, and never removed on unload
   (`action-setup`).
2. The entry is resolved PER CALL through
   `homeassistant.helpers.service.async_get_config_entry`, never captured at
   registration time. With `single_config_entry: true` the `entry_id=None`
   path resolves the one entry and raises the already-translated
   `ServiceValidationError` for "no entry" / "more than one" / "not loaded" —
   three branches this module neither writes nor has to cover.
3. Every error AC 5 enumerates is a `ServiceValidationError` carrying
   `translation_domain` + `translation_key` (`action-exceptions`). Never a bare
   `return`, never a `HomeAssistantError` for bad input. The schemas below are
   the one exception and are NOT translated: a value that is the wrong TYPE
   (`cycle: "afternoon"`, a non-numeric `duration`, a missing `enabled`) fails
   voluptuous and surfaces as `vol.Invalid`, exactly as it does in every core
   integration. None of those is one of AC 5's cases.
4. Range and identity validation live in the HANDLERS, not in the voluptuous
   schemas: a schema failure raises `vol.Invalid`, which is not what AC 5 asks
   for. The `services.yaml` selectors are UI affordances only — the WebSocket
   API bypasses them exactly like it bypasses the config-flow pickers.
5. No `async_register_admin_service`: household automations, scripts and voice
   assistants must be able to call all three.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import voluptuous as vol
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_get_config_entry

from .const import (
    ATTR_CYCLE,
    ATTR_DURATION,
    ATTR_ENABLED,
    ATTR_ZONE_ID,
    CONF_EVENING_DURATION,
    CONF_MORNING_DURATION,
    DOMAIN,
    MAX_ZONE_DURATION_MINUTES,
    MIN_ZONE_DURATION_MINUTES,
    SERVICE_CANCEL_CYCLE,
    SERVICE_SET_SEASON,
    SERVICE_SET_ZONE_DURATION,
    SUBENTRY_TYPE_ZONE,
)

# Importing the engine from the HA side is the ALLOWED direction (the forbidden
# one is engine → homeassistant.*): the overlap rule lives in ONE place and
# this module never duplicates its sum-of-durations math (AD-5).
from .engine.config import build_plan
from .engine.plan import CycleKind, overlap_offender

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant, ServiceCall

    from . import HaIrrigationConfigEntry

# Which stored key a cycle kind addresses. One mapping rather than a branch per
# call site, so a third cycle kind (were one ever added) lands in one place.
_DURATION_KEY: dict[CycleKind, str] = {
    CycleKind.MORNING: CONF_MORNING_DURATION,
    CycleKind.EVENING: CONF_EVENING_DURATION,
}

_SET_SEASON_SCHEMA = vol.Schema({vol.Required(ATTR_ENABLED): cv.boolean})

# `duration` is coerced to an int and NOT range-checked here — see rule 4.
_SET_ZONE_DURATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ZONE_ID): cv.string,
        vol.Required(ATTR_CYCLE): vol.In([kind.value for kind in CycleKind]),
        vol.Required(ATTR_DURATION): vol.Coerce(int),
    },
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the three actions on the component, once."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_CANCEL_CYCLE,
        _async_cancel_cycle,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SEASON,
        _async_set_season,
        schema=_SET_SEASON_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ZONE_DURATION,
        _async_set_zone_duration,
        schema=_SET_ZONE_DURATION_SCHEMA,
    )


def _loaded_entry(call: ServiceCall) -> HaIrrigationConfigEntry:
    """Return the ONE loaded entry, or raise the translated core error.

    The cast is the price of a helper core types as a plain `ConfigEntry`: it
    is this integration's domain by construction, so its `runtime_data` is
    ours.
    """
    return cast(
        "HaIrrigationConfigEntry",
        async_get_config_entry(call.hass, DOMAIN, None),
    )


async def _async_cancel_cycle(call: ServiceCall) -> None:
    """Stop the running cycle: valve off, pump off, filed CANCELLED (AC 4).

    The ONLY thing that stops a cycle. It is a permitted non-watering cause,
    so it raises no anomaly — but calling it with nothing running is an
    operator error and says so.
    """
    entry = _loaded_entry(call)
    if not await entry.runtime_data.runner.async_cancel_cycle():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_cycle_running",
        )


async def _async_set_season(call: ServiceCall) -> None:
    """Turn scheduling on or off in one action (FR10, AC 1).

    Idempotent: calling it twice with the same value is not an error. A cycle
    already RUNNING completes — only `cancel_cycle` stops one.
    """
    entry = _loaded_entry(call)
    await entry.runtime_data.runner.async_set_season(
        enabled=call.data[ATTR_ENABLED],
    )


async def _async_set_zone_duration(call: ServiceCall) -> None:
    """Set one zone's watering minutes for one cycle (FR8).

    Deliberately NOT routed through the runner: a duration is *configuration*,
    and configuration has exactly one write path (AD-8) — `async_update_subentry`
    then the ONE update listener, which reloads the entry at once when the
    engine is idle and defers the reload until the running cycle completes
    otherwise (Story 1.7).

    Deliberately NOT refused while a cycle is running either: the running
    cycle keeps its snapshotted durations and the new value applies to the
    next one, so there is nothing to protect the operator from. Refusing the
    call would contradict FR8's "edit durations at any moment", and the
    cycle-aware branch lives in the listener, never in a service.
    """
    entry = _loaded_entry(call)
    zone_id: str = call.data[ATTR_ZONE_ID]
    duration: int = call.data[ATTR_DURATION]
    key = _DURATION_KEY[CycleKind(call.data[ATTR_CYCLE])]

    # Pinned rather than inferred: `entry.subentries` is a read-only
    # MappingProxyType, and naming it keeps mypy strict from widening it.
    subentries: Mapping[str, ConfigSubentry] = entry.subentries
    subentry = subentries.get(zone_id)
    if subentry is None or subentry.subentry_type != SUBENTRY_TYPE_ZONE:
        # The id is a hex string no operator types from memory, so the message
        # carries the ones that would have worked.
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_zone",
            translation_placeholders={
                "zone_id": zone_id,
                "known": _known_zones(entry),
            },
        )

    if not MIN_ZONE_DURATION_MINUTES <= duration <= MAX_ZONE_DURATION_MINUTES:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_duration",
            translation_placeholders={
                "duration": str(duration),
                "min": str(MIN_ZONE_DURATION_MINUTES),
                "max": str(MAX_ZONE_DURATION_MINUTES),
            },
        )

    proposed: dict[str, object] = {**subentry.data, key: duration}
    # `build_plan` can only raise here if the STORED config is already broken,
    # which `async_setup_entry` refuses to load — and `async_get_config_entry`
    # above already required a LOADED entry. So the error is unreachable by
    # construction and deliberately not caught: swallowing it the way the
    # config flows do would hide a genuine corruption, and unlike the flows
    # this path is not the operator's repair route.
    zones = _proposed_zones(entry, zone_id, proposed)
    if overlap_offender(build_plan(entry.options, zones)) is not None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="cycles_overlap",
        )

    # The ONLY write path for configuration (AD-8): never by assigning to
    # `subentry.data`. Returns immediately — the ONE update listener applies
    # the change (a reload now, or after the running cycle) on the next tick.
    call.hass.config_entries.async_update_subentry(entry, subentry, data=proposed)


def _proposed_zones(
    entry: HaIrrigationConfigEntry,
    zone_id: str,
    proposed: Mapping[str, object],
) -> list[tuple[str, str, Mapping[str, object]]]:
    """Return the entry's zones with `zone_id`'s data replaced by `proposed`.

    The same `(subentry_id, title, data)` items `async_setup_entry` builds, in
    the same watering order — so the overlap check sees exactly the plan the
    reload would produce.
    """
    return [
        (
            subentry.subentry_id,
            subentry.title,
            proposed if subentry.subentry_id == zone_id else subentry.data,
        )
        for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)
    ]


def _known_zones(entry: HaIrrigationConfigEntry) -> str:
    """Return the zones as `id (name)` pairs for an error message.

    A controller with no zones is a supported state (the engine runs zero-zone
    plans), and it is the state in which a wrong `zone_id` is most likely — so
    the message says so rather than trailing off into "are: .".
    """
    pairs = ", ".join(
        f"{subentry.subentry_id} ({subentry.title})"
        for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)
    )
    return pairs or "none — this controller has no zones configured"
