/**
 * The card's ONLY data path: Story 4.1's two WebSocket read commands.
 *
 * The card never reads entity states or attributes and never subscribes to
 * bus events (custom event types are admin-only from the frontend). Today
 * the element follows pushes with `state_subscribe` alone; `fetchState`
 * (`state_get`) is the one-shot fetch a later story will use. Both commands
 * are open to every authenticated user.
 */

import type { HomeAssistant } from "custom-card-helpers";

import type { StateView } from "./types";

export const WS_TYPE_STATE_GET = "ha_irrigation_controller/state_get";
export const WS_TYPE_STATE_SUBSCRIBE = "ha_irrigation_controller/state_subscribe";

/** `home-assistant-js-websocket`'s (unexported) subscription unsubscribe. */
export type UnsubscribeState = () => Promise<void>;

/** The error codes the backend answers a read with (`websocket.py`). */
export const ERR_NOT_FOUND = "not_found";
export const ERR_NOT_LOADED = "not_loaded";

export interface WsError {
  code: string;
  message: string;
}

interface StateMessage {
  type: string;
  entry_id?: string;
}

/** Build a command message, OMITTING `entry_id` when none is configured. */
function stateMessage(type: string, entryId?: string): StateMessage {
  return entryId === undefined ? { type } : { type, entry_id: entryId };
}

/** Fetch one document (`state_get`). */
export function fetchState(hass: HomeAssistant, entryId?: string): Promise<StateView> {
  return hass.callWS<StateView>(stateMessage(WS_TYPE_STATE_GET, entryId));
}

/**
 * Subscribe to the document (`state_subscribe`): `onEvent` receives the
 * full document first and then one per engine push. Resolves to the
 * unsubscribe function; rejects with the backend's `{code, message}` when
 * the command is refused.
 *
 * Opened with `resubscribe: false` ON PURPOSE: the library's automatic
 * re-subscribe after a reconnect has no error path, and on every HA
 * restart the frontend reconnects before this integration's commands exist
 * or its entry is loaded — the refusal would leave a dead subscription and
 * a stale document with no message and no retry. The subscription is
 * therefore bound to ONE socket; the caller listens for the connection's
 * `disconnected` and `ready` events and subscribes again, through the same
 * path as the first time, refusal handling included.
 */
export function subscribeState(
  hass: HomeAssistant,
  onEvent: (view: StateView) => void,
  entryId?: string,
): Promise<UnsubscribeState> {
  return hass.connection.subscribeMessage<StateView>(
    onEvent,
    stateMessage(WS_TYPE_STATE_SUBSCRIBE, entryId),
    { resubscribe: false },
  );
}

/**
 * Normalise whatever a rejected WebSocket promise carries. The library
 * rejects with the backend's `{code, message}` object for a refused
 * command, with a bare error number when the connection is lost, and
 * anything else is possible from a closing socket.
 */
export function asWsError(err: unknown): WsError {
  if (typeof err === "object" && err !== null) {
    const candidate = err as { code?: unknown; message?: unknown };
    if (candidate.code !== undefined || candidate.message !== undefined) {
      return {
        code: candidate.code === undefined ? "unknown" : String(candidate.code),
        message: typeof candidate.message === "string" ? candidate.message : "",
      };
    }
  }
  if (typeof err === "number") {
    return { code: String(err), message: "" };
  }
  if (err instanceof Error) {
    return { code: "unknown", message: err.message };
  }
  return { code: "unknown", message: typeof err === "string" ? err : "" };
}
