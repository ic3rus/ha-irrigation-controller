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
