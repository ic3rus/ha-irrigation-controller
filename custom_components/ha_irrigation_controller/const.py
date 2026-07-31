"""Constants for the HA Irrigation Controller integration."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "ha_irrigation_controller"

# Runtime guard — the hacs.json min-version pin is NOT trusted (hacs/integration#4243).
# Single source of truth: tests/test_manifest.py asserts hacs.json agrees with this.
MIN_HA_VERSION = "2026.7.0"

# Derived (major, minor) for the setup guard. Comparing on month-release
# granularity rather than the full string accepts HA prereleases of the minimum
# release (2026.7.0b5 sorts BELOW 2026.7.0), which the CI beta leg exists to support.
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
DEFAULT_ZONE_DURATION_MINUTES = 10
MIN_ZONE_DURATION_MINUTES = 1
MAX_ZONE_DURATION_MINUTES = 120
