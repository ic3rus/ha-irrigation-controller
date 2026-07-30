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

from custom_components.ha_irrigation_controller.const import DOMAIN, MIN_HA_VERSION

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


def test_engine_has_no_declared_dependency_on_home_assistant() -> None:
    """manifest.json declares no requirements — the guard imports only HA core."""
    manifest = _load_json(INTEGRATION_DIR / "manifest.json")
    assert manifest.get("requirements", []) == [], (
        "New requirements must be justified: the setup guard deliberately uses "
        "homeassistant.const so no third-party version library is needed."
    )
