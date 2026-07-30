"""Engine tests run on plain pytest — zero Home Assistant fixtures (AD-1).

Overrides the root autouse fixture, which would otherwise drag the whole PHCC
harness (bluetooth, zeroconf and network mocks, translation and bcrypt fixtures)
into the hass-free suite and make it depend on Home Assistant being installed.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """Neutralise the root HA fixture for the engine suite."""
