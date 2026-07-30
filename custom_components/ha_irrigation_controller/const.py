"""Constants for the HA Irrigation Controller integration."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "ha_irrigation_controller"

# Runtime guard — the hacs.json min-version pin is NOT trusted (hacs/integration#4243).
MIN_HA_VERSION = "2026.7.0"
