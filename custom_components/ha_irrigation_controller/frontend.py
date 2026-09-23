"""Serve the bundled timeline card and register its Lovelace resource (Story 4.2).

AD-12's serving seam, in one place. Two halves with very different guarantees:

1. **The static path** — `FRONTEND_URL` serves the committed rollup bundle in
   `frontend/`. Registered unconditionally at component setup through
   `hass.http`; it depends on nothing but `http`, and a YAML-mode dashboard
   needs the URL to exist so the operator can point a resource at it.
2. **The Lovelace resource** — on storage-mode resource collections the
   resource is created for the operator (a community-established pattern,
   not an official API), ONE item whose `url` is the bundle path plus
   `?v=<manifest version>` so a release invalidates the browser cache; a
   version change updates that item in place. Everything here is
   best-effort: YAML resources, no lovelace, an unexpected shape from the
   lovelace internals or an exception while writing the item all log ONE
   line pointing at the README's manual fallback and never fail setup.

Registration hangs off `async_when_setup_or_start(hass, "lovelace", ...)`:
`after_dependencies` orders this integration after lovelace only when both
are in the same bootstrap plan, whereas the helper fires when lovelace
finishes setting up OR at `EVENT_HOMEASSISTANT_START`, whichever comes
first, and fires ONCE — one code path for "already loaded", "loads later,
before HA START" and "never loads".
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

# `http/__init__.py` re-exports the dataclass without an `__all__`, which
# strict mypy reads as private; the public path is the one core uses.
from homeassistant.components.http import StaticPathConfig  # type: ignore[attr-defined]
from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_when_setup_or_start

from .const import DOMAIN, FRONTEND_URL, LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# The committed bundle, `card/rollup.config.mjs`'s output — rebuilt from the
# card sources in the same change and compared by the card CI's freshness gate.
BUNDLE_PATH: Final = (
    Path(__file__).parent / "frontend" / "ha-irrigation-timeline-card.js"
)

# The README section every "add it by hand" log line points at.
README_CARD_ANCHOR: Final = (
    "https://github.com/ic3rus/ha-irrigation-controller#dashboard-card"
)

# The resource type Lovelace loads as an ES module — the card bundle's format.
RESOURCE_TYPE_MODULE: Final = "module"


def resource_url(version: str) -> str:
    """Return the resource URL for `version`: the bundle path plus `?v=`."""
    return f"{FRONTEND_URL}?v={version}"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the bundle now and register its resource once lovelace is up.

    Called ONCE from `async_setup` (component setup, never per entry). The
    static path is registered synchronously here, before the lovelace half
    is even scheduled: serving the file must not depend on how — or whether
    — dashboards load their resources.
    """
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(BUNDLE_PATH), cache_headers=True)],
    )
    version = str((await async_get_integration(hass, DOMAIN)).version)

    async def _register_resource(hass: HomeAssistant, _component: str) -> None:
        """Apply the resource rules to whatever lovelace turned out to be."""
        try:
            await _async_register_resource(hass, version)
        except Exception:  # noqa: BLE001 — a dashboard nicety must never fail setup
            LOGGER.warning(
                "Could not register the timeline card as a Lovelace resource; "
                "add it by hand, see %s",
                README_CARD_ANCHOR,
                exc_info=True,
            )

    async_when_setup_or_start(hass, "lovelace", _register_resource)


async def _async_register_resource(hass: HomeAssistant, version: str) -> None:
    """Create or update THE resource item on a storage-mode collection.

    Fires once lovelace is set up or Home Assistant has started, whichever
    comes first. Each way out that does not write an item logs exactly one
    line: DEBUG when lovelace never loaded (no dashboards, nothing to add
    the card to), INFO for YAML resources (the operator manages the list),
    WARNING when the lovelace internals are not the shape this pattern
    relies on. An exception anywhere below is the caller's warning.
    """
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        LOGGER.debug(
            "Lovelace is not loaded; the timeline card resource was not registered. "
            "Should dashboards appear later, add it by hand, see %s",
            README_CARD_ANCHOR,
        )
        return
    if data.resource_mode != MODE_STORAGE:
        LOGGER.info(
            "Lovelace resources are managed in YAML; add the timeline card "
            "resource by hand (%s type: module), see %s",
            resource_url(version),
            README_CARD_ANCHOR,
        )
        return
    resources = data.resources
    if not isinstance(resources, ResourceStorageCollection):
        LOGGER.warning(
            "Lovelace resources have an unexpected shape (%s); add the timeline "
            "card resource by hand, see %s",
            type(resources).__name__,
            README_CARD_ANCHOR,
        )
        return
    # LOAD-BEARING: the collection reads its store lazily. Its own
    # `async_create_item`/`async_update_item` ensure the load (through the
    # private `_async_ensure_loaded`), but `async_items()` below does NOT —
    # skipping this would find nothing and create a duplicate on every start.
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True

    url = resource_url(version)
    existing = next(
        (
            item
            for item in resources.async_items()
            if str(item.get("url", "")).startswith(FRONTEND_URL)
        ),
        None,
    )
    if existing is None:
        await resources.async_create_item(
            {"res_type": RESOURCE_TYPE_MODULE, "url": url},
        )
        LOGGER.debug("Registered the timeline card Lovelace resource %s", url)
    elif existing["url"] != url:
        await resources.async_update_item(existing["id"], {"url": url})
        LOGGER.debug(
            "Updated the timeline card Lovelace resource from %s to %s",
            existing["url"],
            url,
        )
