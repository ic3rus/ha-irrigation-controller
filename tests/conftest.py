"""Shared fixtures for the integration test suite.

Every fixture more than one module needs lives HERE. Importing one from a
sibling test module works, but it makes two suites depend on a third's
internals and costs an `F401`/`F811` noqa at each use site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in every test (mandatory PHCC fixture)."""
    return


@pytest.fixture
async def paris(hass: HomeAssistant) -> None:
    """Run the test in a DST-observing timezone, not the PHCC default."""
    await hass.config.async_set_time_zone("Europe/Paris")
