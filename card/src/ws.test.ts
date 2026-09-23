import type { HomeAssistant } from "custom-card-helpers";
import { describe, expect, it, vi } from "vitest";

import type { StateView } from "./types";
import {
  asWsError,
  fetchState,
  subscribeState,
  WS_TYPE_STATE_GET,
  WS_TYPE_STATE_SUBSCRIBE,
} from "./ws";

function fakeHass(): {
  hass: HomeAssistant;
  callWS: ReturnType<typeof vi.fn>;
  subscribeMessage: ReturnType<typeof vi.fn>;
  unsubscribe: ReturnType<typeof vi.fn>;
} {
  const unsubscribe = vi.fn(async () => undefined);
  const callWS = vi.fn(async () => ({ schema_version: 1 }));
  const subscribeMessage = vi.fn(async () => unsubscribe);
  const hass = { callWS, connection: { subscribeMessage } } as unknown as HomeAssistant;
  return { hass, callWS, subscribeMessage, unsubscribe };
}

describe("ws", () => {
  it("names the two commands of Story 4.1's channel", () => {
    expect(WS_TYPE_STATE_GET).toBe("ha_irrigation_controller/state_get");
    expect(WS_TYPE_STATE_SUBSCRIBE).toBe("ha_irrigation_controller/state_subscribe");
  });

  it("fetches with state_get and OMITS entry_id when none is configured", async () => {
    const { hass, callWS } = fakeHass();
    await fetchState(hass);

    expect(callWS).toHaveBeenCalledTimes(1);
    const message = callWS.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(message).toEqual({ type: WS_TYPE_STATE_GET });
    expect("entry_id" in message).toBe(false);
  });

  it("passes entry_id through when configured", async () => {
    const { hass, callWS } = fakeHass();
    await fetchState(hass, "entry-1");

    expect(callWS).toHaveBeenCalledWith({ type: WS_TYPE_STATE_GET, entry_id: "entry-1" });
  });

  it("subscribes through connection.subscribeMessage and hands back the unsubscribe", async () => {
    const { hass, subscribeMessage, unsubscribe } = fakeHass();
    const onEvent = (_view: StateView): void => undefined;

    const result = await subscribeState(hass, onEvent);

    expect(subscribeMessage).toHaveBeenCalledTimes(1);
    expect(subscribeMessage.mock.calls[0]?.[0]).toBe(onEvent);
    expect(subscribeMessage.mock.calls[0]?.[1]).toEqual({ type: WS_TYPE_STATE_SUBSCRIBE });
    // Bound to one socket: the library's silent auto re-subscribe is opted out of.
    expect(subscribeMessage.mock.calls[0]?.[2]).toEqual({ resubscribe: false });
    expect(result).toBe(unsubscribe);
  });

  it("subscribes with entry_id for the multi-entry case", async () => {
    const { hass, subscribeMessage } = fakeHass();
    await subscribeState(hass, () => undefined, "entry-2");

    expect(subscribeMessage.mock.calls[0]?.[1]).toEqual({
      type: WS_TYPE_STATE_SUBSCRIBE,
      entry_id: "entry-2",
    });
  });

  it("propagates the backend's refusal as a rejection", async () => {
    const { hass, subscribeMessage } = fakeHass();
    subscribeMessage.mockRejectedValueOnce({ code: "not_found", message: "No such entry" });

    await expect(subscribeState(hass, () => undefined)).rejects.toEqual({
      code: "not_found",
      message: "No such entry",
    });
  });

  it("normalises every rejection shape into {code, message}", () => {
    expect(asWsError({ code: "not_loaded", message: "loading" })).toEqual({
      code: "not_loaded",
      message: "loading",
    });
    expect(asWsError({ code: 3 })).toEqual({ code: "3", message: "" });
    expect(asWsError(3)).toEqual({ code: "3", message: "" });
    expect(asWsError(new Error("socket closed"))).toEqual({
      code: "unknown",
      message: "socket closed",
    });
    expect(asWsError("nope")).toEqual({ code: "unknown", message: "nope" });
    expect(asWsError({ message: 42 })).toEqual({ code: "unknown", message: "" });
    expect(asWsError({})).toEqual({ code: "unknown", message: "" });
    expect(asWsError(null)).toEqual({ code: "unknown", message: "" });
    expect(asWsError(undefined)).toEqual({ code: "unknown", message: "" });
  });
});
