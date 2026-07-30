"""The shared entity base class pins the conventions later platforms inherit."""

from __future__ import annotations

from custom_components.ha_irrigation_controller.const import DOMAIN
from custom_components.ha_irrigation_controller.entities.entity import (
    HaIrrigationControllerEntity,
)
from tests.common import controller_entry


def test_entity_follows_naming_conventions() -> None:
    """Base entities get has_entity_name, a stable unique_id and a translation key."""
    entry = controller_entry()
    entity = HaIrrigationControllerEntity(entry, "season")

    assert entity.has_entity_name is True
    assert entity.unique_id == f"{entry.entry_id}_season"
    assert entity.translation_key == "season"
    # No hardcoded name anywhere: the displayed name comes from the translation
    # key, composed by the frontend with the device name.
    assert "_attr_name" not in vars(entity)


def test_entity_is_attached_to_the_controller_device() -> None:
    """Base entities link to the controller device created at setup."""
    entry = controller_entry()
    entity = HaIrrigationControllerEntity(entry, "season")

    assert entity.device_info is not None
    assert entity.device_info["identifiers"] == {(DOMAIN, entry.entry_id)}


def test_unique_ids_are_stable_and_distinct_per_key() -> None:
    """Two keys on the same entry never collide, and neither depends on the name."""
    entry = controller_entry()

    first = HaIrrigationControllerEntity(entry, "season")
    second = HaIrrigationControllerEntity(entry, "next_cycle")

    assert first.unique_id != second.unique_id
    assert HaIrrigationControllerEntity(entry, "season").unique_id == first.unique_id
