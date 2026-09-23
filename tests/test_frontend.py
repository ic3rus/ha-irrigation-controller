"""The card's serving seam: static path and Lovelace resource (Story 4.2, AD-12).

One test per backend row of the spec's matrix. The static path is proven
with a real HTTP GET; the resource rules are proven against a real lovelace
component in storage mode (a fresh store, a seeded one, a seeded older
version), in YAML mode, absent altogether, loaded after us, and with the
lovelace internals misbehaving — setup must succeed and log exactly one
fallback line in every case that writes no item.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import (
    RESOURCE_STORAGE_KEY,
    RESOURCES_STORAGE_VERSION,
    ResourceStorageCollection,
)
from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.setup import async_setup_component

import custom_components.ha_irrigation_controller
from custom_components.ha_irrigation_controller.const import DOMAIN, FRONTEND_URL
from custom_components.ha_irrigation_controller.frontend import (
    BUNDLE_PATH,
    README_CARD_ANCHOR,
    resource_url,
)

if TYPE_CHECKING:
    import pytest
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

MANIFEST_VERSION = json.loads(
    (
        Path(custom_components.ha_irrigation_controller.__file__).parent
        / "manifest.json"
    ).read_text(encoding="utf-8"),
)["version"]

OUR_LOGGER = "custom_components.ha_irrigation_controller"
SEEDED_ID = "0123456789abcdef0123456789abcdef"


def seed_resources(hass_storage: dict[str, Any], *items: dict[str, Any]) -> None:
    """Pre-populate lovelace's resource store the way a previous run left it."""
    hass_storage[RESOURCE_STORAGE_KEY] = {
        "version": RESOURCES_STORAGE_VERSION,
        "minor_version": 1,
        "key": RESOURCE_STORAGE_KEY,
        "data": {"items": list(items)},
    }


def card_resources(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return every resource item that points at the card bundle."""
    return [
        item
        for item in hass.data[LOVELACE_DATA].resources.async_items()
        if str(item["url"]).startswith(FRONTEND_URL)
    ]


def fallback_lines(
    caplog: pytest.LogCaptureFixture,
    level: int,
) -> list[logging.LogRecord]:
    """Return our log records at `level` that point at the README fallback."""
    return [
        record
        for record in caplog.records
        if record.name == OUR_LOGGER
        and record.levelno == level
        and README_CARD_ANCHOR in record.getMessage()
    ]


# --------------------------------------------------------------------------
# The static path
# --------------------------------------------------------------------------


async def test_the_bundle_is_served_from_the_static_path(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
) -> None:
    """Matrix "Bundle served": 200, the committed bytes, cache headers."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    client = await hass_client()
    response = await client.get(FRONTEND_URL)

    assert response.status == 200
    assert await response.read() == BUNDLE_PATH.read_bytes()
    assert "Cache-Control" in response.headers


async def test_the_static_path_does_not_need_lovelace(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
) -> None:
    """Serving never depends on lovelace: no lovelace at all, still 200."""
    assert await async_setup_component(hass, DOMAIN, {})
    hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
    await hass.async_block_till_done()
    assert LOVELACE_DATA not in hass.data

    client = await hass_client()
    response = await client.get(FRONTEND_URL)

    assert response.status == 200


# --------------------------------------------------------------------------
# Storage-mode resources
# --------------------------------------------------------------------------


async def test_storage_mode_first_setup_creates_one_module_resource(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matrix "Storage resources, first setup": ONE module item with `?v=`."""
    assert await async_setup_component(hass, "lovelace", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    items = card_resources(hass)
    assert len(items) == 1
    assert items[0]["type"] == "module"
    assert items[0]["url"] == resource_url(MANIFEST_VERSION)
    assert items[0]["url"] == f"{FRONTEND_URL}?v={MANIFEST_VERSION}"
    assert fallback_lines(caplog, logging.WARNING) == []
    assert fallback_lines(caplog, logging.INFO) == []


async def test_storage_mode_restart_keeps_the_existing_resource(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Matrix "Storage resources, restart": the seeded item, and nothing else."""
    seed_resources(
        hass_storage,
        {"id": SEEDED_ID, "type": "module", "url": resource_url(MANIFEST_VERSION)},
        {"id": "other", "type": "module", "url": "/hacsfiles/other-card.js"},
    )
    assert await async_setup_component(hass, "lovelace", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    items = card_resources(hass)
    assert len(items) == 1
    assert items[0]["id"] == SEEDED_ID
    assert items[0]["url"] == resource_url(MANIFEST_VERSION)
    assert len(hass.data[LOVELACE_DATA].resources.async_items()) == 2


async def test_storage_mode_upgrade_updates_the_resource_in_place(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Matrix "Storage resources, upgrade": same id, new `?v=`, still one item."""
    seed_resources(
        hass_storage,
        {"id": SEEDED_ID, "type": "module", "url": resource_url("0.0.1")},
    )
    assert await async_setup_component(hass, "lovelace", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    items = card_resources(hass)
    assert len(items) == 1
    assert items[0]["id"] == SEEDED_ID
    assert items[0]["url"] == resource_url(MANIFEST_VERSION)
    assert items[0]["type"] == "module"


async def test_lovelace_loaded_after_us_still_gets_the_resource(
    hass: HomeAssistant,
) -> None:
    """Matrix "Lovelace loaded later": registration waits for lovelace's setup."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert LOVELACE_DATA not in hass.data

    assert await async_setup_component(hass, "lovelace", {})
    await hass.async_block_till_done()

    items = card_resources(hass)
    assert len(items) == 1
    assert items[0]["url"] == resource_url(MANIFEST_VERSION)


# --------------------------------------------------------------------------
# The guarded ways out
# --------------------------------------------------------------------------


async def test_yaml_resources_log_the_manual_fallback_once(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matrix "YAML resources": no item, one INFO line naming the README."""
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"mode": "yaml"}})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert card_resources(hass) == []
    lines = fallback_lines(caplog, logging.INFO)
    assert len(lines) == 1
    assert resource_url(MANIFEST_VERSION) in lines[0].getMessage()
    assert fallback_lines(caplog, logging.WARNING) == []


async def test_lovelace_absent_at_start_is_one_debug_line(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matrix "Lovelace absent": HA starts without it → one DEBUG line, no more."""
    caplog.set_level(logging.DEBUG, logger=OUR_LOGGER)
    assert await async_setup_component(hass, DOMAIN, {})
    hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
    await hass.async_block_till_done()

    debug = [
        record
        for record in caplog.records
        if record.name == OUR_LOGGER
        and record.levelno == logging.DEBUG
        and "Lovelace is not loaded" in record.getMessage()
    ]
    assert len(debug) == 1
    assert README_CARD_ANCHOR in debug[0].getMessage()
    assert fallback_lines(caplog, logging.DEBUG) == debug
    assert fallback_lines(caplog, logging.INFO) == []
    assert fallback_lines(caplog, logging.WARNING) == []


async def test_item_creation_failure_is_one_warning_and_setup_succeeds(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matrix "item creation fails": swallowed, one WARNING with the fallback."""
    assert await async_setup_component(hass, "lovelace", {})
    with patch.object(
        ResourceStorageCollection,
        "async_create_item",
        side_effect=RuntimeError("storage exploded"),
    ):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    assert card_resources(hass) == []
    lines = fallback_lines(caplog, logging.WARNING)
    assert len(lines) == 1
    assert lines[0].exc_info is not None


async def test_unexpected_lovelace_internals_are_one_warning(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matrix "Lovelace internals changed": storage mode, but not the collection."""
    assert await async_setup_component(hass, "lovelace", {})
    with patch.object(hass.data[LOVELACE_DATA], "resources", object()):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()

    lines = fallback_lines(caplog, logging.WARNING)
    assert len(lines) == 1
    assert "unexpected shape" in lines[0].getMessage()
    assert card_resources(hass) == []
