"""Anomaly manager: the single AD-9 fan-out (Story 3.1).

One report → one deduplicated Repairs issue, one push (once), the single bus
event, the health projection, the engine-state signal. One clear or one
operator dismissal → the issue goes, health follows, nothing is pushed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.setup import async_setup_component

from custom_components.ha_irrigation_controller.adapters.anomalies import (
    AnomalyManager,
    async_delete_domain_issues,
    issue_id_for,
    subject_keys,
)
from custom_components.ha_irrigation_controller.adapters.notify import (
    HaNotifyAdapter,
    parse_notify_target,
)
from custom_components.ha_irrigation_controller.const import (
    CONF_NOTIFY_TARGET,
    DOMAIN,
    EVENT_HA_IRRIGATION_CONTROLLER,
    engine_state_signal,
)
from custom_components.ha_irrigation_controller.engine.ports import AnomalyKind
from tests.common import controller_entry, register_notify_domain

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import Event, HomeAssistant, ServiceCall
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

VALVE_CONTEXT: dict[str, object] = {
    "cycle_id": "2026-07-31-morning",
    "zone_id": "zone-1",
    "entity_id": "switch.zone_1_valve",
}
PUMP_CONTEXT: dict[str, object] = {
    "cycle_id": "2026-07-31-morning",
    "entity_id": "switch.pool_pump",
}
VALVE_ISSUE = "valve_open_unconfirmed:zone-1"


class RecordingNotify:
    """A `NotifyPort` that records, or raises when told to."""

    def __init__(self) -> None:
        """Start with nothing sent and nothing failing."""
        self.sent: list[tuple[str, str]] = []
        self.raising = False

    async def async_send(self, title: str, message: str) -> None:
        """Record the push, or fail like a target that is down."""
        if self.raising:
            msg = "the companion app is unreachable"
            raise HomeAssistantError(msg)
        self.sent.append((title, message))


def record_events(hass: HomeAssistant) -> list[Event]:
    """Collect every event of the ONE bus type for the rest of the test.

    The listener is a `@callback`: a bare `list.append` is a builtin, which
    HA runs in the executor, so two events fired back to back could be
    recorded out of order. Order matters to the tests below.
    """
    events: list[Event] = []

    @callback
    def _record(event: Event) -> None:
        events.append(event)

    hass.bus.async_listen(EVENT_HA_IRRIGATION_CONTROLLER, _record)
    return events


def count_signals(hass: HomeAssistant, entry: MockConfigEntry) -> list[int]:
    """Count engine-state pushes for the entry; the list's one item is the count."""
    pushes = [0]

    def _on_signal() -> None:
        pushes[0] += 1

    async_dispatcher_connect(hass, engine_state_signal(entry.entry_id), _on_signal)
    return pushes


def issue_of(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    """Return this domain's issue `issue_id`, or None."""
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def domain_issue_ids(hass: HomeAssistant) -> set[str]:
    """Return the ids of every issue of this domain."""
    return {
        issue_id for domain, issue_id in ir.async_get(hass).issues if domain == DOMAIN
    }


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a controller entry known to hass, never set up (the manager is bare)."""
    config_entry = controller_entry()
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
def notify() -> RecordingNotify:
    """Return a recording notify port."""
    return RecordingNotify()


@pytest.fixture
def manager(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    notify: RecordingNotify,
) -> AnomalyManager:
    """Return a started manager wired to the recording notify port."""
    started = AnomalyManager(hass, entry, notify=notify)
    started.async_start()
    return started


# --------------------------------------------------------------------------
# Report: issue, push once, event, health, signal
# --------------------------------------------------------------------------


async def test_first_report_opens_an_issue_pushes_once_and_fires_the_event(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: AnomalyManager,
    notify: RecordingNotify,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matrix "First report": every leg of the fan-out, exactly once."""
    events = record_events(hass)
    pushes = count_signals(hass, entry)

    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    issue = issue_of(hass, VALVE_ISSUE)
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.is_persistent is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "valve_open_unconfirmed"
    assert issue.translation_placeholders == {
        "entity_id": "switch.zone_1_valve",
        "zone_id": "zone-1",
        "cycle_id": "2026-07-31-morning",
        "kind": "-",
        "role": "-",
        "outcome": "-",
    }
    assert issue.data is not None
    assert issue.data["kind"] == "valve_open_unconfirmed"
    assert issue.data["subject"] == "zone-1"
    assert len(notify.sent) == 1
    title, message = notify.sent[0]
    assert "valve open unconfirmed" in title
    assert "zone-1" in message
    assert "switch.zone_1_valve" in message
    assert [event.data for event in events] == [
        {**VALVE_CONTEXT, "event_type": "anomaly", "anomaly": "valve_open_unconfirmed"},
    ]
    assert [record.issue_id for record in manager.open_anomalies] == [VALVE_ISSUE]
    last = manager.last_anomaly
    assert last is not None
    assert last.kind is AnomalyKind.VALVE_OPEN_UNCONFIRMED
    assert last.context == VALVE_CONTEXT
    assert pushes == [1]
    assert "valve_open_unconfirmed" in caplog.text
    assert "2026-07-31-morning" in caplog.text


async def test_a_repeat_report_refreshes_the_issue_and_never_pushes_again(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: AnomalyManager,
    notify: RecordingNotify,
) -> None:
    """Matrix "Repeat report": same issue, new placeholders, event again, NO push."""
    events = record_events(hass)
    pushes = count_signals(hass, entry)
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    manager.report(
        AnomalyKind.VALVE_OPEN_UNCONFIRMED,
        {**VALVE_CONTEXT, "cycle_id": "2026-07-31-evening"},
    )
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == {VALVE_ISSUE}
    issue = issue_of(hass, VALVE_ISSUE)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["cycle_id"] == "2026-07-31-evening"
    assert len(notify.sent) == 1
    assert [event.data["cycle_id"] for event in events] == [
        "2026-07-31-morning",
        "2026-07-31-evening",
    ]
    assert len(manager.open_anomalies) == 1
    assert manager.open_anomalies[0].context["cycle_id"] == "2026-07-31-evening"
    assert pushes == [2]


@pytest.mark.parametrize(
    ("kind", "context", "expected"),
    [
        (
            AnomalyKind.PUMP_ON_UNCONFIRMED,
            PUMP_CONTEXT,
            "pump_on_unconfirmed:switch.pool_pump",
        ),
        (
            AnomalyKind.PUMP_OFF_UNCONFIRMED,
            PUMP_CONTEXT,
            "pump_off_unconfirmed:switch.pool_pump",
        ),
        (AnomalyKind.VALVE_OPEN_UNCONFIRMED, VALVE_CONTEXT, VALVE_ISSUE),
        (
            AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
            VALVE_CONTEXT,
            "valve_close_unconfirmed:zone-1",
        ),
        (
            # Story 3.5's safety close of a valve whose zone was deleted while
            # it was held open by hand: no zone, so the entity id is the
            # subject — never the bare kind, which would be one issue shared
            # by every zone-less valve.
            AnomalyKind.VALVE_CLOSE_UNCONFIRMED,
            {"zone_id": None, "entity_id": "switch.zone_2_valve"},
            "valve_close_unconfirmed:switch.zone_2_valve",
        ),
        (
            AnomalyKind.CONFIGURED_ENTITY_MISSING,
            {
                "entity_id": "switch.zone_1_valve",
                "role": "valve_switch",
                "zone_id": "zone-1",
            },
            "configured_entity_missing:zone-1",
        ),
        (
            AnomalyKind.CONFIGURED_ENTITY_MISSING,
            {"entity_id": "switch.pool_pump", "role": "pump_switch"},
            "configured_entity_missing:pump_switch",
        ),
        (
            AnomalyKind.CYCLE_INTERRUPTED,
            {"cycle_id": "2026-07-31-morning", "kind": "morning", "zone_id": None},
            "cycle_interrupted",
        ),
        (
            AnomalyKind.CYCLE_RECOVERED,
            {
                "cycle_id": "2026-07-31-morning",
                "kind": "morning",
                "zone_id": "zone-1",
                "outcome": "resumed",
            },
            "cycle_recovered",
        ),
        (
            AnomalyKind.MISSED_CYCLE,
            {
                "cycle_id": "2026-07-31-morning",
                "kind": "morning",
                "outcome": "rerun",
            },
            "missed_cycle",
        ),
        (
            AnomalyKind.MANUAL_VALVE_TIMEOUT,
            {"zone_id": "zone-1", "entity_id": "switch.zone_1_valve"},
            "manual_valve_timeout:zone-1",
        ),
        (
            AnomalyKind.MANUAL_VALVE_TIMEOUT,
            {"zone_id": None, "entity_id": "switch.pool_pump"},
            "manual_valve_timeout:switch.pool_pump",
        ),
        (
            AnomalyKind.JOURNAL_SAVE_FAILED,
            {"error": "OSError()"},
            "journal_save_failed",
        ),
    ],
)
async def test_the_issue_id_names_the_kind_and_its_subject(
    hass: HomeAssistant,
    manager: AnomalyManager,
    kind: AnomalyKind,
    context: Mapping[str, object],
    expected: str,
) -> None:
    """Pin the subject table: pump → entity, valve → zone, missing → zone or role."""
    assert issue_id_for(kind, context) == expected

    manager.report(kind, dict(context))
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == {expected}
    issue = issue_of(hass, expected)
    assert issue is not None
    assert issue.translation_key == kind.value


async def test_absent_placeholders_read_as_a_dash(
    hass: HomeAssistant,
    manager: AnomalyManager,
) -> None:
    """Every placeholder is always provided; None and absent both read `-`."""
    manager.report(
        AnomalyKind.CYCLE_INTERRUPTED,
        {"cycle_id": "2026-07-31-morning", "kind": "morning", "zone_id": None},
    )
    await hass.async_block_till_done()

    issue = issue_of(hass, "cycle_interrupted")
    assert issue is not None
    assert issue.translation_placeholders == {
        "entity_id": "-",
        "zone_id": "-",
        "cycle_id": "2026-07-31-morning",
        "kind": "morning",
        "role": "-",
        "outcome": "-",
    }


async def test_cycle_recovered_carries_its_outcome_and_is_never_auto_cleared(
    hass: HomeAssistant,
    manager: AnomalyManager,
    notify: RecordingNotify,
) -> None:
    """Story 3.2 AC 4: one issue, one push, an `outcome` placeholder — acknowledge only.

    The engine never calls `clear` for this kind, so the only way the issue
    goes is the operator's; a second recovery on a later restart refreshes
    the same controller-level issue with the new outcome and pushes nothing.
    """
    manager.report(
        AnomalyKind.CYCLE_RECOVERED,
        {
            "cycle_id": "2026-07-31-morning",
            "kind": "morning",
            "zone_id": "zone-1",
            "outcome": "resumed",
        },
    )
    await hass.async_block_till_done()

    issue = issue_of(hass, "cycle_recovered")
    assert issue is not None
    assert issue.translation_placeholders == {
        "entity_id": "-",
        "zone_id": "zone-1",
        "cycle_id": "2026-07-31-morning",
        "kind": "morning",
        "role": "-",
        "outcome": "resumed",
    }
    assert len(notify.sent) == 1
    assert "cycle recovered" in notify.sent[0][0]
    assert "outcome resumed" in notify.sent[0][1]

    manager.report(
        AnomalyKind.CYCLE_RECOVERED,
        {
            "cycle_id": "2026-08-01-morning",
            "kind": "morning",
            "zone_id": None,
            "outcome": "closed",
        },
    )
    await hass.async_block_till_done()
    issue = issue_of(hass, "cycle_recovered")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["outcome"] == "closed"
    assert len(notify.sent) == 1

    ir.async_delete_issue(hass, DOMAIN, "cycle_recovered")
    await hass.async_block_till_done()
    assert manager.open_anomalies == ()


async def test_missed_cycle_carries_its_outcome_and_is_never_auto_cleared(
    hass: HomeAssistant,
    manager: AnomalyManager,
    notify: RecordingNotify,
) -> None:
    """Story 3.3 AC 5: one issue, one push, `kind` + `outcome` — acknowledge only.

    The engine never calls `clear` for this kind, so the only way the issue
    goes is the operator's. A second missed cycle — the day's other one,
    recorded rather than re-run — refreshes the same controller-level issue
    with the new context and pushes nothing new.
    """
    manager.report(
        AnomalyKind.MISSED_CYCLE,
        {
            "cycle_id": "2026-07-31-morning",
            "kind": "morning",
            "outcome": "rerun",
        },
    )
    await hass.async_block_till_done()

    issue = issue_of(hass, "missed_cycle")
    assert issue is not None
    assert issue.translation_placeholders == {
        "entity_id": "-",
        "zone_id": "-",
        "cycle_id": "2026-07-31-morning",
        "kind": "morning",
        "role": "-",
        "outcome": "rerun",
    }
    assert len(notify.sent) == 1
    assert "missed cycle" in notify.sent[0][0]
    assert "outcome rerun" in notify.sent[0][1]

    manager.report(
        AnomalyKind.MISSED_CYCLE,
        {
            "cycle_id": "2026-07-31-evening",
            "kind": "evening",
            "outcome": "recorded",
        },
    )
    await hass.async_block_till_done()
    issue = issue_of(hass, "missed_cycle")
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["outcome"] == "recorded"
    assert len(notify.sent) == 1

    ir.async_delete_issue(hass, DOMAIN, "missed_cycle")
    await hass.async_block_till_done()
    assert manager.open_anomalies == ()


async def test_a_manual_valve_timeout_is_per_switch_and_never_auto_cleared(
    hass: HomeAssistant,
    manager: AnomalyManager,
    notify: RecordingNotify,
) -> None:
    """Story 3.5: one issue per SWITCH, one push, acknowledge only.

    Two valves left open by hand are two things to go and close, so the kind
    is subject-keyed rather than controller-level: a valve by its zone, the
    pump by its entity id (it has none). The engine never calls `clear` for
    this kind — a safety close is news, not a fault that heals — so the only
    way an issue goes is the operator's.
    """
    manager.report(
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
        {"zone_id": "zone-1", "entity_id": "switch.zone_1_valve"},
    )
    manager.report(
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
        {"zone_id": None, "entity_id": "switch.pool_pump"},
    )
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == {
        "manual_valve_timeout:zone-1",
        "manual_valve_timeout:switch.pool_pump",
    }
    issue = issue_of(hass, "manual_valve_timeout:zone-1")
    assert issue is not None
    assert issue.translation_placeholders == {
        "entity_id": "switch.zone_1_valve",
        "zone_id": "zone-1",
        "cycle_id": "-",
        "kind": "-",
        "role": "-",
        "outcome": "-",
    }
    assert len(notify.sent) == 2
    assert "manual valve timeout" in notify.sent[0][0]
    assert "switch.zone_1_valve" in notify.sent[0][1]

    # Reported again for the same switch: the same issue, no second push.
    manager.report(
        AnomalyKind.MANUAL_VALVE_TIMEOUT,
        {"zone_id": "zone-1", "entity_id": "switch.zone_1_valve"},
    )
    await hass.async_block_till_done()
    assert len(notify.sent) == 2

    ir.async_delete_issue(hass, DOMAIN, "manual_valve_timeout:zone-1")
    ir.async_delete_issue(hass, DOMAIN, "manual_valve_timeout:switch.pool_pump")
    await hass.async_block_till_done()
    assert manager.open_anomalies == ()


async def test_three_anomalies_make_three_issues_three_pushes_three_health_entries(
    hass: HomeAssistant,
    manager: AnomalyManager,
    notify: RecordingNotify,
) -> None:
    """AC 2: different kinds/subjects never collapse into one another."""
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    manager.report(
        AnomalyKind.VALVE_OPEN_UNCONFIRMED,
        {**VALVE_CONTEXT, "zone_id": "zone-2", "entity_id": "switch.zone_2_valve"},
    )
    manager.report(AnomalyKind.PUMP_ON_UNCONFIRMED, dict(PUMP_CONTEXT))
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == {
        VALVE_ISSUE,
        "valve_open_unconfirmed:zone-2",
        "pump_on_unconfirmed:switch.pool_pump",
    }
    assert len(notify.sent) == 3
    assert [
        {"anomaly": record.kind.value, **subject_keys(record.kind, record.context)}
        for record in manager.open_anomalies
    ] == [
        {"anomaly": "valve_open_unconfirmed", "zone_id": "zone-1"},
        {"anomaly": "valve_open_unconfirmed", "zone_id": "zone-2"},
        {"anomaly": "pump_on_unconfirmed", "entity_id": "switch.pool_pump"},
    ]


# --------------------------------------------------------------------------
# Clear: supersession from the engine
# --------------------------------------------------------------------------


async def test_clear_deletes_the_issue_fires_anomaly_cleared_and_never_pushes(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: AnomalyManager,
    notify: RecordingNotify,
) -> None:
    """Matrix "Healthy again": issue gone, `anomaly_cleared` with the subject keys."""
    events = record_events(hass)
    pushes = count_signals(hass, entry)
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    # The engine clears with the NEXT cycle's context: only the subject matters.
    manager.clear(
        AnomalyKind.VALVE_OPEN_UNCONFIRMED,
        {**VALVE_CONTEXT, "cycle_id": "2026-07-31-evening"},
    )
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == set()
    assert manager.open_anomalies == ()
    # `last_anomaly` is history, kept after the clear.
    assert manager.last_anomaly is not None
    assert [event.data for event in events] == [
        {**VALVE_CONTEXT, "event_type": "anomaly", "anomaly": "valve_open_unconfirmed"},
        {
            "event_type": "anomaly_cleared",
            "anomaly": "valve_open_unconfirmed",
            "zone_id": "zone-1",
        },
    ]
    assert len(notify.sent) == 1
    assert pushes == [2]


async def test_clearing_what_is_not_open_is_a_no_op(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: AnomalyManager,
) -> None:
    """No issue, no event, no signal — the engine clears on EVERY confirmation."""
    events = record_events(hass)
    pushes = count_signals(hass, entry)

    manager.clear(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    manager.clear(AnomalyKind.JOURNAL_SAVE_FAILED, {})
    await hass.async_block_till_done()

    assert events == []
    assert pushes == [0]
    assert domain_issue_ids(hass) == set()


async def test_a_clear_of_another_subject_or_kind_leaves_the_issue_open(
    hass: HomeAssistant,
    manager: AnomalyManager,
) -> None:
    """Zone 2 confirming says nothing about zone 1; a close confirming does."""
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    manager.clear(
        AnomalyKind.VALVE_OPEN_UNCONFIRMED,
        {**VALVE_CONTEXT, "zone_id": "zone-2"},
    )
    manager.clear(AnomalyKind.VALVE_CLOSE_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()
    assert domain_issue_ids(hass) == {VALVE_ISSUE}

    manager.clear(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()
    assert domain_issue_ids(hass) == set()


async def test_the_cleared_payload_carries_every_subject_key_of_the_record(
    hass: HomeAssistant,
    manager: AnomalyManager,
) -> None:
    """A missing valve is about its zone AND its role; both travel on the clear."""
    events = record_events(hass)
    context: dict[str, object] = {
        "entity_id": "switch.zone_1_valve",
        "role": "valve_switch",
        "zone_id": "zone-1",
    }
    manager.report(AnomalyKind.CONFIGURED_ENTITY_MISSING, context)
    manager.clear(AnomalyKind.CONFIGURED_ENTITY_MISSING, context)
    await hass.async_block_till_done()

    assert events[1].data == {
        "event_type": "anomaly_cleared",
        "anomaly": "configured_entity_missing",
        "zone_id": "zone-1",
        "role": "valve_switch",
    }


# --------------------------------------------------------------------------
# Dismissal: the operator, through Repairs
# --------------------------------------------------------------------------


async def test_acknowledging_the_issue_forgets_the_anomaly(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: AnomalyManager,
) -> None:
    """Matrix "Operator acknowledges": the fix flow deletes → health empties, signal.

    `ConfirmRepairFlow` deletes the issue on confirmation; what the manager
    sees is the registry's `remove`. No bus event: nothing was cleared by
    the engine, the operator chose to stop looking.
    """
    events = record_events(hass)
    pushes = count_signals(hass, entry)
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    ir.async_delete_issue(hass, DOMAIN, VALVE_ISSUE)
    await hass.async_block_till_done()

    assert manager.open_anomalies == ()
    assert pushes == [2]
    assert [event.data["event_type"] for event in events] == ["anomaly"]


async def test_ignoring_the_issue_deletes_it_and_forgets_the_anomaly(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: AnomalyManager,
) -> None:
    """Matrix "Operator ignores": `dismissed_version` set → issue deleted, forgotten."""
    pushes = count_signals(hass, entry)
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    ir.async_ignore_issue(hass, DOMAIN, VALVE_ISSUE, ignore=True)
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == set()
    assert manager.open_anomalies == ()
    assert pushes == [2]


async def test_a_recurrence_after_dismissal_is_a_fresh_anomaly_with_a_fresh_push(
    hass: HomeAssistant,
    manager: AnomalyManager,
    notify: RecordingNotify,
) -> None:
    """Matrix "Recurrence after dismissal": new issue, second push."""
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()
    ir.async_ignore_issue(hass, DOMAIN, VALVE_ISSUE, ignore=True)
    await hass.async_block_till_done()
    assert len(notify.sent) == 1

    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    issue = issue_of(hass, VALVE_ISSUE)
    assert issue is not None
    assert issue.dismissed_version is None
    assert len(notify.sent) == 2
    assert len(manager.open_anomalies) == 1


async def test_a_refresh_of_an_open_issue_is_not_a_dismissal(
    hass: HomeAssistant,
    manager: AnomalyManager,
) -> None:
    """Our own `update` (new placeholders) must not be read as the operator's."""
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    manager.report(
        AnomalyKind.VALVE_OPEN_UNCONFIRMED,
        {**VALVE_CONTEXT, "cycle_id": "2026-07-31-evening"},
    )
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == {VALVE_ISSUE}
    assert len(manager.open_anomalies) == 1


async def test_another_domains_issues_are_ignored(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: AnomalyManager,
) -> None:
    """The registry listener is filtered on our domain."""
    pushes = count_signals(hass, entry)
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    ir.async_create_issue(
        hass,
        "other_domain",
        VALVE_ISSUE,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="whatever",
    )
    ir.async_delete_issue(hass, "other_domain", VALVE_ISSUE)
    await hass.async_block_till_done()

    assert len(manager.open_anomalies) == 1
    assert pushes == [1]


async def test_stop_drops_the_registry_listener(
    hass: HomeAssistant,
    manager: AnomalyManager,
) -> None:
    """After `async_stop` the operator's actions reach a manager that is gone."""
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    manager.async_stop()
    manager.async_stop()  # idempotent, like every unload hook
    ir.async_delete_issue(hass, DOMAIN, VALVE_ISSUE)
    await hass.async_block_till_done()

    assert len(manager.open_anomalies) == 1


# --------------------------------------------------------------------------
# Push: best-effort, notify once
# --------------------------------------------------------------------------


async def test_without_a_notify_target_everything_but_the_push_happens(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """Matrix "No/invalid target": issue + event + health only."""
    events = record_events(hass)
    manager = AnomalyManager(hass, entry)
    manager.async_start()

    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == {VALVE_ISSUE}
    assert len(events) == 1
    assert len(manager.open_anomalies) == 1


async def test_a_failing_notify_target_logs_a_warning_and_raises_no_anomaly(
    hass: HomeAssistant,
    manager: AnomalyManager,
    notify: RecordingNotify,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Matrix "Notify fails": issue + event + health kept, one WARNING, no loop."""
    events = record_events(hass)
    notify.raising = True

    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == {VALVE_ISSUE}
    assert len(events) == 1
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "Could not push" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "companion app is unreachable" in warnings[0].getMessage()


async def test_the_real_adapter_calls_notify_send_message_on_the_target(
    hass: HomeAssistant,
) -> None:
    """`notify.send_message` with `entity_id`, `title`, `message` — the decision."""
    calls = register_notify_domain(hass)

    await HaNotifyAdapter(hass, "notify.mobile_app_phone").async_send(
        "Irrigation: valve open unconfirmed",
        "zone-1 did not confirm",
    )

    assert [(call.domain, call.service, dict(call.data)) for call in calls] == [
        (
            "notify",
            "send_message",
            {
                ATTR_ENTITY_ID: "notify.mobile_app_phone",
                "title": "Irrigation: valve open unconfirmed",
                "message": "zone-1 did not confirm",
            },
        ),
    ]


async def test_a_missing_notify_service_is_a_warning_not_an_anomaly(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No notify entity exists at all: `ServiceNotFound` ends in the log."""
    manager = AnomalyManager(
        hass,
        entry,
        notify=HaNotifyAdapter(hass, "notify.mobile_app_phone"),
    )
    manager.async_start()

    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == {VALVE_ISSUE}
    assert any(
        record.levelno == logging.WARNING and "Could not push" in record.getMessage()
        for record in caplog.records
    )


async def test_a_raising_notify_handler_is_one_warning_carrying_its_message(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The REAL adapter's failure path: `blocking=True` surfaces the handler's error.

    A push fired without blocking would only log a traceback in a detached
    task; the manager must see the error itself to turn it into the one
    WARNING (and the issue, event and health stay as they are).
    """

    async def _raise(call: ServiceCall) -> None:
        msg = f"{call.data[ATTR_ENTITY_ID]} rejected the message"
        raise HomeAssistantError(msg)

    hass.services.async_register("notify", "send_message", _raise)
    manager = AnomalyManager(
        hass,
        entry,
        notify=HaNotifyAdapter(hass, "notify.mobile_app_phone"),
    )
    manager.async_start()

    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done(wait_background_tasks=True)

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "Could not push" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "notify.mobile_app_phone rejected the message" in warnings[0]
    assert domain_issue_ids(hass) == {VALVE_ISSUE}
    assert len(manager.open_anomalies) == 1


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({CONF_NOTIFY_TARGET: "notify.mobile_app_phone"}, "notify.mobile_app_phone"),
        ({}, None),
        ({CONF_NOTIFY_TARGET: 42}, None),
        ({CONF_NOTIFY_TARGET: "not an entity id"}, None),
        ({CONF_NOTIFY_TARGET: "sensor.rain_gauge"}, None),
    ],
)
def test_parse_notify_target_accepts_only_a_notify_entity_id(
    options: dict[str, Any],
    expected: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Absent or garbage → None with one WARNING; a notify entity id → itself."""
    assert parse_notify_target(options) == expected

    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == (0 if expected is not None else 1)


# --------------------------------------------------------------------------
# The real fix flow (repairs.py) through the Repairs REST API
# --------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::aiohttp.web_exceptions.NotAppKeyWarning")
async def test_the_acknowledge_flow_deletes_the_issue_and_empties_health(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
) -> None:
    """`repairs.py`'s `ConfirmRepairFlow` end to end: form, confirm, gone.

    The `NotAppKeyWarning` filter is scoped to this test: HA's own `http`
    component trips it while setting up (`self.app["hass"] = ...`), and the
    repairs REST views need `http`.
    """
    entry = controller_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await async_setup_component(hass, "repairs", {})
    await hass.async_block_till_done()
    manager = entry.runtime_data.anomalies
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()
    assert domain_issue_ids(hass) == {VALVE_ISSUE}

    client = await hass_client()
    response = await client.post(
        "/api/repairs/issues/fix",
        json={"handler": DOMAIN, "issue_id": VALVE_ISSUE},
    )
    assert response.status == 200
    form = await response.json()
    assert form["type"] == "form"
    assert form["step_id"] == "confirm"
    assert form["description_placeholders"]["zone_id"] == "zone-1"

    response = await client.post(f"/api/repairs/issues/fix/{form['flow_id']}", json={})
    assert response.status == 200
    result = await response.json()
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert domain_issue_ids(hass) == set()
    assert manager.open_anomalies == ()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# --------------------------------------------------------------------------
# Lifecycle: re-seed on start, remove-all
# --------------------------------------------------------------------------


async def test_start_re_seeds_open_anomalies_from_existing_issues_without_pushing(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: AnomalyManager,
) -> None:
    """Matrix "Reload with open issue": the rebuilt manager knows, and clears."""
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    manager.report(
        AnomalyKind.CYCLE_INTERRUPTED,
        {"cycle_id": "2026-07-31-morning", "kind": "morning", "zone_id": None},
    )
    await hass.async_block_till_done()
    manager.async_stop()

    rebuilt_notify = RecordingNotify()
    rebuilt = AnomalyManager(hass, entry, notify=rebuilt_notify)
    rebuilt.async_start()
    await hass.async_block_till_done()

    assert [
        (record.issue_id, record.kind, dict(record.context))
        for record in rebuilt.open_anomalies
    ] == [
        (VALVE_ISSUE, AnomalyKind.VALVE_OPEN_UNCONFIRMED, VALVE_CONTEXT),
        (
            "cycle_interrupted",
            AnomalyKind.CYCLE_INTERRUPTED,
            {"cycle_id": "2026-07-31-morning", "kind": "morning", "zone_id": None},
        ),
    ]
    assert rebuilt_notify.sent == []
    # The projection must not read "open anomalies, last unknown".
    assert rebuilt.last_anomaly is not None
    assert rebuilt.last_anomaly.issue_id == "cycle_interrupted"
    # A re-reported anomaly is NOT new to the rebuilt manager either.
    rebuilt.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()
    assert rebuilt_notify.sent == []

    # ...and the engine can clear what the previous manager opened.
    events = record_events(hass)
    rebuilt.clear(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    await hass.async_block_till_done()
    assert domain_issue_ids(hass) == {"cycle_interrupted"}
    assert events[0].data["event_type"] == "anomaly_cleared"
    assert events[0].data["zone_id"] == "zone-1"


async def test_start_deletes_issues_of_ours_it_cannot_read_back(
    hass: HomeAssistant,
    entry: MockConfigEntry,
) -> None:
    """Health must mirror the open issues exactly: an unreadable one is removed."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        "hand_made",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="valve_open_unconfirmed",
        data={"kind": "not_a_kind", "context": "{}"},
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        "no_data",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="valve_open_unconfirmed",
    )
    # A valid kind whose context is JSON but not an object...
    ir.async_create_issue(
        hass,
        DOMAIN,
        "context_is_a_list",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="valve_open_unconfirmed",
        data={"kind": "valve_open_unconfirmed", "context": "[]"},
    )
    # ...and one with a valid kind but no context key at all.
    ir.async_create_issue(
        hass,
        DOMAIN,
        "context_missing",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="valve_open_unconfirmed",
        data={"kind": "valve_open_unconfirmed"},
    )

    manager = AnomalyManager(hass, entry)
    manager.async_start()
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == set()
    assert manager.open_anomalies == ()


async def test_start_deletes_an_issue_the_operator_ignored_while_nothing_listened(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    manager: AnomalyManager,
) -> None:
    """Ignored while the entry was unloaded: not re-seeded, deleted like a live ignore.

    Re-seeding it would put a Repairs-hidden issue back on the health entity,
    and a later re-report would refresh it invisibly.
    """
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    manager.report(AnomalyKind.PUMP_ON_UNCONFIRMED, dict(PUMP_CONTEXT))
    await hass.async_block_till_done()
    manager.async_stop()
    ir.async_ignore_issue(hass, DOMAIN, VALVE_ISSUE, ignore=True)
    await hass.async_block_till_done()
    assert domain_issue_ids(hass) == {
        VALVE_ISSUE,
        "pump_on_unconfirmed:switch.pool_pump",
    }

    rebuilt = AnomalyManager(hass, entry)
    rebuilt.async_start()
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == {"pump_on_unconfirmed:switch.pool_pump"}
    assert [record.issue_id for record in rebuilt.open_anomalies] == [
        "pump_on_unconfirmed:switch.pool_pump",
    ]


async def test_delete_domain_issues_removes_ours_and_no_other(
    hass: HomeAssistant,
    manager: AnomalyManager,
) -> None:
    """`async_remove_entry`'s hook: every issue of the domain, nothing else."""
    manager.report(AnomalyKind.VALVE_OPEN_UNCONFIRMED, dict(VALVE_CONTEXT))
    manager.report(AnomalyKind.PUMP_ON_UNCONFIRMED, dict(PUMP_CONTEXT))
    ir.async_create_issue(
        hass,
        "other_domain",
        "theirs",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="whatever",
    )
    await hass.async_block_till_done()

    async_delete_domain_issues(hass)
    await hass.async_block_till_done()

    assert domain_issue_ids(hass) == set()
    assert ir.async_get(hass).async_get_issue("other_domain", "theirs") is not None


# --------------------------------------------------------------------------
# Read-only projections (kept from the 1.5 seed)
# --------------------------------------------------------------------------


async def test_recorded_state_is_isolated_from_callers(
    hass: HomeAssistant,
    manager: AnomalyManager,
) -> None:
    """Mutating the context a caller handed in never changes the record kept."""
    context: dict[str, object] = {"cycle_id": "c-1"}
    manager.report(AnomalyKind.JOURNAL_SAVE_FAILED, context)
    context["cycle_id"] = "mutated"
    await hass.async_block_till_done()

    last = manager.last_anomaly
    assert last is not None
    assert last.context == {"cycle_id": "c-1"}


async def test_a_reader_cannot_mutate_the_recorded_context(
    hass: HomeAssistant,
    manager: AnomalyManager,
) -> None:
    """`frozen=True` protects the binding, not the dict — the view does."""
    manager.report(AnomalyKind.JOURNAL_SAVE_FAILED, {"cycle_id": "c-1"})
    await hass.async_block_till_done()

    last = manager.last_anomaly
    assert last is not None
    with pytest.raises(TypeError):
        last.context["cycle_id"] = "mutated"  # type: ignore[index]
    assert last.context == {"cycle_id": "c-1"}


async def test_a_context_key_cannot_hijack_the_event_discriminator(
    hass: HomeAssistant,
    manager: AnomalyManager,
) -> None:
    """`event_type` and `anomaly` are the manager's; a context key loses to them."""
    events = record_events(hass)

    manager.report(
        AnomalyKind.PUMP_ON_UNCONFIRMED,
        {"event_type": "hijacked", "anomaly": "hijacked", "cycle_id": "c-1"},
    )
    await hass.async_block_till_done()

    assert events[0].data == {
        "event_type": "anomaly",
        "anomaly": "pump_on_unconfirmed",
        "cycle_id": "c-1",
    }
