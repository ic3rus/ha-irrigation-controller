"""Hass-free irrigation engine core (AD-1).

This package MUST NOT import anything from ``homeassistant.*`` — ever.
Time enters as injected datetimes; all I/O goes through the ``Protocol``
ports defined in :mod:`.ports`.

Layout: :mod:`.plan` (declarative model, schedule derivation, overlap rule,
irrigation-day helper), :mod:`.runs` (first-class status-enum run objects),
:mod:`.sequencer` (the event-scheduled state machine), :mod:`.config` (the
stored-config → plan builder with load-time validation).
"""
