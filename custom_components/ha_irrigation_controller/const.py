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
