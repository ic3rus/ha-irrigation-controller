"""The WebSocket READ channel: `state_get` and `state_subscribe` (Story 4.1).

AD-10's split: commands stay HA services; reads and live state are custom
WebSocket commands, and both live here. Neither is admin-gated — the card is
a non-admin household member's only health surface — and neither filters the
document per user: the view is one document for every reader.

Four rules hold this module together:

1. Registered in `async_setup`, ONCE, beside the services; `manifest.json`
   declares `websocket_api` so the component is up first.
2. The entry is resolved PER CALL, never captured at registration: an
   omitted `entry_id` means the single entry (`single_config_entry`);
   unknown is `not_found`, no engine to read (`_readable` false) is
   `not_loaded`.
3. The subscription is `subscribeMessage`-shaped: the result carries the
   handshake (`schema_version`, `version`), the first `event` is the full
   document, then one `event` per push of `engine_state_signal(entry_id)`
   — the SAME dispatcher signal the entities read on, sent by the runner's
   `_async_push_state` and the anomaly manager's report, clear and dismissal
   paths, and by nothing else. The unsubscribe callback lives in
   `connection.subscriptions[msg["id"]]`, so Home Assistant releases the
   dispatcher connection when the client unsubscribes or disconnects.
4. A push that lands while the entry is mid-reload is SKIPPED, never an
   error: `runtime_data` is gone between unload and the next setup, and the
   reload's own pushes (its reconcile) then deliver a fresh document on the
   same subscription.

The view is rebuilt on every read (`state_view.current_view`) — no cache, no
payload on the signal, no second push mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

import voluptuous as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.const import ERR_NOT_FOUND
from homeassistant.components.websocket_api.decorators import websocket_command
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    DOMAIN,
    WS_TYPE_STATE_GET,
    WS_TYPE_STATE_SUBSCRIBE,
    engine_state_signal,
)
from .engine.view import STATE_SCHEMA_VERSION
from .state_view import current_view

if TYPE_CHECKING:
    from homeassistant.components.websocket_api.connection import ActiveConnection
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from . import HaIrrigationConfigEntry

# The one error code this module adds to `websocket_api.const`'s: the entry
# exists but has no engine to read (unloading, unloaded, failed setup,
# disabled — or the first half of a reload). Distinct from `not_found` so a
# client can tell "retry shortly" from "wrong id".
ERR_NOT_LOADED: Final = "not_loaded"

# The states in which a document can be composed: the entry is LOADED, or it
# is being set up again after a reload and its fresh `runtime_data` is
# already in place (the reconcile push happens inside `async_setup_entry`).
# Anything else — unloading, unloaded, failed — has no engine. ONE predicate
# (`_readable`) for the fetch, the subscribe AND the push, so a card that
# answers a pushed event with `state_get` mid-reload is never refused.
_READABLE_STATES: Final = frozenset(
    {ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS},
)


def _readable(entry: ConfigEntry) -> bool:
    """Return whether a document can be composed from `entry` right now.

    Between an unload and the next setup `runtime_data` does not exist —
    Home Assistant deletes it — so the attribute check is load-bearing, not
    a belt to the state's suspender: SETUP_IN_PROGRESS is entered before the
    new `runtime_data` is assigned.
    """
    return entry.state in _READABLE_STATES and hasattr(entry, "runtime_data")


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register the two read commands on the component, once."""
    async_register_command(hass, ws_state_get)
    async_register_command(hass, ws_state_subscribe)


def _resolve_entry(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> HaIrrigationConfigEntry | None:
    """Return the readable entry `msg` addresses, or send the error and return None.

    `not_found` when no entry matches (an unknown id, no entry at all, or —
    `single_config_entry` notwithstanding — several entries and no id to pick
    one); `not_loaded` when the entry exists but `_readable` says it has no
    engine to read right now. The cast is the price of `async_entries` typing
    entries as plain `ConfigEntry`: they are this integration's domain by
    construction, so their `runtime_data` is ours.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    wanted = msg.get("entry_id")
    if wanted is not None:
        entry = next((item for item in entries if item.entry_id == wanted), None)
        reason = "No such irrigation controller entry"
    elif len(entries) > 1:
        entry = None
        reason = "Several irrigation controller entries; pass entry_id"
    else:
        entry = entries[0] if entries else None
        reason = "No irrigation controller entry to read"
    if entry is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, reason)
        return None
    if not _readable(entry):
        connection.send_error(
            msg["id"],
            ERR_NOT_LOADED,
            f"Irrigation controller entry {entry.entry_id} is not loaded",
        )
        return None
    return cast("HaIrrigationConfigEntry", entry)


def _pushable_entry(
    hass: HomeAssistant,
    entry_id: str,
) -> HaIrrigationConfigEntry | None:
    """Return the entry a push may compose a document from, or None to skip.

    Re-fetched on every push (rule 2): the subscription outlives reloads,
    and a push landing while `_readable` is false (the torn-down half of a
    reload) is skipped rather than raised on. The reload's own pushes arrive
    with the fresh `runtime_data` already assigned (it is set before the
    platforms are forwarded and the runner started) and deliver the fresh
    document. Same predicate as `_resolve_entry`, so a fetch answering one
    of those pushes is never refused.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or not _readable(entry):
        return None
    return cast("HaIrrigationConfigEntry", entry)


@websocket_command(
    {
        vol.Required("type"): WS_TYPE_STATE_GET,
        vol.Optional("entry_id"): cv.string,
    },
)
@callback
def ws_state_get(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return ONE state view document — the fetch half of the channel."""
    entry = _resolve_entry(hass, connection, msg)
    if entry is None:
        return
    connection.send_result(msg["id"], current_view(entry))


@websocket_command(
    {
        vol.Required("type"): WS_TYPE_STATE_SUBSCRIBE,
        vol.Optional("entry_id"): cv.string,
    },
)
@callback
def ws_state_subscribe(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe to the state view: handshake, full document, then one per push.

    Synchronous on purpose (`@callback`, no `async_response`): nothing here
    awaits, so the dispatcher connection is stored and the first document
    sent in the same loop iteration the command arrived — a push cannot slip
    between the two and be lost. The first document is composed BEFORE the
    connection is stored or the result sent: were the build to raise, the
    client gets one error and no dangling subscription, never a success
    followed by `unknown_error`.
    """
    entry = _resolve_entry(hass, connection, msg)
    if entry is None:
        return
    entry_id = entry.entry_id
    msg_id: int = msg["id"]
    first = current_view(entry)

    @callback
    def _push() -> None:
        """Send the freshly composed document, or skip a mid-reload push."""
        pushed = _pushable_entry(hass, entry_id)
        if pushed is None:
            return
        connection.send_event(msg_id, current_view(pushed))

    connection.subscriptions[msg_id] = async_dispatcher_connect(
        hass,
        engine_state_signal(entry_id),
        _push,
    )
    connection.send_result(
        msg_id,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "version": entry.runtime_data.version,
        },
    )
    connection.send_event(msg_id, first)
