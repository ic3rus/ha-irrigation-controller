"""Constants for the HA Irrigation Controller integration."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "ha_irrigation_controller"

# Runtime guard — the hacs.json min-version pin is NOT trusted (hacs/integration#4243).
# Single source of truth: tests/test_manifest.py asserts hacs.json agrees with this.
# Policy: the floor is the month release of the stack pinned in
# requirements_test.txt, raised when older releases stop being supported
# (2026-09: 2026.9 — the 2026.9 device-registry API is used directly).
MIN_HA_VERSION = "2026.9.0"

# Derived (major, minor) for the setup guard. Comparing on month-release
# granularity rather than the full string accepts HA prereleases of the minimum
# release (2026.9.0b5 sorts BELOW 2026.9.0), which the CI beta leg exists to support.
_MIN_HA_PARTS = MIN_HA_VERSION.split(".")
MIN_HA_MAJOR = int(_MIN_HA_PARTS[0])
MIN_HA_MINOR = int(_MIN_HA_PARTS[1])

# Controller configuration keys. Every one of them is operator-editable, so they
# all live in `entry.options` (entry.data stays empty) — one home, no data/options
# fallback duplication. Zone parameters are config subentries (Story 1.3).
CONF_PUMP_SWITCH = "pump_switch"
CONF_RAIN_SENSOR = "rain_sensor"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"
CONF_MORNING_ENABLED = "morning_enabled"
CONF_MORNING_START = "morning_start"
CONF_EVENING_START = "evening_start"
CONF_ACTUATION_TIMEOUT = "actuation_timeout"

# Actuation confirmation timeout — SECONDS at every surface (UI, storage,
# engine), unlike zone durations (minutes at UI): this bounds how long a switch
# command waits for its state change, and a minutes-grained wait for a relay
# confirmation is meaningless. Absent key = the default (entries created before
# Story 1.5 have no such key and must keep loading).
DEFAULT_ACTUATION_TIMEOUT_S = 10
MIN_ACTUATION_TIMEOUT_S = 1
MAX_ACTUATION_TIMEOUT_S = 120

# Verb-named actions (FR5, AD-10). Registered in `async_setup` and NEVER
# removed on unload (`action-setup`), so an automation referencing one stays
# editable while the entry is down. `run_now` joins them in Story 2.1.
SERVICE_CANCEL_CYCLE = "cancel_cycle"
SERVICE_SET_ZONE_DURATION = "set_zone_duration"
SERVICE_SET_SEASON = "set_season"

# Service field names. `zone_id` is the subentry id — the zone key everywhere
# (AD-8), so the services take the same identity the journal and the card do.
# `duration` is MINUTES, matching every other duration UI surface.
ATTR_ZONE_ID = "zone_id"
ATTR_CYCLE = "cycle"
ATTR_DURATION = "duration"
ATTR_ENABLED = "enabled"

# The SINGLE bus event type this integration ever fires (conventions table).
# Payloads carry an "event_type" discriminator ("anomaly" today; more kinds
# join in later stories) — never a second event type per feature.
EVENT_HA_IRRIGATION_CONTROLLER = "ha_irrigation_controller_event"


def engine_state_signal(entry_id: str) -> str:
    """Return the dispatcher signal pushed after every engine step (AD-6).

    THE one signal entity projections subscribe to: they never poll and never
    read a second authority. Entry-scoped so a reload's fresh runner cannot
    drive the previous entry's (already removed) entities.
    """
    return f"{DOMAIN}_{entry_id}_engine_state"


# TimeSelector serializes "HH:MM:SS" strings; store them verbatim and let the
# engine parse them (Story 1.4). Times are Home Assistant local at UI surfaces.
DEFAULT_MORNING_START = "07:00:00"
DEFAULT_EVENING_START = "20:00:00"

# Zone configuration — one config subentry per zone (AD-8). The subentry id is
# the zone key everywhere (journal, payloads, entities); the operator-given name
# is the subentry TITLE, never stored in data.
#
# ORDER CONTRACT: `entry.get_subentries_of_type(SUBENTRY_TYPE_ZONE)` returns
# zones in insertion order, which persists across restarts (subentries are
# stored as an ordered list). That order IS the watering order — the sequencer
# iterates it as-is. Editing keeps a zone's position; remove + re-add appends.
SUBENTRY_TYPE_ZONE = "zone"
CONF_VALVE_SWITCH = "valve_switch"
CONF_MORNING_DURATION = "morning_duration"
CONF_EVENING_DURATION = "evening_duration"
CONF_RAIN_EXPOSED = "rain_exposed"
CONF_RAIN_FACTOR = "rain_factor"

# Units contract: durations are stored in MINUTES (ints) in subentry data — the
# config flow is a UI surface ("minutes at UI, seconds internally"); the engine
# converts to seconds when it builds plans (Story 1.4). Rain accumulates in mm;
# the rain factor is min/mm (minutes of watering credited per mm of rain).

# Exposed by default: a garden zone is outdoors; a sheltered default would
# silently disable the product's headline feature (rain modulation) on every
# zone. The conservative factor below bounds the mislabel risk.
DEFAULT_RAIN_EXPOSED = True
# min/mm — deliberately conservative: a 10 mm downpour reduces a zone by only
# 10 minutes. The operator calibrates across the season (FR17). 0 = never
# reduce this zone.
DEFAULT_RAIN_FACTOR = 1.0
MIN_RAIN_FACTOR = 0.0
MAX_RAIN_FACTOR = 10.0
DEFAULT_ZONE_DURATION_MINUTES = 10
MIN_ZONE_DURATION_MINUTES = 1
MAX_ZONE_DURATION_MINUTES = 120
