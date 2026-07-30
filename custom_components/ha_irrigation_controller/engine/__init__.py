"""Hass-free irrigation engine core (AD-1).

This package MUST NOT import anything from ``homeassistant.*`` — ever.
Time enters as injected datetimes; all I/O goes through the ``Protocol``
ports defined in :mod:`.ports`. The real engine lands in Story 1.4.
"""
