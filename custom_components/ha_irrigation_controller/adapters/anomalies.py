"""Anomaly manager — the ONE implementation of `AnomalyPort` and the AD-9 fan-out.

Every anomaly of this integration flows through here (no feature calls
Repairs or notify directly) and fans out to exactly four places:

1. a **Repairs issue**, deduplicated per open anomaly — `issue_id =
   "{kind}:{subject}"`, the subject read from the context by a per-kind
   table (pump kinds → `entity_id`, valve kinds → `zone_id`,
   `CONFIGURED_ENTITY_MISSING` → `zone_id` if present else `role`, the two
   controller-level kinds → no subject);
2. one **push notification** per NEWLY opened anomaly, through the notify
   port, as a background task (notify once);
3. the single **bus event** (`event_type: "anomaly"` on every report,
   `"anomaly_cleared"` on every clear);
4. the **health projection** the binary sensor reads, pushed on the same
   engine-state signal as everything else.

The other half of the seam is CLOSING an anomaly: `clear` is what the engine
calls when a subject confirms again (supersession), and the manager listens
to the issue registry so an operator acknowledging (fix flow → `remove`) or
ignoring (`update` with `dismissed_version`) an issue forgets the anomaly.
Health IS the set of open issues, nothing else — which is why the manager
re-seeds itself from the registry on start: a reload rebuilds it, and the
issues that survived the reload are what is still open (no second push).

`report` and `clear` are synchronous (the port is), so everything here is
event-loop-safe callback work — the push is the only await, and it rides a
task.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ..const import (  # noqa: TID252
    DOMAIN,
    EVENT_HA_IRRIGATION_CONTROLLER,
    EVENT_TYPE_ANOMALY,
    EVENT_TYPE_ANOMALY_CLEARED,
    LOGGER,
    engine_state_signal,
)
from ..engine.ports import AnomalyKind  # noqa: TID252

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant
    from homeassistant.helpers.issue_registry import EventIssueRegistryUpdatedData

    from .notify import NotifyPort

# The per-kind SUBJECT table: which context keys identify the thing the
# anomaly is about, in priority order. The first key present is the subject
# of the issue id; every key present is carried in the `anomaly_cleared`
# payload and the health projection. An empty tuple is a controller-level
# kind: one issue for the whole controller.
_SUBJECT_KEYS: Final[dict[AnomalyKind, tuple[str, ...]]] = {
    AnomalyKind.PUMP_ON_UNCONFIRMED: ("entity_id",),
    AnomalyKind.PUMP_OFF_UNCONFIRMED: ("entity_id",),
    AnomalyKind.VALVE_OPEN_UNCONFIRMED: ("zone_id",),
    AnomalyKind.VALVE_CLOSE_UNCONFIRMED: ("zone_id",),
    AnomalyKind.CONFIGURED_ENTITY_MISSING: ("zone_id", "role"),
    AnomalyKind.CYCLE_INTERRUPTED: (),
    AnomalyKind.JOURNAL_SAVE_FAILED: (),
}

# Every placeholder the issue texts may reference, always provided so a
# translation can name any of them for any kind; `-` stands for "absent".
_PLACEHOLDERS: Final = ("entity_id", "zone_id", "cycle_id", "role")
_ABSENT: Final = "-"

# Issue `data` keys — what a rebuilt manager re-seeds from.
_DATA_KIND: Final = "kind"
_DATA_SUBJECT: Final = "subject"
_DATA_CONTEXT: Final = "context"


@dataclass(frozen=True, slots=True)
class AnomalyRecord:
    """One open anomaly: its kind, its issue id and the context it was raised with.

    `frozen=True` protects the field binding, not what it points at — so the
    context is a read-only view, or a caller reading the health projection
    could mutate the record it is only supposed to observe.
    """

    kind: AnomalyKind
    issue_id: str
    context: Mapping[str, object]


def subject_of(kind: AnomalyKind, context: Mapping[str, object]) -> str | None:
    """Return the subject `kind` is about per the table, None for controller-level."""
    return next(
        (
            str(context[key])
            for key in _SUBJECT_KEYS[kind]
            if context.get(key) is not None
        ),
        None,
    )


def subject_keys(kind: AnomalyKind, context: Mapping[str, object]) -> dict[str, object]:
    """Return the subject keys of `kind` present in `context` (payload shape)."""
    return {
        key: context[key] for key in _SUBJECT_KEYS[kind] if context.get(key) is not None
    }


def issue_id_for(kind: AnomalyKind, context: Mapping[str, object]) -> str:
    """Return the deduplication key: `{kind}:{subject}`, or the kind alone."""
    subject = subject_of(kind, context)
    return kind.value if subject is None else f"{kind.value}:{subject}"


@callback
def async_delete_domain_issues(hass: HomeAssistant) -> None:
    """Delete every Repairs issue of this domain (the `async_remove_entry` hook).

    Issues are not persistent, but they outlive the entry within one HA
    process; a removed integration must leave none behind.
    """
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def _is_our_domain(event_data: EventIssueRegistryUpdatedData) -> bool:
    """Bus-level filter: only this domain's issue events reach the manager."""
    return event_data["domain"] == DOMAIN


class AnomalyManager:
    """Fan one anomaly report out to Repairs, notify, the bus and health.

    Constructed with the entry (the signal is entry-scoped and the push task
    is entry-owned), started and stopped through `entry.async_on_unload`.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        notify: NotifyPort | None = None,
    ) -> None:
        """Bind the manager to hass, its entry and the optional notify port."""
        self._hass = hass
        self._entry = entry
        self._notify = notify
        self._signal = engine_state_signal(entry.entry_id)
        # Keyed by issue id — the ONE deduplication key, shared with Repairs.
        self._open: dict[str, AnomalyRecord] = {}
        self._last: AnomalyRecord | None = None
        self._unsubscribe: CALLBACK_TYPE | None = None

    @property
    def open_anomalies(self) -> tuple[AnomalyRecord, ...]:
        """Return the open anomalies, oldest first, read-only (health projection)."""
        return tuple(self._open.values())

    @property
    def last_anomaly(self) -> AnomalyRecord | None:
        """Return the most recent report, read-only (health projection).

        Deliberately NOT cleared with the anomaly: "what went wrong last" is
        history the operator may still want after the fault healed.
        """
        return self._last

    @callback
    def async_start(self) -> None:
        """Re-seed the open set from this domain's issues and watch the registry.

        The reload regime rebuilds the manager on every config change; the
        Repairs issues are what survived, so they ARE the open set — no
        duplicate push, health survives. An issue of ours that cannot be
        read back (hand-edited storage, a kind this version no longer knows)
        is deleted rather than shown: health must mirror the open issues
        exactly, and an issue health cannot describe is not one it can keep.
        An issue the operator IGNORED while no manager was listening is
        deleted too, exactly as the live ignore handler would have done —
        re-seeding it would put a hidden issue back on the health entity.
        The last record re-seeded becomes `last_anomaly`, so the projection
        does not read "open anomalies, last unknown" after a reload.
        """
        self.async_stop()
        registry = ir.async_get(self._hass)
        for (domain, issue_id), issue in list(registry.issues.items()):
            if domain != DOMAIN:
                continue
            record = _record_from_issue(issue)
            if record is None:
                LOGGER.debug("Deleting unreadable Repairs issue %s", issue_id)
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
                continue
            if issue.dismissed_version is not None:
                LOGGER.info("Anomaly %s was ignored by the operator", issue_id)
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
                continue
            self._open[issue_id] = record
            self._last = record
        self._unsubscribe = self._hass.bus.async_listen(
            ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED,
            self._async_issue_registry_updated,
            event_filter=_is_our_domain,
        )

    @callback
    def async_stop(self) -> None:
        """Drop the registry subscription, idempotently (the unload path)."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def report(self, kind: AnomalyKind, context: dict[str, object]) -> None:
        """Report one anomaly: issue, event, record, push iff new, signal.

        Re-reporting an open issue refreshes its context (the placeholders
        follow the latest cycle) and fires the bus event again — an
        automation wants every occurrence — but never pushes again: the
        operator was told once and the issue is still on their dashboard.
        """
        LOGGER.warning("Anomaly %s: %s", kind.value, context)
        issue_id = issue_id_for(kind, context)
        is_new = issue_id not in self._open
        record = AnomalyRecord(
            kind=kind,
            issue_id=issue_id,
            context=MappingProxyType(dict(context)),
        )
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind.value,
            translation_placeholders=_placeholders(context),
            data={
                _DATA_KIND: kind.value,
                _DATA_SUBJECT: subject_of(kind, context),
                _DATA_CONTEXT: json.dumps(context, default=str),
            },
        )
        # The caller's context is spread FIRST: `event_type` is the single
        # discriminator every consumer dispatches on, so a context key of the
        # same name must lose to it, never silently replace it.
        self._hass.bus.async_fire(
            EVENT_HA_IRRIGATION_CONTROLLER,
            {**context, "event_type": EVENT_TYPE_ANOMALY, "anomaly": kind.value},
        )
        self._open[issue_id] = record
        self._last = record
        if is_new and self._notify is not None:
            # hass-owned, NOT entry-owned: the entry unload that raises
            # `cycle_interrupted` cancels the entry's background tasks right
            # after its on-unload hooks, so an entry-owned push would never
            # be delivered. HA shutdown is the only thing that cancels this.
            self._hass.async_create_background_task(
                self._async_push(self._notify, record),
                f"{DOMAIN} anomaly push {issue_id}",
            )
        async_dispatcher_send(self._hass, self._signal)

    def clear(self, kind: AnomalyKind, context: dict[str, object]) -> None:
        """Close `kind` for the subject `context` names; a no-op when not open.

        Clears never notify — nominal weeks stay silent — and the
        `anomaly_cleared` payload carries the subject keys of the RECORD, so
        an automation matching on `zone_id`/`entity_id`/`role` sees the same
        values it saw on the `anomaly` event.
        """
        issue_id = issue_id_for(kind, context)
        record = self._open.pop(issue_id, None)
        if record is None:
            return
        LOGGER.info("Anomaly %s cleared: %s", kind.value, context)
        ir.async_delete_issue(self._hass, DOMAIN, issue_id)
        self._hass.bus.async_fire(
            EVENT_HA_IRRIGATION_CONTROLLER,
            {
                **subject_keys(kind, record.context),
                "event_type": EVENT_TYPE_ANOMALY_CLEARED,
                "anomaly": kind.value,
            },
        )
        async_dispatcher_send(self._hass, self._signal)

    @callback
    def _async_issue_registry_updated(
        self,
        event: Event[EventIssueRegistryUpdatedData],
    ) -> None:
        """Follow the operator: acknowledged (removed) or ignored → forget.

        Our own creates, refreshes and deletes reach this too. A `create` is
        never news, a refresh is an `update` without `dismissed_version`, and
        our own `remove` finds the anomaly already popped — so only the
        operator's actions have any effect here.
        """
        issue_id = event.data["issue_id"]
        if event.data["action"] == "remove":
            if self._open.pop(issue_id, None) is not None:
                LOGGER.info("Anomaly %s acknowledged by the operator", issue_id)
                async_dispatcher_send(self._hass, self._signal)
        elif event.data["action"] == "update":
            issue = ir.async_get(self._hass).async_get_issue(DOMAIN, issue_id)
            if issue is None or issue.dismissed_version is None:
                return
            # Ignored: the issue would linger dismissed, and a recurrence
            # would refresh it invisibly. Delete it so the next report of the
            # same kind and subject is a fresh, visible anomaly with a fresh
            # push.
            LOGGER.info("Anomaly %s ignored by the operator", issue_id)
            self._open.pop(issue_id, None)
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            async_dispatcher_send(self._hass, self._signal)

    async def _async_push(self, notify: NotifyPort, record: AnomalyRecord) -> None:
        """Push one newly opened anomaly, best-effort.

        Whatever the port raises — `ServiceNotFound` for a target that no
        longer exists, the entity's own error — is a WARNING and nothing
        more: an anomaly about the anomaly channel would loop.
        """
        try:
            await notify.async_send(_title(record), _message(record))
        except Exception as err:  # noqa: BLE001 — see docstring: best-effort, never an anomaly
            LOGGER.warning(
                "Could not push anomaly %s to the notify target: %s",
                record.issue_id,
                err,
            )


def _placeholders(context: Mapping[str, object]) -> dict[str, str]:
    """Return every issue placeholder as a string, `-` when absent or None."""
    return {
        key: _ABSENT if context.get(key) is None else str(context[key])
        for key in _PLACEHOLDERS
    }


def _record_from_issue(issue: ir.IssueEntry) -> AnomalyRecord | None:
    """Rebuild the record an issue was created from, None when unreadable."""
    data = issue.data or {}
    try:
        kind = AnomalyKind(str(data[_DATA_KIND]))
        raw = data[_DATA_CONTEXT]
        context = json.loads(raw) if isinstance(raw, str) else None
    except KeyError, ValueError, TypeError:
        return None
    if not isinstance(context, dict):
        return None
    return AnomalyRecord(
        kind=kind,
        issue_id=issue.issue_id,
        context=MappingProxyType(context),
    )


def _title(record: AnomalyRecord) -> str:
    """Return the push title: the kind, humanized."""
    return f"Irrigation: {record.kind.value.replace('_', ' ')}"


def _message(record: AnomalyRecord) -> str:
    """Return the push body: every context key that has a value."""
    details = ", ".join(
        f"{key} {value}" for key, value in record.context.items() if value is not None
    )
    return details or "no details"
