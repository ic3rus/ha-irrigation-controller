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
# The card's contract module (`editor.ts`) owns CARD_VERSION (Story 4.6).
CARD_CONTRACT_SRC = REPO_ROOT / "card" / "src" / "editor.ts"
# The card's stale-bundle handshake owns its STATE_SCHEMA_VERSION mirror (4.7).
CARD_HANDSHAKE_SRC = REPO_ROOT / "card" / "src" / "handshake.ts"
ENGINE_VIEW_SRC = INTEGRATION_DIR / "engine" / "view.py"

# The integration quality scale's rules, as hassfest names them.
BRONZE_RULES: tuple[str, ...] = (
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-flow",
    "config-flow-test-coverage",
    "dependency-transparency",
    "docs-actions",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "entity-event-setup",
    "entity-unique-id",
    "has-entity-name",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "unique-config-entry",
)
SILVER_RULES: tuple[str, ...] = (
    "action-exceptions",
    "config-entry-unloading",
    "docs-configuration-parameters",
    "docs-installation-parameters",
    "entity-unavailable",
    "integration-owner",
    "log-when-unavailable",
    "parallel-updates",
    "reauthentication-flow",
    "test-coverage",
)
# The one Bronze rule still open: an external home-assistant/brands submission.
BRONZE_TODO_ALLOWED = frozenset({"brands"})
RULE_STATUSES = frozenset({"done", "exempt", "todo"})


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
    # `async_setup`, so `websocket_api` must be up first; the card's static
    # path (Story 4.2) calls `hass.http` directly, so `http` is declared in
    # its own right rather than ridden in on `websocket_api`. Lovelace is an
    # AFTER dependency: hassfest wants every imported `homeassistant.components`
    # package declared, and the resource registration waits for lovelace
    # without requiring it (YAML-mode and lovelace-less installs still work).
    assert manifest["dependencies"] == ["http", "websocket_api"]
    assert manifest["after_dependencies"] == ["lovelace"]


def test_min_ha_version_is_consistent_across_the_repo() -> None:
    """hacs.json and the README agree with const.MIN_HA_VERSION."""
    hacs = _load_json(REPO_ROOT / "hacs.json")
    assert hacs["homeassistant"] == MIN_HA_VERSION
    # Release-based distribution: the README in the store, releases only (never main).
    assert hacs["render_readme"] is True
    assert hacs["hide_default_branch"] is True

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
    card_source = CARD_CONTRACT_SRC.read_text(encoding="utf-8")
    card_version = re.search(r'CARD_VERSION\s*=\s*"([^"]+)"', card_source)
    assert card_version is not None, (
        "CARD_VERSION not found in the card contract source"
    )
    assert card_version.group(1) == manifest_version

    # The YAML-mode resource snippet cache-busts with the release version.
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    resource_versions = re.findall(
        r"ha-irrigation-timeline-card\.js\?v=([0-9A-Za-z.+-]+)", readme
    )
    assert resource_versions, (
        "no ha-irrigation-timeline-card.js?v= snippet in README.md"
    )
    assert all(version == manifest_version for version in resource_versions), (
        resource_versions
    )


def test_state_schema_version_is_shared_by_engine_and_card() -> None:
    """The card's stale-bundle check compares against the engine's schema."""
    engine = re.search(
        r"STATE_SCHEMA_VERSION: Final = (\d+)",
        ENGINE_VIEW_SRC.read_text(encoding="utf-8"),
    )
    card = re.search(
        r"STATE_SCHEMA_VERSION = (\d+)",
        CARD_HANDSHAKE_SRC.read_text(encoding="utf-8"),
    )
    assert engine is not None, "STATE_SCHEMA_VERSION not found in engine/view.py"
    assert card is not None, "STATE_SCHEMA_VERSION not found in card/src/handshake.ts"
    assert card.group(1) == engine.group(1)


def _rule_status(entry: Any) -> tuple[Any, Any]:
    """Split a rule into (status, comment): bare string or mapping form."""
    if isinstance(entry, str):
        return entry, None
    assert isinstance(entry, dict)
    return entry.get("status"), entry.get("comment")


def test_quality_scale_tracks_every_bronze_and_silver_rule() -> None:
    """Bronze is met but for `brands`; every Silver rule is stated with a status.

    Each `exempt` or `todo` must say why or what remains, so a later reader
    can tell a decision from an oversight.
    """
    loaded = parse_yaml(
        (INTEGRATION_DIR / "quality_scale.yaml").read_text(encoding="utf-8"),
    )
    assert isinstance(loaded, dict)
    rules: dict[str, Any] = loaded["rules"]

    for name in (*BRONZE_RULES, *SILVER_RULES):
        assert name in rules, f"quality_scale.yaml does not state {name}"
        status, comment = _rule_status(rules[name])
        assert status in RULE_STATUSES, f"{name}: unknown status {status!r}"
        if status in {"exempt", "todo"}:
            assert isinstance(comment, str), f"{name}: {status} without a comment"
            assert comment.strip(), f"{name}: {status} without a comment"

    open_bronze = {
        name for name in BRONZE_RULES if _rule_status(rules[name])[0] == "todo"
    }
    assert open_bronze <= BRONZE_TODO_ALLOWED, (
        f"Bronze rules still todo: {sorted(open_bronze - BRONZE_TODO_ALLOWED)}"
    )


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
