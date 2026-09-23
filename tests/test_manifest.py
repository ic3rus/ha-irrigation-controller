"""Guards for the distribution metadata that acceptance criteria depend on.

AC 2 pins specific `manifest.json` keys and AC 3 pins a minimum HA version that
is repeated in `hacs.json` and the README. Nothing else in the suite would notice
a one-line edit to any of them, so these tests are the tripwire.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from homeassistant.util.yaml import parse_yaml

from custom_components.ha_irrigation_controller.const import (
    DOMAIN,
    MAX_ZONE_DURATION_MINUTES,
    MIN_HA_VERSION,
    MIN_ZONE_DURATION_MINUTES,
    SERVICE_CANCEL_CYCLE,
    SERVICE_RUN_NOW,
    SERVICE_SET_SEASON,
    SERVICE_SET_ZONE_DURATION,
)
from custom_components.ha_irrigation_controller.engine.plan import CycleKind

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "ha_irrigation_controller"
CARD_SRC = REPO_ROOT / "card" / "src" / "ha-irrigation-timeline-card.ts"


def _load_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from the repo."""
    with path.open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = json.load(handle)
        return loaded


def test_manifest_declares_the_keys_ac2_requires() -> None:
    """manifest.json declares the distribution contract from AC 2."""
    manifest = _load_json(INTEGRATION_DIR / "manifest.json")

    assert manifest["domain"] == DOMAIN
    assert manifest["iot_class"] == "calculated"
    assert manifest["integration_type"] == "helper"
    assert manifest["config_flow"] is True
    # Custom integrations must carry a version; HACS validation needs both URLs.
    assert manifest["version"]
    assert manifest["codeowners"]
    assert manifest["documentation"].startswith("https://")
    assert manifest["issue_tracker"].startswith("https://")
    # One controller entry only — enforced by HA core, not by the flow.
    assert manifest["single_config_entry"] is True
    # The WebSocket read channel (Story 4.1) registers its commands in
    # `async_setup`, so `websocket_api` (which pulls `http`) must be up first.
    # Nothing else: no `frontend`, no static paths — those are Story 4.2's.
    assert manifest["dependencies"] == ["websocket_api"]


def test_min_ha_version_is_consistent_across_the_repo() -> None:
    """hacs.json and the README agree with const.MIN_HA_VERSION."""
    hacs = _load_json(REPO_ROOT / "hacs.json")
    assert hacs["homeassistant"] == MIN_HA_VERSION

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert MIN_HA_VERSION in readme


def test_version_is_consistent_across_the_repo() -> None:
    """The four places the release version is written agree with each other."""
    manifest_version = _load_json(INTEGRATION_DIR / "manifest.json")["version"]

    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert pyproject["project"]["version"] == manifest_version

    package_json = _load_json(REPO_ROOT / "card" / "package.json")
    assert package_json["version"] == manifest_version

    # CARD_VERSION is what users read in the console banner and on the card.
    card_source = CARD_SRC.read_text(encoding="utf-8")
    card_version = re.search(r'CARD_VERSION\s*=\s*"([^"]+)"', card_source)
    assert card_version is not None, "CARD_VERSION not found in the card source"
    assert card_version.group(1) == manifest_version


def _load_services_yaml() -> dict[str, Any]:
    """Read `services.yaml` as a mapping of service name → definition."""
    loaded = parse_yaml(
        (INTEGRATION_DIR / "services.yaml").read_text(encoding="utf-8"),
    )
    assert isinstance(loaded, dict)
    return loaded


def _fields_of(definition: Any) -> dict[str, Any]:
    """Return a service definition's fields (a field-less service maps to None)."""
    if definition is None:
        return {}
    fields: dict[str, Any] = definition.get("fields", {})
    return fields


def test_services_yaml_declares_exactly_the_registered_actions() -> None:
    """`services.yaml` and the SERVICE_* constants cannot drift apart.

    A static file no test reads is a static file that goes stale silently: an
    action registered in code but absent here has no UI at all, and one
    described here but never registered is a dead entry in the action picker.
    """
    assert set(_load_services_yaml()) == {
        SERVICE_CANCEL_CYCLE,
        SERVICE_RUN_NOW,
        SERVICE_SET_SEASON,
        SERVICE_SET_ZONE_DURATION,
    }


def test_the_duration_selector_carries_the_const_bounds() -> None:
    """The UI slider and the handler's range check must agree.

    They are enforced in different places on purpose (a selector is not
    validation), which is exactly why nothing but this test would notice one
    of them moving.
    """
    duration = _fields_of(_load_services_yaml()[SERVICE_SET_ZONE_DURATION])["duration"]
    number = duration["selector"]["number"]

    assert number["min"] == MIN_ZONE_DURATION_MINUTES
    assert number["max"] == MAX_ZONE_DURATION_MINUTES


@pytest.mark.parametrize(
    "service",
    [SERVICE_SET_ZONE_DURATION, SERVICE_RUN_NOW],
)
def test_the_cycle_selector_lists_exactly_the_engine_cycle_kinds(service: str) -> None:
    """A third cycle kind must reach BOTH pickers, not only the schemas.

    `run_now` reuses `set_zone_duration`'s selector verbatim rather than
    describing the kinds a second way, and this is what keeps the copy honest.
    """
    cycle = _fields_of(_load_services_yaml()[service])["cycle"]

    assert cycle["selector"]["select"]["options"] == [kind.value for kind in CycleKind]


def test_run_now_requires_the_cycle_field_and_takes_nothing_else() -> None:
    """The caller NAMES the cycle: one required field, no defaulting rule."""
    fields = _fields_of(_load_services_yaml()[SERVICE_RUN_NOW])

    assert set(fields) == {"cycle"}
    assert fields["cycle"]["required"] is True


def test_the_translations_cover_every_service_and_field_both_ways() -> None:
    """Hassfest cross-checks these two files; this names the offender first.

    A service in one and not the other fails CI with a message about "extra
    keys not allowed" on the translation side or a missing name on the yaml
    side — neither of which points at the field that actually drifted.
    """
    services = _load_services_yaml()
    translated = _load_json(INTEGRATION_DIR / "translations" / "en.json")["services"]

    assert set(translated) == set(services)
    for name, definition in services.items():
        entry = translated[name]
        assert entry["name"]
        assert entry["description"]
        assert set(entry.get("fields", {})) == set(_fields_of(definition))
        for field in entry.get("fields", {}).values():
            assert field["name"]
            assert field["description"]


def test_every_exception_translation_key_the_services_raise_exists() -> None:
    """`action-exceptions`: an untranslated key renders as the raw string."""
    translated = _load_json(INTEGRATION_DIR / "translations" / "en.json")["exceptions"]

    for key in (
        "no_cycle_running",
        "cycle_already_running",
        "no_zones",
        "unknown_zone",
        "invalid_duration",
        "cycles_overlap",
    ):
        assert translated[key]["message"], key


def test_engine_has_no_declared_dependency_on_home_assistant() -> None:
    """manifest.json declares no requirements — the guard imports only HA core."""
    manifest = _load_json(INTEGRATION_DIR / "manifest.json")
    assert manifest.get("requirements", []) == [], (
        "New requirements must be justified: the setup guard deliberately uses "
        "homeassistant.const so no third-party version library is needed."
    )
